from __future__ import annotations

import csv
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .apiguard import ApiGuard
from .config import AssistantSettings
from .rag import VectorIndex

LOGGER = logging.getLogger("videocatalog.assistant.tools")

READ_ONLY_SQL = re.compile(r"^\s*select\b", re.IGNORECASE)


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
    ) -> None:
        self.settings = settings
        self.db_path = db_path
        self.working_dir = working_dir
        self.vector_index = vector_index
        self.tmdb_api_key = tmdb_api_key
        self.budget = settings.tool_budget
        self.calls = 0
        self.api_guard = ApiGuard(working_dir, tmdb_api_key)

    # ------------------------------------------------------------------
    def reset_budget(self, budget: Optional[int] = None) -> None:
        self.calls = 0
        if budget is not None:
            self.budget = budget

    def definitions(self) -> List[ToolDefinition]:
        return [
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
                name="db_search_semantic",
                description="Search the semantic vector index for catalog items.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "top_k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
                        "min_score": {"type": "number", "minimum": 0, "maximum": 1},
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
                name="export_csv",
                description="Create a CSV export within the working_dir/exports folder.",
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
            ToolDefinition(
                name="api_tmdb_lookup_cached",
                description="Query TMDB through the local API guard cache.",
                parameters={
                    "type": "object",
                    "properties": {
                        "endpoint": {"type": "string"},
                        "params": {"type": "object"},
                    },
                    "required": ["endpoint"],
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
        ]

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
        result = handler(**resolved_args)
        LOGGER.debug("Tool %s executed (call %d/%d)", name, self.calls, self.budget)
        return {"name": name, "arguments": resolved_args, "result": result}

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
        return {
            "rows": [dict(row) for row in rows],
            "count": len(rows),
        }

    def _tool_db_search_semantic(
        self,
        query: str,
        top_k: Optional[int] = None,
        min_score: Optional[float] = None,
        filters: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        hits = self.vector_index.search(query, top_k=top_k, min_score=min_score)
        filtered = []
        filters = filters or {}
        for hit in hits:
            meta = hit.metadata
            if filters.get("type") and meta.get("type") != filters.get("type"):
                continue
            if filters.get("drive_label") and meta.get("drive") != filters.get("drive_label"):
                continue
            filtered.append({"doc_id": hit.doc_id, "score": hit.score, "metadata": meta})
        return {"results": filtered}

    def _tool_get_paths_for_title(self, title: str, year: Optional[int] = None, limit: int = 12) -> Dict[str, Any]:
        self._ensure_db()
        limit = max(1, min(50, limit))
        like = f"%{title.strip()}%"
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            tables = self._existing_tables(conn)
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
            tables = self._existing_tables(conn)
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

    def _tool_export_csv(self, sql: str, filename: str, dry_run: bool = True, limit: int = 500) -> Dict[str, Any]:
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

    def _tool_api_tmdb_lookup_cached(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = self.api_guard.tmdb_lookup(endpoint, params=params or {})
        return {"endpoint": endpoint, "payload": payload}

    def _tool_help_open_folder(self, path: str) -> Dict[str, Any]:
        resolved = str(Path(path))
        return {"action": "open", "path": resolved}

    # ------------------------------------------------------------------
    def _ensure_db(self) -> None:
        if not self.db_path.exists():
            raise RuntimeError(f"Catalog database missing at {self.db_path}")

    @staticmethod
    def _existing_tables(conn: sqlite3.Connection) -> Iterable[str]:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        return [row[0] for row in rows]
