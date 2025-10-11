"""Orchestration for subtitle/plot cross-checking."""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np

from core.db import connect
from robust import CancellationToken
from structure.service import StructureSettings
from structure.tv_types import TVSettings

from .compare import ComparisonScores, aggregate_scores, cosine_similarity, keyword_overlap, named_entities
from .embed import SentenceEmbedder
from .plotsrc import PlotFetcher
from .subs import SubtitleSampleConfig, SubtitleSnippet, sample_from_video
from .summary import SummaryEngine

LOGGER = logging.getLogger("videocatalog.textverify")


@dataclass(slots=True)
class ModelConfig:
    embed: str = "paraphrase-multilingual-MiniLM-L12-v2"
    summarizer: str = "sshleifer/distilbart-cnn-12-6"


@dataclass(slots=True)
class WeightConfig:
    semantic: float = 0.55
    ner_overlap: float = 0.25
    keyword_overlap: float = 0.20


@dataclass(slots=True)
class ThresholdConfig:
    boost_strong: float = 0.80
    boost_medium: float = 0.65
    flag_diverge: float = 0.40


@dataclass(slots=True)
class TextVerifySettings:
    enable: bool = True
    targets: str = "low-medium"
    subs_sample: SubtitleSampleConfig = field(default_factory=SubtitleSampleConfig)
    summary_tokens: int = 120
    keywords_topk: int = 12
    models: ModelConfig = field(default_factory=ModelConfig)
    weights: WeightConfig = field(default_factory=WeightConfig)
    thresholds: ThresholdConfig = field(default_factory=ThresholdConfig)
    max_items_per_run: int = 200
    gentle_sleep_ms: int = 3
    gpu_allowed: bool = True

    @classmethod
    def from_mapping(cls, mapping: Dict[str, Any]) -> "TextVerifySettings":
        data = dict(mapping or {})
        subs_mapping = data.get("subs_sample") if isinstance(data.get("subs_sample"), dict) else {}
        defaults = SubtitleSampleConfig()
        subs_values = {
            key: subs_mapping.get(key, getattr(defaults, key))
            for key in ("max_lines", "head_lines", "mid_lines", "tail_lines", "max_chars")
        }
        subs = SubtitleSampleConfig(**subs_values)
        models_mapping = data.get("models") if isinstance(data.get("models"), dict) else {}
        models = ModelConfig(
            embed=str(models_mapping.get("embed", ModelConfig().embed) or ModelConfig().embed),
            summarizer=str(models_mapping.get("summarizer", ModelConfig().summarizer) or ModelConfig().summarizer),
        )
        weights_mapping = data.get("weights") if isinstance(data.get("weights"), dict) else {}
        weights = WeightConfig(
            semantic=float(weights_mapping.get("semantic", WeightConfig().semantic)),
            ner_overlap=float(weights_mapping.get("ner_overlap", WeightConfig().ner_overlap)),
            keyword_overlap=float(weights_mapping.get("keyword_overlap", WeightConfig().keyword_overlap)),
        )
        thresholds_mapping = data.get("thresholds") if isinstance(data.get("thresholds"), dict) else {}
        thresholds = ThresholdConfig(
            boost_strong=float(thresholds_mapping.get("boost_strong", ThresholdConfig().boost_strong)),
            boost_medium=float(thresholds_mapping.get("boost_medium", ThresholdConfig().boost_medium)),
            flag_diverge=float(thresholds_mapping.get("flag_diverge", ThresholdConfig().flag_diverge)),
        )
        return cls(
            enable=bool(data.get("enable", True)),
            targets=str(data.get("targets", "low-medium")),
            subs_sample=subs,
            summary_tokens=int(data.get("summary_tokens", 120) or 120),
            keywords_topk=int(data.get("keywords_topk", 12) or 12),
            models=models,
            weights=weights,
            thresholds=thresholds,
            max_items_per_run=int(data.get("max_items_per_run", 200) or 200),
            gentle_sleep_ms=int(data.get("gentle_sleep_ms", 3) or 3),
            gpu_allowed=bool(data.get("gpu_allowed", True)),
        )

    def with_overrides(self, **kwargs: Any) -> "TextVerifySettings":
        payload = {
            "enable": self.enable,
            "targets": self.targets,
            "subs_sample": {
                "max_lines": self.subs_sample.max_lines,
                "head_lines": self.subs_sample.head_lines,
                "mid_lines": self.subs_sample.mid_lines,
                "tail_lines": self.subs_sample.tail_lines,
                "max_chars": self.subs_sample.max_chars,
            },
            "summary_tokens": self.summary_tokens,
            "keywords_topk": self.keywords_topk,
            "models": {
                "embed": self.models.embed,
                "summarizer": self.models.summarizer,
            },
            "weights": {
                "semantic": self.weights.semantic,
                "ner_overlap": self.weights.ner_overlap,
                "keyword_overlap": self.weights.keyword_overlap,
            },
            "thresholds": {
                "boost_strong": self.thresholds.boost_strong,
                "boost_medium": self.thresholds.boost_medium,
                "flag_diverge": self.thresholds.flag_diverge,
            },
            "max_items_per_run": self.max_items_per_run,
            "gentle_sleep_ms": self.gentle_sleep_ms,
            "gpu_allowed": self.gpu_allowed,
        }
        for key, value in kwargs.items():
            payload[key] = value
        return TextVerifySettings.from_mapping(payload)


