"""Smoke tests for VideoCatalog subsystems."""
from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

# Optional dependency for visual smoke test
try:  # pragma: no cover - optional dependency
    from PIL import Image
except Exception:  # pragma: no cover - optional dependency
    Image = None  # type: ignore[assignment]

from core.paths import get_catalog_db_path, resolve_working_dir
from core.settings import load_settings

from .logs import EVENT_RANGES, log_event, persist_snapshot


@dataclass(slots=True)
class SmokeCheck:
    name: str
    ok: bool
    severity: str
    message: str
    hint: Optional[str] = None
    duration_ms: float = 0.0
    data: Dict[str, Any] = field(default_factory=dict)


DEFAULT_SUBSYSTEMS = [
    "movies_structure",
    "tv_structure",
    "apiguard_cache",
    "visualreview",
    "docs_textlite",
    "assistant_tools",
]


def _load_timeout(settings: Dict[str, Any], key: str, default: float = 10.0) -> float:
    diag = settings.get("diagnostics") if isinstance(settings.get("diagnostics"), dict) else {}
    timeouts = diag.get("smoke_timeouts_s") if isinstance(diag.get("smoke_timeouts_s"), dict) else {}
    value = timeouts.get(key, default)
    try:
        numeric = float(value)
        return numeric if numeric > 0 else default
    except Exception:
        return default


def _sample_size(settings: Dict[str, Any], key: str, default: int = 5) -> int:
    diag = settings.get("diagnostics") if isinstance(settings.get("diagnostics"), dict) else {}
    samples = diag.get("sample_sizes") if isinstance(diag.get("sample_sizes"), dict) else {}
    try:
        return max(1, int(samples.get(key, default)))
    except Exception:
        return default


