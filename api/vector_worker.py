"""Background worker that keeps the semantic vector index in sync."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from assistant.config import AssistantSettings
from assistant.rag import VectorIndex

from .db import DataAccess

LOGGER = logging.getLogger("videocatalog.api.vector_worker")


class VectorRefreshWorker:
    """Asynchronously process ``vectors_pending`` rows and refresh the index."""

    def __init__(
        self,
        data_access: DataAccess,
        *,
        idle_interval: float = 5.0,
        batch_limit: int = 32,
    ) -> None:
        self._data = data_access
        self._idle_interval = max(1.0, float(idle_interval))
        self._batch_limit = max(1, int(batch_limit))
        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None
        self._index: Optional[VectorIndex] = None
        self._settings = AssistantSettings.from_settings(self._data.settings_payload)

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
