"""Asynchronous helpers for streaming catalog change events to clients."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, AsyncIterator, Dict, Iterable, List, Optional

from .db import DataAccess

if TYPE_CHECKING:  # pragma: no cover - typing aid
    from assistant_webmon import WebMonitor

LOGGER = logging.getLogger("videocatalog.api.events")


@dataclass(slots=True)
class CatalogEvent:
    """Single catalog change notification."""

    seq: int
    ts_utc: Optional[str]
    kind: str
    payload: Dict[str, Any]


class CatalogEventBroker:
    """Poll the database and fan out catalog change events to subscribers."""

    def __init__(
        self,
        data_access: DataAccess,
        *,
        poll_interval: float = 1.0,
        batch_limit: int = 128,
        monitor: Optional["WebMonitor"] = None,
    ) -> None:
        self._data = data_access
        self._poll_interval = max(0.2, float(poll_interval))
        self._batch_limit = max(1, int(batch_limit))
        self._subscribers: Dict[int, asyncio.Queue[CatalogEvent]] = {}
        self._next_id = 1
        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None
        self._last_seq = 0
        self._monitor = monitor

    async def start(self) -> None:
        """Start the polling loop if it is not already running."""

        if self._task and not self._task.done():
            return
        self._data.ensure_event_stream_schema()
        try:
            self._last_seq = self._data.latest_event_seq()
        except Exception:
            self._last_seq = 0
        self._stop_event.clear()
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._run_loop(), name="catalog-event-loop")

    async def stop(self) -> None:
        """Signal the polling loop to stop and await completion."""

        self._stop_event.set()
        task = self._task
        if task:
            await task
        self._task = None

    async def subscribe(self, *, last_seq: int = 0) -> AsyncIterator[CatalogEvent]:
        """Yield events for a subscriber until cancellation."""

        queue: asyncio.Queue[CatalogEvent] = asyncio.Queue(maxsize=512)
        subscriber_id = self._next_id
        self._next_id += 1
        if last_seq:
            replay = await asyncio.to_thread(
                self._data.fetch_events,
                int(last_seq),
                limit=self._batch_limit,
            )
            for raw in replay:
                await queue.put(self._normalize_event(raw))
        self._subscribers[subscriber_id] = queue
        LOGGER.debug("catalog events: subscriber %s registered", subscriber_id)
        try:
            while True:
                event = await queue.get()
                yield event
        finally:
            self._subscribers.pop(subscriber_id, None)
            LOGGER.debug("catalog events: subscriber %s disconnected", subscriber_id)

    async def _run_loop(self) -> None:
        LOGGER.info("catalog events: polling loop started")
        try:
            while not self._stop_event.is_set():
                events = await asyncio.to_thread(
                    self._data.fetch_events,
                    self._last_seq,
                    limit=self._batch_limit,
                )
                if events:
                    catalog_events = [self._normalize_event(event) for event in events]
                    self._last_seq = max(self._last_seq, catalog_events[-1].seq)
                    await self._broadcast(self._coalesce_events(catalog_events))
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._poll_interval,
                    )
                except asyncio.TimeoutError:
                    continue
        except Exception as exc:  # pragma: no cover - defensive guard
            LOGGER.exception("catalog events: polling loop crashed: %s", exc)
        finally:
            LOGGER.info("catalog events: polling loop stopped")

    async def _broadcast(self, events: Iterable[CatalogEvent]) -> None:
        if not self._subscribers:
            return
        for event in events:
            dead: List[int] = []
            for subscriber_id, queue in self._subscribers.items():
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    LOGGER.warning(
                        "catalog events: subscriber %s queue overflow; dropping event", subscriber_id
                    )
                    if self._monitor:
                        self._monitor.record_event_drop()
                    dead.append(subscriber_id)
            for subscriber_id in dead:
                self._subscribers.pop(subscriber_id, None)

    @staticmethod
    def _normalize_event(raw: Dict[str, Any]) -> CatalogEvent:
        return CatalogEvent(
            seq=int(raw.get("seq", 0)),
            ts_utc=raw.get("ts_utc"),
            kind=str(raw.get("kind", "unknown")),
            payload={
                key: value
                for key, value in (raw.get("payload") or {}).items()
                if key is not None
            },
        )

    @staticmethod
    def _coalesce_events(events: List[CatalogEvent]) -> List[CatalogEvent]:
        if len(events) <= 50:
            return events
        ordered: Dict[str, CatalogEvent] = {}
        for event in events:
            payload = event.payload or {}
            identifier = (
                payload.get("path")
                or payload.get("item_id")
                or payload.get("id")
                or payload.get("doc_id")
                or payload.get("series_id")
            )
            key = f"{event.kind}:{identifier if identifier is not None else event.seq}"
            ordered[key] = event
        return list(ordered.values())

