from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(slots=True)
class TMKToolchain:
    """Paths to the TMK+PDQF helpers."""

    hash_tool: str
    compare_tool: str
    extractor_tool: Optional[str]
    version: Optional[str]


@dataclass(slots=True)
class ChromaprintTool:
    """Metadata about the Chromaprint fpcalc utility."""

    executable: str
    version: Optional[str]


class ToolDiscoveryError(RuntimeError):
    """Raised when a required external helper cannot be located."""


def _executable_name(base: str) -> str:
    if platform.system().lower().startswith("win"):
        return f"{base}.exe"
    return base


def _which_with_override(name: str, override: Optional[str]) -> Optional[str]:
    if override:
        candidate = Path(os.path.expandvars(os.path.expanduser(override)))
        if candidate.is_dir():
            exe = candidate / _executable_name(name)
            if exe.exists():
                return str(exe.resolve())
        elif candidate.exists():
            return str(candidate.resolve())
    path = shutil.which(name)
    return path


def resolve_tmk_toolchain(override: Optional[str] = None) -> Optional[TMKToolchain]:
    """Locate the TMK+PDQF helper binaries."""

    hash_path = _which_with_override("tmk-hash-video", override)
    compare_path = _which_with_override("tmk-compare-post", override)
    extractor_path = _which_with_override("tmk-extract-frame", override)

    if not hash_path or not compare_path:
        return None

    version = _probe_version([hash_path, "--version"]) or _probe_version(
        [compare_path, "--version"]
    )
    return TMKToolchain(
        hash_tool=hash_path,
        compare_tool=compare_path,
        extractor_tool=extractor_path,
        version=version,
    )


def resolve_chromaprint(override: Optional[str] = None) -> Optional[ChromaprintTool]:
    exe = _which_with_override("fpcalc", override)
    if not exe:
        return None
    version = _probe_version([exe, "-version"]) or _probe_version([exe, "--version"])
    return ChromaprintTool(executable=exe, version=version)


def have_videohash() -> bool:
    try:
        import importlib

        importlib.import_module("videohash")
        return True
    except ModuleNotFoundError:
        return False
    except Exception:
        return False


def _probe_version(cmd: list[str]) -> Optional[str]:
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        output = completed.stdout or completed.stderr or ""
    else:
        output = completed.stdout or completed.stderr or ""
    output = output.strip()
    if not output:
        return None
    first_line = output.splitlines()[0]
    return first_line.strip()


def ensure_executable_present(name: str, override: Optional[str] = None) -> str:
    path = _which_with_override(name, override)
    if not path:
        raise ToolDiscoveryError(f"Unable to locate executable: {name}")
    return path
