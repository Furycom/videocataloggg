from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import requests

LOGGER = logging.getLogger("videocatalog.assistant.dashboard")


def _labels_tuple(labels: Optional[Dict[str, Any]]) -> Tuple[Tuple[str, str], ...]:
    if not labels:
        return ()
    return tuple(sorted((str(key), str(value)) for key, value in labels.items()))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class WarmupSettings:
    tmdb_limit: int = 100


@dataclass(slots=True)
class AssistantDashboardSettings:
    enable: bool = True
    flush_every_s: int = 10
    latency_buckets_ms: Tuple[int, ...] = (50, 100, 200, 300, 500, 1000, 2000, 5000)
    warmup: WarmupSettings = field(default_factory=WarmupSettings)

    @classmethod
    def from_settings(cls, payload: Dict[str, Any]) -> "AssistantDashboardSettings":
        section = payload.get("assistant_dashboard") if isinstance(payload, dict) else None
        if not isinstance(section, dict):
            section = {}
        raw_buckets = section.get("latency_buckets_ms")
        if isinstance(raw_buckets, Iterable):
            try:
                buckets = tuple(sorted({int(value) for value in raw_buckets if int(value) > 0}))
            except Exception:
                buckets = cls().latency_buckets_ms
        else:
            buckets = cls().latency_buckets_ms
        warmup_section = section.get("warmup") if isinstance(section.get("warmup"), dict) else {}
        warmup = WarmupSettings(tmdb_limit=int(warmup_section.get("tmdb_limit", 100)))
        return cls(
            enable=bool(section.get("enable", True)),
            flush_every_s=max(3, int(section.get("flush_every_s", 10))),
            latency_buckets_ms=buckets,
            warmup=warmup,
        )

    def to_json(self) -> Dict[str, Any]:
        return {
            "enable": self.enable,
            "flush_every_s": self.flush_every_s,
            "latency_buckets_ms": list(self.latency_buckets_ms),
            "warmup": {"tmdb_limit": self.warmup.tmdb_limit},
        }


class HistogramMetric:
    def __init__(self, buckets: Iterable[int]) -> None:
        numeric_buckets = sorted({int(value) for value in buckets if int(value) > 0})
        if not numeric_buckets:
            numeric_buckets = [50, 100, 200, 300, 500, 1000, 2000, 5000]
        self.buckets: Tuple[int, ...] = tuple(numeric_buckets)
        self.counts: List[float] = [0.0 for _ in range(len(self.buckets) + 1)]
        self.samples: deque[Tuple[float, float]] = deque()

    def observe(self, value_ms: float) -> None:
        now = time.time()
        index = 0
        for idx, threshold in enumerate(self.buckets):
            if value_ms <= threshold:
                index = idx
                break
        else:
            index = len(self.buckets)
        self.counts[index] += 1
        self.samples.append((now, value_ms))
        cutoff = now - 60.0
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()

    def snapshot(self) -> Dict[str, Any]:
        return {
            "buckets": self.buckets,
            "counts": list(self.counts),
        }

    def percentile(self, percentile: float) -> float:
        if not self.samples:
            return 0.0
        now = time.time()
        values = [value for ts, value in self.samples if ts >= now - 60.0]
        if not values:
            return 0.0
        values.sort()
        rank = percentile / 100.0 * (len(values) - 1)
        index = min(len(values) - 1, max(0, int(math.ceil(rank))))
        return float(values[index])


@dataclass(slots=True)
class ActionLogEntry:
    timestamp: str
    action: str
    outcome: str


