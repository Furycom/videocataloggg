from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

import requests

from .config import AssistantSettings

LOGGER = logging.getLogger("videocatalog.assistant.runtime")

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
LLAMA_CPP_HOST = "http://127.0.0.1:11435"


@dataclass(slots=True)
class RuntimeProbe:
    name: str
    available: bool
    details: Dict[str, object]


@dataclass(slots=True)
class RuntimeHandle:
    name: str
    base_url: str
    process: Optional[subprocess.Popen]
    uses_gpu: bool


class RuntimeManager:
    """Detect and manage local LLM runtimes with graceful fallback."""

    def __init__(self, settings: AssistantSettings, working_dir: Path) -> None:
        self.settings = settings
        self.working_dir = working_dir
        self._lock = threading.Lock()
        self._runtime: Optional[RuntimeHandle] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def ensure_runtime(self) -> RuntimeHandle:
        with self._lock:
            if self._runtime is not None:
                return self._runtime
            runtime = (
                self._probe_ollama()
                if self.settings.runtime in ("auto", "ollama")
                else None
            )
            if runtime is None and self.settings.runtime in ("auto", "llama_cpp"):
                runtime = self._start_llama_cpp()
            if runtime is None and self.settings.runtime in ("auto", "mlc"):
                runtime = self._start_mlc_runtime()
            if runtime is None:
                raise RuntimeError("No local LLM runtime available. Install Ollama or enable llama.cpp.")
            self._runtime = runtime
            return runtime

    def current_runtime(self) -> Optional[RuntimeHandle]:
        with self._lock:
            return self._runtime

    # ------------------------------------------------------------------
    # Runtime detection
    # ------------------------------------------------------------------
    def _probe_ollama(self) -> Optional[RuntimeHandle]:
        try:
            response = requests.get(f"{OLLAMA_HOST}/api/version", timeout=2)
            response.raise_for_status()
        except Exception as exc:
            LOGGER.debug("Ollama probe failed: %s", exc)
            return None
        try:
            system = requests.get(f"{OLLAMA_HOST}/api/ps", timeout=2).json()
        except Exception:
            system = {}
        gpu_present = False
        adapters = system.get("devices") if isinstance(system, dict) else None
        if isinstance(adapters, list):
            for adapter in adapters:
                if isinstance(adapter, dict) and adapter.get("accelerator"):
                    gpu_present = True
                    break
        LOGGER.info("Assistant runtime: using Ollama at %s (GPU=%s)", OLLAMA_HOST, gpu_present)
        return RuntimeHandle(name="ollama", base_url=OLLAMA_HOST, process=None, uses_gpu=gpu_present)

    def _start_llama_cpp(self) -> Optional[RuntimeHandle]:
        if not self._check_llama_cpp_import():
            return None
        existing = self._probe_openai_server(LLAMA_CPP_HOST)
        if existing:
            LOGGER.info("Assistant runtime: using existing llama.cpp server at %s", LLAMA_CPP_HOST)
            return RuntimeHandle(name="llama_cpp", base_url=LLAMA_CPP_HOST, process=None, uses_gpu=existing.uses_gpu)

        LOGGER.info("Assistant runtime: launching llama.cpp server at %s", LLAMA_CPP_HOST)
        env = os.environ.copy()
        gpu_env = self._detect_cuda_env()
        if gpu_env:
            env.update(gpu_env)
        command = [sys.executable, "-m", "llama_cpp.server", "--host", "127.0.0.1", "--port", "11435", "--chat_format", "functionary"]
        process = subprocess.Popen(command, cwd=self.working_dir, env=env)
        ready = self._wait_for_http(LLAMA_CPP_HOST, timeout=20)
        if not ready:
            LOGGER.error("llama.cpp server did not become ready")
            process.terminate()
            return None
        uses_gpu = bool(gpu_env)
        return RuntimeHandle(name="llama_cpp", base_url=LLAMA_CPP_HOST, process=process, uses_gpu=uses_gpu)

    def _start_mlc_runtime(self) -> Optional[RuntimeHandle]:
        # Placeholder for future DirectML/Vulkan runtime integration.
        LOGGER.warning("MLC runtime not yet implemented; falling back to CPU-only llama.cpp")
        if self.settings.runtime == "mlc":
            return self._start_llama_cpp()
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _wait_for_http(base_url: str, timeout: int = 10) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                response = requests.get(base_url, timeout=1)
                if response.status_code < 500:
                    return True
            except Exception:
                time.sleep(0.4)
        return False

    @staticmethod
    def _check_llama_cpp_import() -> bool:
        try:
            import llama_cpp  # noqa: F401
        except Exception as exc:
            LOGGER.warning("llama-cpp-python not available: %s", exc)
            return False
        return True

    @staticmethod
    def _probe_openai_server(base_url: str) -> Optional[RuntimeHandle]:
        try:
            response = requests.get(f"{base_url}/v1/models", timeout=2)
            response.raise_for_status()
        except Exception:
            return None
        try:
            payload = response.json()
        except Exception:
            payload = {}
        uses_gpu = False
        data = payload.get("data")
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and "gpu" in json.dumps(item).lower():
                    uses_gpu = True
                    break
        return RuntimeHandle(name="llama_cpp", base_url=base_url, process=None, uses_gpu=uses_gpu)

    @staticmethod
    def _detect_cuda_env() -> Dict[str, str]:
        """Attempt to detect CUDA support and surface best provider env vars."""

        try:
            from gpu.capabilities import probe_gpu  # lazy import to avoid cycles
        except Exception:
            return {}
        caps = probe_gpu()
        if caps.get("cuda_available"):
            return {"LLAMA_CPP_USE_CUBLAS": "1"}
        if caps.get("dml_available"):
            return {"LLAMA_CPP_USE_DIRECTML": "1"}
        return {}

    def shutdown(self) -> None:
        with self._lock:
            runtime = self._runtime
            self._runtime = None
        if runtime and runtime.process:
            runtime.process.terminate()
            try:
                runtime.process.wait(timeout=5)
            except Exception:
                runtime.process.kill()
