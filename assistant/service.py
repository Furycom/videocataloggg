from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from .config import AssistantSettings
from .controller import AssistantController
from .llm_client import LLMClient
from .rag import VectorIndex
from .runtime import RuntimeManager
from .tools import AssistantTooling

LOGGER = logging.getLogger("videocatalog.assistant.service")

SYSTEM_PROMPT = """
You are a VideoCatalog assistant integrated in a Windows desktop app. You MUST obey these policies:
- Never rename, move, delete or modify media files. Media folders are read-only.
- Use provided tools to inspect the SQLite catalog database or semantic index when necessary.
- Prefer concise answers with numbered steps. Mention sources (table names, titles) when appropriate.
- If unsure or lacking permission, explain the limitation instead of guessing.
""".strip()


@dataclass(slots=True)
class AssistantStatus:
    runtime: str
    model: str
    uses_gpu: bool
    rag_enabled: bool
    tool_budget: int
    remaining_budget: int


class AssistantService:
    def __init__(self, settings_payload: Dict[str, object], working_dir: Path, db_path: Path) -> None:
        self.settings = AssistantSettings.from_settings(settings_payload)
        self.working_dir = working_dir
        self.db_path = db_path
        self.vector_index = VectorIndex(self.settings, db_path, working_dir)
        self.runtime_manager = RuntimeManager(self.settings, working_dir)
        tmdb_key = self._resolve_tmdb_key(settings_payload)
        self.tooling = AssistantTooling(self.settings, db_path, working_dir, self.vector_index, tmdb_api_key=tmdb_key)
        self.controller: Optional[AssistantController] = None
        self.history: List[BaseMessage] = []
        self.session_id: Optional[str] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def ensure_ready(self) -> AssistantStatus:
        if not self.settings.enable:
            raise RuntimeError("Assistant is disabled in settings.")
        runtime = self.runtime_manager.ensure_runtime()
        if self.settings.rag.refresh_on_start:
            self.vector_index.ensure_ready(force=True)
        else:
            self.vector_index.ensure_ready()
        self.tooling.reset_budget(self.settings.tool_budget)
        client = LLMClient(runtime, self.settings.model, temperature=self.settings.temperature, ctx=self.settings.ctx)
        self.controller = AssistantController(client, self.tooling, system_prompt=SYSTEM_PROMPT)
        self._ensure_memory_tables()
        return AssistantStatus(
            runtime=runtime.name,
            model=self.settings.model,
            uses_gpu=runtime.uses_gpu,
            rag_enabled=self.settings.rag.enable,
            tool_budget=self.settings.tool_budget,
            remaining_budget=self.settings.tool_budget,
        )

    def start_session(self, scope: str = "global") -> AssistantStatus:
        with self._lock:
            status = self.ensure_ready()
            self.session_id = self._ensure_session(scope)
            self.history = self._load_recent_history(limit=12)
            return status

    def ask(self, text: str, *, use_rag: bool = True) -> Dict[str, object]:
        with self._lock:
            if self.controller is None:
                status = self.start_session()
            else:
                status = self.status()
            rag_snippets = []
            if use_rag and self.settings.rag.enable:
                hits = self.vector_index.search(text)
                for hit in hits[: self.settings.rag.top_k]:
                    rag_snippets.append(f"{hit.metadata.get('title') or hit.metadata.get('path')}: {hit.text[:200]}")
            prompt = text
            if rag_snippets:
                prompt = f"{text}\n\nContext from catalog:\n" + "\n".join(rag_snippets)
            human = HumanMessage(content=prompt)
            messages: List[BaseMessage] = [SystemMessage(content=SYSTEM_PROMPT), *self.history, human]
            if self.controller is None:
                raise RuntimeError("Assistant controller not ready")
            final_state = self.controller.run(messages)
            history = final_state["messages"]
            responses = [msg for msg in history if isinstance(msg, AIMessage)]
            answer = responses[-1] if responses else AIMessage(content="No answer produced.")
            self.history = [msg for msg in history if isinstance(msg, (HumanMessage, AIMessage))][-8:]
            self._persist_exchange(text, answer.content)
            tool_messages = [msg for msg in history if isinstance(msg, ToolMessage)]
            tool_log = []
            for tool_msg in tool_messages[-4:]:
                try:
                    payload = json.loads(tool_msg.content)
                except Exception:
                    payload = {"raw": tool_msg.content}
                tool_log.append({"tool": tool_msg.name, "payload": payload})
            return {
                "answer": answer.content,
                "tool_log": tool_log,
                "status": self.status(),
            }

    def refresh_index(self) -> None:
        self.vector_index.refresh()

    def status(self) -> AssistantStatus:
        runtime = self.runtime_manager.current_runtime()
        uses_gpu = runtime.uses_gpu if runtime else False
        remaining = max(0, self.settings.tool_budget - self.tooling.calls)
        return AssistantStatus(
            runtime=runtime.name if runtime else "unknown",
            model=self.settings.model,
            uses_gpu=uses_gpu,
            rag_enabled=self.settings.rag.enable,
            tool_budget=self.settings.tool_budget,
            remaining_budget=remaining,
        )

    def shutdown(self) -> None:
        self.runtime_manager.shutdown()

    # ------------------------------------------------------------------
    def _ensure_session(self, scope: str) -> str:
        session_id = str(uuid.uuid4())
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS assistant_sessions (
                    id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    created_utc TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS assistant_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_utc TEXT NOT NULL DEFAULT (datetime('now')),
                    FOREIGN KEY(session_id) REFERENCES assistant_sessions(id)
                )
                """
            )
            conn.execute("INSERT INTO assistant_sessions(id, scope) VALUES(?, ?)", (session_id, scope))
            conn.commit()
        return session_id

    def _persist_exchange(self, question: str, answer: str) -> None:
        if not self.session_id:
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO assistant_messages(session_id, role, content) VALUES(?,?,?)",
                (self.session_id, "user", question),
            )
            conn.execute(
                "INSERT INTO assistant_messages(session_id, role, content) VALUES(?,?,?)",
                (self.session_id, "assistant", answer),
            )
            conn.commit()

    def _load_recent_history(self, limit: int = 12) -> List[BaseMessage]:
        if not self.session_id:
            return []
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT role, content FROM assistant_messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (self.session_id, limit),
            ).fetchall()
        messages: List[BaseMessage] = []
        for row in reversed(rows):
            if row["role"] == "assistant":
                messages.append(AIMessage(content=row["content"]))
            else:
                messages.append(HumanMessage(content=row["content"]))
        return messages

    def _ensure_memory_tables(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS assistant_sessions (
                    id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    created_utc TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS assistant_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_utc TEXT NOT NULL DEFAULT (datetime('now')),
                    FOREIGN KEY(session_id) REFERENCES assistant_sessions(id)
                )
                """
            )
            conn.commit()

    @staticmethod
    def _resolve_tmdb_key(settings_payload: Dict[str, object]) -> Optional[str]:
        structure = settings_payload.get("structure") if isinstance(settings_payload.get("structure"), dict) else {}
        tmdb = structure.get("tmdb") if isinstance(structure.get("tmdb"), dict) else {}
        key = tmdb.get("api_key")
        if isinstance(key, str) and key.strip():
            return key.strip()
        return None
