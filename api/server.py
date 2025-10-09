"""FastAPI application exposing a read-only REST interface over the catalog."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Sequence

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .auth import APIKeyAuth
from .db import DataAccess
from .models import (
    DriveStatsResponse,
    DrivesResponse,
    FeatureVectorResponse,
    FeaturesResponse,
    FileResponse,
    HealthResponse,
    InventoryResponse,
)

LOGGER = logging.getLogger("videocatalog.api")


@dataclass(slots=True)
class APIServerConfig:
    """Runtime configuration for the FastAPI application."""

    data_access: DataAccess
    api_key: Optional[str]
    cors_origins: Sequence[str]
    app_version: str = "dev"
    vector_inline_dim: int = 2048


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

    @app.middleware("http")
    async def log_requests(request: Request, call_next):  # type: ignore[override]
        start = time.perf_counter()
        response = None
        try:
            response = await call_next(request)
            return response
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            status_code = response.status_code if response is not None else 500
            client = request.client.host if request.client else "-"
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

    @app.get("/v1/health", response_model=HealthResponse)
    def health_check(_: str = Depends(auth_dependency)) -> HealthResponse:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return HealthResponse(ok=True, version=config.app_version, time_utc=now)

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

    @app.get("/v1/stats", response_model=DriveStatsResponse)
    def stats(
        drive_label: str = Query(..., description="Drive label to query."),
        _: str = Depends(auth_dependency),
    ) -> DriveStatsResponse:
        ensure_drive(drive_label)
        payload = data.drive_stats(drive_label)
        return DriveStatsResponse(**payload)

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

    return app


__all__ = [
    "APIServerConfig",
    "create_app",
]
