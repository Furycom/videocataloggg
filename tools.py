from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Dict, Iterable, Literal, Optional, Tuple

from paths import load_settings, resolve_working_dir, update_settings

ToolName = Literal["mediainfo", "ffmpeg", "smartctl"]


_TOOL_EXECUTABLE = {
    "mediainfo": "mediainfo",
    "ffmpeg": "ffmpeg",
    "smartctl": "smartctl",
}

_TOOL_VERSION_ARGS: Dict[ToolName, list[str]] = {
    "mediainfo": ["--version"],
    "ffmpeg": ["-version"],
    "smartctl": ["--version"],
}

_PORTABLE_FILENAMES: Dict[ToolName, Iterable[str]] = {
    "mediainfo": ("mediainfo.exe", "MediaInfo.exe", "mediainfo"),
    "ffmpeg": ("ffmpeg.exe", "ffmpeg"),
    "smartctl": ("smartctl.exe", "smartctl"),
}

_WINGET_IDS: Dict[ToolName, Tuple[str, ...]] = {
    "mediainfo": ("MediaArea.MediaInfo", "MediaInfo.MediaInfo"),
    "ffmpeg": ("Gyan.FFmpeg", "FFmpeg.FFmpeg"),
    "smartctl": ("smartmontools.smartmontools",),
}

_WORKING_DIR_CACHE: Optional[Path] = None


def _get_working_dir(working_dir: Optional[Path]) -> Path:
    global _WORKING_DIR_CACHE
    if working_dir is not None:
        return Path(working_dir)
    if _WORKING_DIR_CACHE is None:
        _WORKING_DIR_CACHE = resolve_working_dir()
    return _WORKING_DIR_CACHE


def _expand_path(value: str) -> Path:
    expanded = os.path.expanduser(os.path.expandvars(value))
    return Path(expanded)


def _prepend_path(directory: Path) -> None:
    if not directory or not directory.exists():
        return
    path_str = str(directory)
    current = os.environ.get("PATH", "")
    paths = current.split(os.pathsep) if current else []
    if path_str in paths:
        return
    os.environ["PATH"] = os.pathsep.join([path_str] + paths)


def bootstrap_local_bin(working_dir: Optional[Path] = None) -> Optional[Path]:
    """Prepend the working directory's bin folders to PATH."""
    wd = _get_working_dir(working_dir)
    bin_dir = wd / "bin"
    if not bin_dir.exists():
        return None
    _prepend_path(bin_dir)
    for child in bin_dir.iterdir():
        if child.is_dir():
            _prepend_path(child)
    return bin_dir


def find_executable(
    name: str,
    extra_dirs: Optional[Iterable[str]] = None,
    working_dir: Optional[Path] = None,
) -> Optional[str]:
    """Search PATH, the working dir bin, and extra directories for *name*."""

    candidates = [shutil.which(name)]
    wd = _get_working_dir(working_dir)
    bin_dir = wd / "bin"
    search_dirs = []
    if bin_dir.exists():
        search_dirs.append(str(bin_dir))
        for child in bin_dir.iterdir():
            if child.is_dir():
                search_dirs.append(str(child))

    if extra_dirs:
        for entry in extra_dirs:
            if entry:
                search_dirs.append(str(_expand_path(str(entry))))

    for directory in search_dirs:
        candidates.append(shutil.which(name, path=directory))

    for candidate in candidates:
        if candidate:
            return os.path.abspath(candidate)

    # Manual search for Windows executables inside the provided directories.
    for directory in search_dirs:
        path_obj = Path(directory)
        direct = path_obj / name
        if direct.exists():
            return str(direct.resolve())
        if os.name == "nt":
            exe = direct.with_suffix(".exe")
            if exe.exists():
                return str(exe.resolve())
    return None


def get_version(cmd: list[str]) -> Optional[str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, PermissionError, OSError, subprocess.SubprocessError):
        return None
    output = proc.stdout or proc.stderr or ""
    for line in output.splitlines():
        line = line.strip()
        if line:
            return line
    return None


def _load_tool_settings(working_dir: Path) -> Dict[str, Dict[str, str]]:
    settings = load_settings(working_dir)
    tools_section = settings.get("tools") if isinstance(settings, dict) else None
    if isinstance(tools_section, dict):
        return {k: v for k, v in tools_section.items() if isinstance(v, dict)}
    return {}


def _persist_tool_path(tool: ToolName, path: Optional[Path], working_dir: Path) -> None:
    settings = _load_tool_settings(working_dir)
    entry = settings.get(tool, {}) if isinstance(settings.get(tool, {}), dict) else {}
    if path is None:
        entry.pop("path", None)
    else:
        entry["path"] = str(path)
    settings[tool] = entry
    update_settings(working_dir, tools=settings)


def set_manual_tool_path(
    tool: ToolName,
    path: str,
    working_dir: Optional[Path] = None,
) -> Tuple[bool, str]:
    wd = _get_working_dir(working_dir)
    candidate = _expand_path(path)
    if not candidate.exists():
        return False, f"Selected path not found: {candidate}"
    if candidate.is_dir():
        return False, "Please select the executable file, not a directory."
    _persist_tool_path(tool, candidate.resolve(), wd)
    return True, str(candidate.resolve())


