from __future__ import annotations

import json
import logging
from typing import Dict, List, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph

from .llm_client import LLMClient
from .tools import AssistantTooling, ToolBudgetExceeded

LOGGER = logging.getLogger("videocatalog.assistant.controller")


class AssistantState(TypedDict):
    messages: List[object]
    remaining_budget: int
    tool_calls: List[Dict[str, object]]


class AssistantController:
    def __init__(self, client: LLMClient, tooling: AssistantTooling, *, system_prompt: str) -> None:
        self.client = client
        self.tooling = tooling
        self.system_prompt = system_prompt
        self.graph = self._build_graph()

    def _build_graph(self):
        workflow = StateGraph(AssistantState)
        workflow.add_node("model", self._call_model)
        workflow.add_node("tools", self._call_tools)
        workflow.set_entry_point("model")
        workflow.add_conditional_edges(
            "model",
            self._should_continue,
            {"tools": "tools", "end": END},
        )
        workflow.add_edge("tools", "model")
        return workflow.compile()

    def run(self, history: List[object]) -> AssistantState:
        initial_state: AssistantState = {
            "messages": history,
            "remaining_budget": self.tooling.budget,
            "tool_calls": [],
        }
        return self.graph.invoke(initial_state)

    # ------------------------------------------------------------------
    def _call_model(self, state: AssistantState) -> AssistantState:
        messages = state["messages"]
        tool_defs = self._tool_schema() if self.tooling.settings.tools_enabled else []
        response = self.client.chat(messages, tool_defs)
        messages = messages + [response]
        tool_calls = response.additional_kwargs.get("tool_calls") or []
        LOGGER.debug("Assistant model returned %d tool calls", len(tool_calls))
        return {
            "messages": messages,
            "remaining_budget": state["remaining_budget"],
            "tool_calls": tool_calls,
        }

    def _should_continue(self, state: AssistantState) -> str:
        tool_calls = state.get("tool_calls") or []
        if not tool_calls:
            return "end"
        if state["remaining_budget"] <= 0:
            LOGGER.warning("Assistant: tool budget exhausted before execution")
            return "end"
        return "tools"

    def _call_tools(self, state: AssistantState) -> AssistantState:
        messages = state["messages"]
        tool_calls = state.get("tool_calls") or []
        remaining_budget = state["remaining_budget"]
        for call in tool_calls:
            name = call.get("function", {}).get("name")
            arg_json = call.get("function", {}).get("arguments", "{}")
            try:
                arguments = json.loads(arg_json) if isinstance(arg_json, str) else arg_json
            except Exception as exc:
                LOGGER.error("Invalid tool arguments: %s", exc)
                continue
            try:
                result = self.tooling.execute(name, arguments)
            except ToolBudgetExceeded as exc:
                content = json.dumps({"error": str(exc)})
                messages = messages + [ToolMessage(content=content, tool_call_id=call.get("id", name), name=name)]
                remaining_budget = 0
                break
            except Exception as exc:
                LOGGER.exception("Tool %s failed", name)
                result = {"error": str(exc)}
            content = json.dumps(result, ensure_ascii=False)
            messages = messages + [ToolMessage(content=content, tool_call_id=call.get("id", name), name=name)]
            remaining_budget -= 1
        return {
            "messages": messages,
            "remaining_budget": remaining_budget,
            "tool_calls": [],
        }

    def _tool_schema(self) -> List[Dict[str, object]]:
        schema = []
        for tool in self.tooling.definitions():
            schema.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
            )
        return schema
