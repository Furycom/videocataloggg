"""Central orchestrator scheduler."""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from multiprocessing import Process
from pathlib import Path
from typing import Any, Dict, List, Optional

from .checkpoint import ensure_schema as ensure_checkpoint_schema, load_checkpoint
from .gpu import GPUManager
from .logs import OrchestratorLogger
from .registry import JobRegistry, build_default_registry


@dataclass(slots=True)
class WorkerHandle:
    job_id: int
    process: Process
    resource: str
    lease_owner: str
    started: datetime


class Scheduler:
    """Coordinate job dispatch respecting GPU exclusivity and priorities."""

    def __init__(
        self,
        working_dir: Path,
        *,
        settings: Dict[str, Any],
        registry: Optional[JobRegistry] = None,
        logger: Optional[OrchestratorLogger] = None,
    ) -> None:
        self._working_dir = Path(working_dir)
        self._db_path = self._working_dir / "data" / "orchestrator.db"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._settings = dict(settings or {})
        self._registry = registry or build_default_registry()
        self._logger = logger or OrchestratorLogger(self._working_dir)
        orch_cfg = self._settings.get("orchestrator", {})
        gpu_cfg = orch_cfg.get("gpu", {})
        self._poll_interval = float(orch_cfg.get("poll_ms", 500)) / 1000.0
        self._heartbeat_s = int(orch_cfg.get("heartbeat_s", 5))
        self._lease_ttl = int(orch_cfg.get("lease_ttl_s", 120))
        self._concurrency = orch_cfg.get(
            "concurrency",
            {"heavy_ai_gpu": 1, "light_cpu": 2, "io_light": 2},
        )
        self._gpu_manager = GPUManager(
            logger=self._logger,
            safety_margin_mb=int(gpu_cfg.get("safety_margin_mb", 1024)),
            lease_ttl_s=self._lease_ttl,
        )
        self._owner_id = f"orch-{uuid.uuid4()}"
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._running: Dict[int, WorkerHandle] = {}
        self._lock = threading.Lock()
        self._settings_cache: Dict[str, str] = {}
        self._ensure_schema()

    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        ensure_checkpoint_schema(conn)
        self._gpu_manager.ensure_lock_table(conn)
        return conn

    def _ensure_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    resource TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    lease_owner TEXT,
                    lease_utc TEXT,
                    heartbeat_utc TEXT,
                    created_utc TEXT NOT NULL,
                    started_utc TEXT,
                    ended_utc TEXT,
                    error_code TEXT,
                    error_msg TEXT
                );
                CREATE TABLE IF NOT EXISTS job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_utc TEXT NOT NULL,
                    job_id INTEGER,
                    level TEXT,
                    event_id INTEGER,
                    data_json TEXT
                );
                CREATE TABLE IF NOT EXISTS orchestrator_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run_loop, name="orchestrator-scheduler", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = None
        with self._lock:
            thread = self._thread
        if thread:
            thread.join(timeout=2)
        with self._lock:
            self._thread = None

    # ------------------------------------------------------------------
    def enqueue(
        self,
        kind: str,
        payload: Dict[str, Any],
        *,
        priority: int = 50,
        max_attempts: int = 3,
    ) -> int:
        spec = self._registry.get(kind)
        now = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                INSERT INTO jobs (
                    kind, payload_json, priority, resource, status, max_attempts, created_utc
                ) VALUES (?, ?, ?, ?, 'queued', ?, ?)
                """,
                (kind, json.dumps(payload, sort_keys=True), int(priority), spec.resource, int(max_attempts), now),
            )
            conn.commit()
            job_id = int(cursor.lastrowid)
            self._logger.log_job(job_id, kind, "enqueue", 1000, True, priority=priority)
            return job_id
        finally:
            conn.close()

    # ------------------------------------------------------------------
    def pause_all(self) -> None:
        self._set_setting("paused", "1")

    def resume_all(self) -> None:
        self._set_setting("paused", "0")

    def _set_setting(self, key: str, value: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO orchestrator_settings(key, value) VALUES(?, ?)\n                 ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            conn.commit()
            self._settings_cache[key] = value
        finally:
            conn.close()

    def _is_paused(self) -> bool:
        if self._settings_cache.get("paused") is not None:
            return self._settings_cache.get("paused") == "1"
        conn = self._connect()
        try:
            row = conn.execute("SELECT value FROM orchestrator_settings WHERE key=?", ("paused",)).fetchone()
            value = row[0] if row else "0"
            self._settings_cache["paused"] = value
            return value == "1"
        finally:
            conn.close()

    def is_paused(self) -> bool:
        return self._is_paused()

    # ------------------------------------------------------------------
    def cancel_job(self, job_id: int) -> None:
        with self._lock:
            handle = self._running.get(job_id)
        lease_owner = handle.lease_owner if handle else None
        if handle:
            handle.process.terminate()
        conn = self._connect()
        try:
            current_owner = lease_owner
            if not current_owner:
                row = conn.execute("SELECT lease_owner FROM jobs WHERE id=?", (job_id,)).fetchone()
                if row and row["lease_owner"]:
                    current_owner = row["lease_owner"]
            if current_owner:
                self._gpu_manager.release(conn, current_owner)
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE jobs SET status='canceled', ended_utc=? WHERE id=?",
                (now, job_id),
            )
            conn.commit()
        finally:
            conn.close()

    def pause_job(self, job_id: int) -> None:
        conn = self._connect()
        try:
            conn.execute("UPDATE jobs SET status='paused' WHERE id=?", (job_id,))
            conn.commit()
        finally:
            conn.close()

    def resume_job(self, job_id: int) -> None:
        conn = self._connect()
        try:
            conn.execute("UPDATE jobs SET status='queued' WHERE id=? AND status='paused'", (job_id,))
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    def list_jobs(self, *, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_utc DESC LIMIT ? OFFSET ?",
                (int(limit), int(offset)),
            ).fetchall()
            return [self._row_to_job(row) for row in rows]
        finally:
            conn.close()

    def get_job(self, job_id: int) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return self._row_to_job(row) if row else None
        finally:
            conn.close()

    def locks(self) -> Dict[str, Any]:
        conn = self._connect()
        try:
            lock = self._gpu_manager.current_lock(conn)
            return {"gpu_exclusive": lock}
        finally:
            conn.close()

    def job_events(self, job_id: int, *, limit: int = 50) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT ts_utc, level, event_id, data_json FROM job_events WHERE job_id=? ORDER BY id DESC LIMIT ?",
                (job_id, int(limit)),
            ).fetchall()
            events: List[Dict[str, Any]] = []
            for row in rows:
                try:
                    payload = json.loads(row["data_json"]) if row["data_json"] else {}
                except Exception:
                    payload = {}
                events.append(
                    {
                        "ts_utc": row["ts_utc"],
                        "level": row["level"],
                        "event_id": row["event_id"],
                        "data": payload,
                    }
                )
            return events
        finally:
            conn.close()

    def job_checkpoint(self, job_id: int) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        try:
            return load_checkpoint(conn, job_id)
        finally:
            conn.close()

    def _row_to_job(self, row: sqlite3.Row) -> Dict[str, Any]:
        payload = {}
        try:
            payload = json.loads(row["payload_json"])
        except Exception:
            payload = {}
        return {
            "id": row["id"],
            "kind": row["kind"],
            "payload": payload,
            "priority": row["priority"],
            "resource": row["resource"],
            "status": row["status"],
            "attempts": row["attempts"],
            "max_attempts": row["max_attempts"],
            "lease_owner": row["lease_owner"],
            "lease_utc": row["lease_utc"],
            "heartbeat_utc": row["heartbeat_utc"],
            "created_utc": row["created_utc"],
            "started_utc": row["started_utc"],
            "ended_utc": row["ended_utc"],
            "error_code": row["error_code"],
            "error_msg": row["error_msg"],
        }

    # ------------------------------------------------------------------
    def _run_loop(self) -> None:
        from multiprocessing import get_context
        from . import runner as runner_module

        ctx = get_context("spawn")
        while not self._stop_event.is_set():
            try:
                self._reconcile_workers()
                if not self._is_paused():
                    self._dispatch(ctx, runner_module)
                self._requeue_stale_jobs()
            except Exception as exc:  # pragma: no cover - defensive
                self._logger.log_scheduler(1001, False, err=str(exc))
            self._stop_event.wait(self._poll_interval)
        with self._lock:
            for handle in list(self._running.values()):
                handle.process.terminate()
            self._running.clear()

    # ------------------------------------------------------------------
    def _dispatch(self, mp_ctx, runner_module) -> None:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY priority ASC, created_utc ASC",
            ).fetchall()
            for row in rows:
                job_id = int(row["id"])
                kind = str(row["kind"])
                spec = self._registry.get(kind)
                if self._resource_in_use(spec.resource):
                    continue
                try:
                    payload = json.loads(row["payload_json"])
                except Exception:
                    payload = {}
                if spec.preconditions:
                    reason = spec.preconditions(self._context_payload(), payload)
                    if reason:
                        conn.execute(
                            "UPDATE jobs SET status='failed', error_code='PRECONDITION', error_msg=?, ended_utc=? WHERE id=?",
                            (reason, datetime.now(timezone.utc).isoformat(), job_id),
                        )
                        conn.commit()
                        continue
                if spec.resource == "heavy_ai_gpu":
                    required_mb = spec.estimate_vram(payload)
                    preflight = self._gpu_manager.preflight(required_mb)
                    if not preflight.ok:
                        conn.execute(
                            "UPDATE jobs SET status='failed', error_code='GPU_UNAVAILABLE', error_msg=?, ended_utc=? WHERE id=?",
                            (preflight.reason or "GPU unavailable", datetime.now(timezone.utc).isoformat(), job_id),
                        )
                        conn.commit()
                        self._logger.log_job(
                            job_id,
                            kind,
                            "preflight",
                            1100,
                            False,
                            reason=preflight.reason,
                        )
                        continue
                    owner = f"{self._owner_id}:{job_id}"
                    if not self._gpu_manager.acquire_exclusive(conn, owner):
                        continue
                    lease_owner = owner
                    vram = preflight.info.vram_free_mb
                else:
                    lease_owner = None
                    vram = None
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "UPDATE jobs SET status='leased', lease_owner=?, lease_utc=?, attempts=attempts+1 WHERE id=?",
                    (lease_owner, now, job_id),
                )
                conn.commit()
                process = mp_ctx.Process(
                    target=runner_module.run_job,
                    args=(
                        str(self._db_path),
                        job_id,
                        kind,
                        json.dumps(payload, sort_keys=True),
                        json.dumps(self._settings, sort_keys=True),
                        str(self._working_dir),
                        lease_owner,
                        {
                            "heartbeat_s": self._heartbeat_s,
                            "lease_ttl_s": self._lease_ttl,
                            "gpu": {
                                "safety_margin_mb": self._gpu_manager.safety_margin_mb,
                            },
                        },
                    ),
                    name=f"job-{job_id}-{kind}",
                )
                process.start()
                handle = WorkerHandle(
                    job_id=job_id,
                    process=process,
                    resource=spec.resource,
                    lease_owner=lease_owner or "",
                    started=datetime.now(timezone.utc),
                )
                with self._lock:
                    self._running[job_id] = handle
                self._logger.log_job(job_id, kind, "dispatch", 1300, True, lease_owner=lease_owner, free_vram=vram)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    def _context_payload(self) -> "RunnerContext":
        from .registry import RunnerContext

        return RunnerContext(self._working_dir, self._settings)

    def _resource_in_use(self, resource: str) -> bool:
        limit = int(self._concurrency.get(resource, 1))
        if limit <= 0:
            return True
        count = sum(1 for handle in self._running.values() if handle.resource == resource and handle.process.is_alive())
        return count >= limit

    def _reconcile_workers(self) -> None:
        finished: List[int] = []
        with self._lock:
            for job_id, handle in self._running.items():
                if not handle.process.is_alive():
                    handle.process.join(timeout=0.1)
                    finished.append(job_id)
        if finished:
            with self._lock:
                for job_id in finished:
                    self._running.pop(job_id, None)

    def _requeue_stale_jobs(self) -> None:
        conn = self._connect()
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=self._lease_ttl * 2)
            rows = conn.execute(
                "SELECT id, status, attempts, max_attempts FROM jobs WHERE status IN ('running','leased') AND heartbeat_utc IS NOT NULL",
            ).fetchall()
            for row in rows:
                hb = row["heartbeat_utc"]
                try:
                    hb_dt = datetime.fromisoformat(hb) if hb else None
                except Exception:
                    hb_dt = None
                if hb_dt and hb_dt > cutoff:
                    continue
                if int(row["attempts"]) >= int(row["max_attempts"]):
                    conn.execute(
                        "UPDATE jobs SET status='failed', error_code='MAX_ATTEMPTS', ended_utc=? WHERE id=?",
                        (datetime.now(timezone.utc).isoformat(), row["id"]),
                    )
                    continue
                conn.execute(
                    "UPDATE jobs SET status='queued', lease_owner=NULL, lease_utc=NULL WHERE id=?",
                    (row["id"],),
                )
            conn.commit()
        finally:
            conn.close()
