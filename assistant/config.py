from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Literal, Optional

from assistant_monitor import AssistantDashboardSettings


RuntimeMode = Literal["auto", "ollama", "llama_cpp", "mlc"]
IndexBackend = Literal["faiss", "hnswlib"]


@dataclass(slots=True)
class RagSettings:
    enable: bool = True
    top_k: int = 8
    min_score: float = 0.25
    embed_model: str = "bge-small-en"
    index: IndexBackend = "faiss"
    refresh_on_start: bool = False


@dataclass(slots=True)
class AssistantSettings:
    enable: bool = False
    runtime: RuntimeMode = "auto"
    model: str = "qwen2.5:7b-instruct"
    ctx: int = 8192
    temperature: float = 0.3
    tools_enabled: bool = True
    tool_budget: int = 20
    rag: RagSettings = field(default_factory=RagSettings)

    @classmethod
    def from_settings(cls, payload: Dict[str, Any]) -> "AssistantSettings":
        section = payload.get("assistant") or {}
        rag_payload = section.get("rag") or {}
        return cls(
            enable=bool(section.get("enable", False)),
            runtime=str(section.get("runtime", "auto")),
            model=str(section.get("model", "qwen2.5:7b-instruct")),
            ctx=int(section.get("ctx", 8192)),
            temperature=float(section.get("temperature", 0.3)),
            tools_enabled=bool(section.get("tools_enabled", True)),
            tool_budget=int(section.get("tool_budget", 20)),
            rag=RagSettings(
                enable=bool(rag_payload.get("enable", True)),
                top_k=int(rag_payload.get("top_k", 8)),
                min_score=float(rag_payload.get("min_score", 0.25)),
                embed_model=str(rag_payload.get("embed_model", "bge-small-en")),
                index=str(rag_payload.get("index", "faiss")),
                refresh_on_start=bool(rag_payload.get("refresh_on_start", False)),
            ),
        )

    def to_json(self) -> Dict[str, Any]:
        return {
            "enable": self.enable,
            "runtime": self.runtime,
            "model": self.model,
            "ctx": self.ctx,
            "temperature": self.temperature,
            "tools_enabled": self.tools_enabled,
            "tool_budget": self.tool_budget,
            "rag": {
                "enable": self.rag.enable,
                "top_k": self.rag.top_k,
                "min_score": self.rag.min_score,
                "embed_model": self.rag.embed_model,
                "index": self.rag.index,
                "refresh_on_start": self.rag.refresh_on_start,
            },
        }


def ensure_settings_section(path: Path) -> None:
    """Ensure ``assistant`` section exists in ``settings.json`` preserving user overrides."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        payload = {}
    except Exception:
        return
    if "assistant" in payload:
        if "assistant_dashboard" not in payload:
            payload["assistant_dashboard"] = AssistantDashboardSettings().to_json()
        return
    payload["assistant"] = AssistantSettings().to_json()
    payload["assistant_dashboard"] = AssistantDashboardSettings().to_json()
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
