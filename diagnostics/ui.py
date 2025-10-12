"""Helper structures for the diagnostics "Debug" tab."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.paths import resolve_working_dir

from .logs import load_snapshot, query_logs


@dataclass(slots=True)
class DebugAction:
    key: str
    label: str
    description: str
    endpoint: str


@dataclass(slots=True)
class DebugCard:
    key: str
    title: str
    status: str
    message: str
    hint: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)


def _card_status(section: Dict[str, Any]) -> str:
    if not section:
        return "unknown"
    if section.get("ok"):
        return "good"
    items = section.get("items", [])
    severities = {item.get("severity", "INFO").upper() for item in items}
    if "MAJOR" in severities:
        return "bad"
    if "MINOR" in severities:
        return "warn"
    return "info"


def build_debug_state(*, working_dir: Optional[Path] = None) -> Dict[str, Any]:
    resolved = working_dir or resolve_working_dir()
    preflight = load_snapshot("preflight", working_dir=resolved) or {}
    smoke = load_snapshot("smoke", working_dir=resolved) or {}

    cards: List[DebugCard] = []
    for key in ["gpu", "tools", "apis", "filesystem", "database", "settings"]:
        section = preflight.get("sections", {}).get(key, {})
        status = _card_status(section)
        message = section.get("items", [{}])[-1].get("message") if section.get("items") else (
            "All checks passed" if section.get("ok") else "No data"
        )
        hint = None
        if section.get("items"):
            hint = section.get("items")[0].get("hint")
        cards.append(
            DebugCard(
                key=key,
                title=key.upper(),
                status=status,
                message=message,
                hint=hint,
                data=section.get("details", {}),
            )
        )

    actions = [
        DebugAction(key="preflight", label="Run Preflight", description="Run GPU and environment checks", endpoint="/v1/diagnostics/preflight"),
        DebugAction(key="smoke", label="Run Smoke Tests", description="Execute quick subsystem smoke tests", endpoint="/v1/diagnostics/smoke"),
        DebugAction(key="export", label="Export Report…", description="Download diagnostics bundle", endpoint="/v1/diagnostics/download"),
    ]

    smoke_items = smoke.get("items", [])
    smoke_summary = smoke.get("summary", {})

    logs_payload = query_logs(working_dir=resolved, limit=100)

    return {
        "preflight": preflight,
        "smoke": {
            "summary": smoke_summary,
            "items": smoke_items,
        },
        "actions": [action.__dict__ for action in actions],
        "cards": [card.__dict__ for card in cards],
        "logs": logs_payload.get("rows", []),
    }


__all__ = ["build_debug_state", "DebugAction", "DebugCard"]
