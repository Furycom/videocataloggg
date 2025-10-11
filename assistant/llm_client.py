from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional

import requests
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from .runtime import RuntimeHandle

LOGGER = logging.getLogger("videocatalog.assistant.llm")


class LLMClient:
    def __init__(self, runtime: RuntimeHandle, model: str, *, temperature: float = 0.3, ctx: int = 8192) -> None:
        self.runtime = runtime
        self.model = model
        self.temperature = temperature
        self.ctx = ctx

    def chat(self, messages: List[BaseMessage], tools: Optional[List[Dict[str, object]]] = None) -> AIMessage:
        payload_messages = [self._to_payload(msg) for msg in messages]
        tools_payload = tools or []
        if self.runtime.name == "ollama":
            response = self._chat_ollama(payload_messages, tools_payload)
        else:
            response = self._chat_openai(payload_messages, tools_payload)
        return self._to_message(response)

    # ------------------------------------------------------------------
    def _chat_ollama(self, messages: List[Dict[str, object]], tools: List[Dict[str, object]]):
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "format": "json" if not tools else None,
            "options": {
                "temperature": self.temperature,
                "num_ctx": self.ctx,
            },
        }
        if tools:
            payload["tools"] = tools
        response = requests.post(f"{self.runtime.base_url}/api/chat", json=payload, timeout=120)
        response.raise_for_status()
        data = response.json()
        message = data.get("message") or {}
        # Some Ollama builds return list of messages under "messages"
        if not message and isinstance(data.get("messages"), list):
            message = data["messages"][-1]
        return message

    def _chat_openai(self, messages: List[Dict[str, object]], tools: List[Dict[str, object]]):
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "stream": False,
            "max_tokens": 1024,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        response = requests.post(
            f"{self.runtime.base_url}/v1/chat/completions",
            json=payload,
            timeout=120,
        )
        response.raise_for_status()
        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("LLM returned no choices")
        return choices[0].get("message", {})

    # ------------------------------------------------------------------
    def _to_message(self, payload: Dict[str, object]) -> AIMessage:
        tool_calls = payload.get("tool_calls") or []
        if not tool_calls and isinstance(payload.get("content"), str):
            parsed = self._try_parse_tool_call(payload["content"])
            if parsed:
                tool_calls = parsed
        return AIMessage(
            content=payload.get("content", ""),
            additional_kwargs={"tool_calls": tool_calls} if tool_calls else {},
        )

    @staticmethod
    def _to_payload(message: BaseMessage) -> Dict[str, object]:
        if isinstance(message, HumanMessage):
            return {"role": "user", "content": message.content}
        if isinstance(message, SystemMessage):
            return {"role": "system", "content": message.content}
        if isinstance(message, ToolMessage):
            return {
                "role": "tool",
                "content": message.content,
                "name": message.name,
                "tool_call_id": message.tool_call_id,
            }
        if isinstance(message, AIMessage):
            payload = {"role": "assistant", "content": message.content}
            calls = message.additional_kwargs.get("tool_calls")
            if calls:
                payload["tool_calls"] = calls
            return payload
        return {"role": "user", "content": str(message.content)}

    @staticmethod
    def _try_parse_tool_call(content: str) -> List[Dict[str, object]]:
        try:
            data = json.loads(content)
        except Exception:
            return []
        if isinstance(data, dict) and {"name", "arguments"} <= data.keys():
            return [
                {
                    "id": data.get("id", "tool-0"),
                    "type": "function",
                    "function": {"name": data["name"], "arguments": json.dumps(data["arguments"])}
                }
            ]
        if isinstance(data, list):
            calls = []
            for idx, item in enumerate(data):
                if isinstance(item, dict) and {"name", "arguments"} <= item.keys():
                    calls.append(
                        {
                            "id": item.get("id", f"tool-{idx}"),
                            "type": "function",
                            "function": {"name": item["name"], "arguments": json.dumps(item["arguments"])},
                        }
                    )
            return calls
        return []
