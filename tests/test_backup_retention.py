import json
from datetime import datetime, timedelta, timezone

from backup.retention import RetentionPolicy, apply_retention


class StubLogger:
    def __init__(self) -> None:
        self.events = []

    def warning(self, event: str, **extra):  # pragma: no cover - simple recorder
        self.events.append(("warning", event, extra))

    def event(self, *, event: str, phase: str, ok: bool, **extra):  # pragma: no cover - simple recorder
        self.events.append(("event", event, phase, ok, extra))


def _create_manifest(path, *, created: datetime, size_bytes: int) -> None:
    payload = {
        "version": 1,
        "app_version": "0.0.0-test",
        "created_utc": created.isoformat(),
        "files": [
            {"path": "data/catalog.db", "bytes": size_bytes, "sha256": "deadbeef"}
        ],
        "options": {},
        "notes": None,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_apply_retention_removes_old_backups(tmp_path):
    working_dir = tmp_path / "work"
    base = working_dir / "backups"
    base.mkdir(parents=True)

    logger = StubLogger()
    now = datetime.now(timezone.utc)
    sizes = [10, 20, 30, 40]
    backup_ids = []
    for index, size in enumerate(sizes):
        backup_time = now - timedelta(days=index)
        backup_id = backup_time.strftime("%Y%m%d-%H%M%S")
        backup_ids.append(backup_id)
        backup_dir = base / backup_id
        (backup_dir / "files").mkdir(parents=True)
        _create_manifest(backup_dir / "manifest.json", created=backup_time, size_bytes=size)

    policy = RetentionPolicy(keep_last=2, keep_daily=0, keep_weekly=0, max_total_gb=0)
    summary = apply_retention(working_dir, policy, logger=logger)

    assert set(summary.removed) == set(backup_ids[2:])
    assert set(summary.kept) == set(backup_ids[:2])
    assert summary.freed_bytes == sum(sizes[2:])

    for backup_id in summary.removed:
        assert not (base / backup_id).exists()
    for backup_id in summary.kept:
        assert (base / backup_id).exists()
