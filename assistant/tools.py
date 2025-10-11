from __future__ import annotations

import csv
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, TYPE_CHECKING

from .apiguard import ApiGuard
from .config import AssistantSettings
from .rag import VectorIndex
from diagnostics.tools import DiagnosticsTools

LOGGER = logging.getLogger("videocatalog.assistant.tools")

READ_ONLY_SQL = re.compile(r"^\s*select\b", re.IGNORECASE)


if TYPE_CHECKING:  # pragma: no cover - typing only
    from assistant_monitor import AssistantDashboard


class ToolBudgetExceeded(RuntimeError):
    pass


@dataclass(slots=True)
class ToolDefinition:
    name: str
    description: str
    parameters: Dict[str, Any]


class AssistantTooling:
    def __init__(
        self,
        settings: AssistantSettings,
        db_path: Path,
        working_dir: Path,
        vector_index: VectorIndex,
        *,
        tmdb_api_key: Optional[str] = None,
        dashboard: Optional["AssistantDashboard"] = None,
    ) -> None:
        self.settings = settings
        self.db_path = db_path
        self.working_dir = working_dir
        self.vector_index = vector_index
        self.tmdb_api_key = tmdb_api_key
        self.budget = settings.tool_budget
        self.calls = 0
        self.dashboard = dashboard
        self.api_guard = ApiGuard(working_dir, tmdb_api_key, dashboard=dashboard)
        self._ensure_views()
        self.diagnostics = DiagnosticsTools(working_dir)

    # ------------------------------------------------------------------
    def reset_budget(self, budget: Optional[int] = None) -> None:
        self.calls = 0
        if budget is not None:
            self.budget = budget
        if self.dashboard:
            self.dashboard.update_tool_budget(self.budget, self.calls, self.budget - self.calls)

    def definitions(self) -> List[ToolDefinition]:
        definitions = [
            ToolDefinition(
                name="db_get_low_confidence",
                description="Fetch low-confidence catalog items (episodes by default).",
                parameters={
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["episode"], "default": "episode"},
                        "min": {"type": "number", "default": 0.0},
                        "max": {"type": "number", "default": 0.79},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
                    },
                },
            ),
            ToolDefinition(
                name="db_get_candidates",
                description="Retrieve stored ID candidates and metadata for a media path.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "top_k": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                    },
                    "required": ["path"],
                },
            ),
            ToolDefinition(
                name="db_search_semantic",
                description="Search the semantic vector index for catalog items.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "top_k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
                        "min_score": {"type": "number", "minimum": 0, "maximum": 1},
                        "filter": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string"},
                                "drive_label": {"type": "string"},
                            },
                        },
                        "filters": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string"},
                                "drive_label": {"type": "string"},
                            },
                        },
                    },
                    "required": ["query"],
                },
            ),
            ToolDefinition(
                name="api_tmdb_lookup_cached",
                description="Query TMDB through the local API guard cache (episodes only).",
                parameters={
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["episode"], "default": "episode"},
                        "title": {"type": "string"},
                        "year": {"type": ["integer", "null"]},
                        "season": {"type": ["integer", "null"]},
                        "episode": {"type": ["integer", "null"]},
                        "top_k": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3},
                        "endpoint": {"type": "string"},  # legacy
                        "params": {"type": "object"},      # legacy
                    },
                    "required": [],
                },
            ),
            ToolDefinition(
                name="export_csv",
                description="Create a CSV export within the working_dir/exports folder (dry-run by default).",
                parameters={
                    "type": "object",
                    "properties": {
                        "rows": {"type": "array", "items": {"type": "object"}},
                        "name": {"type": "string"},
                        "dry_run": {"type": "boolean", "default": True},
                        "sql": {"type": "string"},
                        "filename": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 2000},
                    },
                },
            ),
            ToolDefinition(
                name="help_open_folder",
                description="Return a shell-open plan for a path without modifying files.",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            ),
            ToolDefinition(
                name="diag_run_preflight",
                description="Run diagnostics preflight checks (GPU, tools, settings).",
                parameters={"type": "object", "properties": {}, "required": []},
            ),
            ToolDefinition(
                name="diag_run_smoke",
                description="Run lightweight smoke tests for selected subsystems.",
                parameters={
                    "type": "object",
                    "properties": {
                        "subsystems": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "budget": {"type": "integer", "minimum": 1, "maximum": 6},
                    },
                },
            ),
            ToolDefinition(
                name="diag_get_logs",
                description="Fetch recent diagnostics log lines.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "object"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200},
                    },
                },
            ),
            ToolDefinition(
                name="diag_get_metrics",
                description="Compute latency and cache metrics from diagnostics logs.",
                parameters={
                    "type": "object",
                    "properties": {
                        "window_min": {"type": "integer", "minimum": 1, "maximum": 720, "default": 60},
                    },
                },
            ),
            ToolDefinition(
                name="diag_sql",
                description="Run a read-only SELECT against diagnostics views (preflight_checks, smoke_checks, log_events).",
                parameters={
                    "type": "object",
                    "properties": {
                        "view": {"type": "string", "enum": ["preflight_checks", "smoke_checks", "log_events"]},
                        "where": {"type": "object"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
                    },
                    "required": ["view"],
                },
            ),
        ]
        # Legacy tools remain available for existing UI flows during migration.
        definitions.extend(
            [
                ToolDefinition(
                    name="db_query_sql",
                    description="Run a read-only SELECT query against catalog views.",
                    parameters={
                        "type": "object",
                        "properties": {
                            "sql": {"type": "string", "description": "SELECT query using only catalog views."},
                            "params": {
                                "type": "array",
                                "items": {"type": ["string", "number", "null"]},
                                "default": [],
                            },
                            "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
                        },
                        "required": ["sql"],
                    },
                ),
                ToolDefinition(
                    name="get_paths_for_title",
                    description="Return candidate file system paths for a given title and optional year.",
                    parameters={
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "year": {"type": ["integer", "null"]},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 12},
                        },
                        "required": ["title"],
                    },
                ),
                ToolDefinition(
                    name="get_low_confidence_items",
                    description="Fetch items pending review due to low confidence signals.",
                    parameters={"type": "object", "properties": {}, "required": []},
                ),
                ToolDefinition(
                    name="create_task",
                    description="Create a follow-up task for manual review (stored in DB).",
                    parameters={
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "details": {"type": "string"},
                            "priority": {"type": "string", "enum": ["low", "normal", "high"], "default": "normal"},
                        },
                        "required": ["title"],
                    },
                ),
                ToolDefinition(
                    name="export_csv_sql",
                    description="(Legacy) Export rows via SQL query.",
                    parameters={
                        "type": "object",
                        "properties": {
                            "sql": {"type": "string"},
                            "filename": {"type": "string"},
                            "dry_run": {"type": "boolean", "default": True},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 500},
                        },
                        "required": ["sql", "filename"],
                    },
                ),
            ]
        )
        return definitions

    # ------------------------------------------------------------------
    def execute(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if not self.settings.tools_enabled:
            raise RuntimeError("Tool execution disabled for this session.")
        if self.calls >= self.budget:
            raise ToolBudgetExceeded("Tool budget exhausted for this session.")
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            raise RuntimeError(f"Unknown tool: {name}")
        self.calls += 1
        resolved_args = dict(arguments or {})
        success = False
        start = time.perf_counter()
        try:
            result = handler(**resolved_args)
            success = True
            LOGGER.debug("Tool %s executed (call %d/%d)", name, self.calls, self.budget)
            return {"name": name, "arguments": resolved_args, "result": result}
        finally:
            duration_ms = (time.perf_counter() - start) * 1000.0
            remaining = max(0, self.budget - self.calls)
            if self.dashboard:
                try:
                    self.dashboard.record_tool_call(name, duration_ms, success=success)
                    self.dashboard.update_tool_budget(self.budget, self.calls, remaining)
                except Exception:  # pragma: no cover - defensive instrumentation
                    LOGGER.debug("Dashboard instrumentation failed for tool %s", name)

    # ------------------------------------------------------------------
    def _tool_db_query_sql(self, sql: str, params: Optional[List[Any]] = None, limit: int = 100) -> Dict[str, Any]:
        self._ensure_db()
        if not READ_ONLY_SQL.match(sql):
            raise RuntimeError("Only SELECT statements are allowed.")
        params = params or []
        limit = max(1, min(500, int(limit)))
        sql_wrapped = f"SELECT * FROM ({sql}) LIMIT {limit}"
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql_wrapped, params).fetchall()
        return {"rows": [dict(row) for row in rows], "count": len(rows)}

    def _tool_db_search_semantic(
        self,
        query: str,
        top_k: Optional[int] = None,
        min_score: Optional[float] = None,
        filter: Optional[Dict[str, str]] = None,
        filters: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        hits = self.vector_index.search(query, top_k=top_k, min_score=min_score)
        filters = filter or filters or {}
        filtered = []
        for hit in hits:
            meta = hit.metadata
            if filters.get("type") and meta.get("type") != filters.get("type"):
                continue
            if filters.get("drive_label") and meta.get("drive") != filters.get("drive_label"):
                continue
            filtered.append({"doc_id": hit.doc_id, "score": hit.score, "metadata": meta, "text": hit.text})
        return {"results": filtered}

    def _tool_db_get_low_confidence(
        self,
        type: str = "episode",
        min: float = 0.0,
        max: float = 0.79,
        limit: int = 50,
    ) -> Dict[str, Any]:
        if type != "episode":
            return {"items": []}
        self._ensure_db()
        limit = max(1, min(100, int(limit)))
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            tables = set(self._existing_tables(conn))
            if "view_low_conf_episodes" in tables:
                base_sql = (
                    "SELECT path, parsed_title, parsed_year, season, episode_numbers_json, confidence, reasons, ids_json "
                    "FROM view_low_conf_episodes"
                )
            elif "tv_episode_profile" in tables:
                base_sql = (
                    "SELECT episode_path AS path, parsed_title, parsed_year, season_number AS season, "
                    "episode_numbers_json, confidence, confidence_reasons AS reasons, ids_json "
                    "FROM tv_episode_profile"
                )
            else:
                return {"items": []}
            where = " WHERE 1=1"
            params: List[Any] = []
            if min is not None:
                where += " AND (confidence IS NULL OR confidence >= ?)"
                params.append(float(min))
            if max is not None:
                where += " AND (confidence IS NULL OR confidence <= ?)"
                params.append(float(max))
            sql = f"SELECT * FROM ({base_sql}{where}) ORDER BY confidence IS NULL, confidence ASC LIMIT {limit}"
            rows = conn.execute(sql, params).fetchall()
        items: List[Dict[str, Any]] = []
        for row in rows:
            ids_payload = self._load_json(row["ids_json"])
            episodes = self._normalize_episode_numbers(row["episode_numbers_json"])
            items.append(
                {
                    "path": row["path"],
                    "parsed_title": row["parsed_title"],
                    "parsed_year": row["parsed_year"],
                    "season": row["season"],
                    "episode": episodes[0] if episodes else None,
                    "episodes": episodes,
                    "confidence": float(row["confidence"]) if row["confidence"] is not None else None,
                    "reasons": self._normalize_reasons(row["reasons"]),
                    "ids": ids_payload,
                }
            )
        return {"items": items}

    def _tool_db_get_candidates(self, path: str, top_k: int = 5) -> Dict[str, Any]:
        self._ensure_db()
        top_k = max(1, min(10, int(top_k)))
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT episode_path, parsed_title, parsed_year, season_number, episode_numbers_json,
                       ids_json, confidence, confidence_reasons
                FROM tv_episode_profile
                WHERE episode_path = ?
                """,
                (path,),
            ).fetchone()
            evidence_row = None
            try:
                evidence_row = conn.execute(
                    "SELECT tmdb_id, imdb_id, oshash_hit, subtitle_langs, plot_score FROM view_episode_evidence WHERE path = ?",
                    (path,),
                ).fetchone()
            except sqlite3.DatabaseError:
                evidence_row = None
        if not row:
            return {"candidates": []}
        ids_payload = self._load_json(row["ids_json"])
        candidates: List[Dict[str, Any]] = []
        base_candidate = self._candidate_from_ids(ids_payload, row)
        if base_candidate:
            candidates.append(base_candidate)
        candidates.extend(self._extract_candidate_list(ids_payload))
        candidates = candidates[:top_k]
        evidence = (
            {
                "tmdb_id": evidence_row["tmdb_id"] if evidence_row else None,
                "imdb_id": evidence_row["imdb_id"] if evidence_row else None,
                "oshash_hit": evidence_row["oshash_hit"] if evidence_row else None,
                "subtitle_langs": self._load_json(evidence_row["subtitle_langs"]) if evidence_row else None,
                "plot_score": evidence_row["plot_score"] if evidence_row else None,
            }
            if evidence_row
            else {}
        )
        return {
            "profile": {
                "path": row["episode_path"],
                "parsed_title": row["parsed_title"],
                "parsed_year": row["parsed_year"],
                "season": row["season_number"],
                "episodes": self._normalize_episode_numbers(row["episode_numbers_json"]),
                "confidence": float(row["confidence"]) if row["confidence"] is not None else None,
                "reasons": self._normalize_reasons(row["confidence_reasons"]),
            },
            "candidates": candidates,
            "evidence": evidence,
        }

    def _tool_get_paths_for_title(self, title: str, year: Optional[int] = None, limit: int = 12) -> Dict[str, Any]:
        self._ensure_db()
        limit = max(1, min(50, limit))
        like = f"%{title.strip()}%"
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            tables = set(self._existing_tables(conn))
            results: List[Dict[str, Any]] = []
            if "inventory_view" in tables:
                query = "SELECT title, year, type, drive_label, path FROM inventory_view WHERE title LIKE ?"
                params: List[Any] = [like]
                if year:
                    query += " AND year=?"
                    params.append(int(year))
                query += " LIMIT ?"
                params.append(limit)
                for row in conn.execute(query, params):
                    results.append(dict(row))
            elif "files" in tables:
                query = "SELECT path FROM files WHERE path LIKE ? LIMIT ?"
                for row in conn.execute(query, [like, limit]):
                    results.append({"path": row["path"]})
        return {"paths": results}

    def _tool_get_low_confidence_items(self) -> Dict[str, Any]:
        self._ensure_db()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            tables = set(self._existing_tables(conn))
            payload: Dict[str, Any] = {}
            if "structure_review_queue" in tables:
                rows = conn.execute(
                    "SELECT title, year, drive_label, reason, created_utc FROM structure_review_queue ORDER BY created_utc DESC LIMIT 25"
                )
                payload["structure"] = [dict(row) for row in rows]
            if "quality_alerts" in tables:
                rows = conn.execute(
                    "SELECT path, issue_code, severity, created_utc FROM quality_alerts ORDER BY created_utc DESC LIMIT 25"
                )
                payload["quality"] = [dict(row) for row in rows]
            return payload

    def _tool_create_task(self, title: str, details: Optional[str] = None, priority: str = "normal") -> Dict[str, Any]:
        self._ensure_db()
        priority = priority.lower()
        if priority not in {"low", "normal", "high"}:
            priority = "normal"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    details TEXT,
                    priority TEXT NOT NULL,
                    created_utc TEXT NOT NULL DEFAULT (datetime('now')),
                    status TEXT NOT NULL DEFAULT 'open'
                )
                """
            )
            conn.execute(
                "INSERT INTO tasks(title, details, priority) VALUES(?,?,?)",
                (title, details, priority),
            )
            conn.commit()
        return {"status": "created", "title": title, "priority": priority}

    def _tool_export_csv(
        self,
        rows: Optional[List[Dict[str, Any]]] = None,
        name: Optional[str] = None,
        dry_run: bool = True,
        sql: Optional[str] = None,
        filename: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        if sql is not None or filename is not None:
            return self._tool_export_csv_sql(
                sql=sql or "", filename=filename or name or "export.csv", dry_run=dry_run, limit=limit or 500
            )
        if rows is None or name is None:
            raise RuntimeError("Provide rows and name for export_csv when not using legacy SQL mode.")
        exports_dir = self.working_dir / "exports"
        exports_dir.mkdir(parents=True, exist_ok=True)
        safe_name = name.replace("..", "_")
        target = exports_dir / safe_name
        if dry_run:
            preview = rows[: min(10, len(rows))]
            return {"dry_run": True, "row_count": len(rows), "preview": preview, "path": str(target)}
        fieldnames = sorted({key for row in rows for key in row.keys()}) if rows else []
        with target.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            if fieldnames:
                writer.writeheader()
            for row in rows:
                writer.writerow(row)
        return {"dry_run": False, "row_count": len(rows), "path": str(target)}

    def _tool_export_csv_sql(
        self, sql: str, filename: str, dry_run: bool = True, limit: int = 500
    ) -> Dict[str, Any]:
        self._ensure_db()
        if not READ_ONLY_SQL.match(sql):
            raise RuntimeError("Only SELECT statements are allowed for exports.")
        limit = max(1, min(2000, limit))
        sql_wrapped = f"SELECT * FROM ({sql}) LIMIT {limit}"
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql_wrapped).fetchall()
        rows_payload = [dict(row) for row in rows]
        if dry_run:
            return {"rows": rows_payload[:10], "row_count": len(rows_payload), "dry_run": True}
        exports_dir = self.working_dir / "exports"
        exports_dir.mkdir(parents=True, exist_ok=True)
        safe_name = filename.replace("..", "_")
        target = exports_dir / safe_name
        with target.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows_payload[0].keys()) if rows_payload else [])
            if rows_payload:
                writer.writeheader()
                writer.writerows(rows_payload)
        return {"path": str(target), "row_count": len(rows_payload), "dry_run": False}

    def _tool_api_tmdb_lookup_cached(
        self,
        kind: str = "episode",
        title: Optional[str] = None,
        year: Optional[int] = None,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        top_k: int = 3,
        endpoint: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        # Legacy signature support
        if endpoint:
            payload = self.api_guard.tmdb_lookup(endpoint, params=params or {})
            return {"endpoint": endpoint, "payload": payload}
        if not self.tmdb_api_key:
            raise RuntimeError("TMDB API key missing; cannot perform lookup.")
        if kind != "episode":
            raise RuntimeError("Only episode lookups are supported in this build.")
        if not title:
            raise RuntimeError("Title is required for TMDB lookup.")
        params = {"query": title}
        if year is not None:
            params["first_air_date_year"] = int(year)
        payload = self.api_guard.tmdb_lookup("search/tv", params=params)
        results: List[Dict[str, Any]] = []
        shows = payload.get("results") if isinstance(payload, dict) else None
        shows = shows or []
        top_k = max(1, min(5, int(top_k)))
        for show in shows[:top_k]:
            candidate = {
                "series_id": show.get("id"),
                "name": show.get("name"),
                "first_air_date": show.get("first_air_date"),
                "popularity": show.get("popularity"),
            }
            if season is not None and episode is not None and show.get("id"):
                try:
                    episode_payload = self.api_guard.tmdb_lookup(
                        f"tv/{show['id']}/season/{int(season)}/episode/{int(episode)}",
                        params={},
                    )
                    candidate["episode"] = {
                        "id": episode_payload.get("id"),
                        "name": episode_payload.get("name"),
                        "air_date": episode_payload.get("air_date"),
                    }
                except Exception as exc:
                    candidate["episode_error"] = str(exc)
            results.append(candidate)
        return {"results": results}

    def _tool_help_open_folder(self, path: str) -> Dict[str, Any]:
        resolved = str(Path(path))
        return {"action": "open", "path": resolved}

    def _tool_diag_run_preflight(self) -> Dict[str, Any]:
        return self.diagnostics.diag_run_preflight()

    def _tool_diag_run_smoke(
        self,
        subsystems: Optional[List[str]] = None,
        budget: Optional[int] = None,
    ) -> Dict[str, Any]:
        return self.diagnostics.diag_run_smoke(subsystems, budget=budget)

    def _tool_diag_get_logs(
        self,
        query: Optional[Dict[str, Any]] = None,
        limit: int = 200,
    ) -> Dict[str, Any]:
        return self.diagnostics.diag_get_logs(query=query, limit=limit)

    def _tool_diag_get_metrics(self, window_min: int = 60) -> Dict[str, Any]:
        return self.diagnostics.diag_get_metrics(window_min=window_min)

    def _tool_diag_sql(
        self,
        view: str,
        where: Optional[Dict[str, Any]] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        return self.diagnostics.diag_sql(view, where=where, limit=limit)

    # ------------------------------------------------------------------
    def _ensure_db(self) -> None:
        if not self.db_path.exists():
            raise RuntimeError(f"Catalog database missing at {self.db_path}")

    @staticmethod
    def _existing_tables(conn: sqlite3.Connection) -> Iterable[str]:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        return [row[0] for row in rows]

    def _ensure_views(self) -> None:
        if not self.db_path.exists():
            return
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    CREATE VIEW IF NOT EXISTS view_low_conf_episodes AS
                    SELECT
                        episode_path AS path,
                        parsed_title,
                        parsed_year,
                        season_number AS season,
                        episode_numbers_json,
                        confidence,
                        confidence_reasons AS reasons,
                        ids_json
                    FROM tv_episode_profile
                    """
                )
                conn.execute(
                    """
                    CREATE VIEW IF NOT EXISTS view_episode_evidence AS
                    SELECT
                        episode_path AS path,
                        json_extract(ids_json, '$.tmdb_episode_id') AS tmdb_id,
                        json_extract(ids_json, '$.imdb_episode_id') AS imdb_id,
                        json_extract(ids_json, '$.oshash_hit') AS oshash_hit,
                        json_extract(ids_json, '$.subtitle_langs') AS subtitle_langs,
                        json_extract(ids_json, '$.plot_score') AS plot_score
                    FROM tv_episode_profile
                    """
                )
        except sqlite3.DatabaseError:
            LOGGER.debug("Assistant: unable to create helper views; continuing without them")

    @staticmethod
    def _load_json(raw: Any) -> Dict[str, Any]:
        if not raw:
            return {}
        if isinstance(raw, dict):
            return raw
        try:
            return json.loads(raw)
        except Exception:
            return {}

    @staticmethod
    def _normalize_episode_numbers(raw: Any) -> List[int]:
        if raw is None:
            return []
        if isinstance(raw, list):
            return [AssistantTooling._safe_int(item) for item in raw if AssistantTooling._safe_int(item) is not None]
        try:
            payload = json.loads(raw)
            if isinstance(payload, list):
                return [AssistantTooling._safe_int(item) for item in payload if AssistantTooling._safe_int(item) is not None]
        except Exception:
            pass
        return []

    @staticmethod
    def _safe_int(value: Any) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_reasons(raw: Any) -> List[str]:
        if not raw:
            return []
        if isinstance(raw, list):
            return [str(item) for item in raw]
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return [str(item) for item in parsed]
            except Exception:
                return [part.strip() for part in raw.split(",") if part.strip()]
        return []

    def _candidate_from_ids(self, ids: Dict[str, Any], row: sqlite3.Row) -> Optional[Dict[str, Any]]:
        if not isinstance(ids, dict):
            return None
        tmdb_episode_id = ids.get("tmdb_episode_id") or ids.get("tmdb_id")
        imdb_episode_id = ids.get("imdb_episode_id") or ids.get("imdb_id")
        if not tmdb_episode_id and not imdb_episode_id:
            return None
        episodes = self._normalize_episode_numbers(row["episode_numbers_json"])
        return {
            "tmdb_id": str(tmdb_episode_id) if tmdb_episode_id else None,
            "imdb_id": str(imdb_episode_id) if imdb_episode_id else None,
            "title": row["parsed_title"],
            "season": row["season_number"],
            "episodes": episodes,
            "score": 0.6 if tmdb_episode_id else 0.4,
            "source": "profile",
        }

    @staticmethod
    def _extract_candidate_list(ids: Dict[str, Any]) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        if not isinstance(ids, dict):
            return candidates
        for key, value in ids.items():
            if not isinstance(value, list):
                continue
            if "candidate" not in key:
                continue
            for entry in value:
                if not isinstance(entry, dict):
                    continue
                tmdb_id = entry.get("tmdb_episode_id") or entry.get("tmdb_id") or entry.get("id")
                imdb_id = entry.get("imdb_episode_id") or entry.get("imdb_id")
                title = entry.get("title") or entry.get("name")
                score = entry.get("score") or entry.get("confidence")
                candidates.append(
                    {
                        "tmdb_id": str(tmdb_id) if tmdb_id else None,
                        "imdb_id": str(imdb_id) if imdb_id else None,
                        "title": title,
                        "season": entry.get("season") or entry.get("season_number"),
                        "episodes": AssistantTooling._normalize_episode_numbers(entry.get("episodes")),
                        "score": float(score) if score is not None else 0.2,
                        "source": key,
                    }
                )
        candidates.sort(key=lambda item: item.get("score", 0), reverse=True)
        return candidates
