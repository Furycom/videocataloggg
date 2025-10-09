"""Structure profiling orchestration."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from . import guess, review, rules, score
from .types import ConfidenceBreakdown, FolderAnalysis, GuessResult, VerificationSignals
from .verify import collect_verification

LOGGER = logging.getLogger("videocatalog.structure")


@dataclass(slots=True)
class StructureSettings:
    enable: bool = True
    weights: Dict[str, float] = field(
        default_factory=lambda: {
            "canon": 0.35,
            "nfo": 0.25,
            "oshash": 0.20,
            "name_match": 0.15,
            "runtime": 0.05,
        }
    )
    low_threshold: float = 0.50
    high_threshold: float = 0.80
    opensubtitles_enabled: bool = True
    opensubtitles_read_kib: int = 64
    opensubtitles_timeout: float = 15.0
    opensubtitles_api_key: Optional[str] = None
    tmdb_enabled: bool = True
    tmdb_api_key: Optional[str] = None
    imdb_enabled: bool = True
    max_candidates: int = 5

    def clone_disabled_external(self) -> "StructureSettings":
        return StructureSettings(
            enable=self.enable,
            weights=dict(self.weights),
            low_threshold=self.low_threshold,
            high_threshold=self.high_threshold,
            opensubtitles_enabled=False,
            opensubtitles_read_kib=self.opensubtitles_read_kib,
            opensubtitles_timeout=self.opensubtitles_timeout,
            opensubtitles_api_key=self.opensubtitles_api_key,
            tmdb_enabled=False,
            tmdb_api_key=self.tmdb_api_key,
            imdb_enabled=False,
            max_candidates=self.max_candidates,
        )


def load_structure_settings(data: Dict[str, object]) -> StructureSettings:
    section = data.get("structure") if isinstance(data, dict) else None
    if not isinstance(section, dict):
        section = {}
    weights = section.get("weights") if isinstance(section.get("weights"), dict) else None
    settings = StructureSettings()
    settings.enable = bool(section.get("enable", settings.enable))
    if weights:
        for key, value in weights.items():
            try:
                settings.weights[key] = float(value)
            except (TypeError, ValueError):
                continue
    settings.low_threshold = float(section.get("low_threshold", settings.low_threshold))
    settings.high_threshold = float(section.get("high_threshold", settings.high_threshold))
    opensubs = section.get("opensubtitles") if isinstance(section.get("opensubtitles"), dict) else {}
    settings.opensubtitles_enabled = bool(opensubs.get("enabled", settings.opensubtitles_enabled))
    settings.opensubtitles_read_kib = int(opensubs.get("read_kib", settings.opensubtitles_read_kib))
    settings.opensubtitles_timeout = float(opensubs.get("timeout_s", settings.opensubtitles_timeout))
    settings.opensubtitles_api_key = (
        str(opensubs.get("api_key")).strip() or None if "api_key" in opensubs else None
    )
    tmdb_section = section.get("tmdb") if isinstance(section.get("tmdb"), dict) else {}
    settings.tmdb_enabled = bool(tmdb_section.get("enabled", settings.tmdb_enabled))
    settings.tmdb_api_key = (
        str(tmdb_section.get("api_key")).strip() or None if "api_key" in tmdb_section else None
    )
    imdb_section = section.get("imdb") if isinstance(section.get("imdb"), dict) else {}
    settings.imdb_enabled = bool(imdb_section.get("enabled", settings.imdb_enabled))
    try:
        settings.max_candidates = max(1, int(section.get("max_candidates", settings.max_candidates)))
    except (TypeError, ValueError):
        pass
    return settings


def ensure_structure_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS folder_profile (
            folder_path TEXT PRIMARY KEY,
            kind TEXT,
            main_video_path TEXT,
            parsed_title TEXT,
            parsed_year INTEGER,
            assets_json TEXT,
            issues_json TEXT,
            confidence REAL,
            source_signals_json TEXT,
            updated_utc TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS folder_candidates (
            folder_path TEXT,
            source TEXT,
            candidate_id TEXT,
            title TEXT,
            year INTEGER,
            score REAL,
            extra_json TEXT,
            PRIMARY KEY (folder_path, source, candidate_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS review_queue (
            folder_path TEXT PRIMARY KEY,
            confidence REAL,
            reasons_json TEXT,
            questions_json TEXT,
            created_utc TEXT NOT NULL
        )
        """
    )


