from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence

GUI_HINT_IMPORTS = {"tkinter", "PySide6", "PyQt5"}
HEAVY_IMPORTS = {"ffmpeg", "onnx", "faiss"}
DEFAULT_OVERSIZE_LINES = 800
TARGET_PACKAGES = [
    "core",
    "fingerprints",
    "structure",
    "docpreview",
    "textlite",
    "visualreview",
    "quality",
    "audit",
    "apiguard",
    "gpu",
    "health",
    "devguard",
    "semantic",
    "search_util",
]


@dataclass(slots=True)
class FunctionMetric:
    name: str
    lines: int


@dataclass(slots=True)
class FileMetric:
    path: Path
    total_lines: int
    functions: Sequence[FunctionMetric]
    heavy_imports: Sequence[str]
    is_gui: bool

    @property
    def oversized(self) -> bool:
        return self.total_lines >= DEFAULT_OVERSIZE_LINES


def collect_metrics(root: Path) -> List[FileMetric]:
    metrics: List[FileMetric] = []
    seen: set[Path] = set()
    for package in TARGET_PACKAGES:
        package_path = root / package
        if package_path.is_dir():
            for path in _iter_python_files(package_path):
                if path in seen:
                    continue
                metrics.append(_metric_for(path))
                seen.add(path)
        elif package_path.with_suffix(".py").is_file():
            single = package_path.with_suffix(".py")
            if single not in seen:
                metrics.append(_metric_for(single))
                seen.add(single)
    for path in root.glob("*.py"):
        if path in seen or path.name.startswith("_"):
            continue
        metrics.append(_metric_for(path))
    return metrics


def _iter_python_files(root: Path) -> Iterable[Path]:
    excluded = {".git", "venv", "env", "__pycache__", ".mypy_cache", ".pytest_cache"}
    for path in root.rglob("*.py"):
        if path.name.startswith("_"):
            continue
        if any(part in excluded for part in path.parts):
            continue
        yield path


def _metric_for(path: Path) -> FileMetric:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return FileMetric(path=path, total_lines=0, functions=[], heavy_imports=[], is_gui=False)
    lines = source.splitlines()
    total_lines = len(lines)
    try:
        tree = ast.parse(source)
    except (SyntaxError, RecursionError):
        return FileMetric(path=path, total_lines=total_lines, functions=[], heavy_imports=[], is_gui=False)

    visitor = _MetricsVisitor()
    try:
        visitor.visit(tree)
    except RecursionError:
        return FileMetric(path=path, total_lines=total_lines, functions=[], heavy_imports=[], is_gui=False)
    functions = [FunctionMetric(name=name, lines=length) for name, length in visitor.functions]
    heavy = sorted(visitor.heavy_imports)
    return FileMetric(
        path=path,
        total_lines=total_lines,
        functions=functions,
        heavy_imports=heavy,
        is_gui=visitor.is_gui,
    )


class _MetricsVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.heavy_imports: set[str] = set()
        self.functions: List[tuple[str, int]] = []
        self.is_gui = False

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            top = alias.name.split(".", 1)[0]
            if top in GUI_HINT_IMPORTS:
                self.is_gui = True
            if top in HEAVY_IMPORTS:
                self.heavy_imports.add(top)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            top = node.module.split(".", 1)[0]
            if top in GUI_HINT_IMPORTS:
                self.is_gui = True
            if top in HEAVY_IMPORTS:
                self.heavy_imports.add(top)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        lines = _node_length(node)
        self.functions.append((node.name, lines))
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        lines = _node_length(node)
        self.functions.append((node.name, lines))
        self.generic_visit(node)


def _node_length(node: ast.AST) -> int:
    end = getattr(node, "end_lineno", node.lineno)
    start = getattr(node, "lineno", end)
    return max(1, end - start + 1)
