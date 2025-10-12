#!/usr/bin/env python3
"""Idempotent migrations and environment preparation for VideoCatalog."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from audit.baseline import ensure_table as ensure_audit_table
from backup.api import _BACKUPS_TABLE_SQL  # type: ignore
from core.db import connect
from core.paths import ensure_working_dir_structure, get_exports_dir, get_logs_dir, resolve_working_dir
from core.settings import load_settings, save_settings
from quality.store import ensure_tables as ensure_quality_tables
from textlite.store import ensure_tables as ensure_textlite_tables

LOGGER = logging.getLogger("videocatalog.upgrade_db")

_TRIGGER_TARGETS: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "movies": ("catalog.movie.upsert", ("id", "item_id", "drive", "path", "folder_path", "updated_utc")),
    "tv_series": ("catalog.tv.upsert", ("id", "series_id", "drive", "title", "updated_utc")),
    "tv_episodes": (
        "catalog.tv.upsert",
        ("id", "series_id", "season_id", "episode_path", "drive", "updated_utc"),
    ),
    "video_quality": (
        "catalog.quality.upsert",
        ("path", "score", "updated_utc", "drive", "container", "duration_s"),
    ),
    "textlite_preview": (
        "catalog.textlite.upsert",
        ("path", "kind", "updated_utc", "bytes_sampled", "lines_sampled"),
    ),
}

_VECTOR_KINDS: Dict[str, str] = {
    "movies": "catalog_movie",
    "tv_series": "catalog_tv",
    "tv_episodes": "catalog_tv",
    "video_quality": "quality",
    "textlite_preview": "textlite",
}


@dataclass(slots=True)
class UpgradeResult:
    working_dir: Path
    catalog_path: Path
    updated_settings: bool = False
    created_paths: List[Path] | None = None
    executed: List[str] | None = None


def _configure_logging(log_path: Optional[Path]) -> None:
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    formatter = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    handlers[0].setFormatter(formatter)
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
    logging.basicConfig(level=logging.INFO, handlers=handlers)


def _ensure_settings(working_dir: Path) -> Tuple[Dict[str, object], bool, Path]:
    settings = load_settings(working_dir)
    updated = False

    user_profile = os.environ.get("USERPROFILE") or str(Path.home())
    catalog_dir = Path(user_profile) / "VideoCatalog"
    catalog_dir.mkdir(parents=True, exist_ok=True)
    catalog_path = catalog_dir / "catalog.db"

    raw_path = settings.get("catalog_db")
    expanded = os.path.expandvars(os.path.expanduser(str(raw_path or catalog_path)))
    if "%" in expanded:
        resolved_path = catalog_path
        settings["catalog_db"] = str(resolved_path)
        updated = True
    else:
        resolved_path = Path(expanded)
        try:
            resolved_path.parent.mkdir(parents=True, exist_ok=True)
            test_file = resolved_path.parent / ".perm_check"
            with open(test_file, "w", encoding="utf-8") as handle:
                handle.write("ok")
            test_file.unlink(missing_ok=True)
        except Exception:
            LOGGER.warning("Catalog path %s not writable; falling back to %s", resolved_path, catalog_path)
            resolved_path = catalog_path
            settings["catalog_db"] = str(resolved_path)
            updated = True

    if not str(raw_path or "").strip() or os.path.normcase(str(resolved_path)) != os.path.normcase(str(raw_path or "")):
        settings["catalog_db"] = str(resolved_path)
        updated = True

    server_cfg = settings.get("server") if isinstance(settings.get("server"), dict) else {}
    api_cfg = settings.get("api") if isinstance(settings.get("api"), dict) else {}
    if server_cfg.get("host") != "127.0.0.1":
        server_cfg["host"] = "127.0.0.1"
        updated = True
    if server_cfg.get("lan_refuse") is not True:
        server_cfg["lan_refuse"] = True
        updated = True
    settings["server"] = server_cfg

    if not api_cfg.get("api_key"):
        api_cfg["api_key"] = "localdev"
        updated = True
    settings["api"] = api_cfg

    if updated:
        save_settings(settings, working_dir)
        LOGGER.info("Persisted updated settings to %s", working_dir / "settings.json")

    return settings, updated, Path(str(resolved_path))


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cursor = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    )
    return cursor.fetchone() is not None


def _table_has_rowid(conn: sqlite3.Connection, table: str) -> bool:
    cursor = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    row = cursor.fetchone()
    if not row or row[0] is None:
        return True
    return "WITHOUT ROWID" not in row[0].upper()


def _table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    cursor = conn.execute(f"PRAGMA table_info('{table.replace("'", "''")}')")
    return [row[1] for row in cursor.fetchall()]


def _identifier_expression(columns: Sequence[str]) -> str:
    for candidate in ("id", "item_id", "path", "folder_path", "episode_path", "series_id"):
        if candidate in columns:
            return f"NEW.{candidate}"
    return "NEW.rowid"


def _build_payload_expr(table: str, columns: Sequence[str], include_rowid: bool) -> str:
    pairs: List[str] = ["'table'", f"'{table}'"]
    if include_rowid:
        pairs.extend(["'rowid'", "NEW.rowid"])
    for name in columns:
        if name in {"updated_utc", "id", "item_id", "path", "drive", "folder_path", "series_id", "season_id", "episode_path", "kind"}:
            pairs.extend([f"'{name}'", f"NEW.{name}"])
    if "score" in columns:
        pairs.extend(["'score'", "NEW.score"])
    if "title" in columns:
        pairs.extend(["'title'", "NEW.title"])
    if "bytes_sampled" in columns:
        pairs.extend(["'bytes_sampled'", "NEW.bytes_sampled"])
    if "lines_sampled" in columns:
        pairs.extend(["'lines_sampled'", "NEW.lines_sampled"])
    return f"json_object({', '.join(pairs)})"


def _ensure_learning_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS learn_examples (
            path TEXT PRIMARY KEY,
            label INTEGER NOT NULL,
            label_source TEXT,
            ts_utc TEXT NOT NULL,
            features_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS learn_models (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version TEXT NOT NULL,
            algo TEXT NOT NULL,
            calibrated TEXT NOT NULL,
            onnx_path TEXT,
            metrics_json TEXT NOT NULL,
            created_utc TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS learn_config (
            k INTEGER NOT NULL DEFAULT 5,
            min_labels INTEGER NOT NULL DEFAULT 200,
            retrain_every_labels INTEGER NOT NULL DEFAULT 100,
            class_weight TEXT NOT NULL DEFAULT 'balanced',
            active_topN INTEGER NOT NULL DEFAULT 200,
            active_strategy TEXT NOT NULL DEFAULT 'uncertainty_diversity'
        )
        """
    )


