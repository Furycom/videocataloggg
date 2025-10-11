"""Realtime metrics collector for the integrated web experience."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Dict, Iterable, List, MutableMapping, Optional, Tuple


@dataclass(slots=True)
class _LagSample:
    ts: float
    value_ms: float


@dataclass(slots=True)
class _Connection:
    client_id: str
    transport: str
    connected_at: float
    last_seen: float


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        return dt.replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


class WebMonitor:
    """Collects realtime metrics for websocket/SSE usage."""

    def __init__(
        self,
        working_dir: Path,
        *,
        flush_interval: float = 10.0,
        labels: Optional[MutableMapping[str, str]] = None,
    ) -> None:
        self._working_dir = Path(working_dir)
        self._flush_interval = max(2.0, float(flush_interval))
        self._labels = dict(labels or {"node": "local"})
        self._db_path = self._working_dir / "data" / "web_metrics.db"

        self._lock = threading.Lock()
        self._lag_samples: Deque[_LagSample] = deque()
        self._lag_window_sec = 120.0
        self._connections: Dict[str, _Connection] = {}
        self._client_last_seen: Dict[str, float] = {}
        self._transport_counts: Dict[str, int] = {"ws": 0, "sse": 0}
        self._totals: Dict[str, float] = {
            "events_pushed_total": 0.0,
            "events_dropped_total": 0.0,
            "ai_requests_total": 0.0,
            "ai_errors_total": 0.0,
        }
        self._last_event_ts: Optional[str] = None

        self._flush_task: Optional[asyncio.Task[None]] = None
        self._stop_event = asyncio.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._flush_task and not self._flush_task.done():
            return
        loop = asyncio.get_running_loop()
        self._stop_event.clear()
        self._flush_task = loop.create_task(self._run_flush_loop(), name="web-monitor-flush")

    async def stop(self) -> None:
        self._stop_event.set()
        task = self._flush_task
        if task:
            await task
        self._flush_task = None

    # ------------------------------------------------------------------
    # Connection tracking
    # ------------------------------------------------------------------

    def register(self, *, client_id: str, transport: str) -> str:
        now = time.time()
        key = f"{transport}:{client_id}:{now:.6f}"
        conn = _Connection(client_id=client_id, transport=transport, connected_at=now, last_seen=now)
        with self._lock:
            self._connections[key] = conn
            self._transport_counts[transport] = self._transport_counts.get(transport, 0) + 1
            self._client_last_seen[client_id] = now
        return key

    def unregister(self, connection_key: str) -> None:
        with self._lock:
            conn = self._connections.pop(connection_key, None)
            if conn:
                current = self._transport_counts.get(conn.transport, 0)
                self._transport_counts[conn.transport] = max(0, current - 1)

    def heartbeat(self, *, client_id: str, transport: str) -> None:
        now = time.time()
        with self._lock:
            for key, conn in list(self._connections.items()):
                if conn.client_id == client_id and conn.transport == transport:
                    conn.last_seen = now
            self._client_last_seen[client_id] = now

    # ------------------------------------------------------------------
    # Metrics recording
    # ------------------------------------------------------------------

    def record_event_delivery(self, *, ts_utc: Optional[str], count: int = 1) -> None:
        now = time.time()
        lag_ms: Optional[float] = None
        if ts_utc:
            source_ts = _parse_ts(ts_utc)
            if source_ts is not None:
                lag_ms = max(0.0, (now - source_ts) * 1000.0)
        with self._lock:
            self._totals["events_pushed_total"] += float(max(0, count))
            if ts_utc:
                self._last_event_ts = ts_utc
            if lag_ms is not None:
                self._lag_samples.append(_LagSample(ts=now, value_ms=lag_ms))
                self._prune_lags_locked(now)

    def record_event_drop(self, count: int = 1) -> None:
        if count <= 0:
            return
        with self._lock:
            self._totals["events_dropped_total"] += float(count)

    def record_ai_request(self, *, error: bool = False) -> None:
        with self._lock:
            self._totals["ai_requests_total"] += 1.0
            if error:
                self._totals["ai_errors_total"] += 1.0

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def snapshot(self, *, client_id: Optional[str] = None) -> Dict[str, object]:
        now = time.time()
        with self._lock:
            self._prune_lags_locked(now)
            lag_values = [sample.value_ms for sample in self._lag_samples]
            p50 = _percentile(lag_values, 50.0)
            p95 = _percentile(lag_values, 95.0)
            last_event_age_ms = None
            if self._last_event_ts:
                source_ts = _parse_ts(self._last_event_ts)
                if source_ts is not None:
                    last_event_age_ms = max(0.0, (now - source_ts) * 1000.0)
            client_state = None
            if client_id:
                last_seen = self._client_last_seen.get(client_id)
                if last_seen is not None:
                    client_state = {
                        "client_id": client_id,
                        "last_seen_utc": datetime.fromtimestamp(last_seen, tz=timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        ),
                        "stale": (now - last_seen) > 60.0,
                    }
                else:
                    client_state = {"client_id": client_id, "last_seen_utc": None, "stale": True}

            snapshot = {
                "ts_utc": _utc_now_iso(),
                "ws_connected": int(self._transport_counts.get("ws", 0)),
                "sse_connected": int(self._transport_counts.get("sse", 0)),
                "events_pushed_total": float(self._totals.get("events_pushed_total", 0.0)),
                "events_dropped_total": float(self._totals.get("events_dropped_total", 0.0)),
                "event_lag_ms_p50": p50,
                "event_lag_ms_p95": p95,
                "last_event_utc": self._last_event_ts,
                "last_event_age_ms": last_event_age_ms,
                "ai_requests_total": float(self._totals.get("ai_requests_total", 0.0)),
                "ai_errors_total": float(self._totals.get("ai_errors_total", 0.0)),
                "client": client_state,
            }
        return snapshot

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def _run_flush_loop(self) -> None:
        await asyncio.to_thread(self._ensure_table)
        try:
            while True:
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self._flush_interval)
                    break
                except asyncio.TimeoutError:
                    await asyncio.to_thread(self._flush_once)
        finally:
            await asyncio.to_thread(self._flush_once)

    def _ensure_table(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path)
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

    def _flush_once(self) -> None:
        snapshot = self.snapshot()
        labels = json.dumps(self._labels, separators=(",", ":"))
        rows = [
            (snapshot["ts_utc"], "ws_connected", labels, float(snapshot["ws_connected"])),
            (snapshot["ts_utc"], "sse_connected", labels, float(snapshot["sse_connected"])),
            (snapshot["ts_utc"], "events_pushed_total", labels, float(snapshot["events_pushed_total"])),
            (snapshot["ts_utc"], "events_dropped_total", labels, float(snapshot["events_dropped_total"])),
            (snapshot["ts_utc"], "event_lag_ms_p50", labels, float(snapshot["event_lag_ms_p50"] or 0.0)),
            (snapshot["ts_utc"], "event_lag_ms_p95", labels, float(snapshot["event_lag_ms_p95"] or 0.0)),
            (snapshot["ts_utc"], "ai_requests_total", labels, float(snapshot["ai_requests_total"])),
            (snapshot["ts_utc"], "ai_errors_total", labels, float(snapshot["ai_errors_total"])),
        ]
        conn = sqlite3.connect(self._db_path)
        try:
            conn.executemany(
                "INSERT INTO web_metrics (ts_utc, series, labels_json, value) VALUES (?, ?, ?, ?)",
                rows,
            )
            conn.commit()
        finally:
            conn.close()

    def _prune_lags_locked(self, now: float) -> None:
        cutoff = now - self._lag_window_sec
        while self._lag_samples and self._lag_samples[0].ts < cutoff:
            self._lag_samples.popleft()


def _percentile(values: Iterable[float], percentile: float) -> Optional[float]:
    seq: List[float] = [float(v) for v in values if v is not None]
    if not seq:
        return None
    seq.sort()
    if percentile <= 0:
        return seq[0]
    if percentile >= 100:
        return seq[-1]
    k = (len(seq) - 1) * (percentile / 100.0)
    f = int(k)
    c = min(f + 1, len(seq) - 1)
    if f == c:
        return seq[f]
    d0 = seq[f] * (c - k)
    d1 = seq[c] * (k - f)
    return d0 + d1


def load_recent_metrics(db_path: Path, *, lookback_seconds: int = 60) -> Dict[str, float]:
    if not db_path.exists():
        return {}
    cutoff = datetime.now(timezone.utc).timestamp() - max(1, lookback_seconds)
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(
            """
            SELECT series, MAX(value) AS value
            FROM web_metrics
            WHERE ts_utc >= ?
            GROUP BY series
            """,
            (cutoff_iso,),
        )
        rows = cursor.fetchall()
    finally:
        conn.close()
    return {row["series"]: float(row["value"] or 0.0) for row in rows}

