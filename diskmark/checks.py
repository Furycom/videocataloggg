from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, Mapping, Optional

MARKER_SCHEMA = 1


def _canonical_payload(payload: Mapping[str, Any]) -> str:
    """Return a canonical JSON string for signature computation."""

    return json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def compute_signature(payload: Mapping[str, Any], key: bytes) -> str:
    """Compute a base64-encoded HMAC-SHA256 signature for *payload*."""

    digest = hmac.new(key, _canonical_payload(payload).encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def _payload_without_sig(payload: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(payload, dict):
        return {k: v for k, v in payload.items() if k != "sig"}
    return {k: payload[k] for k in payload.keys() if k != "sig"}


def verify_signature(payload: Mapping[str, Any], key: bytes) -> bool:
    """Return True when the embedded signature matches the payload."""

    sig = payload.get("sig") if isinstance(payload, Mapping) else None
    if not sig or not isinstance(sig, str):
        return False
    expected = compute_signature(_payload_without_sig(payload), key)
    try:
        return hmac.compare_digest(sig, expected)
    except ValueError:  # pragma: no cover - defensive guard
        return False


@dataclass(slots=True)
class MarkerValidation:
    """Result of validating a marker payload."""

    schema_ok: bool
    signature_ok: Optional[bool]
    reason: Optional[str] = None


def validate_marker(payload: Mapping[str, Any], *, key: Optional[bytes]) -> MarkerValidation:
    schema = payload.get("schema") if isinstance(payload, Mapping) else None
    if schema != MARKER_SCHEMA:
        return MarkerValidation(schema_ok=False, signature_ok=None, reason="schema_mismatch")
    if key is None:
        return MarkerValidation(schema_ok=True, signature_ok=None)
    signature_ok = verify_signature(payload, key)
    return MarkerValidation(schema_ok=True, signature_ok=signature_ok, reason=None if signature_ok else "sig_mismatch")
