"""Retention policy enforcement for backups."""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Set

from .errors import BackupError
from .logs import BackupLogger
from .types import RetentionSummary


@dataclass(slots=True)
class RetentionPolicy:
    keep_last: int = 10
    keep_daily: int = 14
    keep_weekly: int = 8
    max_total_gb: float = 50.0


def _parse_manifest(manifest_path: Path) -> Dict[str, object]:
    if not manifest_path.exists():
        raise BackupError(f"Manifest missing at {manifest_path}")
    with manifest_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


@dataclass(slots=True)
class _BackupMeta:
    backup_id: str
    created: datetime
    size_bytes: int
    path: Path


def _load_backups(base: Path) -> List[_BackupMeta]:
    items: List[_BackupMeta] = []
    if not base.exists():
        return items
    for child in base.iterdir():
        if not child.is_dir() or child.name.startswith("_"):
            continue
        manifest_path = child / "manifest.json"
        try:
            manifest = _parse_manifest(manifest_path)
        except BackupError:
            continue
        created_text = manifest.get("created_utc")
        try:
            created = datetime.fromisoformat(str(created_text))
        except Exception:
            created = datetime.fromtimestamp(child.stat().st_mtime, tz=timezone.utc)
        size = 0
        for entry in manifest.get("files", []) or []:
            if isinstance(entry, dict) and entry.get("bytes") is not None:
                try:
                    size += int(entry.get("bytes"))
                except Exception:
                    continue
        if size <= 0:
            try:
                size = (child / "bundle.zip").stat().st_size
            except Exception:
                size = 0
        items.append(_BackupMeta(backup_id=child.name, created=created, size_bytes=size, path=child))
    items.sort(key=lambda meta: meta.created, reverse=True)
    return items


def _apply_retention_rules(
    items: List[_BackupMeta], policy: RetentionPolicy
) -> tuple[Set[str], Dict[str, Set[str]]]:
    keep: Dict[str, Set[str]] = {}
    now = datetime.now(timezone.utc)

    # Keep the most recent backups unconditionally
    for meta in items[: max(policy.keep_last, 0)]:
        keep.setdefault(meta.backup_id, set()).add("last")

    if policy.keep_daily > 0:
        cutoff = now - timedelta(days=policy.keep_daily)
        seen_days: Set[str] = set()
        for meta in items:
            if meta.created < cutoff:
                continue
            day_key = meta.created.date().isoformat()
            if day_key in seen_days:
                continue
            seen_days.add(day_key)
            keep.setdefault(meta.backup_id, set()).add("daily")
            if len(seen_days) >= policy.keep_daily:
                break

    if policy.keep_weekly > 0:
        cutoff = now - timedelta(weeks=policy.keep_weekly)
        seen_weeks: Set[tuple[int, int]] = set()
        for meta in items:
            if meta.created < cutoff:
                continue
            year, week, _ = meta.created.isocalendar()
            key = (year, week)
            if key in seen_weeks:
                continue
            seen_weeks.add(key)
            keep.setdefault(meta.backup_id, set()).add("weekly")
            if len(seen_weeks) >= policy.keep_weekly:
                break

    return set(keep.keys()), keep


def _enforce_size_cap(
    items: List[_BackupMeta],
    keep_ids: Set[str],
    reasons: Dict[str, Set[str]],
    *,
    policy: RetentionPolicy,
) -> Set[str]:
    if policy.max_total_gb <= 0:
        return keep_ids
    max_bytes = int(policy.max_total_gb * 1024 ** 3)
    total = sum(meta.size_bytes for meta in items if meta.backup_id in keep_ids)
    if total <= max_bytes:
        return keep_ids
    priority_map = {"weekly": 3, "daily": 2, "last": 1}
    ranked = []
    for meta in items:
        if meta.backup_id not in keep_ids:
            continue
        reason_set = reasons.get(meta.backup_id, set())
        priority = max((priority_map.get(reason, 0) for reason in reason_set), default=0)
        ranked.append((priority, meta.created, meta))
    ranked.sort(key=lambda item: (item[0], item[1]))
    for _, _, meta in ranked:
        if total <= max_bytes:
            break
        keep_ids.discard(meta.backup_id)
        total -= meta.size_bytes
    return keep_ids


def apply_retention(working_dir: Path, policy: RetentionPolicy, *, logger: BackupLogger) -> RetentionSummary:
    base = working_dir / "backups"
    items = _load_backups(base)
    keep_ids, reasons = _apply_retention_rules(items, policy)
    keep_ids = _enforce_size_cap(items, keep_ids, reasons, policy=policy)

    removed: List[str] = []
    for meta in items:
        if meta.backup_id in keep_ids:
            continue
        shutil.rmtree(meta.path, ignore_errors=True)
        removed.append(meta.backup_id)
        logger.warning("backup_removed", id=meta.backup_id, reason="retention")

    kept = [meta.backup_id for meta in items if meta.backup_id not in removed]
    freed = sum(meta.size_bytes for meta in items if meta.backup_id in removed)
    logger.event(event="retention_applied", phase="retention", ok=True, removed=len(removed), kept=len(kept))
    return RetentionSummary(removed=removed, kept=kept, freed_bytes=freed)


__all__ = ["RetentionPolicy", "apply_retention"]