class AssistantDashboard:
    """Collect metrics about the local assistant and expose dashboards."""

    def __init__(
        self,
        working_dir: Path,
        db_path: Path,
        settings_payload: Dict[str, Any],
    ) -> None:
        self.working_dir = Path(working_dir)
        self.db_path = Path(db_path)
        self.settings_payload = settings_payload
        self.settings = AssistantDashboardSettings.from_settings(settings_payload)
        assistant_section = settings_payload.get("assistant") if isinstance(settings_payload, dict) else {}
        self.assistant_model = str(assistant_section.get("model", ""))
        self.assistant_ctx = int(assistant_section.get("ctx", 8192) or 8192)
        self.assistant_tool_budget = int(assistant_section.get("tool_budget", 20) or 20)
        self.metrics_path = self.working_dir / "data" / "assistant_metrics.db"
        self.metrics_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._next_flush = time.time() + self.settings.flush_every_s
        self._counters: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], float] = {}
        self._gauges: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], float] = {}
        self._histograms: Dict[str, HistogramMetric] = {
            "llm_infer_latency_ms": HistogramMetric(self.settings.latency_buckets_ms),
        }
        self._tool_latency: Dict[str, HistogramMetric] = {}
        self._tool_budget_state = {
            "total": self.assistant_tool_budget,
            "used": 0,
            "remaining": self.assistant_tool_budget,
        }
        self._runtime_state: Dict[str, Any] = {
            "runtime": "offline",
            "model": self.assistant_model,
            "gpu": False,
            "context": self.assistant_ctx,
            "quantization": None,
            "source": "probe",
        }
        self._api_state: Dict[str, Dict[str, Any]] = {}
        self._action_log: List[ActionLogEntry] = []
        self._current_action: Optional[Dict[str, Any]] = None
        self._action_thread: Optional[threading.Thread] = None
        self._action_cancel: Optional[threading.Event] = None
        if self.settings.enable:
            self.start()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._worker_thread and self._worker_thread.is_alive():
                return
            self._stop_event.clear()
            self._worker_thread = threading.Thread(target=self._worker_loop, name="assistant-dashboard", daemon=True)
            self._worker_thread.start()

    def shutdown(self) -> None:
        self._stop_event.set()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=1.0)
        self._worker_thread = None
        if self._action_thread and self._action_thread.is_alive():
            self._action_cancel.set() if self._action_cancel else None
            self._action_thread.join(timeout=1.0)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def reload_settings(self, payload: Dict[str, Any]) -> None:
        with self._lock:
            self.settings_payload = payload
            self.settings = AssistantDashboardSettings.from_settings(payload)
            assistant_section = payload.get("assistant") if isinstance(payload, dict) else {}
            self.assistant_model = str(assistant_section.get("model", self.assistant_model))
            self.assistant_ctx = int(assistant_section.get("ctx", self.assistant_ctx))
            self.assistant_tool_budget = int(assistant_section.get("tool_budget", self.assistant_tool_budget))
            self._tool_budget_state["total"] = self.assistant_tool_budget
            self._tool_budget_state["remaining"] = max(0, self.assistant_tool_budget - self._tool_budget_state["used"])
            if "llm_infer_latency_ms" not in self._histograms or (
                tuple(self._histograms["llm_infer_latency_ms"].buckets) != self.settings.latency_buckets_ms
            ):
                self._histograms["llm_infer_latency_ms"] = HistogramMetric(self.settings.latency_buckets_ms)
            self._next_flush = time.time() + self.settings.flush_every_s
            if not self.settings.enable:
                self._stop_event.set()
                if self._worker_thread and self._worker_thread.is_alive():
                    self._worker_thread.join(timeout=1.0)
                self._worker_thread = None
            elif not (self._worker_thread and self._worker_thread.is_alive()):
                self.start()

    def update_db_path(self, db_path: Path) -> None:
        with self._lock:
            self.db_path = Path(db_path)

    def record_llm_latency(self, duration_ms: float, *, runtime: str, model: str, ok: bool = True) -> None:
        with self._lock:
            histogram = self._histograms.setdefault(
                "llm_infer_latency_ms", HistogramMetric(self.settings.latency_buckets_ms)
            )
            histogram.observe(duration_ms)
            self._increment_counter(
                "llm_requests_total" if ok else "llm_requests_failed_total",
                {"runtime": runtime},
            )
            self._runtime_state.setdefault("runtime", runtime)
            self._runtime_state.setdefault("model", model)

    def record_tool_call(self, name: str, duration_ms: float, *, success: bool) -> None:
        with self._lock:
            metric = self._tool_latency.setdefault(name, HistogramMetric(self.settings.latency_buckets_ms))
            metric.observe(duration_ms)
            self._increment_counter(
                "tool_calls_total",
                {"name": name},
            )
            if not success:
                self._increment_counter("tool_calls_failed_total", {"name": name})

    def update_tool_budget(self, total: int, used: int, remaining: int) -> None:
        with self._lock:
            self._tool_budget_state = {
                "total": int(total),
                "used": int(used),
                "remaining": int(max(0, remaining)),
            }
            self._set_gauge("tool_budget_total", None, value=float(total))
            self._set_gauge("tool_budget_used", None, value=float(used))
            self._set_gauge("tool_budget_remaining", None, value=float(max(0, remaining)))

    def record_api_event(
        self,
        provider: str,
        *,
        cache_hit: Optional[bool] = None,
        status_code: Optional[int] = None,
        remaining: Optional[int] = None,
        reset_epoch: Optional[int] = None,
        cache_size: Optional[int] = None,
    ) -> None:
        provider_key = provider.lower()
        with self._lock:
            stats = self._api_state.setdefault(
                provider_key,
                {
                    "hits": 0,
                    "misses": 0,
                    "last_status": None,
                    "remaining": None,
                    "reset": None,
                    "size": cache_size or 0,
                },
            )
            self._increment_counter("api_requests_total", {"source": provider_key})
            if cache_hit is True:
                stats["hits"] += 1
                self._increment_counter("api_cache_hits_total", {"source": provider_key})
            elif cache_hit is False:
                stats["misses"] += 1
            if status_code == 429:
                self._increment_counter("api_429_total", {"source": provider_key})
            if remaining is not None:
                stats["remaining"] = int(remaining)
            if reset_epoch is not None:
                stats["reset"] = int(reset_epoch)
            if cache_size is not None:
                stats["size"] = int(cache_size)
            stats["last_status"] = status_code

    def update_runtime(self, *, runtime: str, model: Optional[str] = None, context: Optional[int] = None, gpu: Optional[bool] = None, quantization: Optional[str] = None, source: str = "probe") -> None:
        with self._lock:
            if runtime:
                self._runtime_state["runtime"] = runtime
            if model:
                self._runtime_state["model"] = model
            if context is not None:
                self._runtime_state["context"] = int(context)
            if gpu is not None:
                self._runtime_state["gpu"] = bool(gpu)
            if quantization is not None:
                self._runtime_state["quantization"] = quantization
            self._runtime_state["source"] = source

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            runtime = dict(self._runtime_state)
            latency_hist = self._histograms.get("llm_infer_latency_ms")
            latency = {
                "p50": latency_hist.percentile(50.0) if latency_hist else 0.0,
                "p90": latency_hist.percentile(90.0) if latency_hist else 0.0,
                "p95": latency_hist.percentile(95.0) if latency_hist else 0.0,
            }
            total_tool_calls = sum(
                value for (series, _labels), value in self._counters.items() if series == "tool_calls_total"
            )
            total_tool_failures = sum(
                value for (series, _labels), value in self._counters.items() if series == "tool_calls_failed_total"
            )
            tool_cards = {
                "total": total_tool_calls,
                "failed": total_tool_failures,
                "success": max(0.0, total_tool_calls - total_tool_failures),
            }
            slow_tools: List[Dict[str, Any]] = []
            for name, hist in self._tool_latency.items():
                slow_tools.append(
                    {
                        "name": name,
                        "p95": hist.percentile(95.0),
                    }
                )
            slow_tools.sort(key=lambda item: item["p95"], reverse=True)
            api_cards = {}
            for provider, stats in self._api_state.items():
                hits = stats.get("hits", 0)
                misses = stats.get("misses", 0)
                total = hits + misses
                ratio = float(hits) / total if total else 0.0
                remaining = stats.get("remaining")
                reset = stats.get("reset")
                status = "green"
                if stats.get("last_status") == 429 or (remaining is not None and remaining <= 0):
                    status = "red"
                elif remaining is not None and remaining < 3:
                    status = "yellow"
                api_cards[provider] = {
                    "hit_ratio": ratio,
                    "remaining": remaining,
                    "reset": reset,
                    "size": stats.get("size"),
                    "status": status,
                }
            action_state = dict(self._current_action) if self._current_action else None
            log_entries = [entry.__dict__ for entry in self._action_log[-10:]]
            return {
                "runtime": runtime,
                "latency": latency,
                "tool_budget": dict(self._tool_budget_state),
                "tools": {"summary": tool_cards, "slow": slow_tools[:5]},
                "api": api_cards,
                "action": action_state,
                "log": log_entries,
            }

    def preload_model(self) -> bool:
        return self._start_action("preload_model", self._run_preload_model)

    def warm_tmdb_cache(self, limit: Optional[int] = None) -> bool:
        return self._start_action("warm_tmdb_cache", lambda cancel: self._run_warm_tmdb(limit, cancel))

    def cancel_action(self) -> None:
        with self._lock:
            if self._action_cancel:
                self._action_cancel.set()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _increment_counter(self, series: str, labels: Optional[Dict[str, Any]], *, value: float = 1.0) -> None:
        key = (series, _labels_tuple(labels))
        self._counters[key] = self._counters.get(key, 0.0) + float(value)

    def _set_gauge(self, series: str, labels: Optional[Dict[str, Any]], *, value: float) -> None:
        key = (series, _labels_tuple(labels))
        self._gauges[key] = float(value)

    def _worker_loop(self) -> None:
        heartbeat = 2.0
        while not self._stop_event.wait(heartbeat):
            try:
                self._probe_runtime()
            except Exception as exc:  # pragma: no cover - defensive logging
                LOGGER.debug("Dashboard probe failed: %s", exc)
            if time.time() >= self._next_flush:
                try:
                    self._flush_metrics()
                except Exception as exc:
                    LOGGER.warning("Dashboard flush failed: %s", exc)
                self._next_flush = time.time() + self.settings.flush_every_s

    def _flush_metrics(self) -> None:
        with self._lock:
            snapshot = list(self._counters.items())
            gauge_snapshot = list(self._gauges.items())
            hist_snapshot = {
                name: metric.snapshot()
                for name, metric in {**self._histograms, **{f"tool_latency_ms_{k}": v for k, v in self._tool_latency.items()}}.items()
            }
        if not snapshot and not hist_snapshot and not gauge_snapshot:
            return
        with sqlite3.connect(self.metrics_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS assistant_metrics (
                    ts_utc TEXT NOT NULL,
                    series TEXT NOT NULL,
                    labels_json TEXT NOT NULL,
                    value REAL NOT NULL
                )
                """
            )
            rows = []
            ts = _utc_now()
            for (series, labels), value in snapshot:
                payload = json.dumps(dict(labels))
                rows.append((ts, f"counter.{series}", payload, float(value)))
            for (series, labels), value in gauge_snapshot:
                payload = json.dumps(dict(labels))
                rows.append((ts, f"gauge.{series}", payload, float(value)))
            for name, data in hist_snapshot.items():
                buckets = list(data.get("buckets", ()))
                counts = list(data.get("counts", ()))
                for idx, count in enumerate(counts):
                    label = {"le": str(buckets[idx])} if idx < len(buckets) else {"le": "+Inf"}
                    rows.append((ts, f"histogram.{name}", json.dumps(label), float(count)))
            conn.executemany(
                "INSERT INTO assistant_metrics(ts_utc, series, labels_json, value) VALUES(?,?,?,?)",
                rows,
            )
            conn.commit()

    def _probe_runtime(self) -> None:
        try:
            response = requests.get("http://127.0.0.1:11434/api/tags", timeout=1.5)
            response.raise_for_status()
            payload = response.json()
        except Exception:
            payload = None
        if payload:
            models = payload.get("models") if isinstance(payload, dict) else None
            model = self.assistant_model
            quantization = None
            ctx = self.assistant_ctx
            if isinstance(models, list) and models:
                selected = None
                for entry in models:
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("name") == self.assistant_model:
                        selected = entry
                        break
                if selected is None:
                    selected = models[0]
                model = selected.get("name", model)
                details = selected.get("details") if isinstance(selected.get("details"), dict) else {}
                ctx = int(details.get("context_length", ctx) or ctx)
                quantization = details.get("quantization_level")
            try:
                sys_resp = requests.get("http://127.0.0.1:11434/api/ps", timeout=1.0)
                sys_payload = sys_resp.json()
                devices = sys_payload.get("devices") if isinstance(sys_payload, dict) else []
                gpu = any(
                    isinstance(dev, dict) and bool(dev.get("accelerator")) for dev in (devices or [])
                )
            except Exception:
                gpu = False
            self.update_runtime(runtime="ollama", model=model, context=ctx, gpu=gpu, quantization=quantization, source="probe")
            return

        try:
            response = requests.get("http://127.0.0.1:11435/v1/models", timeout=1.5)
            response.raise_for_status()
            payload = response.json()
        except Exception:
            payload = None
        if payload:
            data = payload.get("data") if isinstance(payload, dict) else None
            model = self.assistant_model
            ctx = self.assistant_ctx
            if isinstance(data, list) and data:
                entry = data[0]
                if isinstance(entry, dict):
                    model = entry.get("id", model)
                    ctx = int(entry.get("context_length", ctx) or ctx)
            self.update_runtime(runtime="llama_cpp", model=model, context=ctx, source="probe")
            return

        self.update_runtime(runtime="offline", source="probe")

    def _start_action(self, name: str, target: Callable[[threading.Event], None]) -> bool:
        with self._lock:
            if self._action_thread and self._action_thread.is_alive():
                return False
            cancel_event = threading.Event()
            thread = threading.Thread(
                target=self._action_wrapper,
                args=(name, target, cancel_event),
                name=f"assistant-{name}",
                daemon=True,
            )
            self._action_thread = thread
            self._action_cancel = cancel_event
            self._current_action = {"name": name, "started": _utc_now(), "status": "running"}
            thread.start()
            return True

    def _action_wrapper(self, name: str, target: Callable[[threading.Event], None], cancel: threading.Event) -> None:
        started = time.time()
        try:
            target(cancel)
            outcome = "ok" if not cancel.is_set() else "cancelled"
        except Exception as exc:
            LOGGER.exception("Assistant dashboard action %s failed", name)
            outcome = f"error: {exc}"
        duration_ms = (time.time() - started) * 1000.0
        with self._lock:
            log_entry = ActionLogEntry(timestamp=_utc_now(), action=name, outcome=outcome)
            self._action_log.append(log_entry)
            self._action_log = self._action_log[-20:]
            existing = dict(self._current_action or {})
            existing.setdefault("name", name)
            existing["status"] = existing.get("status", outcome)
            existing["duration_ms"] = duration_ms
            self._current_action = existing
            self._action_thread = None
            self._action_cancel = None

    def _run_preload_model(self, cancel: threading.Event) -> None:
        runtime = self._runtime_state.get("runtime", "offline")
        model = self._runtime_state.get("model", self.assistant_model) or self.assistant_model
        if runtime not in {"ollama", "llama_cpp"}:
            self.update_runtime(runtime="offline", source="warmup")
            raise RuntimeError("No local runtime detected on localhost:11434 or 11435.")
        if cancel.is_set():
            return
        start = time.perf_counter()
        if runtime == "ollama":
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": "ping"}],
                "stream": False,
                "options": {"temperature": 0.0, "num_ctx": self.assistant_ctx, "num_predict": 8},
            }
            response = requests.post("http://127.0.0.1:11434/api/chat", json=payload, timeout=30)
            response.raise_for_status()
        else:
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": "You are a warmup probe."},
                    {"role": "user", "content": "ping"},
                ],
                "max_tokens": 8,
                "stream": False,
                "temperature": 0.0,
            }
            response = requests.post("http://127.0.0.1:11435/v1/chat/completions", json=payload, timeout=30)
            response.raise_for_status()
        latency_ms = (time.perf_counter() - start) * 1000.0
        self.record_llm_latency(latency_ms, runtime=runtime, model=model, ok=True)
        self.update_runtime(runtime=runtime, model=model, source="warmup")
        with self._lock:
            self._current_action = {
                "name": "preload_model",
                "status": "Model ready",
                "duration_ms": latency_ms,
            }

    def _run_warm_tmdb(self, limit: Optional[int], cancel: threading.Event) -> None:
        from assistant.apiguard import ApiGuard  # Lazy import to avoid cycle

        settings = self.settings_payload
        tmdb_key = None
        structure = settings.get("structure") if isinstance(settings, dict) else {}
        if isinstance(structure, dict):
            tmdb_cfg = structure.get("tmdb") if isinstance(structure.get("tmdb"), dict) else {}
            tmdb_key = tmdb_cfg.get("api_key")
        if not tmdb_key:
            tv_cfg = settings.get("tv") if isinstance(settings, dict) else {}
            if isinstance(tv_cfg, dict):
                tmdb_section = tv_cfg.get("tmdb") if isinstance(tv_cfg.get("tmdb"), dict) else {}
                tmdb_key = tmdb_section.get("api_key")
        if not tmdb_key:
            raise RuntimeError("TMDb API key missing in settings.")
        guard = ApiGuard(self.working_dir, tmdb_key, dashboard=self)
        batch_limit = limit or self.settings.warmup.tmdb_limit
        if batch_limit <= 0:
            return
        movie_rows: List[sqlite3.Row] = []
        tv_rows: List[sqlite3.Row] = []
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            try:
                movie_rows = conn.execute(
                    "SELECT folder_path FROM review_queue ORDER BY created_utc DESC LIMIT ?",
                    (batch_limit,),
                ).fetchall()
            except sqlite3.OperationalError:
                movie_rows = []
            try:
                tv_rows = conn.execute(
                    "SELECT item_type, item_key FROM tv_review_queue ORDER BY created_utc DESC LIMIT ?",
                    (batch_limit,),
                ).fetchall()
            except sqlite3.OperationalError:
                tv_rows = []
        total_requests = 0
        served_from_cache = 0
        rate_limited = False
        for row in movie_rows:
            if cancel.is_set():
                break
            folder = row["folder_path"]
            title = Path(folder).stem.replace(".", " ")
            params = {"query": title, "page": 1}
            try:
                payload = guard.tmdb_lookup("search/movie", params, ttl=3600)
                total_requests += 1
                if getattr(guard, "last_cache_hit", False):
                    served_from_cache += 1
            except RuntimeError as exc:
                if "rate limit" in str(exc).lower():
                    rate_limited = True
                    break
        for row in tv_rows:
            if cancel.is_set():
                break
            item_type = row["item_type"]
            key = row["item_key"]
            title = Path(key).stem.replace(".", " ")
            endpoint = "search/tv"
            params = {"query": title, "page": 1}
            if item_type == "season":
                endpoint = "search/tv"
            try:
                payload = guard.tmdb_lookup(endpoint, params, ttl=3600)
                total_requests += 1
                if getattr(guard, "last_cache_hit", False):
                    served_from_cache += 1
            except RuntimeError as exc:
                if "rate limit" in str(exc).lower():
                    rate_limited = True
                    break
        if rate_limited:
            status = "rate-limited"
        elif total_requests and served_from_cache >= total_requests:
            status = "served-from-cache"
        else:
            status = "completed"
        with self._lock:
            self._current_action = {
                "name": "warm_tmdb_cache",
                "status": status,
                "served_from_cache": served_from_cache,
                "requested": total_requests,
            }


_DASHBOARDS: Dict[Path, AssistantDashboard] = {}
_DASHBOARD_LOCK = threading.Lock()


def get_dashboard(working_dir: Path, db_path: Path, settings_payload: Dict[str, Any]) -> AssistantDashboard:
    key = Path(working_dir).resolve()
    with _DASHBOARD_LOCK:
        dashboard = _DASHBOARDS.get(key)
        if dashboard is None:
            dashboard = AssistantDashboard(key, Path(db_path), settings_payload)
            _DASHBOARDS[key] = dashboard
        else:
            dashboard.update_db_path(db_path)
            dashboard.reload_settings(settings_payload)
        return dashboard


__all__ = [
    "AssistantDashboard",
    "AssistantDashboardSettings",
    "WarmupSettings",
    "get_dashboard",
]

