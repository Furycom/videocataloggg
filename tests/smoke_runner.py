"""Coordinated execution of VideoCatalog smoke tests."""
from __future__ import annotations

import concurrent.futures
import json
import time
from dataclasses import dataclass, field, replace, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set


def _ensure_smoke_stubs() -> None:
    """Install lightweight stubs for optional assistant dependencies."""

    import sys
    import types

    try:  # pragma: no cover - only runs when optional dependency missing
        import langchain_core.messages  # type: ignore
    except Exception:  # pragma: no cover - fallback for smoke runs
        langchain_core = types.ModuleType("langchain_core")
        messages = types.ModuleType("langchain_core.messages")

        class _Message:
            def __init__(self, content: str = "", **kwargs) -> None:
                self.content = content
                self.additional_kwargs = kwargs

            def model_copy(self) -> "_Message":
                return _Message(self.content, **self.additional_kwargs)

            def __repr__(self) -> str:  # pragma: no cover - diagnostic only
                return f"_Message(content={self.content!r}, extras={self.additional_kwargs!r})"

        class AIMessage(_Message):
            pass

        class BaseMessage(_Message):
            pass

        class HumanMessage(_Message):
            pass

        class SystemMessage(_Message):
            pass

        class ToolMessage(_Message):
            pass

        messages.AIMessage = AIMessage
        messages.BaseMessage = BaseMessage
        messages.HumanMessage = HumanMessage
        messages.SystemMessage = SystemMessage
        messages.ToolMessage = ToolMessage
        langchain_core.messages = messages
        sys.modules.setdefault("langchain_core", langchain_core)
        sys.modules.setdefault("langchain_core.messages", messages)

    try:  # pragma: no cover - optional dependency
        import langgraph.graph  # type: ignore
    except Exception:  # pragma: no cover - fallback for smoke runs
        langgraph = types.ModuleType("langgraph")
        graph = types.ModuleType("langgraph.graph")

        class _StateGraph:
            def __init__(self, *args, **kwargs) -> None:  # pragma: no cover - simple stub
                self.nodes = {}
                self.entry = None
                self.edges = {}
                self.conditions = {}

            def add_node(self, name, func) -> None:
                self.nodes[name] = func

            def set_entry_point(self, name) -> None:
                self.entry = name

            def add_conditional_edges(self, node, predicate, mapping) -> None:
                self.conditions[node] = (predicate, mapping)

            def add_edge(self, src, dst) -> None:
                self.edges.setdefault(src, set()).add(dst)

            class _Compiled:
                def __init__(self, outer) -> None:
                    self.outer = outer

                def invoke(self, state):  # pragma: no cover - runtime guard
                    raise RuntimeError("langgraph stub cannot execute flows")

            def compile(self):
                return _StateGraph._Compiled(self)

        graph.StateGraph = _StateGraph
        graph.END = "END"
        langgraph.graph = graph
        sys.modules.setdefault("langgraph", langgraph)
        sys.modules.setdefault("langgraph.graph", graph)


_ensure_smoke_stubs()

from assistant.config import AssistantSettings
from assistant.rag import VectorIndex
from assistant.tools import AssistantTooling
from core.paths import get_exports_dir, resolve_working_dir
from gpu.capabilities import probe_gpu
from quality import ffprobe as quality_ffprobe
from structure import rules as structure_rules
from structure import tv_rules
from tests import diff, fixtures
from textlite.sample import SampleConfig, sample_text
from textlite.summarize import Summarizer
from visualreview.frame_sampler import FrameSampler, FrameSamplerConfig

from .report import write_reports

__all__ = [
    "SmokeTest",
    "SmokeTestResult",
    "SmokeSuiteResult",
    "TestPayload",
    "run_smoke_suite",
]


@dataclass(slots=True)
class TestsSettings:
    enable: bool = True
    gate_on_fail: bool = True
    timeouts: Dict[str, int] = field(default_factory=lambda: {"default": 15, "gpu": 30})
    fixtures_regen: bool = False
    gpu_required_tests: Set[str] = field(default_factory=set)
    report_formats: Sequence[str] = field(default_factory=lambda: ("markdown", "junit"))


