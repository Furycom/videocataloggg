"""Performance profile detection and adaptive defaults for VideoCatalog scans."""

from __future__ import annotations

import contextlib
import os
import platform
import random
import statistics
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Literal, Optional, Tuple


ProfileName = Literal["NETWORK", "USB", "HDD", "SSD"]
ResolvedProfile = Literal["AUTO", "NETWORK", "USB", "HDD", "SSD"]


@dataclass(frozen=True)
class PerformanceConfig:
    profile: ProfileName
    worker_threads: int
    hash_chunk_bytes: int
    ffmpeg_parallel: int
    gentle_io: bool
    source: Literal["auto", "settings", "cli"]
    auto_profile: ProfileName

    def as_dict(self) -> Dict[str, Any]:
        return {
            "profile": self.profile,
            "worker_threads": int(self.worker_threads),
            "hash_chunk_bytes": int(self.hash_chunk_bytes),
            "ffmpeg_parallel": int(self.ffmpeg_parallel),
            "gentle_io": bool(self.gentle_io),
            "source": self.source,
            "auto_profile": self.auto_profile,
        }

    def label(self) -> str:
        chunk_mb = self.hash_chunk_bytes / 1_048_576
        if chunk_mb >= 1:
            chunk_str = f"{chunk_mb:.0f}MB"
        else:
            chunk_str = f"{self.hash_chunk_bytes // 1024}KB"
        return (
            f"{self.profile} (threads={self.worker_threads}, chunk={chunk_str}, "
            f"ffmpeg={self.ffmpeg_parallel})"
        )


_PROFILE_CACHE: Dict[str, ProfileName] = {}


def _norm_mount(path: str) -> str:
    normalized = os.path.abspath(path)
    return os.path.normcase(normalized)


def _windows_drive_type(path: str) -> Optional[int]:
    if platform.system().lower() != "windows":
        return None
    try:  # pragma: no cover - platform specific
        import ctypes

        drive = Path(path).anchor or path
        if len(drive) >= 2 and drive[1] == ":":
            drive = drive[:2] + "\\"
        GetDriveTypeW = ctypes.windll.kernel32.GetDriveTypeW  # type: ignore[attr-defined]
        GetDriveTypeW.restype = ctypes.c_uint
        return int(GetDriveTypeW(ctypes.c_wchar_p(drive)))
    except Exception:
        return None


def _is_network_path(path: str) -> bool:
    sys_name = platform.system().lower()
    if sys_name == "windows":
        if path.startswith("\\\\"):
            return True
        drive_type = _windows_drive_type(path)
        if drive_type == 4:  # DRIVE_REMOTE
            return True
        return False

    if path.startswith("//") or path.startswith("\\\\"):
        return True

    mount_point = Path(path)
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="ignore") as handle:
            rows = handle.readlines()
    except OSError:
        rows = []
    candidates: list[Tuple[str, str]] = []
    for line in rows:
        parts = line.split()
        if len(parts) < 3:
            continue
        mount = parts[1]
        fstype = parts[2].lower()
        candidates.append((mount, fstype))
    candidates.sort(key=lambda item: len(item[0]), reverse=True)
    for mount, fstype in candidates:
        try:
            if not str(mount_point).startswith(mount):
                continue
        except Exception:
            continue
        if fstype in {"cifs", "smbfs", "nfs", "nfs4", "smb3"}:
            return True
        if mount.startswith("//"):
            return True
    return False


def _sample_files(base: Path, limit: int = 40) -> Iterable[Path]:
    stack = [base]
    seen = 0
    while stack and seen < limit:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                        continue
                    if entry.is_file(follow_symlinks=False):
                        seen += 1
                        yield Path(entry.path)
                        if seen >= limit:
                            break
        except OSError:
            continue


