"""Shared helpers for optional Pillow integration in visual review modules."""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing aid only
    from PIL import Image as PILImage

    PillowImage = PILImage.Image
else:  # pragma: no cover - runtime fallback when Pillow is absent
    PillowImage = Any

LOGGER = logging.getLogger("videocatalog.visualreview.frames")


class PillowUnavailableError(RuntimeError):
    """Raised when Pillow is required but not installed."""


def _missing_pillow() -> PillowUnavailableError:
    return PillowUnavailableError(
        "Pillow is required for visual review features. Install the 'pillow' package."
    )


@lru_cache(maxsize=1)
def load_pillow_image():  # type: ignore[no-untyped-def]
    """Return the Pillow Image module or raise :class:`PillowUnavailableError`."""

    try:
        from PIL import Image as pillow_image  # type: ignore[import-untyped]
    except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency guard
        raise _missing_pillow() from exc
    return pillow_image


@lru_cache(maxsize=1)
def load_pillow_ops():  # type: ignore[no-untyped-def]
    """Return the Pillow ImageOps module if available."""

    try:
        from PIL import ImageOps as pillow_ops  # type: ignore[import-untyped]
    except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency guard
        raise _missing_pillow() from exc
    return pillow_ops


_PIL_WARNING_EMITTED = False


def ensure_pillow(logger: logging.Logger = LOGGER) -> bool:
    """Return ``True`` when Pillow is available, logging a warning otherwise."""

    global _PIL_WARNING_EMITTED
    try:
        load_pillow_image()
        return True
    except PillowUnavailableError:
        if not _PIL_WARNING_EMITTED:
            logger.warning(
                "Pillow is not available; visual review image processing is disabled."
            )
            _PIL_WARNING_EMITTED = True
        return False


__all__ = [
    "PillowImage",
    "PillowUnavailableError",
    "ensure_pillow",
    "load_pillow_image",
    "load_pillow_ops",
    "LOGGER",
]
