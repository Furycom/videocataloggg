"""Worker process entry point for orchestrator jobs."""
from __future__ import annotations

import json
import sqlite3
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .checkpoint import load_checkpoint
from .gpu import GPUManager
from .logs import OrchestratorLogger
from .registry import RunnerContext, build_default_registry


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_job(
    db_path: str,
    job_id: int,
    kind: str,
    payload_json: str,
    settings_json: str,
    working_dir: str,
    lease_owner: Optional[str],
    orchestrator_cfg: Dict[str, Any],
) -> None:
    """Run a single job inside an isolated worker process."""

    logger = OrchestratorLogger(Path(working_dir))
    registry = build_default_registry()
    spec = registry.get(kind)
    payload = json.loads(payload_json) if payload_json else {}
    settings = json.loads(settings_json) if settings_json else {}
    ctx = RunnerContext(Path(working_dir), settings)

    heartbeat_s = int(orchestrator_cfg.get("heartbeat_s", 5))
    lease_ttl = int(orchestrator_cfg.get("lease_ttl_s", 120))
    gpu_cfg = orchestrator_cfg.get("gpu", {})
    gpu_manager = GPUManager(
        logger=logger,
        safety_margin_mb=int(gpu_cfg.get("safety_margin_mb", 1024)),
        lease_ttl_s=lease_ttl,
    )

    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    checkpoint = load_checkpoint(conn, job_id) or {}
    start_ts = _utcnow()
    conn.execute(
        "UPDATE jobs SET status='running', started_utc=COALESCE(started_utc, ?), heartbeat_utc=? WHERE id=?",
        (start_ts, start_ts, job_id),
    )
    conn.commit()

    stop_event = threading.Event()

    def _heartbeat_loop() -> None:
        while not stop_event.is_set():
            hb_conn = sqlite3.connect(db_path, check_same_thread=False)
            try:
                hb_conn.execute("PRAGMA journal_mode=WAL")
                now = _utcnow()
                hb_conn.execute("UPDATE jobs SET heartbeat_utc=? WHERE id=?", (now, job_id))
                hb_conn.commit()
                if lease_owner:
                    gpu_manager.refresh_lease(hb_conn, lease_owner)
            finally:
                hb_conn.close()
            stop_event.wait(heartbeat_s)

    heartbeat_thread = threading.Thread(target=_heartbeat_loop, name=f"heartbeat-{job_id}", daemon=True)
    heartbeat_thread.start()

    try:
        status = _current_status(db_path, job_id)
        if status == "canceled":
            conn.execute(
                "UPDATE jobs SET status='canceled', ended_utc=?, error_code='CANCELED' WHERE id=?",
                (_utcnow(), job_id),
            )
            conn.commit()
            return
        spec.runner(ctx, payload, checkpoint)
        conn.execute(
            "UPDATE jobs SET status='done', ended_utc=?, error_code=NULL, error_msg=NULL WHERE id=?",
            (_utcnow(), job_id),
        )
        conn.commit()
    except Exception as exc:
        tb = traceback.format_exc(limit=6)
        error_code = _classify_error(exc)
        conn.execute(
            "UPDATE jobs SET status='failed', ended_utc=?, error_code=?, error_msg=? WHERE id=?",
            (_utcnow(), error_code, str(exc), job_id),
        )
        conn.commit()
        _record_event(db_path, job_id, level="ERROR", event_id=1301, data={"error": str(exc), "trace": tb})
    finally:
        stop_event.set()
        heartbeat_thread.join(timeout=heartbeat_s)
        if lease_owner:
            release_conn = sqlite3.connect(db_path, check_same_thread=False)
            try:
                release_conn.execute("PRAGMA journal_mode=WAL")
                gpu_manager.release(release_conn, lease_owner)
            finally:
                release_conn.close()
        conn.close()


def _classify_error(exc: Exception) -> str:
    text = str(exc).lower()
    if "out of memory" in text or "cuda" in text and "oom" in text:
        return "CUDA_OOM"
    if "rate limit" in text or "429" in text:
        return "RATE_LIMIT"
    return "ERROR"


def _record_event(db_path: str, job_id: int, *, level: str, event_id: int, data: Dict[str, Any]) -> None:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    try:
        conn.execute(
            "INSERT INTO job_events(ts_utc, job_id, level, event_id, data_json) VALUES(?, ?, ?, ?, ?)",
            (_utcnow(), job_id, level, event_id, json.dumps(data, sort_keys=True)),
        )
        conn.commit()
    finally:
        conn.close()


def _current_status(db_path: str, job_id: int) -> Optional[str]:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    try:
        row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()
