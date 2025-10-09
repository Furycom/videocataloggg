from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .tools import ChromaprintTool, resolve_chromaprint


@dataclass(slots=True)
class ChromaprintFingerprint:
    path: Path
    duration_seconds: Optional[float]
    fingerprint: str
    tool_version: Optional[str]


class ChromaprintError(RuntimeError):
    pass


class ChromaprintGenerator:
    def __init__(self, tool: Optional[ChromaprintTool]) -> None:
        self._tool = tool or resolve_chromaprint()

    @property
    def available(self) -> bool:
        return self._tool is not None

    @property
    def executable(self) -> Optional[str]:
        return self._tool.executable if self._tool else None

    def compute(self, source: Path, timeout: int = 900) -> Optional[ChromaprintFingerprint]:
        if not self._tool:
            return None
        cmd = [self._tool.executable, "-json", str(source)]
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                check=False,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise ChromaprintError(str(exc)) from exc
        except subprocess.TimeoutExpired as exc:  # pragma: no cover - defensive
            raise ChromaprintError(f"fpcalc timed out after {timeout}s") from exc
        if completed.returncode != 0 or not completed.stdout.strip():
            raise ChromaprintError(
                completed.stderr.strip() or completed.stdout.strip() or "fpcalc failed"
            )
        payload = json.loads(completed.stdout)
        fingerprint = str(payload.get("fingerprint") or "").strip()
        if not fingerprint:
            raise ChromaprintError("fpcalc did not return a fingerprint")
        duration = payload.get("duration")
        try:
            duration_value = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration_value = None
        return ChromaprintFingerprint(
            path=source,
            duration_seconds=duration_value,
            fingerprint=fingerprint,
            tool_version=self._tool.version,
        )
