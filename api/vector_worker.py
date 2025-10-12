"""Background worker that keeps the semantic vector index in sync."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional, TYPE_CHECKING

from assistant.config import AssistantSettings
from assistant.rag import VectorIndex

from .db import DataAccess

if TYPE_CHECKING:  # pragma: no cover - for typing only
    from orchestrator.api import OrchestratorService

LOGGER = logging.getLogger("videocatalog.api.vector_worker")


class VectorRefreshWorker:
    """Asynchronously process ``vectors_pending`` rows and refresh the index."""

    def __init__(
        self,
        data_access: DataAccess,
        *,
        idle_interval: float = 5.0,
        batch_limit: int = 32,
        orchestrator: Optional["OrchestratorService"] = None,
    ) -> None:
        self._data = data_access
        self._idle_interval = max(1.0, float(idle_interval))
        self._batch_limit = max(1, int(batch_limit))
        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None
        self._index: Optional[VectorIndex] = None
        self._settings = AssistantSettings.from_settings(self._data.settings_payload)
        self._orchestrator = orchestrator

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        if not self._settings.rag.enable:
            LOGGER.info("Vector refresh disabled (assistant.rag.enable=false)")
            return
        self._stop_event.clear()
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._run(), name="vector-refresh-worker")

    async def stop(self) -> None:
        self._stop_event.set()
        task = self._task
        if task:
            await task
        self._task = None

    async def _run(self) -> None:
        LOGGER.info("Vector refresh worker started")
        try:
            orchestrated = self._orchestrator is not None and self._orchestrator.enabled
            if not orchestrated:
                self._index = VectorIndex(
                    self._settings,
                    self._data.catalog_path,
                    self._data.working_dir,
                )
            while not self._stop_event.is_set():
                pending = await asyncio.to_thread(
                    self._data.dequeue_vectors_pending,
                    limit=self._batch_limit,
                )
                if not pending:
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait(),
                            timeout=self._idle_interval,
                        )
                    except asyncio.TimeoutError:
                        continue
                    continue
                if orchestrated:
                    await asyncio.to_thread(self._dispatch_orchestrated_job)
                else:
                    await asyncio.to_thread(self._refresh_index)
        except Exception as exc:  # pragma: no cover - defensive guard
            LOGGER.exception("Vector refresh worker crashed: %s", exc)
        finally:
            LOGGER.info("Vector refresh worker stopped")

    def _refresh_index(self) -> None:
        if not self._index:
            return
        LOGGER.info("Vector refresh worker: rebuilding semantic index")
        self._index.refresh()

    def _dispatch_orchestrated_job(self) -> None:
        if not self._orchestrator or not self._orchestrator.enabled:
            return
        scheduler = self._orchestrator.scheduler
        try:
            jobs = scheduler.list_jobs(limit=200)
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning("Vector refresh: failed to query orchestrator queue: %s", exc)
            return
        for job in jobs:
            if job.get("kind") == "vectors_refresh" and job.get("status") in {
                "queued",
                "leased",
                "running",
            }:
                LOGGER.debug("Vector refresh already pending via orchestrator (job %s)", job.get("id"))
                return
        LOGGER.info("Vector refresh worker: enqueueing orchestrator job")
        scheduler.enqueue("vectors_refresh", {"source": "vector_worker"})
