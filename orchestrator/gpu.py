"""GPU probing and leasing utilities."""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from .logs import OrchestratorLogger


@dataclass(slots=True)
class GPUInfo:
    present: bool
    name: Optional[str]
    vram_total_mb: int
    vram_free_mb: int
    driver_version: Optional[str]
    cuda_ok: bool
    error: Optional[str] = None


@dataclass(slots=True)
class GPUPreflightResult:
    ok: bool
    info: GPUInfo
    reason: Optional[str] = None


class GPUManager:
    """Probe GPU availability and manage exclusive leases."""

    def __init__(self, *, logger: OrchestratorLogger, safety_margin_mb: int, lease_ttl_s: int) -> None:
        self._logger = logger
        self._safety_margin_mb = max(0, int(safety_margin_mb))
        self._lease_ttl = max(30, int(lease_ttl_s))

    @property
    def safety_margin_mb(self) -> int:
        return self._safety_margin_mb

    # ------------------------------------------------------------------
    def ensure_lock_table(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS resource_locks (
                name TEXT PRIMARY KEY,
                owner TEXT,
                lease_utc TEXT,
                ttl_sec INTEGER NOT NULL
            )
            """
        )

    # ------------------------------------------------------------------
    def probe(self) -> GPUInfo:
        try:
            import pynvml  # type: ignore

            pynvml.nvmlInit()
            count = pynvml.nvmlDeviceGetCount()
            if count == 0:
                return GPUInfo(False, None, 0, 0, None, False, error="No NVIDIA GPU detected")
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            name = pynvml.nvmlDeviceGetName(handle)
            driver = pynvml.nvmlSystemGetDriverVersion()
            try:
                cuda_ok = bool(pynvml.nvmlDeviceGetCudaComputeCapability(handle))
            except Exception:
                cuda_ok = True
            return GPUInfo(
                present=True,
                name=name.decode("utf-8") if isinstance(name, bytes) else str(name),
                vram_total_mb=int(mem.total / 1024 / 1024),
                vram_free_mb=int(mem.free / 1024 / 1024),
                driver_version=driver.decode("utf-8") if isinstance(driver, bytes) else str(driver),
                cuda_ok=cuda_ok,
            )
        except Exception:
            return self._probe_via_nvidia_smi()

    def _probe_via_nvidia_smi(self) -> GPUInfo:
        if not shutil.which("nvidia-smi"):
            return GPUInfo(False, None, 0, 0, None, False, error="nvidia-smi not available")
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=name,memory.total,memory.free,driver_version",
                    "--format=csv,noheader",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            line = result.stdout.strip().splitlines()[0]
            parts = [part.strip() for part in line.split(",")]
            name = parts[0] if parts else "NVIDIA"
            total_mb = int(parts[1].split()[0]) if len(parts) > 1 else 0
            free_mb = int(parts[2].split()[0]) if len(parts) > 2 else 0
            driver = parts[3] if len(parts) > 3 else None
            return GPUInfo(True, name, total_mb, free_mb, driver, cuda_ok=True)
        except Exception as exc:
            return GPUInfo(False, None, 0, 0, None, False, error=str(exc))

    # ------------------------------------------------------------------
    def preflight(self, required_vram_mb: int) -> GPUPreflightResult:
        info = self.probe()
        if not info.present:
            return GPUPreflightResult(False, info, reason=info.error or "GPU not detected")
        if not info.cuda_ok:
            return GPUPreflightResult(False, info, reason="CUDA not available")
        if info.vram_total_mb < 8192:
            return GPUPreflightResult(False, info, reason="GPU VRAM below 8GB requirement")
        required = int(required_vram_mb) + self._safety_margin_mb
        if info.vram_free_mb < required:
            return GPUPreflightResult(
                False,
                info,
                reason=f"Insufficient free VRAM: required {required} MB, have {info.vram_free_mb} MB",
            )
        return GPUPreflightResult(True, info)

    # ------------------------------------------------------------------
    def acquire_exclusive(self, conn: sqlite3.Connection, owner: str) -> bool:
        self.ensure_lock_table(conn)
        now = datetime.now(timezone.utc)
        row = conn.execute("SELECT owner, lease_utc, ttl_sec FROM resource_locks WHERE name=?", ("gpu_exclusive",)).fetchone()
        if row:
            lease_owner, lease_utc, ttl = row
            lease_dt = datetime.fromisoformat(lease_utc) if lease_utc else None
            ttl = int(ttl or self._lease_ttl)
            if lease_dt and lease_dt + timedelta(seconds=ttl) > now:
                return lease_owner == owner
        conn.execute(
            """
            INSERT INTO resource_locks (name, owner, lease_utc, ttl_sec)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET owner=excluded.owner, lease_utc=excluded.lease_utc, ttl_sec=excluded.ttl_sec
            """,
            ("gpu_exclusive", owner, now.isoformat(), self._lease_ttl),
        )
        conn.commit()
        self._logger.log_gpu(1200, True, owner=owner, ttl=self._lease_ttl)
        return True

    def refresh_lease(self, conn: sqlite3.Connection, owner: str) -> None:
        self.ensure_lock_table(conn)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE resource_locks SET lease_utc=?, ttl_sec=? WHERE name=? AND owner=?",
            (now, self._lease_ttl, "gpu_exclusive", owner),
        )
        conn.commit()

    def release(self, conn: sqlite3.Connection, owner: str) -> None:
        self.ensure_lock_table(conn)
        conn.execute("DELETE FROM resource_locks WHERE name=? AND owner=?", ("gpu_exclusive", owner))
        conn.commit()
        self._logger.log_gpu(1201, True, owner=owner, action="release")

    def current_lock(self, conn: sqlite3.Connection) -> Optional[Dict[str, str]]:
        self.ensure_lock_table(conn)
        row = conn.execute("SELECT owner, lease_utc, ttl_sec FROM resource_locks WHERE name=?", ("gpu_exclusive",)).fetchone()
        if not row:
            return None
        owner, lease_utc, ttl = row
        return {"owner": owner or "", "lease_utc": lease_utc or "", "ttl_sec": int(ttl or self._lease_ttl)}
