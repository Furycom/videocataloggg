"""Deterministic fixture generation for VideoCatalog smoke tests."""
from __future__ import annotations

import json
import os
import subprocess
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from core.paths import resolve_working_dir

__all__ = ["FixtureError", "FixtureResult", "prepare_fixtures", "get_testdata_root"]


class FixtureError(RuntimeError):
    """Raised when fixture generation fails."""


@dataclass(slots=True)
class FixtureResult:
    root: Path
    created: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def get_testdata_root(working_dir: Path | None = None) -> Path:
    base = working_dir or resolve_working_dir()
    root = base / "testdata"
    root.mkdir(parents=True, exist_ok=True)
    return root


def prepare_fixtures(*, force: bool = False) -> FixtureResult:
    working_dir = resolve_working_dir()
    root = get_testdata_root(working_dir)
    created: List[str] = []
    warnings: List[str] = []

    _prepare_structure(root / "structure", created, force=force)
    _prepare_text(root / "text", created, force=force)
    warnings.extend(_prepare_video(root / "video", created, force=force))
    _prepare_api(root / "api", created, force=force)

    return FixtureResult(root=root, created=created, warnings=warnings)


# ---------------------------------------------------------------------------
# Structure fixtures
# ---------------------------------------------------------------------------


def _prepare_structure(base: Path, created: List[str], *, force: bool) -> None:
    movies = base / "Movies" / "The Matrix (1999)"
    episodes = base / "Shows" / "Example Show (2019)" / "Season 01"

    if force and base.exists():
        for path in sorted(base.rglob("*"), reverse=True):
            try:
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            except OSError:
                continue

    # Movies fixture
    movies.mkdir(parents=True, exist_ok=True)
    _write_text(
        movies / "The Matrix (1999).nfo",
        textwrap.dedent(
            """
            <movie>
              <title>The Matrix</title>
              <year>1999</year>
              <id>tt0133093</id>
            </movie>
            """
        ).strip(),
    )
    _touch_file(movies / "The Matrix (1999).mkv")
    _write_text(
        movies / "The Matrix (1999).srt",
        "\n".join(
            [
                "1",
                "00:00:00,000 --> 00:00:02,000",
                "Wake up, Neo.",
                "",
                "2",
                "00:00:02,000 --> 00:00:04,000",
                "The Matrix has you...",
            ]
        ),
    )
    _write_text(movies / "poster.txt", "poster placeholder")

    # Shows fixture
    episodes.mkdir(parents=True, exist_ok=True)
    _write_text(
        episodes.parent / "show.nfo",
        json.dumps({"title": "Example Show", "year": 2019, "tmdb_id": 42000}, indent=2),
    )
    episode_specs = [
        ("S01E01", "Pilot"),
        ("S01E02", "Orbit"),
    ]
    for code, title in episode_specs:
        stem = f"Example Show - {code} - {title}"
        _touch_file(episodes / f"{stem}.mkv")
        _write_text(
            episodes / f"{stem}.srt",
            "\n".join(
                [
                    "1",
                    "00:00:00,000 --> 00:00:01,500",
                    f"Episode {title} intro.",
                ]
            ),
        )
    _write_text(
        episodes / "episode.nfo",
        json.dumps(
            {
                "episodes": [
                    {"season": 1, "episode": 1, "title": "Pilot"},
                    {"season": 1, "episode": 2, "title": "Orbit"},
                ]
            },
            indent=2,
        ),
    )
    created.extend(
        [
            os.path.relpath(path, base.parent)
            for path in [movies, episodes]
            if os.path.isdir(path)
        ]
    )


# ---------------------------------------------------------------------------
# Text fixtures
# ---------------------------------------------------------------------------


