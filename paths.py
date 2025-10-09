from __future__ import annotations

"""Backward-compatible shim for legacy imports."""

from .core.paths import *  # noqa: F401,F403
from .core.paths import __all__ as _core_paths_all
from .core.settings import load_settings, save_settings, update_settings

__all__ = list(dict.fromkeys((*_core_paths_all, "load_settings", "save_settings", "update_settings")))
