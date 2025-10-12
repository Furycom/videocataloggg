"""FastAPI application exposing a read-only REST interface over the catalog."""
from __future__ import annotations

import logging
import time
import asyncio
import json
import random
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from assistant_webmon import WebMonitor
from orchestrator.api import OrchestratorConfig, OrchestratorService
from diagnostics.api import DiagnosticsAPI

from .auth import APIKeyAuth
from .assistant_gateway import AssistantGateway
from .db import DataAccess
from .models import (
    AssistantAskRequest,
    AssistantAskResponse,
    AssistantStatusResponse,
    PlaylistAiRequest,
    PlaylistAiResponse,
    PlaylistAiPlanItem,
    PlaylistCandidate,
    PlaylistBuildRequest,
    PlaylistBuildResponse,
    PlaylistBuildItem,
    PlaylistExportRequest,
    PlaylistExportResponse,
    PlaylistSuggestResponse,
    RealtimeHeartbeatRequest,
    RealtimeHeartbeatResponse,
    RealtimeStatusResponse,
    CatalogEpisodesResponse,
    CatalogItemDetailResponse,
    CatalogMoviesResponse,
    CatalogOpenFolderRequest,
    CatalogOpenFolderResponse,
    CatalogSearchResponse,
    CatalogSeasonRow,
    CatalogSeasonsResponse,
    CatalogSeriesResponse,
    CatalogSummaryResponse,
    DriveStatsResponse,
    DrivesResponse,
    FeatureVectorResponse,
    FeaturesResponse,
    FileResponse,
    HealthResponse,
    HeaviestFoldersReport,
    InventoryResponse,
    LargestFilesReport,
    MusicResponse,
    MusicReviewResponse,
    OverviewReport,
    RecentChangesReport,
    SemanticIndexRequest,
    SemanticOperationResponse,
    SemanticSearchHit,
    SemanticSearchResponse,
    SemanticStatusResponse,
    StructureDetailsResponse,
    StructureReviewResponse,
    StructureSummaryResponse,
    TopExtensionsReport,
    DocPreviewResponse,
    TextVerifyDetailsResponse,
    TextVerifyResponse,
    TextLiteResponse,
)
from semantic import SemanticPhaseError

from .events import CatalogEventBroker
from .vector_worker import VectorRefreshWorker

LOGGER = logging.getLogger("videocatalog.api")


@dataclass(slots=True)
class APIServerConfig:
    """Runtime configuration for the FastAPI application."""

    data_access: DataAccess
    api_key: Optional[str]
    cors_origins: Sequence[str]
    app_version: str = "dev"
    vector_inline_dim: int = 2048
    lan_only: bool = True


_LOCAL_CLIENT_SENTINELS = {
    "127.0.0.1",
    "::1",
    "localhost",
    "testclient",
}


def _normalise_remote_host(host: Optional[str]) -> Optional[str]:
    if host is None:
        return None
    value = host.strip().lower()
    if not value:
        return None
    if value.startswith("::ffff:"):
        value = value.rsplit(":", 1)[-1]
        if value.startswith("::ffff:"):
            value = value.split("::ffff:")[-1]
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    if "%" in value:  # strip IPv6 scope id
        value = value.split("%", 1)[0]
    return value


def _is_loopback_host(host: Optional[str]) -> bool:
    value = _normalise_remote_host(host)
    if value is None:
        return True
    if value in _LOCAL_CLIENT_SENTINELS:
        return True
    if value.startswith("127."):
        return True
    return False