def _movies_structure(settings: Dict[str, Any], working_dir: Path) -> SmokeCheck:
    db_path = get_catalog_db_path(working_dir)
    if not db_path.exists():
        return SmokeCheck(
            name="movies_structure",
            ok=True,
            severity="INFO",
            message="Catalog DB missing; movies smoke skipped.",
        )
    sample = _sample_size(settings, "movies")
    start = time.perf_counter()
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error as exc:
        return SmokeCheck(
            name="movies_structure",
            ok=False,
            severity="MAJOR",
            message="Failed to open catalog for movies smoke test",
            hint=str(exc),
        )
    try:
        try:
            rows = conn.execute(
                "SELECT folder_path, confidence FROM folder_profile ORDER BY updated_utc DESC LIMIT ?",
                (sample,),
            ).fetchall()
        except sqlite3.DatabaseError:
            rows = []
        if not rows:
            return SmokeCheck(
                name="movies_structure",
                ok=True,
                severity="INFO",
                message="No folder_profile rows yet",
                duration_ms=(time.perf_counter() - start) * 1000,
            )
        confidences = [row[1] for row in rows if row[1] is not None]
        avg_conf = sum(confidences) / len(confidences) if confidences else None
        low = [row[0] for row in rows if row[1] is not None and row[1] < 0.5]
        ok = len(low) < max(1, len(rows) // 2)
        return SmokeCheck(
            name="movies_structure",
            ok=ok,
            severity="INFO" if ok else "MINOR",
            message="Movies profile healthy" if ok else "Many low-confidence movie folders",
            hint=None if ok else "Review structure queue for low-confidence movies.",
            duration_ms=(time.perf_counter() - start) * 1000,
            data={"rows": len(rows), "low_conf": len(low), "avg_conf": avg_conf},
        )
    finally:
        conn.close()


def _tv_structure(settings: Dict[str, Any], working_dir: Path) -> SmokeCheck:
    db_path = get_catalog_db_path(working_dir)
    if not db_path.exists():
        return SmokeCheck(
            name="tv_structure",
            ok=True,
            severity="INFO",
            message="Catalog DB missing; TV smoke skipped.",
        )
    sample = _sample_size(settings, "tv")
    start = time.perf_counter()
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error as exc:
        return SmokeCheck(
            name="tv_structure",
            ok=False,
            severity="MAJOR",
            message="Failed to open catalog for TV smoke test",
            hint=str(exc),
        )
    try:
        try:
            rows = conn.execute(
                "SELECT episode_path, season_number, episode_numbers_json, confidence FROM tv_episode_profile ORDER BY updated_utc DESC LIMIT ?",
                (sample,),
            ).fetchall()
        except sqlite3.DatabaseError:
            rows = []
        if not rows:
            return SmokeCheck(
                name="tv_structure",
                ok=True,
                severity="INFO",
                message="No tv_episode_profile rows yet",
                duration_ms=(time.perf_counter() - start) * 1000,
            )
        inconsistent = 0
        for row in rows:
            try:
                payload = json.loads(row[2] or "[]")
            except json.JSONDecodeError:
                inconsistent += 1
                continue
            if not payload:
                inconsistent += 1
        ok = inconsistent == 0
        return SmokeCheck(
            name="tv_structure",
            ok=ok,
            severity="INFO" if ok else "MINOR",
            message="TV episodes look consistent" if ok else "TV episodes missing numbering",
            hint=None if ok else "Re-run structure profiler for affected shows.",
            duration_ms=(time.perf_counter() - start) * 1000,
            data={"rows": len(rows), "inconsistent": inconsistent},
        )
    finally:
        conn.close()


def _apiguard_cache(settings: Dict[str, Any], working_dir: Path) -> SmokeCheck:
    cache_path = working_dir / "cache" / "tmdb_cache.json"
    if not cache_path.exists():
        return SmokeCheck(
            name="apiguard_cache",
            ok=True,
            severity="INFO",
            message="TMDB cache empty",
        )
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return SmokeCheck(
            name="apiguard_cache",
            ok=False,
            severity="MINOR",
            message="TMDB cache unreadable",
            hint=str(exc),
        )
    entries = payload.get("entries") if isinstance(payload, dict) else {}
    ok = isinstance(entries, dict)
    cache_size = len(entries) if isinstance(entries, dict) else 0
    return SmokeCheck(
        name="apiguard_cache",
        ok=ok,
        severity="INFO" if ok else "MINOR",
        message=f"TMDB cache has {cache_size} entries" if ok else "TMDB cache corrupted",
        hint=None if ok else "Delete cache/tmdb_cache.json and retry",
        data={"cache_size": cache_size},
    )


def _visualreview(settings: Dict[str, Any], working_dir: Path) -> SmokeCheck:
    if Image is None:
        return SmokeCheck(
            name="visualreview",
            ok=False,
            severity="MINOR",
            message="Pillow not available for visual smoke test",
            hint="Install pillow package.",
        )
    timeout = _load_timeout(settings, "visual", 10.0)
    start = time.perf_counter()
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=64x64:d=0.2",
        "-frames:v",
        "1",
        "-f",
        "image2pipe",
        "-vcodec",
        "png",
        "-",
    ]
    try:
        proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    except FileNotFoundError:
        return SmokeCheck(
            name="visualreview",
            ok=False,
            severity="MINOR",
            message="ffmpeg not available for visual review",
            hint="Install ffmpeg to enable visual review features.",
        )
    except subprocess.TimeoutExpired:
        return SmokeCheck(
            name="visualreview",
            ok=False,
            severity="MINOR",
            message="ffmpeg timed out generating preview",
            hint="Check ffmpeg installation and GPU availability.",
        )
    if proc.returncode != 0 or not proc.stdout:
        return SmokeCheck(
            name="visualreview",
            ok=False,
            severity="MINOR",
            message="ffmpeg failed to produce preview frame",
            hint=proc.stderr.decode("utf-8", "ignore")[:200],
        )
    try:
        image = Image.open(io.BytesIO(proc.stdout))
        contact = Image.new("RGB", image.size, color=(20, 20, 20))
        contact.paste(image, (0, 0))
    except Exception as exc:
        return SmokeCheck(
            name="visualreview",
            ok=False,
            severity="MINOR",
            message="Failed to assemble contact sheet",
            hint=str(exc),
        )
    return SmokeCheck(
        name="visualreview",
        ok=True,
        severity="INFO",
        message="Visual review ffmpeg pipeline ok",
        duration_ms=(time.perf_counter() - start) * 1000,
        data={"frame_size": image.size},
    )


def _docs_textlite(settings: Dict[str, Any], working_dir: Path) -> SmokeCheck:
    sample = _sample_size(settings, "docs", 3)
    start = time.perf_counter()
    conn = sqlite3.connect(":memory:")
    from textlite.store import PreviewRow, ensure_tables, upsert_many

    ensure_tables(conn)
    rows = [
        PreviewRow(path=f"diagnostics://sample/{idx}", kind="text", bytes_sampled=42, lines_sampled=5, summary="ok", keywords=["demo"], schema_json=None)
        for idx in range(sample)
    ]
    try:
        upsert_many(conn, rows)
        cur = conn.execute("SELECT COUNT(*) FROM textlite_preview")
        count = cur.fetchone()[0]
        fts_count = conn.execute("SELECT COUNT(*) FROM textlite_fts").fetchone()[0]
        ok = count == sample and fts_count == sample
        return SmokeCheck(
            name="docs_textlite",
            ok=ok,
            severity="INFO" if ok else "MINOR",
            message="TextLite preview pipeline ok" if ok else "TextLite FTS mismatch",
            hint=None if ok else "Check textlite_preview/textlite_fts tables",
            duration_ms=(time.perf_counter() - start) * 1000,
            data={"count": count, "fts": fts_count},
        )
    finally:
        conn.close()


