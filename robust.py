"""Helpers for robust filesystem enumeration on huge trees and network shares."""

from __future__ import annotations

import errno
import os
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

_WINDOWS = sys.platform.startswith("win")

_WINDOWS_MAX_PATH = 260
_LONG_PATH_PREFIX = "\\\\?\\"
_UNC_PREFIX = "\\\\"
_LONG_UNC_PREFIX = "\\\\?\\UNC\\"

_TRANSIENT_ERRNOS = {
    errno.ESTALE,
    errno.ETIMEDOUT,
    errno.EIO,
    errno.ECONNRESET,
    errno.ECONNABORTED,
    errno.ENETRESET,
    errno.ENETDOWN,
    errno.ENETUNREACH,
}


class PathTooLongError(OSError):
    """Raised when a path exceeds platform limits and cannot be processed."""


@dataclass(slots=True)
class RobustSettings:
    batch_files: int = 1000
    batch_seconds: float = 2.0
    queue_max: int = 10_000
    skip_hidden: bool = False
    ignore: Sequence[str] = field(default_factory=list)
    follow_symlinks: bool = False
    long_paths: str = "auto"
    op_timeout_s: float = 30.0
    skip_globs: Sequence[str] = field(default_factory=tuple)

    def as_log_line(self) -> str:
        ignore = ",".join(self.ignore) if self.ignore else "—"
        globs = ",".join(self.skip_globs) if self.skip_globs else "—"
        return (
            "batch_files=%s, batch_seconds=%s, queue_max=%s, "
            "skip_hidden=%s, ignore=%s, skip_glob=%s, "
            "follow_symlinks=%s, long_paths=%s, op_timeout=%ss"
            % (
                int(self.batch_files),
                float(self.batch_seconds),
                int(self.queue_max),
                str(bool(self.skip_hidden)).lower(),
                ignore,
                globs,
                str(bool(self.follow_symlinks)).lower(),
                self.long_paths,
                int(self.op_timeout_s),
            )
        )


def merge_settings(raw: dict | None, overrides: dict | None) -> RobustSettings:
    data = dict(raw or {})
    override = dict(overrides or {})
    cfg = RobustSettings()
    for key in (
        "batch_files",
        "batch_seconds",
        "queue_max",
        "skip_hidden",
        "ignore",
        "follow_symlinks",
        "long_paths",
        "op_timeout_s",
    ):
        if key in data:
            setattr(cfg, key, data[key])
    if "skip_globs" in data:
        setattr(cfg, "skip_globs", tuple(data["skip_globs"]))
    for key, value in override.items():
        if hasattr(cfg, key):
            setattr(cfg, key, value)
    if isinstance(cfg.ignore, list):
        cfg.ignore = tuple(str(p).strip() for p in cfg.ignore if str(p).strip())
    if isinstance(cfg.skip_globs, list) or isinstance(cfg.skip_globs, tuple):
        cfg.skip_globs = tuple(str(p).strip() for p in cfg.skip_globs if str(p).strip())
    cfg.batch_files = max(1, int(cfg.batch_files))
    cfg.batch_seconds = max(0.5, float(cfg.batch_seconds))
    cfg.queue_max = max(100, int(cfg.queue_max))
    cfg.op_timeout_s = max(5.0, float(cfg.op_timeout_s))
    mode = str(cfg.long_paths or "auto").lower()
    if mode not in {"auto", "force", "off"}:
        mode = "auto"
    cfg.long_paths = mode
    cfg.follow_symlinks = bool(cfg.follow_symlinks)
    cfg.skip_hidden = bool(cfg.skip_hidden)
    return cfg


def normalize_path(path: str) -> str:
    return unicodedata.normalize("NFC", path)


def key_for_path(path: str, *, casefold: bool) -> str:
    norm = normalize_path(path)
    return norm.casefold() if casefold else norm


def _needs_long_prefix(path: str) -> bool:
    return len(path) >= (_WINDOWS_MAX_PATH - 12)


def to_fs_path(path: str, *, mode: str) -> str:
    if not _WINDOWS:
        return path
    if path.startswith(_LONG_PATH_PREFIX):
        return path
    norm = path.replace("/", "\\")
    if mode == "off":
        if len(norm) >= _WINDOWS_MAX_PATH:
            raise PathTooLongError(norm)
        return norm
    if mode == "force" or (mode == "auto" and _needs_long_prefix(norm)):
        if norm.startswith(_UNC_PREFIX):
            trimmed = norm.lstrip("\\")
            return f"{_LONG_UNC_PREFIX}{trimmed}"
        return f"{_LONG_PATH_PREFIX}{norm}"
    return norm


def from_fs_path(path: str) -> str:
    if not _WINDOWS:
        return path
    if path.startswith(_LONG_UNC_PREFIX):
        return f"\\{path[len(_LONG_UNC_PREFIX):]}"
    if path.startswith(_LONG_PATH_PREFIX):
        return path[len(_LONG_PATH_PREFIX):]
    return path


def is_hidden(entry: os.DirEntry[str], *, fs_path: str, display_path: str) -> bool:
    name = entry.name
    if name.startswith('.'):
        return True
    if not _WINDOWS:
        return False
    try:
        import ctypes

        GetFileAttributesW = ctypes.windll.kernel32.GetFileAttributesW
        GetFileAttributesW.restype = ctypes.c_uint32
        attrs = GetFileAttributesW(ctypes.c_wchar_p(fs_path))
        if attrs == 0xFFFFFFFF:
            return False
        FILE_ATTRIBUTE_HIDDEN = 0x2
        FILE_ATTRIBUTE_SYSTEM = 0x4
        return bool(attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM))
    except Exception:
        return False


def is_transient(exc: OSError) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    err_no = getattr(exc, "errno", None)
    if err_no in _TRANSIENT_ERRNOS:
        return True
    win_err = getattr(exc, "winerror", None)
    if win_err in {121, 64, 65, 67, 71}:  # network timeouts, net name deleted, etc.
        return True
    return False


def should_ignore(path: str, *, patterns: Sequence[str]) -> bool:
    if not patterns:
        return False
    from fnmatch import fnmatch

    for pattern in patterns:
        if fnmatch(os.path.basename(path), pattern) or fnmatch(path, pattern):
            return True
    return False


def clamp_batch_seconds(base: float, profile: str) -> float:
    if profile == "NETWORK":
        return max(1.0, min(base, 3.0))
    return base


class CancellationToken:
    def __init__(self) -> None:
        self._evt = threading.Event()

    def set(self) -> None:
        self._evt.set()

    def is_set(self) -> bool:
        return self._evt.is_set()

    def wait(self, timeout: float) -> bool:
        return self._evt.wait(timeout)


__all__ = [
    "RobustSettings",
    "merge_settings",
    "normalize_path",
    "key_for_path",
    "to_fs_path",
    "from_fs_path",
    "is_hidden",
    "is_transient",
    "should_ignore",
    "PathTooLongError",
    "clamp_batch_seconds",
    "CancellationToken",
]
