"""Contact sheet composition utilities."""
from __future__ import annotations

import io
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from .frame_sampler import FrameSample
from .pillow_support import (
    LOGGER,
    PillowImage,
    PillowUnavailableError,
    ensure_pillow,
    load_pillow_image,
    load_pillow_ops,
)


@dataclass(slots=True)
class ContactSheetConfig:
    """Layout controls for contact sheet generation."""

    columns: int = 4
    max_rows: Optional[int] = None
    cell_size: Tuple[int, int] = (320, 180)
    background: Tuple[int, int, int] = (16, 16, 16)
    margin: int = 24
    padding: int = 6
    format: str = "WEBP"
    quality: int = 80
    optimize: bool = True


@dataclass(slots=True)
class ContactSheetResult:
    """Return payload describing the generated contact sheet."""

    image: PillowImage
    format: str
    width: int
    height: int
    frame_count: int
    optimize: bool = True

    def to_bytes(self, *, quality: Optional[int] = None, format: Optional[str] = None) -> bytes:
        buffer = io.BytesIO()
        fmt = (format or self.format or "WEBP").upper()
        save_kwargs = {}
        if fmt in {"JPEG", "JPG"}:
            save_kwargs["quality"] = int(quality or 85)
            save_kwargs.setdefault("optimize", self.optimize)
            save_kwargs.setdefault("progressive", True)
        elif fmt == "WEBP":
            save_kwargs["quality"] = int(quality or 80)
            save_kwargs.setdefault("method", 6)
            if self.optimize:
                save_kwargs.setdefault("lossless", False)
        self.image.save(buffer, format=fmt, **save_kwargs)
        return buffer.getvalue()


class ContactSheetBuilder:
    """Compose a tiled preview image from sampled frames."""

    def __init__(self, config: Optional[ContactSheetConfig] = None) -> None:
        self._config = config or ContactSheetConfig()
        self._columns = max(1, int(self._config.columns))
        self._max_rows = (
            max(1, int(self._config.max_rows)) if self._config.max_rows else None
        )
        self._cell_width = max(16, int(self._config.cell_size[0]))
        self._cell_height = max(16, int(self._config.cell_size[1]))
        self._background = tuple(self._config.background)
        self._margin = max(0, int(self._config.margin))
        self._padding = max(0, int(self._config.padding))
        self._format = (self._config.format or "WEBP").upper()
        self._quality = int(self._config.quality)
        self._optimize = bool(self._config.optimize)

    def build(
        self,
        samples: Sequence[FrameSample] | Sequence[PillowImage],
    ) -> Optional[ContactSheetResult]:
        if not ensure_pillow(LOGGER):
            return None

        try:
            pillow_image = load_pillow_image()
            pillow_ops = load_pillow_ops()
        except PillowUnavailableError:
            return None

        frames = self._normalize_samples(samples)
        if not frames:
            return None
        grid = self._layout(len(frames))
        sheet = pillow_image.new(
            "RGB",
            (
                grid.columns * self._cell_width
                + self._margin * 2
                + self._padding * (grid.columns - 1),
                grid.rows * self._cell_height
                + self._margin * 2
                + self._padding * (grid.rows - 1),
            ),
            self._background,
        )
        positions = self._iter_positions(grid.columns, grid.rows)
        for frame, (x, y) in zip(frames, positions):
            resized = pillow_ops.fit(
                frame,
                (self._cell_width, self._cell_height),
                method=pillow_image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )
            offset_x = self._margin + x * (self._cell_width + self._padding)
            offset_y = self._margin + y * (self._cell_height + self._padding)
            sheet.paste(resized, (offset_x, offset_y))
        return ContactSheetResult(
            image=sheet,
            format=self._format,
            width=sheet.width,
            height=sheet.height,
            frame_count=len(frames),
            optimize=self._optimize,
        )

    @property
    def output_format(self) -> str:
        return self._format

    @property
    def quality(self) -> int:
        return self._quality

    def export(
        self,
        samples: Sequence[FrameSample] | Sequence[PillowImage],
        destination: Path,
        *,
        format: Optional[str] = None,
        quality: Optional[int] = None,
    ) -> Optional[ContactSheetResult]:
        result = self.build(samples)
        if result is None:
            return None
        fmt = (format or result.format or self._format).upper()
        quality_value = quality or self._quality
        data = result.to_bytes(quality=quality_value, format=fmt)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with open(destination, "wb") as handle:
            handle.write(data)
        return ContactSheetResult(
            image=result.image,
            format=fmt,
            width=result.width,
            height=result.height,
            frame_count=result.frame_count,
            optimize=result.optimize,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _normalize_samples(
        self, samples: Sequence[FrameSample] | Sequence[PillowImage]
    ) -> List[PillowImage]:
        try:
            pillow_image = load_pillow_image()
        except PillowUnavailableError:
            return []
        frames: List[PillowImage] = []
        for sample in samples:
            if isinstance(sample, FrameSample):
                frames.append(sample.image.copy())
            elif isinstance(sample, pillow_image.Image):
                frames.append(sample.copy())
        unique: List[PillowImage] = []
        hashes = set()
        for frame in frames:
            key = (frame.width, frame.height, frame.tobytes()[0:32])
            if key in hashes:
                continue
            hashes.add(key)
            unique.append(frame)
        return unique

    def _iter_positions(self, columns: int, rows: int) -> Iterable[Tuple[int, int]]:
        for index in range(columns * rows):
            yield index % columns, index // columns

    def _layout(self, frame_count: int) -> "_Grid":
        columns = min(self._columns, frame_count)
        if columns <= 0:
            columns = 1
        rows = int(math.ceil(frame_count / columns))
        if self._max_rows is not None:
            rows = min(rows, self._max_rows)
        return _Grid(columns=columns, rows=max(1, rows))


@dataclass(slots=True)
class _Grid:
    columns: int
    rows: int
