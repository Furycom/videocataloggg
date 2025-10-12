"""Error hierarchy for backup operations."""
from __future__ import annotations


class BackupError(RuntimeError):
    """Base exception for backup related failures."""


class BackupVerificationError(BackupError):
    """Raised when verification of a snapshot fails."""


class BackupRestoreError(BackupError):
    """Raised when restoring a snapshot fails."""


__all__ = ["BackupError", "BackupVerificationError", "BackupRestoreError"]