def _ensure_event_triggers(conn: sqlite3.Connection) -> List[str]:
    created: List[str] = []
    for table, (event_kind, _) in _TRIGGER_TARGETS.items():
        if not _table_exists(conn, table):
            continue
        columns = _table_columns(conn, table)
        include_rowid = _table_has_rowid(conn, table)
        payload_expr = _build_payload_expr(table, columns, include_rowid)
        vector_kind = _VECTOR_KINDS.get(table)
        identifier_expr = _identifier_expression(columns)
        doc_expr = f"printf('{table}:%s', {identifier_expr})"

        trigger_insert = f"trg_{table}_ai_events"
        trigger_update = f"trg_{table}_au_events"

        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {trigger_insert}
            AFTER INSERT ON {table}
            BEGIN
                INSERT INTO events_queue(kind, payload_json)
                VALUES('{event_kind}', {payload_expr});
                INSERT INTO vectors_pending(doc_id, kind)
                VALUES({doc_expr}, '{vector_kind or table}')
                ON CONFLICT(doc_id) DO UPDATE SET ts_utc=strftime('%Y-%m-%dT%H:%M:%SZ','now');
            END;
            """
        )
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {trigger_update}
            AFTER UPDATE ON {table}
            BEGIN
                INSERT INTO events_queue(kind, payload_json)
                VALUES('{event_kind}', {payload_expr});
                INSERT INTO vectors_pending(doc_id, kind)
                VALUES({doc_expr}, '{vector_kind or table}')
                ON CONFLICT(doc_id) DO UPDATE SET ts_utc=strftime('%Y-%m-%dT%H:%M:%SZ','now');
            END;
            """
        )
        created.extend([trigger_insert, trigger_update])
    return created