def _assistant_tools(settings: Dict[str, Any], working_dir: Path) -> SmokeCheck:
    db_path = get_catalog_db_path(working_dir)
    if not db_path.exists():
        return SmokeCheck(
            name="assistant_tools",
            ok=True,
            severity="INFO",
            message="Catalog DB missing; assistant tool smoke skipped.",
        )
    start = time.perf_counter()
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error as exc:
        return SmokeCheck(
            name="assistant_tools",
            ok=False,
            severity="MAJOR",
            message="Failed to open catalog for assistant tools",
            hint=str(exc),
        )
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "tv_episode_profile" not in tables:
            return SmokeCheck(
                name="assistant_tools",
                ok=True,
                severity="INFO",
                message="assistant low-confidence query skipped (table missing)",
                duration_ms=(time.perf_counter() - start) * 1000,
            )
        rows = conn.execute(
            "SELECT COUNT(*) FROM tv_episode_profile WHERE confidence IS NULL OR confidence < 0.4"
        ).fetchone()
        low_conf = rows[0] if rows else 0
        ok = True
        severity = "INFO" if low_conf < 5 else "MINOR"
        message = "Assistant tool query ok" if severity == "INFO" else "Assistant reports many low-confidence episodes"
        return SmokeCheck(
            name="assistant_tools",
            ok=ok,
            severity=severity,
            message=message,
            duration_ms=(time.perf_counter() - start) * 1000,
            data={"low_conf": int(low_conf)},
        )
    finally:
        conn.close()


_TEST_REGISTRY: Dict[str, Callable[[Dict[str, Any], Path], SmokeCheck]] = {
    "movies_structure": _movies_structure,
    "tv_structure": _tv_structure,
    "apiguard_cache": _apiguard_cache,
    "visualreview": _visualreview,
    "docs_textlite": _docs_textlite,
    "assistant_tools": _assistant_tools,
}


def run_smoke_tests(
    subsystems: Optional[Iterable[str]] = None,
    *,
    budget: Optional[int] = None,
    working_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    resolved_working_dir = working_dir or resolve_working_dir()
    settings = load_settings(resolved_working_dir) or {}
    selected = [name for name in (subsystems or DEFAULT_SUBSYSTEMS) if name in _TEST_REGISTRY]
    if budget is not None:
        selected = selected[: max(1, int(budget))]

    results: List[SmokeCheck] = []
    for index, name in enumerate(selected):
        test = _TEST_REGISTRY[name]
        timeout = _load_timeout(settings, name.split("_")[0] if "_" in name else name)
        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(test, settings, resolved_working_dir)
            try:
                check = future.result(timeout=timeout)
            except TimeoutError:
                check = SmokeCheck(
                    name=name,
                    ok=False,
                    severity="MINOR",
                    message=f"Smoke test {name} timed out",
                    hint=f"Increase diagnostics.smoke_timeouts_s.{name} if necessary",
                )
            except Exception as exc:  # pragma: no cover - defensive
                check = SmokeCheck(
                    name=name,
                    ok=False,
                    severity="MAJOR",
                    message=f"Smoke test {name} crashed",
                    hint=str(exc),
                )
        if check.duration_ms <= 0:
            check.duration_ms = (time.perf_counter() - start) * 1000
        results.append(check)
        log_event(
            event_id=EVENT_RANGES["smoke"][0] + index,
            level="INFO" if check.ok else "WARNING",
            module="diagnostics.smoke",
            op=name,
            duration_ms=check.duration_ms,
            ok=check.ok,
            hint=check.hint,
            extra=check.data,
            working_dir=resolved_working_dir,
        )

    summary = {"MAJOR": 0, "MINOR": 0, "INFO": 0}
    for check in results:
        if check.ok:
            continue
        severity = check.severity.upper()
        summary[severity] = summary.get(severity, 0) + 1

    payload = {
        "ts": time.time(),
        "summary": summary,
        "checks": [
            {
                "name": check.name,
                "ok": check.ok,
                "severity": check.severity,
                "message": check.message,
                "hint": check.hint,
                "duration_ms": check.duration_ms,
                "data": check.data,
            }
            for check in results
        ],
    }
    persist_snapshot("smoke", payload, working_dir=resolved_working_dir)
    return payload


__all__ = ["run_smoke_tests", "DEFAULT_SUBSYSTEMS"]
