"""Semantic indexing helpers."""
from __future__ import annotations

import hashlib
import logging
import math
import random
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from core.paths import get_shards_dir

from .config import SemanticConfig
from .db import delete_all, semantic_connection, upsert_document

LOGGER = logging.getLogger("videocatalog.semantic")


@dataclass(slots=True)
class IndexBuildStats:
    """Summary for an indexing run."""

    processed: int = 0
    updated: int = 0
    skipped: int = 0
    shards_seen: int = 0

    def as_dict(self) -> Dict[str, int]:
        return {
            "processed": self.processed,
            "updated": self.updated,
            "skipped": self.skipped,
            "shards": self.shards_seen,
        }


def _hash_vector(key: str, *, dim: int) -> List[float]:
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=32).digest()
    seed = int.from_bytes(digest[:8], "big", signed=False)
    rng = random.Random(seed)
    values: List[float] = [rng.uniform(-1.0, 1.0) for _ in range(dim)]
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


def _make_content(payload: Dict[str, Optional[str]]) -> str:
    parts: List[str] = []
    for value in payload.values():
        if not value:
            continue
        parts.append(str(value))
    return " \n".join(parts)


def _shard_inventory_rows(shard_path: Path) -> Iterator[sqlite3.Row]:
    if not shard_path.exists():
        return iter(())
    conn = sqlite3.connect(shard_path)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(
            """
            SELECT path, category, mime, ext, size_bytes, mtime_utc, drive_label
            FROM inventory
            WHERE path IS NOT NULL
            """
        )
        for row in cursor.fetchall():
            yield row
    except sqlite3.DatabaseError:
        LOGGER.warning("Shard missing inventory table: %s", shard_path)
    finally:
        conn.close()


class SemanticIndexer:
    """Build or rebuild the semantic index."""

    def __init__(self, config: SemanticConfig) -> None:
        self.config = config
        self.working_dir = config.working_dir

    def build(self, *, rebuild: bool = False) -> Dict[str, int]:
        self.config.require_index_phase()
        stats = IndexBuildStats()
        shards_dir = get_shards_dir(self.working_dir)
        shard_paths = sorted(path for path in shards_dir.glob("*.db"))
        if rebuild:
            LOGGER.info("Semantic index rebuild requested — clearing tables")
        with semantic_connection(self.working_dir) as conn:
            if rebuild:
                delete_all(conn)
            for shard in shard_paths:
                stats.shards_seen += 1
                stats.processed += self._index_shard(conn, shard)
        return stats.as_dict()

    def _index_shard(self, conn: sqlite3.Connection, shard_path: Path) -> int:
        drive_label = shard_path.stem
        processed = 0
        for row in _shard_inventory_rows(shard_path):
            processed += 1
            path = row["path"]
            category = row["category"] or ""
            mime = row["mime"] or ""
            ext = row["ext"] or ""
            size = row["size_bytes"] or 0
            mtime = row["mtime_utc"]
            drive = row["drive_label"] or drive_label
            content = _make_content(
                {
                    "path": path,
                    "category": category,
                    "mime": mime,
                    "extension": ext,
                }
            )
            embedding = _hash_vector(f"{drive}:{path}", dim=self.config.vector_dim)
            metadata = {
                "category": category,
                "mime": mime,
                "extension": ext,
                "size_bytes": int(size or 0),
                "mtime_utc": mtime,
            }
            upsert_document(
                conn,
                drive_label=drive,
                path=path,
                content=content,
                embedding=embedding,
                dim=self.config.vector_dim,
                embedding_norm=1.0,
                kind="inventory",
                updated_utc=_normalize_timestamp(mtime),
                metadata=metadata,
            )
        return processed


class SemanticTranscriber:
    """Populate transcript placeholders for multimedia rows."""

    def __init__(self, config: SemanticConfig) -> None:
        self.config = config

    def run(self) -> Dict[str, int]:
        self.config.require_transcribe_phase()
        updates = 0
        with semantic_connection(self.config.working_dir) as conn:
            cursor = conn.execute(
                "SELECT id, path, metadata FROM semantic_documents WHERE kind = 'inventory'"
            )
            rows = cursor.fetchall()
            for row in rows:
                metadata = _ensure_metadata_dict(row["metadata"])
                if metadata.get("transcript"):
                    continue
                transcript = f"Transcript placeholder for {row['path']}"
                conn.execute(
                    "UPDATE semantic_documents SET metadata = json_set(COALESCE(metadata,'{}'), '$.transcript', ?) WHERE id = ?",
                    (transcript, int(row["id"])),
                )
                updates += 1
        return {"transcribed": updates}


def _normalize_timestamp(value: Optional[str]) -> Optional[str]:
    if value in (None, ""):
        return None
    try:
        if value.endswith("Z"):
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return str(value)


def _ensure_metadata_dict(value: Optional[str]) -> Dict[str, object]:
    if not value:
        return {}
    import json

    try:
        payload = json.loads(value)
        if isinstance(payload, dict):
            return payload
        return {"value": payload}
    except json.JSONDecodeError:
        return {}
