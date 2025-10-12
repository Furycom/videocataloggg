from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Dict, Optional, TYPE_CHECKING

import requests

LOGGER = logging.getLogger("videocatalog.assistant.apiguard")


if TYPE_CHECKING:  # pragma: no cover - for typing only
    from assistant_monitor import AssistantDashboard


class ApiGuard:
    """Simple quota-aware HTTP cache for third-party APIs."""

    def __init__(
        self,
        working_dir: Path,
        tmdb_api_key: Optional[str] = None,
        dashboard: Optional["AssistantDashboard"] = None,
    ) -> None:
        self.cache_dir = working_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.tmdb_api_key = tmdb_api_key
        self._lock = threading.Lock()
        self._tmdb_cache_path = self.cache_dir / "tmdb_cache.json"
        self._opensub_cache_path = self.cache_dir / "opensubtitles_cache.json"
        self._tmdb_cache: Dict[str, Dict[str, object]] = {}
        self._opensub_cache: Dict[str, Dict[str, object]] = {}
        self._tmdb_budget: Dict[str, int] = {}
        self._dashboard = dashboard
        self.last_cache_hit: bool = False
        self._load_tmdb_cache()
        self._load_opensubtitles_cache()

    # ------------------------------------------------------------------
    def tmdb_lookup(
        self,
        endpoint: str,
        params: Optional[Dict[str, object]] = None,
        *,
        ttl: int = 3600,
        cache_only: bool = False,
    ) -> Dict[str, object]:
        params = dict(params or {})
        # Avoid leaking actual API keys in cache keys during cache-only probes.
        api_key = self.tmdb_api_key or params.get("api_key") or ""
        if not cache_only:
            if not api_key:
                raise RuntimeError("TMDB API key missing. Configure structure.tmdb.api_key in settings.")
        params.setdefault("api_key", api_key)
        cache_key = json.dumps([endpoint, sorted(params.items())], sort_keys=True)
        with self._lock:
            cached = self._tmdb_cache.get(cache_key)
            if cached and time.time() - cached.get("ts", 0) < ttl:
                self.last_cache_hit = True
                self._report_api_event(cache_hit=True, cache_size=len(self._tmdb_cache))
                return cached["payload"]
            if cache_only:
                self.last_cache_hit = bool(cached)
                return dict(cached["payload"]) if cached else {}
            if not self._consume_budget("tmdb", limit_per_minute=35):
                self._report_api_event(cache_hit=None, status_code=429, remaining=0)
                raise RuntimeError("TMDB rate limit reached for this minute; try again later.")
        url = f"https://api.themoviedb.org/3/{endpoint.lstrip('/')}"
        response = requests.get(url, params=params, timeout=20)
        response.raise_for_status()
        payload = response.json()
        with self._lock:
            self._tmdb_cache[cache_key] = {"ts": time.time(), "payload": payload}
            self._save_tmdb_cache()
        remaining = _parse_int(response.headers.get("X-RateLimit-Remaining"))
        reset = _parse_int(response.headers.get("X-RateLimit-Reset"))
        self.last_cache_hit = False
        self._report_api_event(
            cache_hit=False,
            status_code=response.status_code,
            remaining=remaining,
            reset_epoch=reset,
            cache_size=len(self._tmdb_cache),
        )
        return payload

    # ------------------------------------------------------------------
    def _load_tmdb_cache(self) -> None:
        if not self._tmdb_cache_path.exists():
            return
        try:
            payload = json.loads(self._tmdb_cache_path.read_text(encoding="utf-8"))
        except Exception as exc:
            LOGGER.warning("Failed to load TMDB cache: %s", exc)
            return
        if isinstance(payload, dict):
            self._tmdb_cache = payload.get("entries", {})
            self._tmdb_budget = payload.get("budget", {})

    def _save_tmdb_cache(self) -> None:
        payload = {"entries": self._tmdb_cache, "budget": self._tmdb_budget}
        try:
            self._tmdb_cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as exc:
            LOGGER.warning("Failed to persist TMDB cache: %s", exc)

    # ------------------------------------------------------------------
    def _load_opensubtitles_cache(self) -> None:
        if not self._opensub_cache_path.exists():
            return
        try:
            payload = json.loads(self._opensub_cache_path.read_text(encoding="utf-8"))
        except Exception as exc:
            LOGGER.warning("Failed to load OpenSubtitles cache: %s", exc)
            return
        if isinstance(payload, dict):
            entries = payload.get("entries")
            if isinstance(entries, dict):
                self._opensub_cache = {str(key): value for key, value in entries.items() if isinstance(value, dict)}

    def opensubtitles_lookup(
        self,
        oshash: str,
        *,
        cache_only: bool = False,
    ) -> Dict[str, object]:
        key = str(oshash)
        with self._lock:
            payload = self._opensub_cache.get(key)
            if payload:
                self.last_cache_hit = True
                return dict(payload)
            self.last_cache_hit = False
        if cache_only:
            return {}
        raise RuntimeError("OpenSubtitles cache miss and live lookup disabled in this context.")

    def cache_snapshot(self) -> Dict[str, object]:
        with self._lock:
            tmdb_keys = sorted(self._tmdb_cache.keys())
            opensub_keys = sorted(self._opensub_cache.keys())
            return {
                "tmdb": {
                    "entries": len(tmdb_keys),
                    "keys": tmdb_keys,
                },
                "opensubtitles": {
                    "entries": len(opensub_keys),
                    "keys": opensub_keys,
                },
            }

    def _consume_budget(self, bucket: str, *, limit_per_minute: int) -> bool:
        now = int(time.time())
        minute = now // 60
        count = self._tmdb_budget.get(str(minute), 0)
        if count >= limit_per_minute:
            return False
        self._tmdb_budget[str(minute)] = count + 1
        # Drop stale buckets
        for key in list(self._tmdb_budget.keys()):
            if int(key) < minute - 5:
                self._tmdb_budget.pop(key, None)
        return True

    def _report_api_event(
        self,
        cache_hit: Optional[bool],
        *,
        status_code: Optional[int] = None,
        remaining: Optional[int] = None,
        reset_epoch: Optional[int] = None,
        cache_size: Optional[int] = None,
    ) -> None:
        if self._dashboard is None:
            return
        try:
            self._dashboard.record_api_event(
                "tmdb",
                cache_hit=cache_hit,
                status_code=status_code,
                remaining=remaining,
                reset_epoch=reset_epoch,
                cache_size=cache_size,
            )
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.debug("Failed to record TMDb metrics: %s", exc)


def _parse_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