def _probe_temp_file(directory: Path, size_bytes: int = 512 * 1024) -> Optional[Path]:
    try:
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as handle:
            chunk = os.urandom(64 * 1024)
            remaining = size_bytes
            while remaining > 0:
                to_write = min(len(chunk), remaining)
                handle.write(chunk[:to_write])
                remaining -= to_write
        return Path(handle.name)
    except Exception:
        return None


def _random_read_probe(file_path: Path, *, attempts: int = 5, chunk: int = 256 * 1024) -> Tuple[list[float], int]:
    latencies: list[float] = []
    total_bytes = 0
    try:
        size = file_path.stat().st_size
    except OSError:
        return latencies, total_bytes
    if size <= 0:
        return latencies, total_bytes
    max_offset = max(0, size - chunk)
    try:
        with open(file_path, "rb") as handle:
            for _ in range(attempts):
                offset = random.randint(0, max_offset) if max_offset > 0 else 0
                handle.seek(offset)
                start = time.perf_counter()
                data = handle.read(chunk)
                elapsed = time.perf_counter() - start
                if not data:
                    break
                latencies.append(max(0.0, elapsed))
                total_bytes += len(data)
    except OSError:
        return latencies, total_bytes
    return latencies, total_bytes


def _fallback_latency(base: Path) -> list[float]:
    start = time.perf_counter()
    count = 0
    try:
        with os.scandir(base) as entries:
            for count, _ in enumerate(entries, start=1):
                if count >= 100:
                    break
    except OSError:
        pass
    elapsed = time.perf_counter() - start
    if count <= 0:
        return []
    return [elapsed / max(1, count)]


def _probe_filesystem(base: Path) -> Tuple[float, float]:
    latencies: list[float] = []
    total_bytes = 0
    sample_file: Optional[Path] = None
    for candidate in _sample_files(base):
        try:
            if candidate.stat().st_size >= 128 * 1024:
                sample_file = candidate
                break
            sample_file = candidate
        except OSError:
            continue
    temp_file = None
    if sample_file is None:
        temp_file = _probe_temp_file(base)
        sample_file = temp_file
    if sample_file is not None:
        latencies, total_bytes = _random_read_probe(sample_file)
    if temp_file is not None:
        with contextlib.suppress(Exception):
            temp_file.unlink()
    if not latencies:
        latencies = _fallback_latency(base)
    if not latencies:
        return 0.0, 0.0
    median_latency_ms = statistics.median(latencies) * 1000.0
    total_latency = sum(latencies)
    throughput_mb_s = 0.0
    if total_latency > 0 and total_bytes > 0:
        throughput_mb_s = (total_bytes / total_latency) / 1_000_000.0
    return median_latency_ms, throughput_mb_s


def detect_profile(mount_path: str) -> ProfileName:
    key = _norm_mount(mount_path)
    cached = _PROFILE_CACHE.get(key)
    if cached:
        return cached

    path_obj = Path(mount_path)
    profile: ProfileName
    if _is_network_path(str(path_obj)):
        profile = "NETWORK"
    else:
        sys_name = platform.system().lower()
        if sys_name == "windows":
            drive_type = _windows_drive_type(str(path_obj))
            if drive_type == 2:  # DRIVE_REMOVABLE
                profile = "USB"
            else:
                latency_ms, throughput = _probe_filesystem(path_obj)
                if latency_ms and throughput:
                    if latency_ms < 1.0 and throughput > 300.0:
                        profile = "SSD"
                    elif throughput >= 80.0:
                        profile = "HDD"
                    else:
                        profile = "USB"
                else:
                    profile = "HDD"
        else:
            latency_ms, throughput = _probe_filesystem(path_obj)
            if latency_ms and throughput:
                if latency_ms < 1.0 and throughput > 300.0:
                    profile = "SSD"
                elif throughput >= 80.0:
                    profile = "HDD"
                else:
                    profile = "USB"
            else:
                profile = "HDD"

    _PROFILE_CACHE[key] = profile
    return profile