@dataclass(slots=True)
class StructureSummary:
    processed: int = 0
    movies: int = 0
    confident: int = 0
    medium: int = 0
    low: int = 0


class StructureProfiler:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        settings: StructureSettings,
        drive_label: str,
        mount_path: Path,
    ) -> None:
        self.conn = conn
        self.settings = settings
        self.drive_label = drive_label
        self.mount_path = Path(mount_path)
        self.conn.row_factory = sqlite3.Row
        ensure_structure_tables(self.conn)

    def _relative(self, path: Path) -> str:
        try:
            relative = path.relative_to(self.mount_path)
        except ValueError:
            relative = path
        return relative.as_posix()

    def _iter_folders(self) -> Iterable[Path]:
        for root, dirs, files in os.walk(self.mount_path):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            if not files:
                continue
            video_present = any(Path(root, name).suffix.lower() in rules.VIDEO_EXTENSIONS for name in files)
            if not video_present:
                continue
            yield Path(root)

    def _apply_nfo(self, analysis: FolderAnalysis) -> None:
        for nfo_path in analysis.nfo_files:
            ids = guess.parse_nfo_identifiers(nfo_path)
            if ids:
                analysis.nfo_ids.update(ids)

    def _guess(self, analysis: FolderAnalysis) -> Dict[str, GuessResult]:
        folder_guess = guess.parse_folder_name(analysis.folder_path)
        if analysis.main_video is not None:
            video_guess = guess.parse_video_name(analysis.main_video.path)
        else:
            video_guess = GuessResult(raw={})
        return {"folder": folder_guess, "video": video_guess}

    def _verification(self, analysis: FolderAnalysis, guesses: Dict[str, GuessResult], verify_external: bool) -> VerificationSignals:
        settings = self.settings if verify_external else self.settings.clone_disabled_external()
        return collect_verification(
            folder_name=analysis.folder_path.name,
            title_guess=guesses["folder"],
            video_guess=guesses["video"],
            main_video=analysis.main_video,
            settings=settings,
        )

    def profile(self, *, verify_external: bool = False) -> StructureSummary:
        summary = StructureSummary()
        for folder_path in self._iter_folders():
            rel_path = self._relative(folder_path)
            analysis = rules.profile_folder(folder_path, rel_path=rel_path)
            self._apply_nfo(analysis)
            guesses = self._guess(analysis)
            verification = self._verification(analysis, guesses, verify_external)
            breakdown = score.compute_confidence(analysis, verification, self.settings)
            parsed_title = (
                guesses["video"].title
                or guesses["folder"].title
                or (analysis.canonical and folder_path.name.rsplit("(", 1)[0].strip())
            )
            parsed_year = (
                guesses["video"].year
                or guesses["folder"].year
                or analysis.detected_year
            )
            self._store_profile(
                analysis=analysis,
                breakdown=breakdown,
                verification=verification,
                parsed_title=parsed_title,
                parsed_year=parsed_year,
            )
            summary.processed += 1
            if analysis.kind == "movie":
                summary.movies += 1
            if breakdown.confidence >= self.settings.high_threshold:
                summary.confident += 1
            elif breakdown.confidence >= self.settings.low_threshold:
                summary.medium += 1
            else:
                summary.low += 1
        return summary

    def _store_profile(
        self,
        *,
        analysis: FolderAnalysis,
        breakdown: ConfidenceBreakdown,
        verification: VerificationSignals,
        parsed_title: Optional[str],
        parsed_year: Optional[int],
    ) -> None:
        folder_key = analysis.rel_path
        main_video = analysis.main_video.path if analysis.main_video else None
        main_video_rel = self._relative(main_video) if main_video else None
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.conn:
            assets_payload = dict(analysis.assets)
            if analysis.nfo_ids:
                assets_payload.setdefault("ids", dict(analysis.nfo_ids))
            self.conn.execute(
                """
                INSERT INTO folder_profile (
                    folder_path, kind, main_video_path, parsed_title, parsed_year,
                    assets_json, issues_json, confidence, source_signals_json, updated_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(folder_path) DO UPDATE SET
                    kind=excluded.kind,
                    main_video_path=excluded.main_video_path,
                    parsed_title=excluded.parsed_title,
                    parsed_year=excluded.parsed_year,
                    assets_json=excluded.assets_json,
                    issues_json=excluded.issues_json,
                    confidence=excluded.confidence,
                    source_signals_json=excluded.source_signals_json,
                    updated_utc=excluded.updated_utc
                """,
                (
                    folder_key,
                    analysis.kind,
                    main_video_rel,
                    parsed_title,
                    parsed_year,
                    json.dumps(assets_payload, ensure_ascii=False),
                    json.dumps(analysis.issues, ensure_ascii=False),
                    float(breakdown.confidence),
                    json.dumps(breakdown.signals, ensure_ascii=False),
                    now,
                ),
            )
            self.conn.execute(
                "DELETE FROM folder_candidates WHERE folder_path = ?",
                (folder_key,),
            )
            for candidate in verification.candidates:
                key = candidate.candidate_id or f"{candidate.source}:{candidate.title or candidate.extra.get('from', '')}"
                self.conn.execute(
                    """
                    INSERT OR REPLACE INTO folder_candidates (
                        folder_path, source, candidate_id, title, year, score, extra_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        folder_key,
                        candidate.source,
                        key,
                        candidate.title,
                        candidate.year,
                        float(candidate.score),
                        json.dumps(candidate.extra, ensure_ascii=False),
                    ),
                )
            if breakdown.confidence < self.settings.low_threshold:
                reasons = review.build_reasons(analysis, breakdown)
                questions = review.build_questions(analysis, verification)
                self.conn.execute(
                    """
                    INSERT INTO review_queue (folder_path, confidence, reasons_json, questions_json, created_utc)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(folder_path) DO UPDATE SET
                        confidence=excluded.confidence,
                        reasons_json=excluded.reasons_json,
                        questions_json=excluded.questions_json
                    """,
                    (
                        folder_key,
                        float(breakdown.confidence),
                        json.dumps(reasons, ensure_ascii=False),
                        json.dumps(questions, ensure_ascii=False),
                        now,
                    ),
                )
            else:
                self.conn.execute("DELETE FROM review_queue WHERE folder_path = ?", (folder_key,))

    def export_review(self, destination: Path) -> Dict[str, object]:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.conn:
            cursor = self.conn.execute(
                """
                SELECT rq.folder_path, rq.confidence, rq.reasons_json, rq.questions_json,
                       fp.kind, fp.parsed_title, fp.parsed_year, fp.issues_json
                FROM review_queue AS rq
                LEFT JOIN folder_profile AS fp ON fp.folder_path = rq.folder_path
                ORDER BY rq.confidence ASC
                """
            )
            rows = cursor.fetchall()
        payload: List[Dict[str, object]] = []
        for row in rows:
            try:
                reasons = json.loads(row["reasons_json"])
            except Exception:
                reasons = []
            try:
                questions = json.loads(row["questions_json"])
            except Exception:
                questions = []
            try:
                issues = json.loads(row["issues_json"])
            except Exception:
                issues = []
            payload.append(
                {
                    "folder_path": row["folder_path"],
                    "confidence": row["confidence"],
                    "kind": row["kind"],
                    "parsed_title": row["parsed_title"],
                    "parsed_year": row["parsed_year"],
                    "issues": issues,
                    "reasons": reasons,
                    "questions": questions,
                }
            )
        destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "exported": len(payload),
            "path": str(destination),
        }


__all__ = [
    "StructureProfiler",
    "StructureSettings",
    "StructureSummary",
    "ensure_structure_tables",
    "load_structure_settings",
]