@dataclass(slots=True)
class TextVerifySummary:
    processed: int = 0
    boosted_strong: int = 0
    boosted_medium: int = 0
    flagged: int = 0
    skipped_no_plot: int = 0
    skipped_no_subs: int = 0
    elapsed_s: float = 0.0


@dataclass(slots=True)
class _Candidate:
    kind: str
    key: str
    rel_video_path: str
    confidence: float
    parsed_title: Optional[str]
    parsed_year: Optional[int]
    extras: Dict[str, Any]


def ensure_textverify_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS textverify_artifacts (
            path TEXT PRIMARY KEY,
            has_local_subs INTEGER,
            subs_lang TEXT,
            subs_lines_used INTEGER,
            summary TEXT,
            keywords TEXT,
            plot_source TEXT,
            plot_excerpt TEXT,
            semantic_sim REAL,
            ner_overlap REAL,
            keyword_overlap REAL,
            aggregated_score REAL,
            updated_utc TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS textverify_cache (
            path TEXT PRIMARY KEY,
            embed_vec BLOB,
            dim INTEGER,
            updated_utc TEXT NOT NULL
        )
        """
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TextVerifyRunner:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        settings: TextVerifySettings,
        mount_path: Path,
        structure_settings: Optional[StructureSettings] = None,
        tv_settings: Optional[TVSettings] = None,
        progress_callback: Optional[Any] = None,
        cancellation: Optional[CancellationToken] = None,
        gentle_sleep: float = 0.0,
    ) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        ensure_textverify_tables(self.conn)
        self.settings = settings
        self.mount_path = Path(mount_path)
        self.structure_settings = structure_settings
        self.tv_settings = tv_settings
        self.progress_callback = progress_callback
        self.cancellation = cancellation
        self.gentle_sleep = max(0.0, float(gentle_sleep))
        self._summary = SummaryEngine(
            model_name=settings.models.summarizer,
            target_tokens=settings.summary_tokens,
            allow_gpu=settings.gpu_allowed,
        )
        self._embedder = SentenceEmbedder(settings.models.embed, allow_gpu=settings.gpu_allowed)
        self._plot_fetcher = PlotFetcher(
            tmdb_api_key=(structure_settings.tmdb_api_key if structure_settings else None),
            imdb_enabled=bool(structure_settings.imdb_enabled if structure_settings else True),
            opensubs_api_key=(structure_settings.opensubtitles_api_key if structure_settings else None),
            opensubs_timeout=float(structure_settings.opensubtitles_timeout if structure_settings else 15.0)
            if structure_settings
            else 15.0,
        )
        self._tv_thresholds = None
        if tv_settings is not None:
            scoring = tv_settings.scoring
            self._tv_thresholds = (scoring.threshold_low, scoring.threshold_high)
        self._structure_thresholds = None
        if structure_settings is not None:
            self._structure_thresholds = (
                structure_settings.low_threshold,
                structure_settings.high_threshold,
            )

    def run(self, *, limit: Optional[int] = None) -> TextVerifySummary:
        start = time.monotonic()
        summary = TextVerifySummary()
        candidates = list(self._iter_candidates(limit=limit))
        total = len(candidates)
        LOGGER.info("Text verify processing %s items", total)
        for index, candidate in enumerate(candidates, start=1):
            if self._should_cancel():
                LOGGER.info("Text verify cancelled after %s items", summary.processed)
                break
            try:
                result = self._process_candidate(candidate)
            except Exception as exc:
                LOGGER.warning("Text verify failed for %s: %s", candidate.key, exc)
                continue
            summary.processed += 1
            if result.get("boost") == "strong":
                summary.boosted_strong += 1
            elif result.get("boost") == "medium":
                summary.boosted_medium += 1
            if result.get("flagged"):
                summary.flagged += 1
            if result.get("skipped_no_plot"):
                summary.skipped_no_plot += 1
            if result.get("skipped_no_subs"):
                summary.skipped_no_subs += 1
            self._emit_progress(index, total, candidate.key, result)
            if self.gentle_sleep:
                time.sleep(self.gentle_sleep)
        summary.elapsed_s = time.monotonic() - start
        return summary

    def _emit_progress(self, processed: int, total: int, key: str, result: Dict[str, Any]) -> None:
        if self.progress_callback is None:
            return
        payload = {
            "type": "textverify",
            "processed": processed,
            "total": total,
            "current": key,
            "result": result,
        }
        try:
            self.progress_callback(payload)
        except Exception:
            pass

    def _should_cancel(self) -> bool:
        token = self.cancellation
        if token is None:
            return False
        try:
            if hasattr(token, "is_cancelled"):
                return bool(token.is_cancelled())  # type: ignore[attr-defined]
            return bool(token.is_set())
        except Exception:
            return False

    def _iter_candidates(self, *, limit: Optional[int]) -> Iterable[_Candidate]:
        effective_limit = self.settings.max_items_per_run
        if limit is not None:
            effective_limit = min(effective_limit, int(limit))
        targets = (self.settings.targets or "low-medium").lower()
        low_threshold = 0.5
        high_threshold = 0.8
        if self._structure_thresholds:
            low_threshold, high_threshold = self._structure_thresholds
        max_conf = 1.0
        if targets == "low":
            max_conf = low_threshold
        elif targets == "low-medium":
            max_conf = high_threshold
        try:
            cursor = self.conn.execute(
                """
                SELECT folder_path, main_video_path, parsed_title, parsed_year, confidence, assets_json, kind
                FROM folder_profile
                WHERE kind = 'movie' AND main_video_path IS NOT NULL AND confidence < ?
                ORDER BY confidence ASC
                LIMIT ?
                """,
                (max_conf, effective_limit),
            )
        except sqlite3.DatabaseError as exc:
            LOGGER.debug("Movie query failed: %s", exc)
            return []
        for row in cursor.fetchall():
            extras: Dict[str, Any] = {}
            try:
                extras = json.loads(row["assets_json"]) if row["assets_json"] else {}
            except Exception:
                extras = {}
            yield _Candidate(
                kind="movie",
                key=row["folder_path"],
                rel_video_path=row["main_video_path"],
                confidence=float(row["confidence"] or 0.0),
                parsed_title=row["parsed_title"],
                parsed_year=row["parsed_year"],
                extras=extras,
            )
        # TV episodes
        try:
            cursor = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='tv_episode_profile'"
            )
            if cursor.fetchone() is None:
                return
        except sqlite3.DatabaseError:
            return
        tv_low, tv_high = (0.5, 0.8)
        if self._tv_thresholds:
            tv_low, tv_high = self._tv_thresholds
        if targets == "low":
            tv_limit = tv_low
        elif targets == "low-medium":
            tv_limit = tv_high
        else:
            tv_limit = 1.0
        try:
            cursor = self.conn.execute(
                """
                SELECT episode_path, confidence, parsed_title, ids_json, episode_numbers_json, season_number
                FROM tv_episode_profile
                WHERE confidence < ?
                ORDER BY confidence ASC
                LIMIT ?
                """,
                (tv_limit, effective_limit),
            )
        except sqlite3.DatabaseError as exc:
            LOGGER.debug("TV episode query failed: %s", exc)
            return
        for row in cursor.fetchall():
            extras = {}
            try:
                extras = json.loads(row["ids_json"]) if row["ids_json"] else {}
            except Exception:
                extras = {}
            extras["episode_numbers_json"] = row["episode_numbers_json"]
            extras["season_number"] = row["season_number"]
            yield _Candidate(
                kind="tv_episode",
                key=row["episode_path"],
                rel_video_path=row["episode_path"],
                confidence=float(row["confidence"] or 0.0),
                parsed_title=row["parsed_title"],
                parsed_year=None,
                extras=extras,
            )

    def _resolve_media_path(self, relative: str) -> Path:
        path = Path(relative)
        if path.is_absolute():
            return path
        return self.mount_path / path

    def _process_candidate(self, candidate: _Candidate) -> Dict[str, Any]:
        rel_path = candidate.rel_video_path
        abs_path = self._resolve_media_path(rel_path)
        result: Dict[str, Any] = {
            "path": rel_path,
            "kind": candidate.kind,
        }
        if not abs_path.exists():
            LOGGER.debug("Video missing for %s", abs_path)
            result["skipped_no_subs"] = True
            self._store_artifact(
                rel_path,
                has_subs=False,
                snippet=None,
                summary_text="",
                keywords=[],
                plot=None,
                scores=None,
            )
            try:
                self.conn.commit()
            except sqlite3.DatabaseError:
                pass
            return result
        snippet = sample_from_video(abs_path, self.settings.subs_sample)
        if snippet is None or not snippet.text.strip():
            result["skipped_no_subs"] = True
        plot = self._fetch_plot(candidate)
        if plot is None:
            result["skipped_no_plot"] = True
        if not snippet or not snippet.text.strip() or plot is None:
            self._store_artifact(
                rel_path,
                has_subs=bool(snippet and snippet.text.strip()),
                snippet=snippet,
                summary_text="",
                keywords=[],
                plot=plot,
                scores=None,
            )
            try:
                self.conn.commit()
            except sqlite3.DatabaseError:
                pass
            return result
        summary = self._summary.run(snippet.text, topk=self.settings.keywords_topk)
        plot_keywords = self._summary.keywords(plot.text, self.settings.keywords_topk)
        summary_embedding = self._embedder.encode(summary.summary or snippet.text)
        plot_embedding = self._embedder.encode(plot.text)
        semantic = cosine_similarity(summary_embedding.vector, plot_embedding.vector)
        ner_subs = named_entities(summary.summary or snippet.text)
        ner_plot = named_entities(plot.text)
        ner_score = 0.0
        if ner_subs and ner_plot:
            ner_score = len(ner_subs & ner_plot) / len(ner_subs | ner_plot)
        keyword_score = keyword_overlap(summary.keywords, plot_keywords)
        scores = aggregate_scores(
            semantic,
            ner_score,
            keyword_score,
            weight_semantic=self.settings.weights.semantic,
            weight_ner=self.settings.weights.ner_overlap,
            weight_keywords=self.settings.weights.keyword_overlap,
        )
        self._store_artifact(
            rel_path,
            has_subs=True,
            snippet=snippet,
            summary_text=summary.summary,
            keywords=summary.keywords,
            plot=plot,
            scores=scores,
        )
        boost = self._apply_confidence(candidate, scores)
        if boost:
            result["boost"] = boost
        if scores.aggregated < self.settings.thresholds.flag_diverge:
            self._flag_divergence(candidate, scores)
            result["flagged"] = True
        try:
            self.conn.commit()
        except sqlite3.DatabaseError:
            pass
        return result

    def _fetch_plot(self, candidate: _Candidate):
        if candidate.kind == "movie":
            ids = candidate.extras.get("ids", {}) if isinstance(candidate.extras, dict) else {}
            tmdb_id = ids.get("tmdb") if isinstance(ids, dict) else None
            imdb_id = ids.get("imdb") if isinstance(ids, dict) else None
            candidate_title = candidate.parsed_title
            candidate_year = candidate.parsed_year
            fallback = self._top_folder_candidate(candidate.key)
            if fallback:
                if not tmdb_id and fallback.get("source") == "tmdb":
                    tmdb_id = fallback.get("candidate_id")
                if not imdb_id and fallback.get("source") == "imdb":
                    imdb_id = fallback.get("candidate_id")
                if not candidate_title:
                    candidate_title = fallback.get("title")
                if candidate_year is None and fallback.get("year") is not None:
                    try:
                        candidate_year = int(fallback.get("year"))
                    except (TypeError, ValueError):
                        candidate_year = None
            return self._plot_fetcher.movie_plot(
                str(tmdb_id) if tmdb_id else None,
                candidate_title,
                candidate_year,
                imdb_id=str(imdb_id) if imdb_id else None,
            )
        ids_json = candidate.extras
        tmdb_series_id = None
        imdb_episode_id = None
        episode_numbers: Sequence[int] = []
        if isinstance(ids_json, dict):
            tmdb_series_id = (
                ids_json.get("tmdb_series_id")
                or ids_json.get("tmdb_show_id")
                or ids_json.get("tmdb_id")
            )
            imdb_episode_id = ids_json.get("imdb_episode_id") or ids_json.get("imdb_id")
            episodes_raw = ids_json.get("episode_numbers_json")
            if isinstance(episodes_raw, str):
                try:
                    parsed = json.loads(episodes_raw)
                except Exception:
                    parsed = []
            else:
                parsed = episodes_raw
            if isinstance(parsed, (list, tuple)):
                episode_numbers = []
                for num in parsed:
                    try:
                        episode_numbers.append(int(num))
                    except (TypeError, ValueError):
                        continue
            season_number = ids_json.get("season_number")
            if isinstance(season_number, str) and season_number.isdigit():
                season_number = int(season_number)
        else:
            season_number = None
        return self._plot_fetcher.episode_plot(
            tmdb_series_id=str(tmdb_series_id) if tmdb_series_id else None,
            season_number=int(season_number) if isinstance(season_number, int) else None,
            episode_numbers=episode_numbers,
            imdb_episode_id=str(imdb_episode_id) if imdb_episode_id else None,
        )

    def _top_folder_candidate(self, folder_path: str) -> Optional[Dict[str, Any]]:
        try:
            row = self.conn.execute(
                """
                SELECT source, candidate_id, title, year
                FROM folder_candidates
                WHERE folder_path = ?
                ORDER BY score DESC
                LIMIT 1
                """,
                (folder_path,),
            ).fetchone()
        except sqlite3.DatabaseError:
            return None
        if not row:
            return None
        return {
            "source": row["source"],
            "candidate_id": row["candidate_id"],
            "title": row["title"],
            "year": row["year"],
        }

    def _store_artifact(
        self,
        path: str,
        *,
        has_subs: bool,
        snippet: Optional[SubtitleSnippet],
        summary_text: str,
        keywords: Sequence[str],
        plot,
        scores: Optional[ComparisonScores],
    ) -> None:
        keywords_payload = json.dumps(list(keywords), ensure_ascii=False)
        plot_source = plot.source if plot else "none"
        plot_excerpt = plot.excerpt if plot else ""
        semantic = scores.semantic if scores else 0.0
        ner = scores.ner_overlap if scores else 0.0
        kw = scores.keyword_overlap if scores else 0.0
        agg = scores.aggregated if scores else 0.0
        self.conn.execute(
            """
            INSERT INTO textverify_artifacts (
                path, has_local_subs, subs_lang, subs_lines_used, summary, keywords,
                plot_source, plot_excerpt, semantic_sim, ner_overlap, keyword_overlap,
                aggregated_score, updated_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                has_local_subs=excluded.has_local_subs,
                subs_lang=excluded.subs_lang,
                subs_lines_used=excluded.subs_lines_used,
                summary=excluded.summary,
                keywords=excluded.keywords,
                plot_source=excluded.plot_source,
                plot_excerpt=excluded.plot_excerpt,
                semantic_sim=excluded.semantic_sim,
                ner_overlap=excluded.ner_overlap,
                keyword_overlap=excluded.keyword_overlap,
                aggregated_score=excluded.aggregated_score,
                updated_utc=excluded.updated_utc
            """,
            (
                path,
                1 if has_subs else 0,
                snippet.language if snippet else None,
                snippet.lines_used if snippet else None,
                (summary_text or "")[:1200],
                keywords_payload,
                plot_source,
                (plot_excerpt or "")[:1200],
                float(semantic),
                float(ner),
                float(kw),
                float(agg),
                _utc_now(),
            ),
        )

    def _apply_confidence(self, candidate: _Candidate, scores: ComparisonScores) -> Optional[str]:
        thresholds = self.settings.thresholds
        delta = 0.0
        boost_label: Optional[str] = None
        if scores.aggregated >= thresholds.boost_strong:
            delta = 0.15
            boost_label = "strong"
        elif scores.aggregated >= thresholds.boost_medium:
            delta = 0.08
            boost_label = "medium"
        if delta <= 0:
            return None
        if candidate.kind == "movie":
            self.conn.execute(
                "UPDATE folder_profile SET confidence = MIN(1.0, confidence + ?) WHERE folder_path = ?",
                (delta, candidate.key),
            )
            self.conn.execute(
                "UPDATE review_queue SET confidence = MIN(1.0, confidence + ?) WHERE folder_path = ?",
                (delta, candidate.key),
            )
        else:
            self.conn.execute(
                "UPDATE tv_episode_profile SET confidence = MIN(1.0, confidence + ?) WHERE episode_path = ?",
                (delta, candidate.key),
            )
            self.conn.execute(
                "UPDATE tv_review_queue SET confidence = MIN(1.0, confidence + ?) WHERE item_key = ?",
                (delta, candidate.key),
            )
        return boost_label

    def _flag_divergence(self, candidate: _Candidate, scores: ComparisonScores) -> None:
        reason = "plot/subtitle mismatch flagged by textverify"
        if candidate.kind == "movie":
            row = self.conn.execute(
                "SELECT reasons_json FROM review_queue WHERE folder_path = ?",
                (candidate.key,),
            ).fetchone()
            if row:
                try:
                    reasons = json.loads(row["reasons_json"]) if row["reasons_json"] else []
                except Exception:
                    reasons = []
                if reason not in reasons:
                    reasons.append(reason)
                self.conn.execute(
                    "UPDATE review_queue SET reasons_json = ? WHERE folder_path = ?",
                    (json.dumps(reasons, ensure_ascii=False), candidate.key),
                )
            else:
                self.conn.execute(
                    "INSERT OR IGNORE INTO review_queue (folder_path, confidence, reasons_json, questions_json, created_utc) VALUES (?, ?, ?, ?, ?)",
                    (
                        candidate.key,
                        float(candidate.confidence),
                        json.dumps([reason], ensure_ascii=False),
                        json.dumps([], ensure_ascii=False),
                        _utc_now(),
                    ),
                )
        else:
            row = self.conn.execute(
                "SELECT reasons_json FROM tv_review_queue WHERE item_key = ?",
                (candidate.key,),
            ).fetchone()
            if row:
                try:
                    reasons = json.loads(row["reasons_json"]) if row["reasons_json"] else []
                except Exception:
                    reasons = []
                if reason not in reasons:
                    reasons.append(reason)
                self.conn.execute(
                    "UPDATE tv_review_queue SET reasons_json = ? WHERE item_key = ?",
                    (json.dumps(reasons, ensure_ascii=False), candidate.key),
                )
            else:
                self.conn.execute(
                    "INSERT OR IGNORE INTO tv_review_queue (item_type, item_key, confidence, reasons_json, questions_json, created_utc) VALUES ('episode', ?, ?, ?, ?, ?)",
                    (
                        candidate.key,
                        float(candidate.confidence),
                        json.dumps([reason], ensure_ascii=False),
                        json.dumps([], ensure_ascii=False),
                        _utc_now(),
                    ),
                )


def run_for_shard(
    shard_path: Path,
    *,
    settings: TextVerifySettings,
    mount_path: Path,
    structure_settings: Optional[StructureSettings] = None,
    tv_settings: Optional[TVSettings] = None,
    progress_callback: Optional[Any] = None,
    cancellation: Optional[CancellationToken] = None,
    gentle_sleep: float = 0.0,
    limit: Optional[int] = None,
) -> TextVerifySummary:
    conn = connect(shard_path, read_only=False, check_same_thread=False)
    try:
        runner = TextVerifyRunner(
            conn,
            settings=settings,
            mount_path=mount_path,
            structure_settings=structure_settings,
            tv_settings=tv_settings,
            progress_callback=progress_callback,
            cancellation=cancellation,
            gentle_sleep=gentle_sleep,
        )
        return runner.run(limit=limit)
    finally:
        conn.close()


__all__ = [
    "TextVerifyRunner",
    "TextVerifySettings",
    "TextVerifySummary",
    "ensure_textverify_tables",
    "run_for_shard",
]