def clear_manual_tool_path(tool: ToolName, working_dir: Optional[Path] = None) -> None:
    wd = _get_working_dir(working_dir)
    _persist_tool_path(tool, None, wd)


def probe_tool(tool: ToolName, working_dir: Optional[Path] = None) -> Dict[str, object]:
    wd = _get_working_dir(working_dir)
    bootstrap_local_bin(wd)
    errors = []
    resolved_path: Optional[Path] = None

    tool_settings = _load_tool_settings(wd).get(tool, {})
    persisted_path = tool_settings.get("path") if isinstance(tool_settings, dict) else None
    persisted_obj: Optional[Path] = None
    if isinstance(persisted_path, str) and persisted_path.strip():
        persisted_obj = _expand_path(persisted_path)
        if persisted_obj.is_file():
            resolved_path = persisted_obj
        elif persisted_obj.is_dir():
            extra_dir = [str(persisted_obj)]
            found = find_executable(_TOOL_EXECUTABLE[tool], extra_dirs=extra_dir, working_dir=wd)
            if found:
                resolved_path = Path(found)
            else:
                errors.append(f"Persisted directory does not contain {_TOOL_EXECUTABLE[tool]}: {persisted_obj}")
        else:
            errors.append(f"Persisted path not found: {persisted_obj}")

    if resolved_path is None:
        extra_dirs = []
        if persisted_obj and persisted_obj.is_dir():
            extra_dirs.append(str(persisted_obj))
        found = find_executable(_TOOL_EXECUTABLE[tool], extra_dirs=extra_dirs, working_dir=wd)
        if found:
            resolved_path = Path(found)

    present = resolved_path is not None and resolved_path.exists()
    version: Optional[str] = None
    if present:
        cmd = [str(resolved_path)] + _TOOL_VERSION_ARGS[tool]
        version = get_version(cmd)
        if version is None:
            errors.append("Unable to read version information.")
    else:
        resolved_path = None

    return {
        "name": tool,
        "present": bool(present),
        "version": version,
        "path": str(resolved_path) if resolved_path else None,
        "errors": errors,
    }


def get_winget_candidates(tool: ToolName) -> Tuple[str, ...]:
    return _WINGET_IDS[tool]


def winget_available() -> bool:
    return shutil.which("winget") is not None


def install_tool_via_winget(
    tool: ToolName,
    cancel_event: Optional["threading.Event"] = None,
    output_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, str, bool]:
    """Install *tool* using winget. Returns (success, message, cancelled)."""

    if not winget_available():
        return False, "winget is not available on this system.", False

    ids = _WINGET_IDS[tool]
    cancelled = False
    for package_id in ids:
        for args in (["--silent"], []):
            if cancel_event and cancel_event.is_set():
                cancelled = True
                return False, "Installation cancelled.", True
            command = ["winget", "install", "--id", package_id] + args
            if output_callback:
                output_callback("Running: " + " ".join(command))
            try:
                proc = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    shell=False,
                )
            except FileNotFoundError:
                return False, "winget executable not found.", False

            assert proc.stdout is not None
            for line in proc.stdout:
                if output_callback:
                    output_callback(line.rstrip())
                if cancel_event and cancel_event.is_set():
                    cancelled = True
                    proc.terminate()
                    proc.wait(timeout=5)
                    return False, "Installation cancelled.", True

            return_code = proc.wait()
            if return_code == 0:
                message = f"Installed {tool} via winget (package {package_id})."
                return True, message, False
            if args:
                # Retry without --silent once.
                if output_callback:
                    output_callback("winget --silent failed; retrying interactively…")
                continue
            else:
                if output_callback:
                    output_callback(f"winget install failed with exit code {return_code}.")
    return False, "All winget installation attempts failed.", cancelled


def setup_portable_tool(
    tool: ToolName,
    source_folder: str,
    working_dir: Optional[Path] = None,
) -> Tuple[bool, str, Optional[str]]:
    wd = _get_working_dir(working_dir)
    folder = _expand_path(source_folder)
    if not folder.exists() or not folder.is_dir():
        return False, "Selected folder is not valid.", None

    search_dirs = [folder]
    bin_sub = folder / "bin"
    if bin_sub.is_dir():
        search_dirs.append(bin_sub)

    target_file: Optional[Path] = None
    expected_names = {name.lower(): name for name in _PORTABLE_FILENAMES[tool]}

    for directory in search_dirs:
        try:
            for entry in directory.iterdir():
                if entry.is_file() and entry.name.lower() in expected_names:
                    target_file = entry
                    break
        except PermissionError:
            continue
        if target_file:
            break

    if not target_file:
        return False, f"Could not find {_TOOL_EXECUTABLE[tool]} executable in the selected folder.", None

    dest_root = wd / "bin" / tool
    dest_root.mkdir(parents=True, exist_ok=True)
    dest_path = dest_root / target_file.name
    shutil.copy2(str(target_file), str(dest_path))
    _prepend_path(dest_root)
    _persist_tool_path(tool, dest_path.resolve(), wd)
    return True, f"Using portable {tool} at {dest_path}", str(dest_path.resolve())

