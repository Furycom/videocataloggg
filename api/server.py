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
    HeaviestFoldersReport,
    InventoryResponse,
    LargestFilesReport,
    OverviewReport,
    RecentChangesReport,
    TopExtensionsReport,
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

    def clamp_limit(value: Optional[int], default: int) -> int:
        try:
            parsed = int(value) if value is not None else int(default)
        except (TypeError, ValueError):
            parsed = int(default)
        parsed = max(1, parsed)
        return min(parsed, data.max_page_size)

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

    return app


__all__ = [
    "APIServerConfig",
    "create_app",
]
