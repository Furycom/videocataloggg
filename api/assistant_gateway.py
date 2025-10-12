"""Gateway that manages access to the local assistant from the API."""
from __future__ import annotations

import logging
import threading
from dataclasses import asdict
from typing import Any, Dict, Optional

from assistant.config import AssistantSettings
from assistant.service import AssistantService
from gpu.capabilities import probe_nvml

from .db import DataAccess

LOGGER = logging.getLogger("videocatalog.api.assistant")

_REQUIRED_VRAM_BYTES = 8 * 1024 * 1024 * 1024


class AssistantGateway:
    """Lazy assistant service wrapper enforcing GPU requirements."""

    def __init__(self, data_access: DataAccess) -> None:
        self._data = data_access
        self._lock = threading.Lock()
        self._service: Optional[AssistantService] = None
        self._settings = AssistantSettings.from_settings(self._data.settings_payload)
        self._gpu_info = probe_nvml()
        self._gpu_ready = self._check_gpu_ready(self._gpu_info)
        self._status_message = self._resolve_status_message()

    @staticmethod
    def _check_gpu_ready(info: Dict[str, Any]) -> bool:
        if not info.get("has_nvidia"):
            return False
        try:
            vram = int(info.get("vram_bytes") or 0)
        except Exception:
            vram = 0
        return vram >= _REQUIRED_VRAM_BYTES

    def _resolve_status_message(self) -> str:
        if not self._gpu_ready:
            reason = self._gpu_info.get("error") or self._gpu_info.get("nv_error")
            if reason:
                return f"AI disabled (GPU required: {reason})"
            return "AI disabled (GPU required)"
        if not self._settings.enable:
            return "Assistant disabled in settings"
        return "Assistant ready"

    @property
    def enabled(self) -> bool:
        return self._settings.enable and self._gpu_ready

    def status(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "requested": bool(self._settings.enable),
            "gpu_ready": bool(self._gpu_ready),
            "enabled": bool(self.enabled),
            "disabled_by_gpu": bool(not self._gpu_ready),
            "message": self._status_message,
            "gpu": {
                "has_nvidia": bool(self._gpu_info.get("has_nvidia")),
                "name": self._gpu_info.get("name"),
                "vram_bytes": self._gpu_info.get("vram_bytes"),
                "error": self._gpu_info.get("error") or self._gpu_info.get("nv_error"),
            },
        }
        payload["tool_budget"] = self.tool_budget_snapshot()
        service = self._service
        if service is not None:
            try:
                payload["runtime"] = asdict(service.status())
            except Exception as exc:
                LOGGER.debug("Unable to fetch assistant runtime status: %s", exc)
        return payload

    def tool_budget_snapshot(self) -> Dict[str, int]:
        total = int(self._settings.tool_budget)
        remaining = 0
        if self._gpu_ready and self._settings.enable:
            remaining = total
        service = self._service
        if service is not None:
            try:
                status = service.status()
                total = int(getattr(status, "tool_budget", total))
                remaining = int(getattr(status, "remaining_budget", remaining))
            except Exception as exc:
                LOGGER.debug("Unable to read assistant tool budget: %s", exc)
        remaining = max(0, min(total, remaining))
        return {"total": max(0, total), "remaining": remaining}

    def ask_context(
        self,
        item_id: str,
        item_payload: Dict[str, Any],
        question: str,
        *,
        tool_budget: Optional[int] = None,
        use_rag: bool = True,
    ) -> Dict[str, Any]:
        if not self.enabled:
            raise RuntimeError(self._status_message)
        service = self._ensure_service()
        return service.ask_context(
            item_id,
            item_payload,
            question,
            tool_budget=tool_budget,
            use_rag=use_rag,
        )

    def shutdown(self) -> None:
        with self._lock:
            service = self._service
            self._service = None
        if service:
            try:
                service.shutdown()
            except Exception as exc:
                LOGGER.debug("Assistant shutdown raised: %s", exc)

    def _ensure_service(self) -> AssistantService:
        with self._lock:
            if self._service is None:
                LOGGER.info("Initialising assistant service")
                self._service = AssistantService(
                    self._data.settings_payload,
                    self._data.working_dir,
                    self._data.catalog_path,
                )
            return self._service

