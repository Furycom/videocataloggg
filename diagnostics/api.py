"""FastAPI router exposing diagnostics operations."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from core.paths import resolve_working_dir
from core.settings import load_settings

from .preflight import run_preflight
from .report import export_report_bundle, get_report, list_reports
from .smoke import run_smoke


class SmokeRequest(BaseModel):
    targets: Optional[List[str]] = Field(default=None, description="Selected smoke tests")
    budget: Optional[int] = Field(default=None, description="Optional max number of smoke tests")


class DiagnosticsAPI:
    """Router helper for diagnostics REST endpoints."""

    def __init__(self, working_dir: Optional[Path] = None, settings: Optional[Dict[str, Any]] = None) -> None:
        self.working_dir = working_dir or resolve_working_dir()
        self.settings = settings or load_settings(self.working_dir) or {}

    def router(self) -> APIRouter:
        router = APIRouter(prefix="/v1/diagnostics", tags=["diagnostics"])

        @router.post("/preflight")
        def run_preflight_endpoint() -> Dict[str, Any]:
            return run_preflight(working_dir=self.working_dir, settings=self.settings)

        @router.post("/smoke")
        def run_smoke_endpoint(request: SmokeRequest) -> Dict[str, Any]:
            return run_smoke(
                request.targets,
                budget=request.budget,
                working_dir=self.working_dir,
                settings=self.settings,
            )

        @router.get("/reports")
        def list_reports_endpoint() -> List[Dict[str, Any]]:
            return list_reports(working_dir=self.working_dir)

        @router.get("/report")
        def get_report_endpoint(id: str) -> Dict[str, Any]:
            payload = get_report(id, working_dir=self.working_dir)
            if payload is None:
                raise HTTPException(status_code=404, detail="Report not found")
            return payload

        @router.get("/download")
        def download_report_endpoint(id: Optional[str] = None) -> FileResponse:
            if id:
                report = get_report(id, working_dir=self.working_dir)
                if report is None:
                    raise HTTPException(status_code=404, detail="Report not found")
            bundle = export_report_bundle(working_dir=self.working_dir)
            return FileResponse(path=bundle, filename=bundle.name, media_type="application/zip")

        return router


__all__ = ["DiagnosticsAPI", "SmokeRequest"]