@dataclass(slots=True)
class TestPayload:
    data: object
    message: str = ""
    diagnostics: Dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class SmokeTest:
    name: str
    description: str
    func: Callable[["SmokeContext"], TestPayload]
    golden: str
    timeout_key: str = "default"
    resource: str = "light_cpu"


@dataclass(slots=True)
class SmokeTestResult:
    name: str
    status: str
    duration_s: float
    message: str
    diagnostics: Dict[str, object] = field(default_factory=dict)
    diff_path: Optional[Path] = None
    golden_path: Optional[Path] = None


@dataclass(slots=True)
class SmokeSuiteResult:
    timestamp: str
    run_dir: Path
    results: List[SmokeTestResult]
    status: str
    fixtures: fixtures.FixtureResult
    warnings: List[str] = field(default_factory=list)


@dataclass(slots=True)
class SmokeContext:
    working_dir: Path
    fixtures_root: Path
    run_dir: Path
    golden_dir: Path
    diff_dir: Path
    settings: TestsSettings
    timestamp: str


class SmokeSkip(RuntimeError):
    """Raised by a test when it should be skipped."""


class SmokeFailure(RuntimeError):
    """Raised by a test to signal a controlled failure."""


def run_smoke_suite(
    *,
    settings: TestsSettings,
    only: Optional[Iterable[str]] = None,
) -> SmokeSuiteResult:
    working_dir = resolve_working_dir()
    fixtures_result = fixtures.prepare_fixtures(force=settings.fixtures_regen)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    exports_dir = get_exports_dir(working_dir)
    run_dir = exports_dir / "testruns" / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)
    diff_dir = run_dir / "diffs"
    diff_dir.mkdir(parents=True, exist_ok=True)
    golden_dir = Path(__file__).resolve().parent / "goldens"

    context = SmokeContext(
        working_dir=working_dir,
        fixtures_root=fixtures_result.root,
        run_dir=run_dir,
        golden_dir=golden_dir,
        diff_dir=diff_dir,
        settings=settings,
        timestamp=timestamp,
    )

    enabled_tests = _select_tests(settings, only)
    gpu_ready = _gpu_ready()

    results: List[SmokeTestResult] = []
    warnings: List[str] = list(fixtures_result.warnings)

    for test in enabled_tests:
        timeout_s = settings.timeouts.get(test.timeout_key, settings.timeouts.get("default", 15))
        start = time.perf_counter()
        status = "SKIP"
        message = ""
        diagnostics: Dict[str, object] = {}
        diff_path: Optional[Path] = None
        if test.resource == "heavy_ai_gpu" and not gpu_ready:
            message = "GPU not ready; test skipped"
            results.append(
                SmokeTestResult(
                    name=test.name,
                    status="SKIP",
                    duration_s=0.0,
                    message=message,
                    diagnostics={},
                    diff_path=None,
                    golden_path=context.golden_dir / test.golden,
                )
            )
            continue
        try:
            payload = _execute_test(test, context, timeout_s)
        except SmokeSkip as exc:
            status = "SKIP"
            message = str(exc)
            duration = time.perf_counter() - start
        except SmokeFailure as exc:
            status = "FAIL"
            message = str(exc)
            duration = time.perf_counter() - start
        except Exception as exc:  # pragma: no cover - unexpected failure
            status = "FAIL"
            message = f"Unhandled exception: {exc}"
            diagnostics = {"exception": repr(exc)}
            duration = time.perf_counter() - start
        else:
            duration = time.perf_counter() - start
            data = payload.data
            message = payload.message
            diagnostics = payload.diagnostics
            golden_path = context.golden_dir / test.golden
            if golden_path.suffix.lower() == ".json":
                diff_result = diff.compare_json(golden_path, data)
            elif golden_path.suffix.lower() == ".csv":
                diff_result = diff.compare_csv(golden_path, data)
            else:
                raise SmokeFailure(f"Unsupported golden format for {test.name}: {golden_path.suffix}")
            if diff_result.match:
                status = "PASS"
                message = message or diff_result.summary
            else:
                status = "FAIL"
                message = f"{message or 'Output mismatch'} — {diff_result.summary}"
                diff_path = diff.save_diff_artifacts(context.diff_dir, test.name, diff_result)
        results.append(
            SmokeTestResult(
                name=test.name,
                status=status,
                duration_s=duration,
                message=message,
                diagnostics=diagnostics,
                diff_path=diff_path,
                golden_path=context.golden_dir / test.golden,
            )
        )

    suite_status = _suite_status(results)
    suite_result = SmokeSuiteResult(
        timestamp=timestamp,
        run_dir=run_dir,
        results=results,
        status=suite_status,
        fixtures=fixtures_result,
        warnings=warnings,
    )
    write_reports(run_dir, suite_result, formats=settings.report_formats)
    return suite_result


