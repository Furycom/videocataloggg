"""Structured logging helpers for backup operations."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict

LOGGER = logging.getLogger("videocatalog.backup")


class BackupLogger:
    """Write structured JSONL entries for backup related events."""

    def __init__(self, working_dir: Path) -> None:
        self._working_dir = Path(working_dir)
        self._log_path = self._working_dir / "logs" / "backup.jsonl"
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    # ------------------------------------------------------------------
    def _write(self, payload: Dict[str, Any], *, level: int) -> None:
        payload.setdefault("ts", datetime.now(timezone.utc).isoformat())
        line = json.dumps(payload, sort_keys=True)
        with self._lock:
            with self._log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        LOGGER.log(level, "%s", line)

    def event(self, *, event: str, phase: str, ok: bool, **extra: Any) -> None:
        payload = {
            "event": event,
            "phase": phase,
            "ok": bool(ok),
        }
        if extra:
            payload.update(extra)
        level = logging.INFO if ok else logging.ERROR
        self._write(payload, level=level)

    def info(self, event: str, **extra: Any) -> None:
        self._write({"event": event, **extra, "ok": True}, level=logging.INFO)

    def warning(self, event: str, **extra: Any) -> None:
        self._write({"event": event, **extra, "ok": False}, level=logging.WARNING)

    def error(self, event: str, **extra: Any) -> None:
        self._write({"event": event, **extra, "ok": False}, level=logging.ERROR)


__all__ = ["BackupLogger"]
