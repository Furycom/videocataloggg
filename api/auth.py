"""Simple header-based authentication for the local API."""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, Header, HTTPException, status


class APIKeyAuth:
    """Dependency enforcing a static API key provided via ``X-API-Key`` header."""

    def __init__(self, expected_key: Optional[str]) -> None:
        self._expected = (expected_key or "").strip()

    def __call__(self, x_api_key: Optional[str] = Header(None)) -> str:
        if not self._expected:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="API key is not configured.",
            )
        if not x_api_key or x_api_key.strip() != self._expected:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key.",
            )
        return self._expected


def require_api_key(auth: APIKeyAuth = Depends()) -> str:
    """Convenience dependency mirroring FastAPI's dependency resolution."""

    return auth()