def _ensure_catalog_schema(catalog_path: Path) -> List[str]:
    executed: List[str] = []
    conn = connect(catalog_path, read_only=False, check_same_thread=False)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events_queue (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                kind TEXT NOT NULL,
                payload_json TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_queue_kind_seq ON events_queue(kind, seq)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vectors_pending (
                doc_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                ts_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
            )
            """
        )
        executed.append("events_queue")
        executed.append("vectors_pending")

        ensure_quality_tables(conn)
        ensure_textlite_tables(conn)
        _ensure_learning_tables(conn)
        ensure_audit_table(conn)
        conn.execute(_BACKUPS_TABLE_SQL)
        executed.extend([
            "quality_tables",
            "textlite_tables",
            "learning_tables",
            "audit_baseline",
            "backups",
        ])

        created_triggers = _ensure_event_triggers(conn)
        executed.extend(created_triggers)

        conn.commit()
    finally:
        conn.close()
    return executed


def _ensure_orchestrator_schema(working_dir: Path) -> List[str]:
    executed: List[str] = []
    db_path = working_dir / "data" / "orchestrator.db"
    conn = connect(db_path, read_only=False, check_same_thread=False)
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_checkpoints (
                job_id INTEGER PRIMARY KEY,
                ckpt_json TEXT NOT NULL,
                updated_utc TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS resource_locks (
                name TEXT PRIMARY KEY,
                owner TEXT,
                lease_utc TEXT,
                ttl_sec INTEGER NOT NULL
            )
            """
        )
        conn.commit()
        executed.extend([
            "orchestrator.jobs",
            "orchestrator.job_events",
            "orchestrator.settings",
            "orchestrator.job_checkpoints",
            "orchestrator.resource_locks",
        ])
    finally:
        conn.close()
    return executed


def _ensure_web_metrics_schema(working_dir: Path) -> List[str]:
    db_path = working_dir / "data" / "web_metrics.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS web_metrics (
                ts_utc TEXT NOT NULL,
                series TEXT NOT NULL,
                labels_json TEXT NOT NULL,
                value REAL NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()
    return ["web_metrics"]


def prepare_environment(*, log_path: Optional[Path], prepare_only: bool) -> UpgradeResult:
    _configure_logging(log_path)
    working_dir = resolve_working_dir()
    ensure_working_dir_structure(working_dir)
    exports_dir = get_exports_dir(working_dir)
    logs_dir = get_logs_dir(working_dir)
    created = [exports_dir, exports_dir / "backups", logs_dir, working_dir / "backups", working_dir / "vectors"]

    settings, updated, catalog_path = _ensure_settings(working_dir)
    LOGGER.info("Working directory: %s", working_dir)
    LOGGER.info("Catalog path: %s", catalog_path)

    executed: List[str] = []
    if not prepare_only:
        executed.extend(_ensure_catalog_schema(catalog_path))
        executed.extend(_ensure_orchestrator_schema(working_dir))
        executed.extend(_ensure_web_metrics_schema(working_dir))

    wal_path = Path(str(catalog_path) + "-wal")
    shm_path = Path(str(catalog_path) + "-shm")
    for path in (wal_path, shm_path):
        if path.exists():
            try:
                path.unlink()
                LOGGER.info("Removed stray journal file %s", path)
            except Exception as exc:
                LOGGER.warning("Unable to remove %s: %s", path, exc)

    return UpgradeResult(
        working_dir=working_dir,
        catalog_path=catalog_path,
        updated_settings=updated,
        created_paths=created,
        executed=executed,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Upgrade VideoCatalog SQLite schemas")
    parser.add_argument("--log", dest="log_path", default=None, help="Optional log file path")
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Only update settings and directory structure without modifying databases.",
    )
    args = parser.parse_args(argv)

    log_path = Path(args.log_path).expanduser().resolve() if args.log_path else None

    try:
        result = prepare_environment(log_path=log_path, prepare_only=bool(args.prepare_only))
    except Exception as exc:
        LOGGER.exception("Upgrade failed: %s", exc)
        return 1

    summary = {
        "working_dir": str(result.working_dir),
        "catalog_path": str(result.catalog_path),
        "updated_settings": bool(result.updated_settings),
        "executed": result.executed or [],
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
