"""Client helpers for the experimental Search+ experience.

The Search+ tab aggregates three separate backends:

* semantic index – dense vector similarity search results.
* transcripts – natural language matches from ASR transcripts.
* inventory – keyword results served by the existing inventory API.

The helpers below provide a light abstraction over the HTTP APIs so the GUI
can run searches on background threads while surfacing consistent status
updates. Each request is best-effort; failures are captured and returned to
the caller so the UI can display non-blocking error banners.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen


StatusCallback = Optional[Callable[[str, str, Dict[str, Any]], None]]


@dataclass(slots=True)
class SearchPlusResult:
    """Normalized result payload returned by :class:`SearchPlusClient`."""

    path: str
    drive: str
    title: str
    snippet: str
    score: float
    source: str
    thumbnail: Optional[str] = None
    transcript_url: Optional[str] = None
    inventory_url: Optional[str] = None
    extras: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SearchPlusResponse:
    """Aggregate response for a Search+ query."""

    results: List[SearchPlusResult]
    durations_ms: Dict[str, int] = field(default_factory=dict)
    errors: Dict[str, str] = field(default_factory=dict)
    source_counts: Dict[str, int] = field(default_factory=dict)


class SearchPlusClient:
    """Thin HTTP client used by the GUI to run Search+ queries."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: Optional[str] = None,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip() if api_key else None
        self.timeout = float(timeout)

    # ------------------------------------------------------------------
    def search(
        self,
        query: str,
        *,
        mode: str,
        type_filter: Optional[str] = None,
        since: Optional[str] = None,
        drives: Optional[Sequence[str]] = None,
        limit: int = 60,
        cancel_event: Optional[Any] = None,
        status_callback: StatusCallback = None,
    ) -> SearchPlusResponse:
        """Run a federated Search+ query across the configured backends."""

        normalized_mode = (mode or "").strip().lower()
        services = self._services_for_mode(normalized_mode)
        drives_list = list(drives or [])
        for service in services:
            self._notify(status_callback, service, "queued", {})

        combined: Dict[str, SearchPlusResult] = {}
        durations: Dict[str, int] = {}
        errors: Dict[str, str] = {}
        source_counts: Dict[str, int] = {}

        if not query or not query.strip():
            return SearchPlusResponse([], durations, errors, source_counts)

        for service in services:
            if cancel_event and getattr(cancel_event, "is_set", lambda: False)():
                self._notify(status_callback, service, "cancelled", {})
                break
            self._notify(status_callback, service, "running", {})
            try:
                partial, elapsed_ms = self._collect_service(
                    service,
                    query=query,
                    type_filter=type_filter,
                    since=since,
                    drives=drives_list,
                    limit=limit,
                )
            except Exception as exc:  # pragma: no cover - network failures
                message = str(exc) or exc.__class__.__name__
                errors[service] = message
                self._notify(status_callback, service, "error", {"message": message})
                continue
            durations[service] = int(elapsed_ms)
            source_counts[service] = len(partial)
            self._notify(
                status_callback,
                service,
                "done",
                {"count": len(partial), "elapsed_ms": int(elapsed_ms)},
            )
            for result in partial:
                key = f"{result.drive}::{result.path}"
                existing = combined.get(key)
                if existing is None:
                    result.extras.setdefault("sources", [result.source])
                    combined[key] = result
                    continue
                # Merge metadata when the same path appears across services.
                existing_sources = existing.extras.setdefault("sources", [])
                if result.source not in existing_sources:
                    existing_sources.append(result.source)
                if result.score > existing.score:
                    existing.score = result.score
                    if result.snippet:
                        existing.snippet = result.snippet
                if result.thumbnail and not existing.thumbnail:
                    existing.thumbnail = result.thumbnail
                if result.transcript_url and not existing.transcript_url:
                    existing.transcript_url = result.transcript_url
                if result.inventory_url and not existing.inventory_url:
                    existing.inventory_url = result.inventory_url

        merged_results = sorted(
            combined.values(),
            key=lambda item: item.score,
            reverse=True,
        )
        return SearchPlusResponse(
            merged_results,
            durations_ms=durations,
            errors=errors,
            source_counts=source_counts,
        )

    # ------------------------------------------------------------------
    def _services_for_mode(self, mode: str) -> List[str]:
        if mode == "keyword":
            return ["inventory"]
        if mode == "hybrid":
            return ["semantic", "transcripts", "inventory"]
        # Default fallback — semantic mode.
        return ["semantic", "transcripts"]

    def _collect_service(
        self,
        service: str,
        *,
        query: str,
        type_filter: Optional[str],
        since: Optional[str],
        drives: Sequence[str],
        limit: int,
    ) -> tuple[List[SearchPlusResult], float]:
        start = time.perf_counter()
        if service == "semantic":
            results = self._fetch_semantic(query, type_filter=type_filter, since=since, limit=limit)
        elif service == "transcripts":
            results = self._fetch_transcripts(query, type_filter=type_filter, since=since, limit=limit)
        elif service == "inventory":
            results = self._fetch_inventory(
                query,
                type_filter=type_filter,
                since=since,
                drives=drives,
                limit=limit,
            )
        else:  # pragma: no cover - defensive
            raise ValueError(f"Unknown service '{service}'")
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        return results, elapsed_ms

    # ------------------------------------------------------------------
    def _fetch_semantic(
        self,
        query: str,
        *,
        type_filter: Optional[str],
        since: Optional[str],
        limit: int,
    ) -> List[SearchPlusResult]:
        params = {
            "q": query,
            "limit": max(1, int(limit)),
        }
        if type_filter:
            params["category"] = type_filter
        if since:
            params["since"] = since
        payload = self._request_json("/v1/search/semantic", params)
        results: List[SearchPlusResult] = []
        for row in payload.get("results", []) if isinstance(payload, dict) else []:
            path = str(row.get("path") or "")
            drive = str(row.get("drive_label") or row.get("drive") or "")
            title = str(row.get("title") or os.path.basename(path) or "")
            snippet = str(row.get("snippet") or row.get("summary") or "")
            try:
                score = float(row.get("score", 0.0))
            except (TypeError, ValueError):
                score = 0.0
            results.append(
                SearchPlusResult(
                    path=path,
                    drive=drive,
                    title=title,
                    snippet=snippet,
                    score=score,
                    source="semantic",
                    thumbnail=row.get("thumbnail"),
                    transcript_url=row.get("transcript_url"),
                    inventory_url=row.get("inventory_url"),
                    extras={"raw": row},
                )
            )
        return results

    def _fetch_transcripts(
        self,
        query: str,
        *,
        type_filter: Optional[str],
        since: Optional[str],
        limit: int,
    ) -> List[SearchPlusResult]:
        params = {
            "q": query,
            "limit": max(1, int(limit)),
        }
        if type_filter:
            params["category"] = type_filter
        if since:
            params["since"] = since
        payload = self._request_json("/v1/search/transcripts", params)
        results: List[SearchPlusResult] = []
        for row in payload.get("results", []) if isinstance(payload, dict) else []:
            path = str(row.get("path") or "")
            drive = str(row.get("drive_label") or row.get("drive") or "")
            title = str(row.get("title") or os.path.basename(path) or "")
            snippet = str(row.get("excerpt") or row.get("snippet") or "")
            try:
                score = float(row.get("score", 0.0))
            except (TypeError, ValueError):
                score = 0.0
            results.append(
                SearchPlusResult(
                    path=path,
                    drive=drive,
                    title=title,
                    snippet=snippet,
                    score=score,
                    source="transcripts",
                    thumbnail=row.get("thumbnail"),
                    transcript_url=row.get("transcript_url") or row.get("url"),
                    inventory_url=row.get("inventory_url"),
                    extras={"raw": row},
                )
            )
        return results

    def _fetch_inventory(
        self,
        query: str,
        *,
        type_filter: Optional[str],
        since: Optional[str],
        drives: Sequence[str],
        limit: int,
    ) -> List[SearchPlusResult]:
        if not drives:
            return []
        try:
            per_drive = max(1, int(limit) // max(1, len(drives)))
        except (TypeError, ValueError):
            per_drive = 20
        all_results: List[SearchPlusResult] = []
        for drive in drives:
            params = {
                "drive_label": drive,
                "q": query,
                "limit": per_drive,
            }
            if type_filter and type_filter.lower() != "any":
                params["category"] = type_filter
            if since:
                params["since"] = since
            payload = self._request_json("/v1/inventory", params)
            rows = payload.get("results", []) if isinstance(payload, dict) else []
            for row in rows:
                path = str(row.get("path") or "")
                name = str(row.get("name") or os.path.basename(path) or "")
                snippet = f"Category: {row.get('category') or 'n/a'}"
                score = self._keyword_score(query, name)
                transcript_url = row.get("transcript_url") or None
                inventory_url = self._inventory_detail_url(drive, path)
                all_results.append(
                    SearchPlusResult(
                        path=path,
                        drive=str(drive),
                        title=name,
                        snippet=snippet,
                        score=score,
                        source="inventory",
                        thumbnail=row.get("thumbnail"),
                        transcript_url=transcript_url,
                        inventory_url=inventory_url,
                        extras={"raw": row},
                    )
                )
        return all_results

    # ------------------------------------------------------------------
    def _request_json(self, endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}{endpoint}"
        query = urlencode({k: v for k, v in params.items() if v not in (None, "")})
        if query:
            url = f"{url}?{query}"
        request = Request(url)
        request.add_header("Accept", "application/json")
        if self.api_key:
            request.add_header("X-API-Key", self.api_key)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                data = response.read()
        except HTTPError as exc:  # pragma: no cover - depends on remote API
            raise RuntimeError(f"HTTP {exc.code} {exc.reason or ''}".strip()) from exc
        except URLError as exc:  # pragma: no cover - depends on remote API
            raise RuntimeError(str(exc.reason or exc)) from exc
        if not data:
            return {}
        try:
            decoded = data.decode("utf-8")
            return json.loads(decoded) if decoded else {}
        except json.JSONDecodeError as exc:
            raise RuntimeError("Invalid JSON response") from exc

    def _inventory_detail_url(self, drive: str, path: str) -> str:
        safe_path = quote(path or "", safe="")
        safe_drive = quote(drive or "", safe="")
        return f"{self.base_url}/v1/file?drive_label={safe_drive}&path={safe_path}"

    @staticmethod
    def _keyword_score(query: str, name: str) -> float:
        if not query or not name:
            return 0.0
        normalized_query = query.lower().strip()
        normalized_name = name.lower().strip()
        if not normalized_query or not normalized_name:
            return 0.0
        if normalized_query in normalized_name:
            ratio = len(normalized_query) / max(len(normalized_name), 1)
            return min(1.0, max(0.2, ratio))
        tokens = [token for token in normalized_query.split() if token]
        if not tokens:
            return 0.0
        hits = sum(1 for token in tokens if token in normalized_name)
        if not hits:
            return 0.0
        return min(1.0, hits / len(tokens))

    @staticmethod
    def _notify(callback: StatusCallback, service: str, state: str, meta: Dict[str, Any]) -> None:
        if callback is None:
            return
        try:
            callback(service, state, meta)
        except Exception:  # pragma: no cover - defensive callback guard
            pass


__all__ = ["SearchPlusClient", "SearchPlusResult", "SearchPlusResponse"]

