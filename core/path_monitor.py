from __future__ import annotations

import json
import time
from typing import Optional

from .paths import get_logs_dir, resolve_working_dir

_PATH_EVENTS = "path_events.jsonl"


def record_path_event(code: str, *, where: str, hint: str, details: Optional[str] = None, severity: str = "MAJOR") -> None:
    logs_dir = get_logs_dir(resolve_working_dir())
    logs_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": time.time(),
        "code": code,
        "where": where,
        "hint": hint,
        "severity": severity,
    }
    if details:
        payload["details"] = details
    try:
        with open(logs_dir / _PATH_EVENTS, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        return