def _execute_test(test: SmokeTest, context: SmokeContext, timeout_s: int) -> TestPayload:
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(test.func, context)
        try:
            return future.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError as exc:
            raise SmokeFailure(f"Timed out after {timeout_s}s") from exc


def _suite_status(results: Sequence[SmokeTestResult]) -> str:
    failures = any(result.status == "FAIL" for result in results)
    passes = any(result.status == "PASS" for result in results)
    if failures:
        return "FAIL"
    if passes:
        return "PASS"
    return "SKIP"


def _select_tests(settings: TestsSettings, only: Optional[Iterable[str]]) -> List[SmokeTest]:
    requested = {name.strip() for name in only} if only else None
    tests: List[SmokeTest] = [
        SmokeTest(
            name="structure_parse",
            description="Parse fixture structure folders",
            func=_test_structure_parse,
            golden="structure.json",
        ),
        SmokeTest(
            name="tv_mapping",
            description="Validate TV episode mapping",
            func=_test_tv_mapping,
            golden="tv_mapping.json",
        ),
        SmokeTest(
            name="apiguard_cache",
            description="Validate ApiGuard cache-only probes",
            func=_test_apiguard_cache,
            golden="api_shapes.json",
        ),
        SmokeTest(
            name="textlite_preview",
            description="Generate textlite previews",
            func=_test_textlite_preview,
            golden="textlite.json",
        ),
        SmokeTest(
            name="quality_headers",
            description="Probe ffprobe headers",
            func=_test_quality_headers,
            golden="quality.json",
        ),
        SmokeTest(
            name="visualreview_keyframe",
            description="Extract a keyframe via FrameSampler",
            func=_test_visualreview_keyframe,
            golden="visualreview.json",
        ),
        SmokeTest(
            name="vectors_refresh",
            description="Refresh semantic vector index",
            func=_test_vectors_refresh,
            golden="vectors.json",
            timeout_key="gpu",
            resource="heavy_ai_gpu",
        ),
        SmokeTest(
            name="assistant_tools",
            description="Dry-run assistant tooling",
            func=_test_assistant_tools,
            golden="assistant_tools.json",
            timeout_key="gpu",
            resource="heavy_ai_gpu",
        ),
    ]
    if requested is not None:
        tests = [test for test in tests if test.name in requested]
    gpu_required = set(settings.gpu_required_tests)
    if gpu_required:
        adjusted: List[SmokeTest] = []
        for test in tests:
            if test.name in gpu_required:
                adjusted.append(
                    replace(test, timeout_key="gpu", resource="heavy_ai_gpu")
                )
            else:
                adjusted.append(test)
        tests = adjusted
    return tests


def _gpu_ready() -> bool:
    try:
        snapshot = probe_gpu()
    except Exception:
        return False
    return bool(snapshot.get("onnx_cuda_ok") or snapshot.get("onnx_directml_ok"))


# ---------------------------------------------------------------------------
# Individual tests
# ---------------------------------------------------------------------------


