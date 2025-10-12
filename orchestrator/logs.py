"""Structured logging helpers for the orchestrator."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Optional


LOGGER = logging.getLogger("videocatalog.orchestrator.logs")


class OrchestratorLogger:
    """Write structured JSONL events for the orchestrator."""

    def __init__(self, working_dir: Path) -> None:
        self._working_dir = Path(working_dir)
        self._log_path = self._working_dir / "logs" / "orchestrator.jsonl"
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    # ------------------------------------------------------------------
    def log_event(
        self,
        *,
        level: str,
        event_id: int,
        job_id: Optional[int],
        kind: Optional[str],
        phase: str,
        ok: bool,
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "event_id": int(event_id),
            "job_id": job_id,
            "kind": kind,
            "phase": phase,
            "ok": ok,
        }
        if data:
            payload.update(data)
        line = json.dumps(payload, sort_keys=True)
        with self._lock:
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        LOGGER.log(getattr(logging, level, logging.INFO), "%s", line)

    # ------------------------------------------------------------------
    def log_scheduler(self, event_id: int, ok: bool, **data: Any) -> None:
        self.log_event(level="INFO", event_id=event_id, job_id=None, kind=None, phase="scheduler", ok=ok, data=data)

    def log_gpu(self, event_id: int, ok: bool, **data: Any) -> None:
        self.log_event(level="INFO", event_id=event_id, job_id=None, kind=None, phase="gpu", ok=ok, data=data)

    def log_job(self, job_id: int, kind: str, phase: str, event_id: int, ok: bool, **data: Any) -> None:
        self.log_event(
            level="INFO",
            event_id=event_id,
            job_id=job_id,
            kind=kind,
            phase=phase,
            ok=ok,
            data=data,
        )

    def log_error(self, event_id: int, job_id: Optional[int], kind: Optional[str], phase: str, err: Exception, **data: Any) -> None:
        payload = dict(data)
        payload["err"] = type(err).__name__
        payload["err_msg"] = str(err)
        self.log_event(
            level="ERROR",
            event_id=event_id,
            job_id=job_id,
            kind=kind,
            phase=phase,
            ok=False,
            data=payload,
        )