DEFAULTS: Dict[ProfileName, Dict[str, Callable[[int], int] | Any]] = {
    "SSD": {
        "worker_threads": lambda cpu: min(32, max(8, cpu * 2)),
        "hash_chunk_bytes": 1_048_576,
        "ffmpeg_parallel": lambda cpu: max(1, min(4, cpu)),
        "gentle_io": False,
    },
    "HDD": {
        "worker_threads": lambda cpu: min(16, max(6, cpu)),
        "hash_chunk_bytes": 524_288,
        "ffmpeg_parallel": lambda cpu: max(1, min(2, cpu)),
        "gentle_io": False,
    },
    "USB": {
        "worker_threads": lambda cpu: min(8, max(4, cpu)),
        "hash_chunk_bytes": 262_144,
        "ffmpeg_parallel": 1,
        "gentle_io": True,
    },
    "NETWORK": {
        "worker_threads": lambda cpu: 6,
        "hash_chunk_bytes": 262_144,
        "ffmpeg_parallel": 1,
        "gentle_io": True,
    },
}


def _coerce_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lower = value.strip().lower()
        if lower in {"1", "true", "yes", "on"}:
            return True
        if lower in {"0", "false", "no", "off"}:
            return False
    return None


def _choice_from(value: Any) -> Optional[ProfileName | Literal["AUTO"]]:
    if value is None:
        return None
    if isinstance(value, str):
        upper = value.strip().upper()
        if upper in {"AUTO", "SSD", "HDD", "USB", "NETWORK"}:
            return upper  # type: ignore[return-value]
    return None


def resolve_performance_config(
    mount_path: str,
    *,
    settings: Optional[Dict[str, Any]] = None,
    cli_overrides: Optional[Dict[str, Any]] = None,
    cpu_count: Optional[int] = None,
) -> PerformanceConfig:
    settings = settings or {}
    cli_overrides = cli_overrides or {}

    auto_profile = detect_profile(mount_path)
    cpu = cpu_count if cpu_count is not None else (os.cpu_count() or 4)

    settings_block = settings.get("performance") if isinstance(settings, dict) else None
    if not isinstance(settings_block, dict):
        settings_block = {}

    cli_profile = _choice_from(cli_overrides.get("profile"))
    settings_profile = _choice_from(settings_block.get("profile"))
    selected_profile: ProfileName
    source: Literal["auto", "settings", "cli"]
    if cli_profile and cli_profile != "AUTO":
        selected_profile = cli_profile  # type: ignore[assignment]
        source = "cli"
    elif settings_profile and settings_profile != "AUTO":
        selected_profile = settings_profile  # type: ignore[assignment]
        source = "settings"
    else:
        selected_profile = auto_profile
        source = "auto"

    defaults = DEFAULTS[selected_profile]

    cli_threads = _coerce_int(cli_overrides.get("worker_threads"))
    settings_threads = _coerce_int(settings_block.get("worker_threads"))
    if cli_threads is not None and cli_threads > 0:
        worker_threads = cli_threads
    elif settings_threads is not None and settings_threads > 0:
        worker_threads = settings_threads
    else:
        default_threads = defaults["worker_threads"](cpu) if callable(defaults["worker_threads"]) else defaults["worker_threads"]
        worker_threads = int(default_threads)

    cli_chunk = _coerce_int(cli_overrides.get("hash_chunk_bytes")) or _coerce_int(cli_overrides.get("chunk"))
    settings_chunk = _coerce_int(settings_block.get("hash_chunk_bytes"))
    if cli_chunk is not None and cli_chunk > 0:
        hash_chunk_bytes = cli_chunk
    elif settings_chunk is not None and settings_chunk > 0:
        hash_chunk_bytes = settings_chunk
    else:
        hash_chunk_bytes = int(defaults["hash_chunk_bytes"])

    cli_ffmpeg = _coerce_int(cli_overrides.get("ffmpeg_parallel"))
    settings_ffmpeg = _coerce_int(settings_block.get("ffmpeg_parallel"))
    if cli_ffmpeg is not None and cli_ffmpeg > 0:
        ffmpeg_parallel = cli_ffmpeg
    elif settings_ffmpeg is not None and settings_ffmpeg > 0:
        ffmpeg_parallel = settings_ffmpeg
    else:
        default_ffmpeg = defaults["ffmpeg_parallel"]
        ffmpeg_parallel = (
            default_ffmpeg(cpu) if callable(default_ffmpeg) else int(default_ffmpeg)
        )

    default_gentle = bool(defaults.get("gentle_io"))
    cli_gentle = cli_overrides.get("gentle_io")
    settings_gentle = settings_block.get("gentle_io")
    gentle_flag = _coerce_bool(cli_gentle)
    if gentle_flag is None:
        gentle_flag = _coerce_bool(settings_gentle)
    if gentle_flag is None:
        gentle_flag = default_gentle or (selected_profile == "NETWORK")

    worker_threads = max(1, min(64, worker_threads))
    ffmpeg_parallel = max(1, min(worker_threads, ffmpeg_parallel))
    hash_chunk_bytes = max(64 * 1024, hash_chunk_bytes)

    return PerformanceConfig(
        profile=selected_profile,
        worker_threads=worker_threads,
        hash_chunk_bytes=hash_chunk_bytes,
        ffmpeg_parallel=ffmpeg_parallel,
        gentle_io=bool(gentle_flag),
        source=source,
        auto_profile=auto_profile,
    )