def create_app(config: APIServerConfig) -> FastAPI:
    """Create a FastAPI application bound to the given configuration."""

    app = FastAPI(
        title="VideoCatalog Local API",
        version=config.app_version,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    allowed_origins: List[str] = [origin for origin in config.cors_origins if origin]
    if allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,
            allow_methods=["GET"],
            allow_headers=["*"],
        )

    auth_dependency = APIKeyAuth(config.api_key)
    data = config.data_access
    orchestrator_service = OrchestratorService(
        OrchestratorConfig(working_dir=str(data.working_dir), settings=data.settings_payload)
    )
    web_monitor = WebMonitor(data.working_dir)
    event_broker = CatalogEventBroker(data, monitor=web_monitor)
    vector_worker = VectorRefreshWorker(data, orchestrator=orchestrator_service)
    assistant_gateway = AssistantGateway(data)
    diagnostics_api = DiagnosticsAPI(data.working_dir, data.settings_payload)
    lan_only = bool(config.lan_only)

    @app.on_event("startup")
    async def _startup() -> None:
        orchestrator_service.start()
        await event_broker.start()
        await vector_worker.start()
        await web_monitor.start()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await event_broker.stop()
        await vector_worker.stop()
        await web_monitor.stop()
        assistant_gateway.shutdown()
        orchestrator_service.stop()

    @app.middleware("http")
    async def log_requests(request: Request, call_next):  # type: ignore[override]
        start = time.perf_counter()
        response: Optional[Response] = None
        client_host = request.client.host if request.client else None
        try:
            if lan_only and not _is_loopback_host(client_host):
                LOGGER.warning("Rejected non-local HTTP request from %s", client_host or "<unknown>")
                response = JSONResponse(status_code=status.HTTP_403_FORBIDDEN, content={"error": "LAN access disabled"})
            else:
                response = await call_next(request)
            return response
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            status_code = response.status_code if response is not None else 500
            client = client_host or "-"
            LOGGER.info(
                "%s %s -> %s (%.1f ms) ip=%s",
                request.method,
                request.url.path,
                status_code,
                duration_ms,
                client,
            )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_request: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return JSONResponse(status_code=exc.status_code, content={"error": detail})

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "invalid parameters", "details": exc.errors()},
        )

    def ensure_drive(drive_label: str) -> None:
        try:
            data._shard_path_for(drive_label)  # type: ignore[attr-defined]
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="unknown drive_label")
        except LookupError:
            raise HTTPException(status_code=404, detail="unknown drive_label")

    def clamp_limit(value: Optional[int], default: int) -> int:
        try:
            parsed = int(value) if value is not None else int(default)
        except (TypeError, ValueError):
            parsed = int(default)
        parsed = max(1, parsed)
        return min(parsed, data.max_page_size)

    def parse_langs(value: Optional[str]) -> List[str]:
        if not value:
            return []
        return [token.strip() for token in str(value).split(",") if token.strip()]

    def media_url(token: Optional[str]) -> Optional[str]:
        if not token:
            return None
        return f"/v1/catalog/thumb?id={token}"

    def mask_media_path(path: Optional[str]) -> str:
        if not path:
            return ""
        normalized = str(path).replace("\\", "/")
        segments = [segment for segment in normalized.split("/") if segment]
        if not segments:
            return normalized
        if len(segments) <= 2:
            return normalized
        return "/".join(["…"] + segments[-2:])

    def parse_bool_token(value: Optional[str]) -> Optional[bool]:
        if value is None:
            return None
        token = value.strip().lower()
        if token in {"1", "true", "yes", "y", "required", "present"}:
            return True
        if token in {"0", "false", "no", "n", "absent"}:
            return False
        return None

    def resolve_open_plan(path_value: Optional[str]) -> Dict[str, str]:
        path = str(path_value or "").strip()
        if not path:
            return {"plan": "shell_open", "path": ""}
        return {"plan": "shell_open", "path": path}

    @app.get("/v1/catalog/subscribe")
    async def catalog_subscribe(
        request: Request,
        last_seq: Optional[int] = Query(None, ge=0),
        api_key: Optional[str] = Query(None),
        client_id: Optional[str] = Query(None, min_length=4, max_length=128),
    ) -> StreamingResponse:
        expected_key = (config.api_key or "").strip()
        provided = request.headers.get("x-api-key") or (api_key or "").strip()
        if not expected_key or not provided or provided.strip() != expected_key:
            raise HTTPException(status_code=401, detail="Invalid or missing API key")
        assigned_client = client_id or secrets.token_hex(8)

        async def event_stream():
            connection_key = web_monitor.register(client_id=assigned_client, transport="sse")
            web_monitor.heartbeat(client_id=assigned_client, transport="sse")
            try:
                async for event in event_broker.subscribe(last_seq=last_seq or 0):
                    payload = {
                        "seq": event.seq,
                        "ts_utc": event.ts_utc,
                        "kind": event.kind,
                        "payload": event.payload,
                    }
                    web_monitor.record_event_delivery(ts_utc=event.ts_utc)
                    web_monitor.heartbeat(client_id=assigned_client, transport="sse")
                    yield f"data: {json.dumps(payload)}\n\n"
                    if await request.is_disconnected():
                        break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.debug("SSE stream stopped: %s", exc)
            finally:
                web_monitor.unregister(connection_key)

        headers = {
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Realtime-Transport": "sse",
        }
        return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)

    @app.websocket("/v1/catalog/subscribe")
    async def catalog_subscribe_ws(websocket: WebSocket):
        expected_key = (config.api_key or "").strip()
        provided = websocket.headers.get("x-api-key") or websocket.query_params.get("api_key")
        if not expected_key or not provided or provided.strip() != expected_key:
            await websocket.close(code=4401)
            return
        if lan_only and not _is_loopback_host(websocket.client.host if websocket.client else None):
            LOGGER.warning("Rejected non-local WebSocket connection from %s", (websocket.client.host if websocket.client else "<unknown>"))
            await websocket.close(code=4403)
            return
        assigned_client = websocket.query_params.get("client_id") or secrets.token_hex(8)
        connection_key: Optional[str] = None
        await websocket.accept()
        connection_key = web_monitor.register(client_id=assigned_client, transport="ws")
        web_monitor.heartbeat(client_id=assigned_client, transport="ws")
        try:
            try:
                last_seq_value = int(websocket.query_params.get("last_seq", "0"))
            except ValueError:
                last_seq_value = 0
            async for event in event_broker.subscribe(last_seq=last_seq_value):
                payload = {
                    "seq": event.seq,
                    "ts_utc": event.ts_utc,
                    "kind": event.kind,
                    "payload": event.payload,
                }
                await websocket.send_json(payload)
                web_monitor.record_event_delivery(ts_utc=event.ts_utc)
                web_monitor.heartbeat(client_id=assigned_client, transport="ws")
        except WebSocketDisconnect:
            return
        except Exception as exc:
            LOGGER.debug("WebSocket stream closed: %s", exc)
        finally:
            if connection_key:
                web_monitor.unregister(connection_key)
            try:
                await websocket.close()
            except Exception:
                pass

    @app.get("/v1/catalog/realtime/status", response_model=RealtimeStatusResponse)
    def realtime_status(
        client_id: Optional[str] = Query(None, min_length=4, max_length=128),
        _: str = Depends(auth_dependency),
    ) -> RealtimeStatusResponse:
        snapshot = web_monitor.snapshot(client_id=client_id)
        return RealtimeStatusResponse(**snapshot)

    @app.post("/v1/catalog/realtime/heartbeat", response_model=RealtimeHeartbeatResponse)
    def realtime_heartbeat(
        payload: RealtimeHeartbeatRequest,
        _: str = Depends(auth_dependency),
    ) -> RealtimeHeartbeatResponse:
        web_monitor.heartbeat(client_id=payload.client_id, transport=payload.transport)
        return RealtimeHeartbeatResponse(acknowledged=True, ts_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    @app.get("/v1/health", response_model=HealthResponse)
    def health_check(_: str = Depends(auth_dependency)) -> HealthResponse:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        status_payload = assistant_gateway.status()
        realtime = web_monitor.snapshot()
        budget = status_payload.get("tool_budget") or assistant_gateway.tool_budget_snapshot()
        try:
            budget_total = int(budget.get("total")) if budget.get("total") is not None else None
        except Exception:
            budget_total = None
        try:
            budget_remaining = int(budget.get("remaining", 0))
        except Exception:
            budget_remaining = 0
        return HealthResponse(
            ok=True,
            version=config.app_version,
            time_utc=now,
            gpu_ready=bool(status_payload.get("gpu_ready")),
            ws_clients=int(realtime.get("ws_connected", 0) or 0),
            sse_clients=int(realtime.get("sse_connected", 0) or 0),
            tool_budget_remaining=max(0, budget_remaining),
            tool_budget_total=budget_total,
            last_event_age_ms=realtime.get("last_event_age_ms"),
        )

    @app.get("/v1/assistant/status", response_model=AssistantStatusResponse)
    def assistant_status(_: str = Depends(auth_dependency)) -> AssistantStatusResponse:
        return AssistantStatusResponse(**assistant_gateway.status())

    @app.post("/v1/assistant/ask", response_model=AssistantAskResponse)
    def assistant_ask(
        request: AssistantAskRequest,
        _: str = Depends(auth_dependency),
    ) -> AssistantAskResponse:
        if request.mode != "context":
            raise HTTPException(status_code=400, detail="unsupported assistant mode")
        status_payload = assistant_gateway.status()
        if not status_payload.get("enabled", False):
            web_monitor.record_ai_request(error=True)
            message = status_payload.get("message") or "assistant unavailable"
            raise HTTPException(status_code=409, detail=message)
        detail = data.catalog_item_detail(request.item_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="catalog item not found")
        try:
            result = assistant_gateway.ask_context(
                request.item_id,
                detail,
                request.question,
                tool_budget=request.tool_budget,
                use_rag=bool(request.rag),
            )
        except RuntimeError as exc:
            web_monitor.record_ai_request(error=True)
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            web_monitor.record_ai_request(error=True)
            LOGGER.exception("Assistant ask failed: %s", exc)
            raise HTTPException(status_code=500, detail="assistant error") from exc
        web_monitor.record_ai_request(error=False)
        return AssistantAskResponse(**result)

    @app.get("/v1/drives", response_model=DrivesResponse)
    def drives(_: str = Depends(auth_dependency)) -> DrivesResponse:
        results = data.list_drives()
        return DrivesResponse(results=results)

    @app.get("/v1/inventory", response_model=InventoryResponse)
    def inventory(
        drive_label: str = Query(..., description="Drive label to query."),
        q: Optional[str] = Query(None, description="Substring filter across name/path."),
        category: Optional[str] = Query(None, description="Filter by category."),
        ext: Optional[str] = Query(None, description="Filter by extension."),
        mime: Optional[str] = Query(None, description="Filter by MIME prefix."),
        since: Optional[str] = Query(None, description="Return rows with mtime >= this ISO timestamp."),
        limit: Optional[int] = Query(None, ge=1),
        offset: Optional[int] = Query(None, ge=0),
        _: str = Depends(auth_dependency),
    ) -> InventoryResponse:
        ensure_drive(drive_label)
        try:
            results, pagination, next_offset, total = data.inventory_page(
                drive_label,
                q=q,
                category=category,
                ext=ext,
                mime=mime,
                since=since,
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return InventoryResponse(
            results=results,
            limit=pagination.limit,
            offset=pagination.offset,
            next_offset=next_offset,
            total_estimate=total,
        )

    @app.get("/v1/file", response_model=FileResponse)
    def file_details(
        drive_label: str = Query(..., description="Drive label to query."),
        path: str = Query(..., description="Exact inventory path."),
        _: str = Depends(auth_dependency),
    ) -> FileResponse:
        ensure_drive(drive_label)
        row = data.inventory_row(drive_label, path)
        if row is None:
            raise HTTPException(status_code=404, detail="file not found")
        return FileResponse(**row)

    @app.get("/v1/docs/preview", response_model=DocPreviewResponse)
    def doc_previews(
        drive_label: str = Query(..., description="Drive label to query."),
        q: Optional[str] = Query(None, description="Optional FTS query across summary and keywords."),
        limit: Optional[int] = Query(None, ge=1),
        offset: Optional[int] = Query(None, ge=0),
        _: str = Depends(auth_dependency),
    ) -> DocPreviewResponse:
        ensure_drive(drive_label)
        results, pagination, next_offset, total = data.doc_preview_page(
            drive_label,
            q=q,
            limit=limit,
            offset=offset,
        )
        return DocPreviewResponse(
            results=results,
            limit=pagination.limit,
            offset=pagination.offset,
            next_offset=next_offset,
            total_estimate=total,
        )

    @app.get("/v1/textlite/preview", response_model=TextLiteResponse)
    def textlite_previews(
        drive_label: str = Query(..., description="Drive label to query."),
        q: Optional[str] = Query(None, description="Optional FTS query across TextLite summaries and keywords."),
        limit: Optional[int] = Query(None, ge=1),
        offset: Optional[int] = Query(None, ge=0),
        _: str = Depends(auth_dependency),
    ) -> TextLiteResponse:
        ensure_drive(drive_label)
        results, pagination, next_offset, total = data.textlite_page(
            drive_label,
            q=q,
            limit=limit,
            offset=offset,
        )
        return TextLiteResponse(
            results=results,
            limit=pagination.limit,
            offset=pagination.offset,
            next_offset=next_offset,
            total_estimate=total,
        )

    @app.get("/v1/textverify/summary", response_model=TextVerifyResponse)
    def textverify_summary(
        drive_label: str = Query(..., description="Drive label to query."),
        min_score: Optional[float] = Query(
            None,
            ge=0.0,
            le=1.0,
            description="Optional minimum aggregated score (0..1) required for inclusion.",
        ),
        limit: Optional[int] = Query(None, ge=1),
        offset: Optional[int] = Query(None, ge=0),
        _: str = Depends(auth_dependency),
    ) -> TextVerifyResponse:
        ensure_drive(drive_label)
        try:
            results, pagination, next_offset, total = data.textverify_page(
                drive_label,
                min_score=min_score,
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return TextVerifyResponse(
            results=results,
            limit=pagination.limit,
            offset=pagination.offset,
            next_offset=next_offset,
            total_estimate=total,
        )

    @app.get("/v1/textverify/details", response_model=TextVerifyDetailsResponse)
    def textverify_details(
        drive_label: str = Query(..., description="Drive label to query."),
        path: str = Query(..., description="Relative media path for the artifact."),
        _: str = Depends(auth_dependency),
    ) -> TextVerifyDetailsResponse:
        ensure_drive(drive_label)
        row = data.textverify_details(drive_label, path)
        if row is None:
            raise HTTPException(status_code=404, detail="text verification artifact not found")
        return TextVerifyDetailsResponse(**row)

    @app.get("/v1/music", response_model=MusicResponse)
    def music_metadata(
        drive_label: str = Query(..., description="Drive label to query."),
        q: Optional[str] = Query(
            None,
            description="Substring filter across path, artist, title, or album.",
        ),
        ext: Optional[str] = Query(
            None, description="Filter by lowercase file extension (without dot)."
        ),
        min_confidence: Optional[float] = Query(
            None,
            ge=0.0,
            le=1.0,
            description="Minimum confidence (0..1) required to include a row.",
        ),
        limit: Optional[int] = Query(None, ge=1),
        offset: Optional[int] = Query(None, ge=0),
        _: str = Depends(auth_dependency),
    ) -> MusicResponse:
        ensure_drive(drive_label)
        try:
            results, pagination, next_offset, total = data.music_page(
                drive_label,
                q=q,
                ext=ext,
                min_confidence=min_confidence,
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return MusicResponse(
            drive_label=drive_label,
            results=results,
            limit=pagination.limit,
            offset=pagination.offset,
            next_offset=next_offset,
            total_estimate=total,
        )

    @app.get("/v1/music/review", response_model=MusicReviewResponse)
    def music_review_queue(
        drive_label: str = Query(..., description="Drive label to query."),
        limit: Optional[int] = Query(None, ge=1),
        offset: Optional[int] = Query(None, ge=0),
        _: str = Depends(auth_dependency),
    ) -> MusicReviewResponse:
        ensure_drive(drive_label)
        results, pagination, next_offset, total = data.music_review_page(
            drive_label,
            limit=limit,
            offset=offset,
        )
        return MusicReviewResponse(
            drive_label=drive_label,
            results=results,
            limit=pagination.limit,
            offset=pagination.offset,
            next_offset=next_offset,
            total_estimate=total,
        )

    @app.get("/v1/catalog/movies", response_model=CatalogMoviesResponse)
    def catalog_movies(
        query: Optional[str] = Query(None, description="Free text query against title/path."),
        year_min: Optional[int] = Query(None, ge=1800),
        year_max: Optional[int] = Query(None, ge=1800),
        conf_min: Optional[float] = Query(None, ge=0.0, le=1.0),
        quality_min: Optional[int] = Query(None),
        lang_audio: Optional[str] = Query(None, description="Comma separated audio language codes."),
        lang_sub: Optional[str] = Query(None, description="Comma separated subtitle language codes."),
        drive: Optional[str] = Query(None, description="Restrict to a single drive label."),
        sort: str = Query("title", description="Sort field: title, year, confidence, quality."),
        order: str = Query("asc", description="Sort order asc|desc."),
        limit: Optional[int] = Query(None, ge=1),
        offset: Optional[int] = Query(None, ge=0),
        only_low_confidence: bool = Query(False, description="Return only low-confidence rows."),
        _: str = Depends(auth_dependency),
    ) -> CatalogMoviesResponse:
        results, pagination, next_offset, total = data.catalog_movies_page(
            query=query,
            year_min=year_min,
            year_max=year_max,
            confidence_min=conf_min,
            quality_min=quality_min,
            audio_langs=parse_langs(lang_audio),
            subs_langs=parse_langs(lang_sub),
            drive=drive,
            only_low_confidence=only_low_confidence,
            sort=sort,
            order=order,
            limit=limit,
            offset=offset,
        )
        payload = [
            {
                **row,
                "poster_thumb": media_url(row.get("poster_thumb")),
                "contact_sheet": media_url(row.get("contact_sheet")),
            }
            for row in results
        ]
        return CatalogMoviesResponse(
            results=payload,
            limit=pagination.limit,
            offset=pagination.offset,
            next_offset=next_offset,
            total_estimate=total,
        )

    @app.get("/v1/catalog/tv/series", response_model=CatalogSeriesResponse)
    def catalog_series(
        query: Optional[str] = Query(None, description="Free text query against title/path."),
        conf_min: Optional[float] = Query(None, ge=0.0, le=1.0),
        drive: Optional[str] = Query(None, description="Restrict to a single drive label."),
        sort: str = Query("title", description="Sort field: title, confidence, seasons."),
        order: str = Query("asc", description="Sort order asc|desc."),
        limit: Optional[int] = Query(None, ge=1),
        offset: Optional[int] = Query(None, ge=0),
        only_low_confidence: bool = Query(False, description="Return only low confidence entries."),
        _: str = Depends(auth_dependency),
    ) -> CatalogSeriesResponse:
        results, pagination, next_offset, total = data.catalog_tv_series_page(
            query=query,
            confidence_min=conf_min,
            drive=drive,
            only_low_confidence=only_low_confidence,
            sort=sort,
            order=order,
            limit=limit,
            offset=offset,
        )
        payload = [
            {
                **row,
                "poster_thumb": media_url(row.get("poster_thumb")),
            }
            for row in results
        ]
        return CatalogSeriesResponse(
            results=payload,
            limit=pagination.limit,
            offset=pagination.offset,
            next_offset=next_offset,
            total_estimate=total,
        )

    @app.get("/v1/catalog/tv/seasons", response_model=CatalogSeasonsResponse)
    def catalog_seasons(
        series_id: str = Query(..., description="Series identifier returned by /tv/series."),
        _: str = Depends(auth_dependency),
    ) -> CatalogSeasonsResponse:
        seasons, meta = data.catalog_tv_seasons(series_id)
        payload = [CatalogSeasonRow(**season) for season in seasons]
        return CatalogSeasonsResponse(series=meta, seasons=payload)

    @app.get("/v1/catalog/tv/episodes", response_model=CatalogEpisodesResponse)
    def catalog_episodes(
        series_id: str = Query(..., description="Series identifier returned by /tv/series."),
        season: Optional[int] = Query(None, description="Optional season number filter."),
        _: str = Depends(auth_dependency),
    ) -> CatalogEpisodesResponse:
        episodes, season_meta = data.catalog_tv_episodes(series_id, season_number=season)
        payload = [
            {
                **row,
                "poster_thumb": media_url(row.get("poster_thumb")),
                "quality": row.get("quality_score"),
            }
            for row in episodes
        ]
        return CatalogEpisodesResponse(season=season_meta or {}, episodes=payload)

    @app.get("/v1/catalog/item", response_model=CatalogItemDetailResponse)
    def catalog_item_detail(
        id: str = Query(..., description="Item identifier returned by the catalog endpoints."),
        _: str = Depends(auth_dependency),
    ) -> CatalogItemDetailResponse:
        detail = data.catalog_item_detail(id)
        if detail is None:
            raise HTTPException(status_code=404, detail="catalog item not found")
        kind, _, _ = data._parse_item_id(id) or (None, None, None)
        if kind == "movie":
            movie_payload = dict(detail)
            movie_payload["poster_thumb"] = media_url(movie_payload.get("poster_thumb"))
            movie_payload["contact_sheet"] = media_url(movie_payload.get("contact_sheet"))
            return CatalogItemDetailResponse(kind="movie", movie=movie_payload)
        if kind == "episode":
            episode_payload = dict(detail)
            episode_payload["poster_thumb"] = media_url(episode_payload.get("poster_thumb"))
            return CatalogItemDetailResponse(kind="episode", episode=episode_payload)
        return CatalogItemDetailResponse(kind="series", series=detail)

    @app.get("/v1/catalog/thumb")
    def catalog_thumb(
        id: str = Query(..., description="Media token returned by the catalog listing endpoints."),
        _: str = Depends(auth_dependency),
    ) -> Response:
        blob = data.catalog_fetch_media_blob(id)
        if blob is None:
            raise HTTPException(status_code=404, detail="thumbnail not found")
        payload, mime = blob
        return Response(content=payload, media_type=mime, headers={"Cache-Control": "max-age=3600"})

    @app.post("/v1/catalog/open-folder", response_model=CatalogOpenFolderResponse)
    def catalog_open_folder(
        request: CatalogOpenFolderRequest,
        _: str = Depends(auth_dependency),
    ) -> CatalogOpenFolderResponse:
        path = request.path.strip()
        if not path:
            raise HTTPException(status_code=400, detail="path is required")
        return CatalogOpenFolderResponse(plan="shell_open", path=path)

    @app.get("/v1/playlist/suggest", response_model=PlaylistSuggestResponse)
    def playlist_suggest(
        dur_min: Optional[int] = Query(None, ge=0, description="Minimum runtime in minutes."),
        dur_max: Optional[int] = Query(None, ge=0, description="Maximum runtime in minutes."),
        conf_min: Optional[float] = Query(None, ge=0.0, le=1.0, description="Minimum confidence threshold."),
        qual_min: Optional[int] = Query(None, ge=0, le=100, description="Minimum quality score."),
        audio: Optional[str] = Query(None, description="Comma separated audio language codes."),
        subs: Optional[str] = Query(
            None, description="Subtitle requirement: yes/no/any (defaults to any)."
        ),
        year_min: Optional[int] = Query(None, ge=1900, description="Minimum release/air year."),
        year_max: Optional[int] = Query(None, ge=1900, description="Maximum release/air year."),
        drive: Optional[str] = Query(None, description="Restrict suggestions to a drive label."),
        genres: Optional[str] = Query(None, description="Comma separated list of preferred genres."),
        limit: Optional[int] = Query(40, ge=1, le=200, description="Maximum candidates to return."),
        _: str = Depends(auth_dependency),
    ) -> PlaylistSuggestResponse:
        audio_filters = parse_langs(audio)
        genre_filters = parse_langs(genres)
        subs_flag = parse_bool_token(subs)
        resolved_limit = clamp_limit(limit, 40)
        candidates = data.playlist_candidates(
            dur_min=dur_min,
            dur_max=dur_max,
            conf_min=conf_min,
            qual_min=qual_min,
            audio_langs=audio_filters,
            subs_required=subs_flag,
            year_min=year_min,
            year_max=year_max,
            drive=drive,
            genres=genre_filters,
            limit=resolved_limit,
        )
        payload = [
            PlaylistCandidate(
                id=row["id"],
                kind=row.get("kind", "movie"),
                drive=row.get("drive"),
                path=row.get("path") or "",
                masked_path=mask_media_path(row.get("path")),
                title=row.get("title"),
                year=row.get("year"),
                duration_min=row.get("duration_min"),
                quality=row.get("quality"),
                confidence=float(row.get("confidence") or 0.0),
                langs_audio=row.get("langs_audio") or [],
                langs_subs=row.get("langs_subs") or [],
                subs_present=bool(row.get("subs_present")),
                genres=row.get("genres") or [],
            )
            for row in candidates
        ]
        return PlaylistSuggestResponse(limit=resolved_limit, candidates=payload)

    @app.post("/v1/playlist/build", response_model=PlaylistBuildResponse)
    def playlist_build(
        request: PlaylistBuildRequest,
        _: str = Depends(auth_dependency),
    ) -> PlaylistBuildResponse:
        if not request.items:
            raise HTTPException(status_code=400, detail="items list cannot be empty")
        mode = (request.mode or "weighted").strip().lower()
        if mode not in {"weighted", "sort_quality", "sort_confidence"}:
            raise HTTPException(status_code=400, detail="invalid mode")
        resolved = data.playlist_items_by_ids(request.items)
        missing = [item for item in request.items if item not in resolved]
        if missing:
            raise HTTPException(status_code=404, detail=f"items not found: {', '.join(missing)}")
        items = [resolved[item_id] for item_id in request.items if item_id in resolved]
        if mode == "sort_quality":
            items.sort(
                key=lambda row: (
                    row.get("quality") if row.get("quality") is not None else -1,
                    row.get("confidence") or 0.0,
                    row.get("duration_min") or 0,
                ),
                reverse=True,
            )
        elif mode == "sort_confidence":
            items.sort(
                key=lambda row: (
                    row.get("confidence") or 0.0,
                    row.get("quality") if row.get("quality") is not None else -1,
                    row.get("duration_min") or 0,
                ),
                reverse=True,
            )
        else:
            rng = random.Random(secrets.randbits(64))

            def _weight(row: Dict[str, Any]) -> float:
                quality = float(row.get("quality") or 0) / 100.0
                confidence = float(row.get("confidence") or 0.0)
                return max(0.05, min(1.0, (quality * 0.6) + (confidence * 0.4)))

            weighted: List[Tuple[float, Dict[str, Any]]] = []
            for row in items:
                weight = _weight(row)
                score = rng.random() ** (1.0 / max(weight, 1e-3))
                weighted.append((score, row))
            weighted.sort(key=lambda pair: pair[0], reverse=True)
            items = [row for _, row in weighted]

        target = request.target_minutes if request.target_minutes and request.target_minutes > 0 else None
        playlist_rows = []
        total_minutes = 0
        for row in items:
            duration = row.get("duration_min") or 0
            next_total = total_minutes + (duration or 0)
            if target and total_minutes >= target and playlist_rows:
                break
            total_minutes = next_total
            playlist_rows.append(
                PlaylistBuildItem(
                    id=row["id"],
                    kind=row.get("kind", "movie"),
                    drive=row.get("drive"),
                    title=row.get("title"),
                    year=row.get("year"),
                    duration_min=row.get("duration_min"),
                    cumulative_minutes=total_minutes,
                    path=row.get("path") or "",
                    masked_path=mask_media_path(row.get("path")),
                    quality=row.get("quality"),
                    confidence=float(row.get("confidence") or 0.0),
                    langs_audio=row.get("langs_audio") or [],
                    langs_subs=row.get("langs_subs") or [],
                    subs_present=bool(row.get("subs_present")),
                    open_plan=resolve_open_plan(
                        row.get("folder_path")
                        if row.get("kind") == "movie"
                        else str(Path(str(row.get("path") or "")).parent)
                    ),
                )
            )
        return PlaylistBuildResponse(
            mode=mode,
            target_minutes=target,
            total_minutes=total_minutes,
            items=playlist_rows,
        )

    @app.post("/v1/playlist/export", response_model=PlaylistExportResponse)
    def playlist_export(
        request: PlaylistExportRequest,
        _: str = Depends(auth_dependency),
    ) -> PlaylistExportResponse:
        if not request.items:
            raise HTTPException(status_code=400, detail="items list cannot be empty")
        export_path = data.playlist_export(
            request.name,
            request.format,
            [item.model_dump() for item in request.items],
        )
        return PlaylistExportResponse(
            format=request.format,
            path=str(export_path),
            count=len(request.items),
        )

    @app.post("/v1/playlist/open-folder", response_model=CatalogOpenFolderResponse)
    def playlist_open_folder(
        request: CatalogOpenFolderRequest,
        _: str = Depends(auth_dependency),
    ) -> CatalogOpenFolderResponse:
        path = request.path.strip()
        if not path:
            raise HTTPException(status_code=400, detail="path is required")
        return CatalogOpenFolderResponse(plan="shell_open", path=path)

    @app.post("/v1/playlist/ai", response_model=PlaylistAiResponse)
    def playlist_ai(
        request: PlaylistAiRequest,
        _: str = Depends(auth_dependency),
    ) -> PlaylistAiResponse:
        if not assistant_gateway.enabled:
            raise HTTPException(status_code=503, detail="assistant not available")
        constraints = request.constraints
        candidates = data.playlist_candidates(
            dur_min=constraints.dur_min,
            dur_max=constraints.dur_max,
            conf_min=constraints.conf_min,
            qual_min=constraints.qual_min,
            audio_langs=constraints.langs_audio,
            genres=constraints.genres,
            limit=12,
        )
        plan_items = []
        for row in candidates[:6]:
            plan_items.append(
                PlaylistAiPlanItem(
                    id=row["id"],
                    reason=f"Matches request '{request.question}' with quality {row.get('quality')} and confidence {(row.get('confidence') or 0.0):.2f}",
                    confidence=float(row.get("confidence") or 0.0),
                    quality=row.get("quality"),
                )
            )
        reasoning = (
            "Selected up to six high quality matches within the requested constraints. "
            "Results favour catalog entries with higher quality and confidence scores."
        )
        sources = [
            "db_query_sql:view_movies_list",  # documented pseudo tool usage
            "db_query_sql:view_tv_episodes_list",
        ]
        return PlaylistAiResponse(mode="catalog_sampler", items=plan_items, reasoning=reasoning, sources=sources)

    @app.get("/v1/catalog/summary", response_model=CatalogSummaryResponse)
    def catalog_summary(
        _: str = Depends(auth_dependency),
    ) -> CatalogSummaryResponse:
        summary = data.catalog_summary()
        return CatalogSummaryResponse(**summary)

    @app.get("/v1/catalog/search", response_model=CatalogSearchResponse)
    def catalog_search(
        q: str = Query(..., min_length=1, description="Query string to execute."),
        mode: str = Query("fts", description="Search mode: fts or semantic."),
        top_k: int = Query(20, ge=1, le=100),
        _: str = Depends(auth_dependency),
    ) -> CatalogSearchResponse:
        hits = data.catalog_search(q, mode=mode, top_k=top_k)
        return CatalogSearchResponse(mode=mode, query=q, results=hits)

    @app.get("/v1/stats", response_model=DriveStatsResponse)
    def stats(
        drive_label: str = Query(..., description="Drive label to query."),
        _: str = Depends(auth_dependency),
    ) -> DriveStatsResponse:
        ensure_drive(drive_label)
        payload = data.drive_stats(drive_label)
        return DriveStatsResponse(**payload)

    @app.get("/v1/reports/overview", response_model=OverviewReport)
    def reports_overview(
        drive_label: str = Query(..., description="Drive label to query."),
        _: str = Depends(auth_dependency),
    ) -> OverviewReport:
        ensure_drive(drive_label)
        result = data.report_overview(drive_label)
        categories = [
            {
                "category": cat.category,
                "files": cat.files,
                "bytes": cat.bytes,
            }
            for cat in result.categories
        ]
        return OverviewReport(
            drive_label=drive_label,
            total_files=result.total_files,
            total_size=result.total_size,
            average_size=result.average_size,
            source=result.source,
            categories=categories,
        )

    @app.get("/v1/structure/summary", response_model=StructureSummaryResponse)
    def structure_summary(
        drive_label: str = Query(..., description="Drive label to query."),
        _: str = Depends(auth_dependency),
    ) -> StructureSummaryResponse:
        ensure_drive(drive_label)
        payload = data.structure_summary(drive_label)
        return StructureSummaryResponse(**payload)

    @app.get("/v1/structure/review", response_model=StructureReviewResponse)
    def structure_review(
        drive_label: str = Query(..., description="Drive label to query."),
        limit: Optional[int] = Query(None, ge=1),
        offset: Optional[int] = Query(None, ge=0),
        _: str = Depends(auth_dependency),
    ) -> StructureReviewResponse:
        ensure_drive(drive_label)
        rows, pagination, next_offset, total = data.structure_review_page(
            drive_label, limit=limit, offset=offset
        )
        return StructureReviewResponse(
            drive_label=drive_label,
            results=rows,
            limit=pagination.limit,
            offset=pagination.offset,
            next_offset=next_offset,
            total_estimate=total,
        )

    @app.get("/v1/structure/details", response_model=StructureDetailsResponse)
    def structure_details(
        drive_label: str = Query(..., description="Drive label to query."),
        folder_path: str = Query(..., description="Folder path relative to the drive."),
        _: str = Depends(auth_dependency),
    ) -> StructureDetailsResponse:
        ensure_drive(drive_label)
        profile = data.structure_details(drive_label, folder_path)
        if profile is None:
            raise HTTPException(status_code=404, detail="structure profile not found")
        return StructureDetailsResponse(drive_label=drive_label, profile=profile)

    @app.get("/v1/reports/top-extensions", response_model=TopExtensionsReport)
    def reports_top_extensions(
        drive_label: str = Query(..., description="Drive label to query."),
        limit: Optional[int] = Query(20, ge=1),
        _: str = Depends(auth_dependency),
    ) -> TopExtensionsReport:
        ensure_drive(drive_label)
        resolved_limit = clamp_limit(limit, 20)
        result = data.report_top_extensions(drive_label, resolved_limit)
        entries = [
            {
                "extension": entry.extension or "",
                "files": entry.files,
                "bytes": entry.bytes,
                "rank_count": entry.rank_count,
                "rank_size": entry.rank_size,
            }
            for entry in result.entries
        ]
        return TopExtensionsReport(
            drive_label=drive_label,
            limit=resolved_limit,
            entries=entries,
        )

    @app.get("/v1/reports/largest-files", response_model=LargestFilesReport)
    def reports_largest_files(
        drive_label: str = Query(..., description="Drive label to query."),
        limit: Optional[int] = Query(100, ge=1),
        _: str = Depends(auth_dependency),
    ) -> LargestFilesReport:
        ensure_drive(drive_label)
        resolved_limit = clamp_limit(limit, 100)
        rows = data.report_largest_files(drive_label, resolved_limit)
        results = [
            {
                "path": row.path,
                "size_bytes": row.size_bytes,
                "mtime_utc": row.mtime_utc,
                "category": row.category,
            }
            for row in rows
        ]
        return LargestFilesReport(
            drive_label=drive_label,
            limit=resolved_limit,
            results=results,
        )

    @app.get("/v1/reports/heaviest-folders", response_model=HeaviestFoldersReport)
    def reports_heaviest_folders(
        drive_label: str = Query(..., description="Drive label to query."),
        depth: Optional[int] = Query(2, ge=0),
        limit: Optional[int] = Query(20, ge=1),
        _: str = Depends(auth_dependency),
    ) -> HeaviestFoldersReport:
        ensure_drive(drive_label)
        resolved_limit = clamp_limit(limit, 20)
        resolved_depth = max(0, int(depth or 0))
        rows = data.report_heaviest_folders(drive_label, resolved_depth, resolved_limit)
        results = [
            {"folder": row.folder, "files": row.files, "bytes": row.bytes}
            for row in rows
        ]
        return HeaviestFoldersReport(
            drive_label=drive_label,
            depth=resolved_depth,
            limit=resolved_limit,
            results=results,
        )

    @app.get("/v1/reports/recent", response_model=RecentChangesReport)
    def reports_recent(
        drive_label: str = Query(..., description="Drive label to query."),
        days: Optional[int] = Query(30, ge=0),
        limit: Optional[int] = Query(20, ge=1),
        _: str = Depends(auth_dependency),
    ) -> RecentChangesReport:
        ensure_drive(drive_label)
        resolved_limit = clamp_limit(limit, 20)
        resolved_days = max(0, int(days or 0))
        recent = data.report_recent_changes(drive_label, resolved_days, resolved_limit)
        results = [
            {
                "path": row.path,
                "size_bytes": row.size_bytes,
                "mtime_utc": row.mtime_utc,
                "category": row.category,
            }
            for row in recent.rows
        ]
        return RecentChangesReport(
            drive_label=drive_label,
            days=resolved_days,
            limit=resolved_limit,
            total=recent.total,
            results=results,
        )

    @app.get("/v1/features", response_model=FeaturesResponse)
    def features(
        drive_label: str = Query(..., description="Drive label to query."),
        path: Optional[str] = Query(None, description="Substring filter on path."),
        kind: Optional[str] = Query(None, description="Feature kind (image/video)."),
        limit: Optional[int] = Query(None, ge=1),
        offset: Optional[int] = Query(None, ge=0),
        _: str = Depends(auth_dependency),
    ) -> FeaturesResponse:
        ensure_drive(drive_label)
        results, pagination, next_offset, total = data.features_page(
            drive_label,
            path_query=path,
            kind=kind,
            limit=limit,
            offset=offset,
        )
        return FeaturesResponse(
            results=results,
            limit=pagination.limit,
            offset=pagination.offset,
            next_offset=next_offset,
            total_estimate=total,
        )

    @app.get("/v1/features/vector", response_model=FeatureVectorResponse)
    def feature_vector(
        drive_label: str = Query(..., description="Drive label to query."),
        path: str = Query(..., description="Exact inventory path."),
        raw: bool = Query(False, description="Set to true to allow large vectors."),
        _: str = Depends(auth_dependency),
    ) -> FeatureVectorResponse:
        ensure_drive(drive_label)
        row = data.feature_vector(drive_label, path)
        if row is None:
            raise HTTPException(status_code=404, detail="feature not found")
        if not raw and row.get("dim", 0) > config.vector_inline_dim:
            raise HTTPException(
                status_code=400,
                detail=(
                    "vector dimensionality exceeds inline guard; "
                    "retry with raw=true if you intend to download"
                ),
            )
        return FeatureVectorResponse(**row)

    @app.get("/v1/semantic/search", response_model=SemanticSearchResponse)
    def semantic_search(
        q: str = Query(..., description="Query string used for semantic search."),
        mode: str = Query("ann", description="Search mode: ann, text, or hybrid."),
        drive_label: Optional[str] = Query(
            None, description="Optional drive label to scope the search."
        ),
        hybrid: bool = Query(
            False, description="Enable hybrid scoring (ANN + FTS) regardless of mode."
        ),
        limit: Optional[int] = Query(None, ge=1),
        offset: Optional[int] = Query(None, ge=0),
        _: str = Depends(auth_dependency),
    ) -> SemanticSearchResponse:
        mode_value = (mode or "ann").lower()
        if mode_value not in {"ann", "text", "hybrid"}:
            mode_value = "ann"
        if drive_label:
            ensure_drive(drive_label)
        try:
            results, pagination, next_offset, total = data.semantic_search(
                q,
                mode=mode_value,
                limit=limit,
                offset=offset,
                drive_label=drive_label,
                hybrid=bool(hybrid),
            )
        except SemanticPhaseError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        hits = [SemanticSearchHit(**row) for row in results]
        return SemanticSearchResponse(
            query=q,
            mode=mode_value,
            hybrid=bool(hybrid or mode_value == "hybrid"),
            results=hits,
            limit=pagination.limit,
            offset=pagination.offset,
            next_offset=next_offset,
            total_estimate=total,
        )

    @app.get("/v1/semantic/index", response_model=SemanticStatusResponse)
    def semantic_index_status(_: str = Depends(auth_dependency)) -> SemanticStatusResponse:
        try:
            payload = data.semantic_status()
        except SemanticPhaseError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return SemanticStatusResponse(**payload)

    @app.post("/v1/semantic/index", response_model=SemanticOperationResponse)
    def semantic_index(
        body: SemanticIndexRequest,
        _: str = Depends(auth_dependency),
    ) -> SemanticOperationResponse:
        mode_value = (body.mode or "build").lower()
        if mode_value not in {"build", "rebuild"}:
            raise HTTPException(status_code=400, detail="mode must be build or rebuild")
        rebuild = mode_value == "rebuild"
        try:
            stats = data.semantic_index(rebuild=rebuild)
        except SemanticPhaseError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return SemanticOperationResponse(ok=True, action=mode_value, stats=stats)

    @app.post("/v1/semantic/transcribe", response_model=SemanticOperationResponse)
    def semantic_transcribe(_: str = Depends(auth_dependency)) -> SemanticOperationResponse:
        try:
            stats = data.semantic_transcribe()
        except SemanticPhaseError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return SemanticOperationResponse(ok=True, action="transcribe", stats=stats)

    app.include_router(orchestrator_service.router())
    app.include_router(diagnostics_api.router())

    dist_dir = (
        Path(__file__).resolve().parent.parent / "web" / "catalog-ui" / "dist"
    )
    if dist_dir.is_dir():
        app.mount("/", StaticFiles(directory=dist_dir, html=True), name="catalog-ui")

    return app


__all__ = [
    "APIServerConfig",
    "create_app",
]
