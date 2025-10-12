"""Job registry describing orchestrator workloads."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional


RunnerFn = Callable[["RunnerContext", Dict[str, Any], Dict[str, Any]], None]
EstimatorFn = Callable[[Dict[str, Any]], int]
RuntimeEstimatorFn = Callable[[Dict[str, Any]], int]
PreconditionFn = Callable[["RunnerContext", Dict[str, Any]], Optional[str]]


@dataclass(slots=True)
class RunnerContext:
    working_dir: Path
    settings: Dict[str, Any]


@dataclass(slots=True)
class JobSpec:
    kind: str
    resource: str
    runner: RunnerFn
    estimate_vram: EstimatorFn
    estimate_runtime: RuntimeEstimatorFn
    preconditions: Optional[PreconditionFn] = None


class JobRegistry:
    """Authoritative registry for orchestrator job kinds."""

    def __init__(self) -> None:
        self._jobs: Dict[str, JobSpec] = {}

    def register(self, spec: JobSpec) -> None:
        if spec.kind in self._jobs:
            raise ValueError(f"Job kind already registered: {spec.kind}")
        self._jobs[spec.kind] = spec

    def get(self, kind: str) -> JobSpec:
        try:
            return self._jobs[kind]
        except KeyError as exc:  # pragma: no cover - defensive
            raise KeyError(f"Unknown job kind: {kind}") from exc

    def all_specs(self) -> Dict[str, JobSpec]:
        return dict(self._jobs)


# ----------------------------------------------------------------------
# Default runner implementations
# ----------------------------------------------------------------------

def _ensure_vectors_index(ctx: RunnerContext, payload: Dict[str, Any], checkpoint: Dict[str, Any]) -> None:
    from assistant.config import AssistantSettings
    from assistant.rag import VectorIndex
    from core.paths import get_catalog_db_path

    assistant_settings = AssistantSettings.from_settings(ctx.settings)
    if not assistant_settings.rag.enable:
        return
    index = VectorIndex(assistant_settings, get_catalog_db_path(ctx.working_dir), ctx.working_dir)
    index.refresh()


def _assistant_warmup(ctx: RunnerContext, payload: Dict[str, Any], checkpoint: Dict[str, Any]) -> None:
    from assistant.config import AssistantSettings
    from assistant.runtime import RuntimeManager

    assistant_settings = AssistantSettings.from_settings(ctx.settings)
    manager = RuntimeManager(assistant_settings, ctx.working_dir)
    manager.ensure_runtime()


def _noop_light_job(ctx: RunnerContext, payload: Dict[str, Any], checkpoint: Dict[str, Any]) -> None:
    # Placeholder for lightweight housekeeping jobs such as quality checks or API guard warmups.
    return None


def _tests_gate_precondition(ctx: RunnerContext, payload: Dict[str, Any]) -> Optional[str]:
    try:
        from tests.api import orchestrator_gate_reason
    except Exception:  # pragma: no cover - optional dependency
        return None
    return orchestrator_gate_reason(ctx.settings, working_dir=ctx.working_dir)


def build_default_registry() -> JobRegistry:
    registry = JobRegistry()

    registry.register(
        JobSpec(
            kind="vectors_refresh",
            resource="heavy_ai_gpu",
            runner=_ensure_vectors_index,
            estimate_vram=lambda payload: int(payload.get("vram_hint_mb", 4096)),
            estimate_runtime=lambda payload: int(payload.get("estimate_s", 300)),
            preconditions=_tests_gate_precondition,
        )
    )

    registry.register(
        JobSpec(
            kind="assistant_warmup",
            resource="heavy_ai_gpu",
            runner=_assistant_warmup,
            estimate_vram=lambda payload: 2048,
            estimate_runtime=lambda payload: 60,
            preconditions=_tests_gate_precondition,
        )
    )

    registry.register(
        JobSpec(
            kind="textverify",
            resource="heavy_ai_gpu",
            runner=_noop_light_job,
            estimate_vram=lambda payload: int(payload.get("vram_hint_mb", 6144)),
            estimate_runtime=lambda payload: int(payload.get("estimate_s", 600)),
            preconditions=_tests_gate_precondition,
        )
    )

    registry.register(
        JobSpec(
            kind="visualreview",
            resource="heavy_ai_gpu",
            runner=_noop_light_job,
            estimate_vram=lambda payload: int(payload.get("vram_hint_mb", 4096)),
            estimate_runtime=lambda payload: int(payload.get("estimate_s", 900)),
            preconditions=_tests_gate_precondition,
        )
    )

    registry.register(
        JobSpec(
            kind="quality",
            resource="light_cpu",
            runner=_noop_light_job,
            estimate_vram=lambda payload: 0,
            estimate_runtime=lambda payload: int(payload.get("estimate_s", 120)),
        )
    )

    registry.register(
        JobSpec(
            kind="apiguard_warmup",
            resource="io_light",
            runner=_noop_light_job,
            estimate_vram=lambda payload: 0,
            estimate_runtime=lambda payload: int(payload.get("estimate_s", 45)),
        )
    )

    return registry