class RateController:
    def __init__(
        self,
        *,
        enabled: bool,
        worker_threads: int,
        base_sleep_range: Optional[Tuple[float, float]] = None,
        latency_threshold: float = 0.04,
    ) -> None:
        self.enabled = enabled
        self.worker_threads = max(1, worker_threads)
        self.base_sleep_range = base_sleep_range or (0.0, 0.0)
        self.latency_threshold = max(0.0, latency_threshold)
        self._latencies: list[float] = []
        self._backoff = 0.0
        self._last_latency_ts = 0.0

    def _average_latency(self) -> float:
        if not self._latencies:
            return 0.0
        return sum(self._latencies) / len(self._latencies)

    def note_io(self, seconds: float) -> None:
        if not self.enabled:
            return
        seconds = max(0.0, float(seconds))
        self._latencies.append(seconds)
        if len(self._latencies) > 50:
            self._latencies = self._latencies[-50:]
        self._last_latency_ts = time.monotonic()

    def note_error(self) -> None:
        if not self.enabled:
            return
        self._backoff = min(0.05, max(self._backoff, 0.02))

    def note_success(self) -> None:
        if not self.enabled:
            return
        self._backoff = max(0.0, self._backoff * 0.6)

    def before_task(self, backlog: int) -> float:
        if not self.enabled:
            return 0.0
        base = 0.0
        if self.base_sleep_range[1] > 0:
            base = random.uniform(*self.base_sleep_range)
        avg_latency = self._average_latency()
        backlog_limit = self.worker_threads * 2
        if backlog > backlog_limit or avg_latency > self.latency_threshold:
            self._backoff = min(0.05, self._backoff * 1.5 + 0.005)
        else:
            since_last = time.monotonic() - self._last_latency_ts if self._last_latency_ts else 0.0
            if since_last > 5:
                self._latencies.clear()
            self._backoff = max(0.0, self._backoff * 0.5 - 0.002)
        return base + self._backoff


def enumerate_sleep_range(profile: ProfileName, gentle: bool) -> Optional[Tuple[float, float]]:
    if not gentle:
        if profile == "NETWORK":
            return (0.002, 0.005)
        return None
    if profile == "NETWORK":
        return (0.003, 0.006)
    if profile == "USB":
        return (0.0015, 0.003)
    return None

