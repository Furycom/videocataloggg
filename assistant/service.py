from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from .config import AssistantSettings
from .controller import AssistantController
from .episode_flow import EpisodeAssistant
from .llm_client import LLMClient
from .rag import VectorIndex
from .runtime import RuntimeManager
from .tools import AssistantTooling
from assistant_monitor import AssistantDashboard, get_dashboard

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
    def __init__(
        self,
        settings_payload: Dict[str, object],
        working_dir: Path,
        db_path: Path,
        dashboard: Optional[AssistantDashboard] = None,
    ) -> None:
        self.settings = AssistantSettings.from_settings(settings_payload)
        self.working_dir = working_dir
        self.db_path = db_path
        self.vector_index = VectorIndex(self.settings, db_path, working_dir)
        self.runtime_manager = RuntimeManager(self.settings, working_dir)
        tmdb_key = self._resolve_tmdb_key(settings_payload)
        self.dashboard = dashboard or get_dashboard(working_dir, db_path, settings_payload)
        self.tooling = AssistantTooling(
            self.settings,
            db_path,
            working_dir,
            self.vector_index,
            tmdb_api_key=tmdb_key,
            dashboard=self.dashboard,
        )
        self.controller: Optional[AssistantController] = None
        self.history: List[BaseMessage] = []
        self.session_id: Optional[str] = None
        self._lock = threading.Lock()
        self.episode_assistant = EpisodeAssistant(self.tooling)

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
        self.episode_assistant = EpisodeAssistant(self.tooling)
        client = LLMClient(runtime, self.settings.model, temperature=self.settings.temperature, ctx=self.settings.ctx)
        self.controller = AssistantController(
            client,
            self.tooling,
            system_prompt=SYSTEM_PROMPT,
            dashboard=self.dashboard,
        )
        self._ensure_memory_tables()
        try:
            self.dashboard.update_runtime(
                runtime=runtime.name,
                model=self.settings.model,
                context=self.settings.ctx,
                gpu=runtime.uses_gpu,
                source="service",
            )
            self.dashboard.update_tool_budget(self.settings.tool_budget, 0, self.settings.tool_budget)
        except Exception:
            LOGGER.debug("Dashboard update failed during ensure_ready")
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
            self.ensure_ready()
            self.session_id = self._ensure_session(scope)
            self.history = self._load_recent_history(limit=12)
            return status

    def ask(self, text: str, *, use_rag: bool = True) -> Dict[str, object]:
        with self._lock:
            if self.controller is None:
                status = self.start_session()
            else:
                status = self.status()
            if self.episode_assistant.can_handle(text):
                result = self.episode_assistant.run(text)
                answer_text = result.get("answer", "No answer produced.")
                self.history = [*self.history, HumanMessage(content=text), AIMessage(content=answer_text)][-8:]
                self._persist_exchange(text, answer_text)
                return {
                    "answer": answer_text,
                    "tool_log": result.get("tool_log", []),
                    "status": self.status(),
                }
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

    def ask_context(
        self,
        item_id: str,
        item_payload: Dict[str, object],
        question: str,
        *,
        tool_budget: Optional[int] = None,
        use_rag: bool = True,
    ) -> Dict[str, object]:
        """Ask a contextual question anchored to a catalog item."""

        with self._lock:
            self.ensure_ready()
            if tool_budget is not None and tool_budget > 0:
                budget = min(int(tool_budget), self.settings.tool_budget)
                self.tooling.reset_budget(max(1, budget))
            else:
                self.tooling.reset_budget(self.settings.tool_budget)
            context_blob = json.dumps(item_payload, ensure_ascii=False, indent=2, sort_keys=True)
            prompt = (
                "You must ground your answer on the provided catalog item context.\n"
                "If a tool budget is exhausted you must stop and summarise.\n"
                "Context follows in JSON format.\n"
                f"Item ID: {item_id}\n"
                f"Context:```json\n{context_blob}\n```\n"
                "Answer the user question using markdown and list the data sources you relied on.\n"
                f"Question: {question.strip()}"
            )
            start = time.perf_counter()
            result = self.ask(prompt, use_rag=use_rag)
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            answer_text = str(result.get("answer", ""))
            tool_log = result.get("tool_log", [])
            status_payload = result.get("status")
            status_dict = (
                asdict(status_payload)
                if isinstance(status_payload, AssistantStatus)
                else status_payload
            )
            sources = [{"type": "catalog", "ref": item_id}]
            for entry in tool_log:
                payload = entry.get("payload") if isinstance(entry, dict) else None
                if isinstance(payload, dict):
                    ref = payload.get("source") or payload.get("table") or payload.get("ref")
                    if ref:
                        sources.append({"type": entry.get("tool", "tool"), "ref": str(ref)})
            formatted_tool_calls = []
            for call in tool_log:
                tool_name = call.get("tool") if isinstance(call, dict) else None
                payload = call.get("payload") if isinstance(call, dict) else None
                formatted_tool_calls.append({"tool": tool_name, "payload": payload})
            return {
                "answer_markdown": answer_text,
                "sources": sources,
                "tool_calls": formatted_tool_calls,
                "elapsed_ms": elapsed_ms,
                "status": status_dict,
            }

    def refresh_index(self) -> None:
        self.vector_index.refresh()

    def status(self) -> AssistantStatus:
        runtime = self.runtime_manager.current_runtime()
        uses_gpu = runtime.uses_gpu if runtime else False
        remaining = max(0, self.settings.tool_budget - self.tooling.calls)
        if self.dashboard:
            try:
                total = self.settings.tool_budget
                self.dashboard.update_tool_budget(total, self.tooling.calls, remaining)
            except Exception:
                LOGGER.debug("Dashboard update failed while reporting status")
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
