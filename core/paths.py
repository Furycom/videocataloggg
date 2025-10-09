from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

__all__ = [
    "ensure_working_dir_structure",
    "get_catalog_db_path",
    "get_data_dir",
    "get_default_settings_paths",
    "get_drive_types_path",
    "get_exports_dir",
    "get_logs_dir",
    "get_scans_dir",
    "get_shard_db_path",
    "get_shards_dir",
    "is_unc",
    "resolve_working_dir",
    "safe_label",
    "to_long_path",
]

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_IS_WINDOWS = os.name == "nt"
_WINDOWS_MAX_PATH = 260
_LONG_PATH_PREFIX = "\\\\?\\"
_UNC_PREFIX = "\\\\"
_LONG_UNC_PREFIX = "\\\\?\\UNC\\"


def is_unc(path: str | os.PathLike[str]) -> bool:
    """Return True if *path* points to a UNC network location."""

    text = str(path)
    if text.startswith(_LONG_UNC_PREFIX):
        return True
    if text.startswith(_LONG_PATH_PREFIX):
        return text[len(_LONG_PATH_PREFIX) :].startswith(_UNC_PREFIX)
    return text.startswith(_UNC_PREFIX)


def _needs_long_prefix(path: str) -> bool:
    return len(path) >= (_WINDOWS_MAX_PATH - 12)


def to_long_path(path: str | os.PathLike[str]) -> str:
    """Return a version of *path* that is safe for Windows long-path APIs."""

    text = str(path)
    if not _IS_WINDOWS:
        return text
    normalized = text.replace("/", "\\")
    if normalized.startswith(_LONG_PATH_PREFIX):
        return normalized
    if normalized.startswith(_UNC_PREFIX):
        trimmed = normalized.lstrip("\\")
        return f"{_LONG_UNC_PREFIX}{trimmed}"
    if _needs_long_prefix(normalized):
        return f"{_LONG_PATH_PREFIX}{normalized}"
    return normalized


def _expand_path(value: str) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(value))
    return Path(expanded).resolve()


def _ensure_writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except Exception:
        return False
    test_file = path / f".write_test_{os.getpid()}"
    try:
        with open(test_file, "w", encoding="utf-8") as handle:
            handle.write("ok")
        test_file.unlink(missing_ok=True)
        return True
    except Exception:
        try:
            if test_file.exists():
                test_file.unlink()
        except Exception:  # pragma: no cover - defensive cleanup
            pass
        return False


def _prepare_working_dir(candidate: Path) -> Optional[Path]:
    if not _ensure_writable_dir(candidate):
        return None
    try:
        data_dir = candidate / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        return candidate
    except Exception:
        return None


def _read_settings(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        return None
    except Exception:
        return None
    return None


def _program_data_dir() -> Optional[Path]:
    program_data = os.environ.get("ProgramData")
    if not program_data:
        return None
    try:
        return _expand_path(program_data)
    except Exception:
        return None


def _local_appdata_dir() -> Path:
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        try:
            return _expand_path(local_appdata)
        except Exception:
            pass
    return Path.home() / ".videocatalog"


def resolve_working_dir() -> Path:
    """Resolve the VideoCatalog working directory, creating it if required."""

    env_home = os.environ.get("VIDEOCATALOG_HOME")
    if env_home:
        try:
            env_path = _expand_path(env_home)
        except Exception:
            env_path = None
        if env_path:
            prepared = _prepare_working_dir(env_path)
            if prepared is not None:
                return prepared

    program_data = _program_data_dir()

    legacy_settings_paths = []
    if program_data is not None:
        legacy_settings_paths.append(program_data / "VideoCatalog" / "settings.json")
    legacy_settings_paths.append(_PROJECT_ROOT / "settings.json")

    for legacy_path in legacy_settings_paths:
        data = _read_settings(legacy_path)
        if not data:
            continue
        working_dir_value = data.get("working_dir")
        candidate_path: Optional[Path] = None
        if isinstance(working_dir_value, str) and working_dir_value.strip():
            try:
                candidate_path = _expand_path(working_dir_value)
            except Exception:
                candidate_path = None
        if candidate_path is None:
            catalog_db = data.get("catalog_db")
            if isinstance(catalog_db, str) and catalog_db.strip():
                try:
                    catalog_path = _expand_path(catalog_db)
                    parent = catalog_path.parent
                    if parent.name.lower() == "data":
                        candidate_path = parent.parent
                    else:
                        candidate_path = parent
                except Exception:
                    candidate_path = None
        if candidate_path is not None:
            prepared = _prepare_working_dir(candidate_path)
            if prepared is not None:
                return prepared

    if program_data is not None:
        pd_candidate = program_data / "VideoCatalog"
        prepared = _prepare_working_dir(pd_candidate)
        if prepared is not None:
            return prepared

    local_base = _local_appdata_dir()
    prepared = _prepare_working_dir(local_base / "VideoCatalog")
    if prepared is not None:
        return prepared

    fallback = Path.home() / "VideoCatalog"
    fallback.mkdir(parents=True, exist_ok=True)
    (fallback / "data").mkdir(parents=True, exist_ok=True)
    return fallback


def get_data_dir(working_dir: Path) -> Path:
    return working_dir / "data"


def get_catalog_db_path(working_dir: Path) -> Path:
    return get_data_dir(working_dir) / "catalog.db"


def get_shards_dir(working_dir: Path) -> Path:
    return get_data_dir(working_dir) / "shards"


_SAFE_LABEL_PATTERN = re.compile(r"[^A-Za-z0-9_-]+")


def safe_label(label: str) -> str:
    """Return a filesystem-safe label used for shard filenames."""

    cleaned = _SAFE_LABEL_PATTERN.sub("_", label.strip())
    return cleaned or "drive"


def get_shard_db_path(working_dir: Path, label: str) -> Path:
    return get_shards_dir(working_dir) / f"{safe_label(label)}.db"


def get_logs_dir(working_dir: Path) -> Path:
    return working_dir / "logs"


def get_scans_dir(working_dir: Path) -> Path:
    return working_dir / "scans"


def get_exports_dir(working_dir: Path) -> Path:
    return working_dir / "exports"


def get_drive_types_path(working_dir: Path) -> Path:
    return working_dir / "drive_types.json"


def ensure_working_dir_structure(working_dir: Path) -> None:
    for directory in (
        working_dir,
        get_data_dir(working_dir),
        get_shards_dir(working_dir),
        get_logs_dir(working_dir),
        get_scans_dir(working_dir),
        get_exports_dir(working_dir),
    ):
        directory.mkdir(parents=True, exist_ok=True)


def get_default_settings_paths(working_dir: Path) -> list[Path]:
    """Return the search order for settings.json files."""

    paths = [working_dir / "settings.json"]
    program_data = _program_data_dir()
    if program_data is not None:
        paths.append(program_data / "VideoCatalog" / "settings.json")
    paths.append(_PROJECT_ROOT / "settings.json")
    return paths