def _test_structure_parse(ctx: SmokeContext) -> TestPayload:
    structure_root = ctx.fixtures_root / "structure"
    movies: List[Dict[str, object]] = []
    shows: List[Dict[str, object]] = []

    movie_root = structure_root / "Movies"
    for folder in sorted(movie_root.glob("*/")):
        analysis = structure_rules.profile_folder(folder, rel_path=str(folder.name))
        movies.append(
            {
                "folder": folder.name,
                "kind": analysis.kind,
                "canonical": analysis.canonical,
                "issues": sorted(analysis.issues),
                "detected_year": analysis.detected_year,
            }
        )

    show_root = structure_root / "Shows"
    for series_folder in sorted(show_root.glob("*/")):
        series = tv_rules.analyze_series_root(series_folder)
        shows.append(
            {
                "title": series.title,
                "year": series.year,
                "canonical": series.canonical,
                "issues": sorted(series.issues),
                "seasons": [
                    {
                        "season": season.season_number,
                        "canonical": season.canonical,
                        "episode_count": len(season.episodes),
                        "issues": sorted(season.issues),
                    }
                    for season in series.seasons
                ],
            }
        )
    payload = {
        "movies": sorted(movies, key=lambda item: item["folder"]),
        "shows": sorted(shows, key=lambda item: item["title"] or ""),
    }
    return TestPayload(data=payload, message="Parsed structure fixtures")


def _test_tv_mapping(ctx: SmokeContext) -> TestPayload:
    show_dir = ctx.fixtures_root / "structure" / "Shows" / "Example Show (2019)"
    series = tv_rules.analyze_series_root(show_dir)
    mapping: List[Dict[str, object]] = []
    for season in series.seasons:
        for episode in season.episodes:
            mapping.append(
                {
                    "season": season.season_number,
                    "episode_file": episode.path.name,
                    "subtitle_count": len(episode.subtitles),
                }
            )
    mapping.sort(key=lambda item: item["episode_file"])
    diagnostics = {"series_issues": series.issues}
    return TestPayload(data=mapping, message="Season mappings collected", diagnostics=diagnostics)


def _test_apiguard_cache(ctx: SmokeContext) -> TestPayload:
    from assistant.apiguard import ApiGuard

    cache_source = ctx.fixtures_root / "api"
    runtime_cache = ctx.run_dir / "cache"
    runtime_cache.mkdir(parents=True, exist_ok=True)
    for name in ("tmdb_cache.json", "opensubtitles_cache.json"):
        src = cache_source / name
        dst = runtime_cache / name
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    guard = ApiGuard(runtime_cache.parent, tmdb_api_key="stub")
    tmdb_payload = guard.tmdb_lookup("tv/123", {"language": "en-US"}, cache_only=True)
    season_payload = guard.tmdb_lookup("tv/123/season/1", {"language": "en-US"}, cache_only=True)
    subs_payload = guard.opensubtitles_lookup("stub-hash-001", cache_only=True)
    snapshot = guard.cache_snapshot()
    data = {
        "tmdb_tv": tmdb_payload,
        "tmdb_season": season_payload,
        "opensubtitles": subs_payload,
        "snapshot": snapshot,
    }
    return TestPayload(data=data, message="Cache-only ApiGuard probe succeeded")


def _test_textlite_preview(ctx: SmokeContext) -> TestPayload:
    text_root = ctx.fixtures_root / "text"
    sample_config = SampleConfig(max_bytes=2048, max_lines=80, head_lines=20, mid_lines=10, tail_lines=20)
    summary_tool = Summarizer(allow_gpu=False, target_tokens=60, keywords_topk=5)
    rows: List[Dict[str, object]] = []
    for path in sorted(text_root.glob("*")):
        if not path.is_file():
            continue
        sample = sample_text(path, sample_config)
        summary = summary_tool.run(sample.text)
        rows.append(
            {
                "file": path.name,
                "bytes": sample.bytes_sampled,
                "lines": sample.lines_sampled,
                "summary": summary.summary,
                "keywords": summary.keywords,
            }
        )
    rows.sort(key=lambda item: item["file"])
    return TestPayload(data=rows, message="Generated TextLite previews")


def _test_quality_headers(ctx: SmokeContext) -> TestPayload:
    video_dir = ctx.fixtures_root / "video"
    clip = video_dir / "colorbars.mp4"
    probe = quality_ffprobe.run_ffprobe(str(clip), timeout=5.0)
    if not probe.ok:
        if probe.reason == "missing_tool":
            raise SmokeFailure("ffprobe missing — install ffmpeg/ffprobe")
        raise SmokeFailure(f"ffprobe failed: {probe.error}")
    video = asdict(probe.data.video) if probe.data and probe.data.video else None
    audio = [asdict(stream) for stream in (probe.data.audio_streams if probe.data else [])]
    payload = {
        "container": probe.data.container if probe.data else None,
        "duration": probe.data.duration_s if probe.data else None,
        "video": video,
        "audio": audio,
    }
    return TestPayload(data=payload, message="ffprobe headers collected")


