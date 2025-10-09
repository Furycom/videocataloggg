from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .tools import TMKToolchain, resolve_tmk_toolchain


@dataclass(slots=True)
class TMKSignature:
    path: Path
    duration_seconds: Optional[float]
    signature: bytes
    tool_version: Optional[str]


class TMKVideoError(RuntimeError):
    pass


class TMKVideoFingerprinter:
    def __init__(self, toolchain: Optional[TMKToolchain]) -> None:
        self._toolchain = toolchain or resolve_tmk_toolchain()

    @property
    def available(self) -> bool:
        return self._toolchain is not None

    def compute(
        self,
        source: Path,
        *,
        duration_hint: Optional[float] = None,
        timeout: int = 1800,
    ) -> Optional[TMKSignature]:
        if not self._toolchain:
            return None
        with tempfile.TemporaryDirectory(prefix="tmk_sig_") as tmpdir:
            sig_path = Path(tmpdir) / "signature.tmk"
            json_path = Path(tmpdir) / "signature.json"
            base_cmd = [
                self._toolchain.hash_tool,
                "-i",
                str(source),
                "-o",
                str(sig_path),
            ]
            if self._toolchain.extractor_tool:
                base_cmd.extend(["--pdq-path", self._toolchain.extractor_tool])

            def _invoke(cmd: list[str]) -> subprocess.CompletedProcess[str]:
                try:
                    return subprocess.run(
                        cmd,
                        capture_output=True,
                        check=False,
                        text=True,
                        timeout=timeout,
                    )
                except FileNotFoundError as exc:
                    raise TMKVideoError(str(exc)) from exc
                except subprocess.TimeoutExpired as exc:
                    raise TMKVideoError(
                        f"tmk-hash-video timed out after {timeout}s"
                    ) from exc

            cmd = base_cmd + ["--json", str(json_path)]
            completed = _invoke(cmd)
            if completed.returncode != 0:
                stderr = (completed.stderr or "").strip()
                stdout = (completed.stdout or "").strip()
                if "unrecognized" in stderr.lower() or "unknown option" in stderr.lower():
                    completed = _invoke(base_cmd)
                else:
                    raise TMKVideoError(stderr or stdout or "tmk-hash-video failed")
            if completed.returncode != 0:
                stderr = (completed.stderr or "").strip()
                stdout = (completed.stdout or "").strip()
                raise TMKVideoError(stderr or stdout or "tmk-hash-video failed")
            if not sig_path.exists():
                raise TMKVideoError("tmk-hash-video did not create a signature")
            signature_bytes = sig_path.read_bytes()
            duration_value = duration_hint
            if json_path.exists():
                try:
                    payload = json.loads(json_path.read_text())
                    duration_value = float(payload.get("duration"))
                except Exception:
                    pass
            return TMKSignature(
                path=source,
                duration_seconds=duration_value,
                signature=signature_bytes,
                tool_version=self._toolchain.version,
            )

    def similarity(self, sig_a: bytes, sig_b: bytes, timeout: int = 600) -> float:
        if not self._toolchain:
            raise TMKVideoError("TMK toolchain not available")
        with tempfile.TemporaryDirectory(prefix="tmk_cmp_") as tmpdir:
            file_a = Path(tmpdir) / "a.tmk"
            file_b = Path(tmpdir) / "b.tmk"
            file_a.write_bytes(sig_a)
            file_b.write_bytes(sig_b)
            cmd = [
                self._toolchain.compare_tool,
                str(file_a),
                str(file_b),
            ]
            try:
                completed = subprocess.run(
                    cmd,
                    capture_output=True,
                    check=False,
                    text=True,
                    timeout=timeout,
                )
            except FileNotFoundError as exc:
                raise TMKVideoError(str(exc)) from exc
            except subprocess.TimeoutExpired as exc:
                raise TMKVideoError(f"tmk-compare-post timed out after {timeout}s") from exc
            output = completed.stdout or completed.stderr or ""
            if completed.returncode != 0:
                raise TMKVideoError(output.strip() or "tmk-compare-post failed")
            return _parse_similarity(output)


def _parse_similarity(output: str) -> float:
    text = output.strip()
    if not text:
        return 0.0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            return float(stripped.split()[-1])
        except (ValueError, IndexError):
            continue
    try:
        return float(text)
    except ValueError:
        return 0.0
