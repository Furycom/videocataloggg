from __future__ import annotations

import base64
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from core.settings import update_settings
from core.versioning import get_app_version

from .checks import MARKER_SCHEMA, compute_signature, validate_marker

APP_VERSION = get_app_version()

APP_NAME = "VideoCatalog"

__all__ = [
    "MarkerRuntime",
    "MarkerReadResult",
    "MarkerWriteResult",
    "load_marker",
    "prepare_runtime",
    "write_marker",
]


@dataclass(slots=True)
class MarkerRuntime:
    enabled: bool
    filename: str
    write_hidden: bool
    write_readonly: bool
    use_hmac: bool
    hmac_key: Optional[bytes]
    catalog_uuid: Optional[str]
    app_name: str = APP_NAME
    app_version: str = APP_VERSION


@dataclass(slots=True)
class MarkerReadResult:
    path: Path
    exists: bool
    content: Optional[dict[str, Any]]
    schema_ok: bool
    signature_ok: Optional[bool]
    error: Optional[str] = None


@dataclass(slots=True)
class MarkerWriteResult:
    path: Path
    status: str
    message: str
    content: dict[str, Any]
    written: bool


def prepare_runtime(
    working_dir: Path,
    settings: Mapping[str, Any],
    *,
    enable_override: Optional[bool] = None,
    filename_override: Optional[str] = None,
) -> MarkerRuntime:
    cfg = dict(settings.get("disk_marker") or {})
    enabled = bool(cfg.get("enable", False))
    if enable_override is not None:
        enabled = bool(enable_override)
        cfg["enable"] = enabled
    filename = str(cfg.get("filename") or ".videocatalog.id")
    if filename_override:
        filename = filename_override.strip()
        if filename:
            cfg["filename"] = filename
    if not filename:
        filename = ".videocatalog.id"
    write_hidden = bool(cfg.get("write_hidden", True))
    write_readonly = bool(cfg.get("write_readonly", True))
    use_hmac = bool(cfg.get("use_hmac", True))
    catalog_uuid = cfg.get("catalog_uuid")
    hmac_b64 = cfg.get("hmac_key")
    mutated = False
    if enabled and not catalog_uuid:
        catalog_uuid = str(uuid.uuid4())
        cfg["catalog_uuid"] = catalog_uuid
        mutated = True
    hmac_bytes: Optional[bytes] = None
    if use_hmac and enabled:
        if not hmac_b64:
            hmac_b64 = base64.b64encode(os.urandom(32)).decode("ascii")
            cfg["hmac_key"] = hmac_b64
            mutated = True
        try:
            hmac_bytes = base64.b64decode(hmac_b64.encode("ascii")) if hmac_b64 else None
        except Exception:  # pragma: no cover - defensive guard
            hmac_bytes = None
    if mutated:
        update_settings(working_dir, disk_marker=cfg)
        if hasattr(settings, "__setitem__"):
            try:
                settings["disk_marker"] = cfg  # type: ignore[index]
            except Exception:
                pass
    return MarkerRuntime(
        enabled=enabled,
        filename=filename,
        write_hidden=write_hidden,
        write_readonly=write_readonly,
        use_hmac=use_hmac,
        hmac_key=hmac_bytes,
        catalog_uuid=catalog_uuid,
    )


def load_marker(path: Path, *, hmac_key: Optional[bytes]) -> MarkerReadResult:
    if not path.exists():
        return MarkerReadResult(path=path, exists=False, content=None, schema_ok=False, signature_ok=None)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            content = json.load(handle)
    except Exception as exc:
        return MarkerReadResult(
            path=path,
            exists=True,
            content=None,
            schema_ok=False,
            signature_ok=None,
            error=str(exc),
        )
    if not isinstance(content, dict):
        return MarkerReadResult(
            path=path,
            exists=True,
            content=None,
            schema_ok=False,
            signature_ok=None,
            error="invalid_format",
        )
    validation = validate_marker(content, key=hmac_key)
    return MarkerReadResult(
        path=path,
        exists=True,
        content=content,
        schema_ok=validation.schema_ok,
        signature_ok=validation.signature_ok,
        error=validation.reason,
    )


def _apply_attributes(path: Path, *, hidden: bool, readonly: bool) -> None:
    if os.name == "nt":  # pragma: win32-only
        import ctypes

        FILE_ATTRIBUTE_READONLY = 0x00000001
        FILE_ATTRIBUTE_HIDDEN = 0x00000002
        FILE_ATTRIBUTE_SYSTEM = 0x00000004

        attrs = 0
        if readonly:
            attrs |= FILE_ATTRIBUTE_READONLY
        if hidden:
            attrs |= FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM
        if attrs:
            try:
                ctypes.windll.kernel32.SetFileAttributesW(str(path), attrs)
            except Exception:
                pass
    else:  # pragma: no cover - non-Windows fallback
        if readonly:
            try:
                os.chmod(path, 0o444)
            except OSError:
                pass


def write_marker(
    root: Path,
    runtime: MarkerRuntime,
    *,
    payload: Mapping[str, Any],
) -> MarkerWriteResult:
    target = Path(root) / runtime.filename
    tmp = target.with_name(target.name + ".tmp")
    data = dict(payload)
    data["schema"] = MARKER_SCHEMA
    if runtime.use_hmac and runtime.hmac_key:
        data["sig"] = compute_signature({k: v for k, v in data.items() if k != "sig"}, runtime.hmac_key)
    elif "sig" in data:
        data.pop("sig", None)
    existed = target.exists()
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except OSError as exc:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return MarkerWriteResult(
            path=target,
            status="error",
            message=str(exc),
            content=data,
            written=False,
        )
    _apply_attributes(target, hidden=runtime.write_hidden, readonly=runtime.write_readonly)
    message = "updated" if existed else "created"
    return MarkerWriteResult(path=target, status="ok", message=message, content=data, written=True)
