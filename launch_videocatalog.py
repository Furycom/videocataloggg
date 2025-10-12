"""Compatibility shim launching the VideoCatalog A2.0 web UI."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _find_launcher() -> tuple[list[str], str]:
    root = Path(__file__).resolve().parent
    scripts_dir = root / "scripts"
    ps1 = scripts_dir / "start_videocatalog.ps1"
    bat = scripts_dir / "start_videocatalog.bat"

    for candidate in ("powershell.exe", "powershell", "pwsh.exe", "pwsh"):
        resolved = shutil.which(candidate)
        if resolved and ps1.exists():
            return ([resolved, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)], "powershell")
    if os.name == "nt" and bat.exists():
        return (["cmd.exe", "/c", str(bat)], "batch")
    if bat.exists():
        return ([str(bat)], "batch")
    raise FileNotFoundError("Unable to locate VideoCatalog launcher scripts.")


def main() -> int:
    print("VideoCatalog A2.0: launching local Web UI…", flush=True)
    try:
        command, kind = _find_launcher()
    except FileNotFoundError as exc:
        print(f"Launcher error: {exc}", file=sys.stderr)
        return 2

    try:
        result = subprocess.run(command, check=False)
    except FileNotFoundError as exc:
        print(f"Failed to start {kind} launcher: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 130
    return int(result.returncode)


if __name__ == "__main__":
    sys.exit(main())
