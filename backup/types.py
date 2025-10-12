"""Common dataclasses shared across backup modules."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


@dataclass(slots=True)
class BackupOptions:
    """User supplied options controlling snapshot contents."""

    include_vectors: bool = False
    include_thumbs: bool = False
    include_logs_tail: bool = True
    note: Optional[str] = None


@dataclass(slots=True)
class BackupArtifact:
    """Single file captured as part of a snapshot."""

    path: Path
    relative_path: str
    size_bytes: int
    sha256: str


@dataclass(slots=True)
class BackupManifest:
    version: int
    app_version: str
    created_utc: str
    files: List[Dict[str, object]]
    options: Dict[str, object]
    notes: Optional[str]


@dataclass(slots=True)
class BackupResult:
    backup_id: str
    directory: Path
    manifest_path: Path
    bundle_path: Path
    artifacts: List[BackupArtifact]
    manifest: BackupManifest


@dataclass(slots=True)
class BackupSummary:
    id: str
    created_utc: str
    size_bytes: int
    include_vectors: bool
    include_thumbs: bool
    verified: bool
    notes: Optional[str]
    path: Path


@dataclass(slots=True)
class RetentionSummary:
    removed: List[str]
    kept: List[str]
    freed_bytes: int


__all__ = [
    "BackupArtifact",
    "BackupManifest",
    "BackupOptions",
    "BackupResult",
    "BackupSummary",
    "RetentionSummary",
]
