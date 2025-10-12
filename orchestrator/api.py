"""Public API surface for orchestrator control."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .logs import OrchestratorLogger
from .registry import build_default_registry
from .scheduler import Scheduler


@dataclass(slots=True)
class OrchestratorConfig:
    working_dir: str
    settings: Dict[str, Any]


class EnqueueRequest(BaseModel):
    kind: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    priority: int = 50
    max_attempts: int = 3


class JobActionRequest(BaseModel):
    job_id: int


class QueueResponse(BaseModel):
    jobs: List[Dict[str, Any]]
    paused: bool


class JobDetailResponse(BaseModel):
    job: Dict[str, Any]
    checkpoint: Optional[Dict[str, Any]] = None
    events: List[Dict[str, Any]] = Field(default_factory=list)


class LocksResponse(BaseModel):
    locks: Dict[str, Any]


class OrchestratorService:
    """Facade combining scheduler and API helpers."""

    def __init__(self, config: OrchestratorConfig) -> None:
        self._settings = dict(config.settings)
        self._enabled = bool(self._settings.get("orchestrator", {}).get("enable", True))
        self._scheduler = Scheduler(
            working_dir=config.working_dir,
            settings=self._settings,
            registry=build_default_registry(),
            logger=OrchestratorLogger(config.working_dir),
        )

    @property
    def scheduler(self) -> Scheduler:
        return self._scheduler

    @property
    def enabled(self) -> bool:
        return self._enabled

    def start(self) -> None:
        if self._enabled:
            self._scheduler.start()

    def stop(self) -> None:
        if self._enabled:
            self._scheduler.stop()

    def router(self) -> APIRouter:
        router = APIRouter(prefix="/v1/orch", tags=["orchestrator"])

        def require_enabled() -> None:
            if not self._enabled:
                raise HTTPException(status_code=503, detail="Orchestrator disabled in settings")

        @router.post("/enqueue", response_model=Dict[str, int])
        def enqueue(request: EnqueueRequest) -> Dict[str, int]:
            require_enabled()
            self._scheduler.start()
            job_id = self._scheduler.enqueue(
                request.kind,
                request.payload,
                priority=request.priority,
                max_attempts=request.max_attempts,
            )
            return {"job_id": job_id}

        @router.post("/pause", response_model=Dict[str, str])
        def pause(request: JobActionRequest) -> Dict[str, str]:
            require_enabled()
            self._scheduler.pause_job(request.job_id)
            return {"status": "paused"}

        @router.post("/resume", response_model=Dict[str, str])
        def resume(request: JobActionRequest) -> Dict[str, str]:
            require_enabled()
            self._scheduler.resume_job(request.job_id)
            return {"status": "queued"}

        @router.post("/cancel", response_model=Dict[str, str])
        def cancel(request: JobActionRequest) -> Dict[str, str]:
            require_enabled()
            self._scheduler.cancel_job(request.job_id)
            return {"status": "canceled"}

        @router.post("/pause_all", response_model=Dict[str, str])
        def pause_all() -> Dict[str, str]:
            require_enabled()
            self._scheduler.pause_all()
            return {"paused": "true"}

        @router.post("/resume_all", response_model=Dict[str, str])
        def resume_all() -> Dict[str, str]:
            require_enabled()
            self._scheduler.resume_all()
            return {"paused": "false"}

        @router.get("/queue", response_model=QueueResponse)
        def queue(status: Optional[str] = None, kind: Optional[str] = None, limit: int = 100, offset: int = 0) -> QueueResponse:
            jobs = self._scheduler.list_jobs(limit=limit, offset=offset)
            if status:
                jobs = [job for job in jobs if job["status"] == status]
            if kind:
                jobs = [job for job in jobs if job["kind"] == kind]
            return QueueResponse(jobs=jobs, paused=(not self._enabled) or self._scheduler.is_paused())

        @router.get("/job", response_model=JobDetailResponse)
        def job(job_id: int) -> JobDetailResponse:
            job = self._scheduler.get_job(job_id)
            if not job:
                raise HTTPException(status_code=404, detail="Job not found")
            checkpoint = self._scheduler.job_checkpoint(job_id)
            events = self._scheduler.job_events(job_id)
            return JobDetailResponse(job=job, checkpoint=checkpoint, events=events)

        @router.get("/locks", response_model=LocksResponse)
        def locks() -> LocksResponse:
            return LocksResponse(locks=self._scheduler.locks())

        return router