def _prepare_text(base: Path, created: List[str], *, force: bool) -> None:
    if force and base.exists():
        for path in sorted(base.rglob("*"), reverse=True):
            try:
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            except OSError:
                continue
    base.mkdir(parents=True, exist_ok=True)

    _write_text(
        base / "release_notes.txt",
        textwrap.dedent(
            """
            VideoCatalog Smoke Fixtures v1.0
            - Deterministic sample content for smoke tests.
            - Created automatically; safe to delete.
            """
        ).strip(),
    )
    _write_text(
        base / "overview.md",
        """# Example Overview\n\nThis document summarises the fixture dataset.""",
    )
    _write_text(
        base / "metrics.csv",
        "category,value\nmovies,2\nepisodes,2\n",
    )
    _write_text(
        base / "snippets.json",
        json.dumps(
            {
                "movies": ["The Matrix"],
                "episodes": [
                    {"title": "Pilot", "season": 1, "episode": 1},
                    {"title": "Orbit", "season": 1, "episode": 2},
                ],
            },
            indent=2,
        ),
    )
    created.append(os.path.relpath(base, base.parent))


# ---------------------------------------------------------------------------
# Video fixtures
# ---------------------------------------------------------------------------


def _prepare_video(base: Path, created: List[str], *, force: bool) -> List[str]:
    warnings: List[str] = []
    if force and base.exists():
        for path in sorted(base.glob("*.mp4")):
            try:
                path.unlink()
            except OSError:
                continue
    base.mkdir(parents=True, exist_ok=True)

    clips = [
        (base / "colorbars.mp4", "smptebars"),
        (base / "testsrc.mp4", "testsrc=size=320x180:rate=25"),
    ]
    for path, source in clips:
        if path.exists() and not force:
            continue
        try:
            _generate_video(path, source)
        except FixtureError as exc:
            warnings.append(str(exc))
    created.append(os.path.relpath(base, base.parent))
    return warnings


def _generate_video(target: Path, source: str) -> None:
    ffmpeg = _find_ffmpeg()
    if ffmpeg is None:
        raise FixtureError("ffmpeg not available for video fixture generation")
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        source,
        "-t",
        "3",
        "-pix_fmt",
        "yuv420p",
        str(target),
    ]
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as exc:
        raise FixtureError("ffmpeg not available for video fixture generation") from exc
    except subprocess.CalledProcessError as exc:
        raise FixtureError(f"ffmpeg failed to generate fixture: {exc.stderr.decode('utf-8', 'ignore')}")


def _find_ffmpeg() -> str | None:
    for candidate in (os.environ.get("VIDEOCATALOG_FFMPEG"), "ffmpeg"):
        if not candidate:
            continue
        if shutil.which(candidate):
            return candidate
    return None


# ---------------------------------------------------------------------------
# API cache fixtures
# ---------------------------------------------------------------------------


def _prepare_api(base: Path, created: List[str], *, force: bool) -> None:
    if force and base.exists():
        for path in sorted(base.rglob("*.json")):
            try:
                path.unlink()
            except OSError:
                continue
    base.mkdir(parents=True, exist_ok=True)

    tmdb_payload = {
        "entries": {
            json.dumps(["tv/123", [("api_key", "stub"), ("language", "en-US")]], sort_keys=True): {
                "ts": 0,
                "payload": {"id": 123, "name": "Example Show", "number_of_seasons": 1},
            },
            json.dumps(
                [
                    "tv/123/season/1",
                    [("api_key", "stub"), ("language", "en-US")],
                ],
                sort_keys=True,
            ): {
                "ts": 0,
                "payload": {
                    "episodes": [
                        {"season_number": 1, "episode_number": 1, "name": "Pilot"},
                        {"season_number": 1, "episode_number": 2, "name": "Orbit"},
                    ]
                },
            },
        },
        "budget": {},
    }
    opensub_payload = {
        "entries": {
            "stub-hash-001": {
                "items": [
                    {"language": "en", "file_name": "Example.Show.S01E01.srt", "score": 0.95}
                ]
            }
        }
    }
    _write_text(base / "tmdb_cache.json", json.dumps(tmdb_payload, indent=2))
    _write_text(base / "opensubtitles_cache.json", json.dumps(opensub_payload, indent=2))
    created.append(os.path.relpath(base, base.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _touch_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


import shutil  # Placed at end to avoid circular import issues during module import

