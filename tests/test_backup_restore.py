import hashlib
import json
from datetime import datetime, timezone

import pytest

from backup.errors import BackupRestoreError
from backup.restore import restore_backup


class StubLogger:
    def __init__(self) -> None:
        self.events = []

    def info(self, event: str, **extra):  # pragma: no cover - recorder
        self.events.append(("info", event, extra))

    def error(self, event: str, **extra):  # pragma: no cover - recorder
        self.events.append(("error", event, extra))

    def event(self, *, event: str, phase: str, ok: bool, **extra):  # pragma: no cover - recorder
        self.events.append(("event", event, phase, ok, extra))


def _write_backup(working_dir, backup_id: str, payload: bytes) -> None:
    backup_dir = working_dir / "backups" / backup_id
    (backup_dir / "files" / "data").mkdir(parents=True, exist_ok=True)
    snapshot_path = backup_dir / "files" / "data" / "catalog.db"
    snapshot_path.write_bytes(payload)
    manifest = {
        "version": 1,
        "app_version": "0.0.0-test",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "files": [
            {
                "path": "data/catalog.db",
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
        "options": {},
        "notes": None,
    }
    (backup_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_restore_rolls_back_on_failure(tmp_path, monkeypatch):
    working_dir = tmp_path / "work"
    (working_dir / "backups").mkdir(parents=True)
    (working_dir / "data").mkdir(parents=True)

    backup_id = "20240101-000000"
    _write_backup(working_dir, backup_id, b"snapshot")

    target_path = working_dir / "data" / "catalog.db"
    target_path.write_text("active", encoding="utf-8")

    logger = StubLogger()

    def boom(path):
        raise BackupRestoreError("simulated failure")

    monkeypatch.setattr("backup.restore._quick_check", boom)

    with pytest.raises(BackupRestoreError):
        restore_backup(working_dir, backup_id, logger=logger, include_settings=False)

    assert target_path.read_text(encoding="utf-8") == "active"
    safety_dir = working_dir / "backups" / "_safety"
    assert any(safety_dir.iterdir())
