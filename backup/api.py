"""Public API for backup operations."""
from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Dict, List, Optional, TYPE_CHECKING

from core.db import connect
from core.paths import get_catalog_db_path, resolve_working_dir

from .create import create_backup
from .errors import BackupError
from .logs import BackupLogger
from .restore import restore_backup
from .retention import RetentionPolicy, apply_retention
from .types import BackupOptions, BackupResult, BackupSummary, RetentionSummary
from .verify import verify_backup

if TYPE_CHECKING:  # pragma: no cover - typing guard
    from orchestrator.scheduler import Scheduler

_BACKUPS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS backups (
  id TEXT PRIMARY KEY,
  created_utc TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  include_vectors INTEGER NOT NULL,
  include_thumbs INTEGER NOT NULL,
  verified INTEGER NOT NULL DEFAULT 0,
  notes TEXT
)
"""


def _ensure_table(conn) -> None:
    conn.execute(_BACKUPS_TABLE_SQL)
    conn.commit()


class _OrchestratorGuard(contextlib.AbstractContextManager):
    def __init__(self, scheduler: Optional["Scheduler"], logger: BackupLogger, phase: str, timeout: float) -> None:
        self._scheduler = scheduler
        self._logger = logger
        self._phase = phase
        self._timeout = timeout

    def __enter__(self) -> None:
        if not self._scheduler:
            return None
        self._logger.event(event="orchestrator_pause", phase=self._phase, ok=True)
        self._scheduler.pause_all()
        if not self._scheduler.await_idle(timeout=self._timeout):
            active = self._scheduler.active_jobs()
            self._scheduler.resume_all()
            raise BackupError(f"Timed out waiting for orchestrator to quiesce (active={active})")
        return None

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._scheduler:
            self._scheduler.resume_all()
            self._logger.event(event="orchestrator_resume", phase=self._phase, ok=exc is None)
        return False


class BackupService:
    """Coordinate backup, verification, restore, and retention workflows."""

    def __init__(
        self,
        *,
        working_dir: Optional[Path] = None,
        settings: Optional[Dict[str, object]] = None,
        scheduler: Optional["Scheduler"] = None,
    ) -> None:
        self._working_dir = Path(working_dir or resolve_working_dir())
        self._settings = dict(settings or {})
        self._scheduler = scheduler
        self._logger = BackupLogger(self._working_dir)
        self._catalog_path = get_catalog_db_path(self._working_dir)

    # ------------------------------------------------------------------
    @property
    def working_dir(self) -> Path:
        return self._working_dir

    # ------------------------------------------------------------------
    def _connect_catalog(self):
        conn = connect(self._catalog_path, read_only=False)
        conn.row_factory = lambda cursor, row: {cursor.description[idx][0]: row[idx] for idx in range(len(row))}
        _ensure_table(conn)
        return conn

    def _retention_policy(self) -> RetentionPolicy:
        raw = self._settings.get("backup")
        retention = {}
        if isinstance(raw, dict):
            retention = raw.get("retention", {}) if isinstance(raw.get("retention"), dict) else {}
        return RetentionPolicy(
            keep_last=int(retention.get("keep_last", 10) or 0),
            keep_daily=int(retention.get("keep_daily", 14) or 0),
            keep_weekly=int(retention.get("keep_weekly", 8) or 0),
            max_total_gb=float(retention.get("max_total_gb", 50) or 0),
        )

    def _quiesce(self, phase: str) -> _OrchestratorGuard:
        timeout = 90.0
        raw = self._settings.get("backup")
        if isinstance(raw, dict) and raw.get("quiesce_timeout_s"):
            try:
                timeout = float(raw.get("quiesce_timeout_s"))
            except Exception:
                timeout = 90.0
        return _OrchestratorGuard(self._scheduler, self._logger, phase, timeout)

    # ------------------------------------------------------------------
    def create_snapshot(self, options: BackupOptions) -> BackupResult:
        with self._quiesce("create"):
            result = create_backup(self._working_dir, logger=self._logger, options=options)
        total_size = sum(int(entry.get("bytes") or 0) for entry in result.manifest.files)
        with self._connect_catalog() as conn:
            conn.execute(
                """
                INSERT INTO backups(id, created_utc, size_bytes, include_vectors, include_thumbs, verified, notes)
                VALUES(?, ?, ?, ?, ?, 0, ?)
                ON CONFLICT(id) DO UPDATE SET created_utc=excluded.created_utc,
                    size_bytes=excluded.size_bytes,
                    include_vectors=excluded.include_vectors,
                    include_thumbs=excluded.include_thumbs,
                    notes=excluded.notes
                """,
                (
                    result.backup_id,
                    result.manifest.created_utc,
                    int(total_size),
                    int(options.include_vectors),
                    int(options.include_thumbs),
                    options.note,
                ),
            )
            conn.commit()
        self.apply_retention()
        return result

    # ------------------------------------------------------------------
    def list_backups(self) -> List[BackupSummary]:
        summaries: List[BackupSummary] = []
        base = self._working_dir / "backups"
        with self._connect_catalog() as conn:
            rows = conn.execute(
                "SELECT id, created_utc, size_bytes, include_vectors, include_thumbs, verified, notes FROM backups ORDER BY created_utc DESC"
            ).fetchall()
        for row in rows:
            backup_id = str(row["id"])
            path = base / backup_id
            summary = BackupSummary(
                id=backup_id,
                created_utc=str(row.get("created_utc") or ""),
                size_bytes=int(row.get("size_bytes") or 0),
                include_vectors=bool(row.get("include_vectors")),
                include_thumbs=bool(row.get("include_thumbs")),
                verified=bool(row.get("verified")),
                notes=row.get("notes"),
                path=path,
            )
            summaries.append(summary)
        return summaries

    # ------------------------------------------------------------------
    def verify_snapshot(self, backup_id: str) -> Dict[str, object]:
        result = verify_backup(self._working_dir, backup_id, logger=self._logger)
        with self._connect_catalog() as conn:
            conn.execute("UPDATE backups SET verified=1 WHERE id=?", (backup_id,))
            conn.commit()
        return result

    # ------------------------------------------------------------------
    def restore_snapshot(self, backup_id: str, *, include_settings: bool = False) -> Dict[str, object]:
        with self._quiesce("restore"):
            result = restore_backup(
                self._working_dir,
                backup_id,
                logger=self._logger,
                include_settings=include_settings,
            )
        return result

    # ------------------------------------------------------------------
    def apply_retention(self) -> RetentionSummary:
        policy = self._retention_policy()
        summary = apply_retention(self._working_dir, policy, logger=self._logger)
        if summary.removed:
            with self._connect_catalog() as conn:
                conn.executemany("DELETE FROM backups WHERE id=?", ((backup_id,) for backup_id in summary.removed))
                conn.commit()
        return summary

    # ------------------------------------------------------------------
    def update_note(self, backup_id: str, note: Optional[str]) -> None:
        with self._connect_catalog() as conn:
            conn.execute("UPDATE backups SET notes=? WHERE id=?", (note, backup_id))
            conn.commit()
        manifest_path = self._working_dir / "backups" / backup_id / "manifest.json"
        if manifest_path.exists():
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                data["notes"] = note
                manifest_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            except Exception:
                pass


__all__ = [
    "BackupError",
    "BackupOptions",
    "BackupService",
    "BackupSummary",
    "RetentionPolicy",
    "RetentionSummary",
]