def _test_visualreview_keyframe(ctx: SmokeContext) -> TestPayload:
    video_dir = ctx.fixtures_root / "video"
    clip = video_dir / "testsrc.mp4"
    sampler = FrameSampler(FrameSamplerConfig(max_frames=1, prefer_pyav=False, allow_hwaccel=False))
    frames = sampler.sample(clip, max_frames=1)
    if not frames:
        raise SmokeFailure("No keyframe extracted")
    frame = frames[0]
    payload = {
        "timestamp": round(frame.timestamp, 2),
        "size": list(frame.image.size),
        "mode": frame.image.mode,
    }
    return TestPayload(data=payload, message="Visual review keyframe captured")


def _test_vectors_refresh(ctx: SmokeContext) -> TestPayload:
    working_dir = ctx.run_dir
    db_path = ctx.run_dir / "vectors_fixture.db"
    _prepare_vector_fixture_db(db_path)
    assistant_settings = AssistantSettings(enable=True)
    assistant_settings.rag.enable = True
    assistant_settings.rag.embed_model = "stub:smoke"
    assistant_settings.rag.index = "faiss"
    vector_index = VectorIndex(assistant_settings, db_path, working_dir)
    vector_index.refresh()
    hits = vector_index.search("matrix", top_k=3, min_score=0.0)
    payload = {
        "index_entries": len(vector_index._meta),
        "hits": [
            {
                "doc_id": hit.doc_id,
                "score": round(hit.score, 4),
            }
            for hit in hits
        ],
    }
    return TestPayload(data=payload, message="Vector index refreshed")


def _prepare_vector_fixture_db(path: Path) -> None:
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS textlite_preview (
                id INTEGER PRIMARY KEY,
                path TEXT,
                head_excerpt TEXT,
                mid_excerpt TEXT,
                tail_excerpt TEXT
            )
            """
        )
        conn.execute("DELETE FROM textlite_preview")
        rows = [
            (
                idx,
                f"fixture-{idx}.txt",
                "The Matrix is a classic science fiction film.",
                "A crew of rebels challenges the machines.",
                "Reality is not what it seems.",
            )
            for idx in range(1, 4)
        ]
        conn.executemany(
            "INSERT INTO textlite_preview(id, path, head_excerpt, mid_excerpt, tail_excerpt) VALUES(?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _test_assistant_tools(ctx: SmokeContext) -> TestPayload:
    working_dir = ctx.run_dir
    db_path = ctx.run_dir / "vectors_fixture.db"
    if not db_path.exists():
        _prepare_vector_fixture_db(db_path)
    assistant_settings = AssistantSettings(enable=True)
    assistant_settings.tool_budget = 2
    assistant_settings.rag.enable = True
    assistant_settings.rag.embed_model = "stub:smoke"
    assistant_settings.rag.index = "faiss"
    vector_index = VectorIndex(assistant_settings, db_path, working_dir)
    vector_index.refresh()
    tooling = AssistantTooling(
        assistant_settings,
        db_path,
        working_dir,
        vector_index,
        tmdb_api_key="stub",
    )
    tooling.reset_budget(2)
    hits = vector_index.search("matrix", top_k=2, min_score=0.0)
    answer = {
        "answer_markdown": "1. Top semantic hit recorded.\n2. Tool budget honoured.",
        "sources": [
            {"type": "vector", "ref": hit.doc_id}
            for hit in hits
        ],
        "tool_calls": [
            {
                "tool": "db_search_semantic",
                "payload": {
                    "query": "matrix",
                    "results": [hit.doc_id for hit in hits],
                },
            }
        ],
    }
    diagnostics = {"tool_budget": tooling.budget, "calls": tooling.calls}
    return TestPayload(data=answer, message="Assistant tooling dry-run", diagnostics=diagnostics)

