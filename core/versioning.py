"""Helpers for resolving the running VideoCatalog application version."""
from __future__ import annotations

import importlib
import importlib.util
import sys
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Optional

_API_PACKAGE = "api"
_API_INIT = Path(__file__).resolve().parent.parent / _API_PACKAGE / "__init__.py"


def _module_matches_repo(module: ModuleType) -> bool:
    """Return ``True`` if ``module`` refers to the repository ``api`` package."""
    module_path = getattr(module, "__file__", None)
    if not module_path:
        return False
    try:
        return Path(module_path).resolve() == _API_INIT
    except OSError:
        return False


def _load_repo_api_module() -> Optional[ModuleType]:
    """Load the repository ``api`` package if available on the import path."""
    module = sys.modules.get(_API_PACKAGE)
    if module and _module_matches_repo(module):
        return module
    try:
        module = importlib.import_module(_API_PACKAGE)
    except Exception:
        return None
    return module if _module_matches_repo(module) else None


def _load_version_from_file() -> Optional[str]:
    """Load ``__version__`` from the repository ``api`` package file."""
    if not _API_INIT.exists():
        return None
    spec = importlib.util.spec_from_file_location("videocatalog.api", _API_INIT)
    if not spec or not spec.loader:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except Exception:
        return None
    version = getattr(module, "__version__", None)
    return str(version) if isinstance(version, str) else None


@lru_cache(maxsize=1)
def get_app_version() -> str:
    """Return the VideoCatalog application version string.

    This helper avoids accidental imports of similarly named test modules (for
    example ``tests/api.py``) by verifying the resolved module path. When the
    package cannot be imported it falls back to loading ``__init__.py`` from the
    repository directly and ultimately to a safe default.
    """

    module = _load_repo_api_module()
    if module is not None:
        version = getattr(module, "__version__", None)
        if isinstance(version, str):
            return version
    version = _load_version_from_file()
    if version:
        return version
    return "0.0.0"


__all__ = ["get_app_version"]
