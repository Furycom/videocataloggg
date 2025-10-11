from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, MutableMapping, Sequence, Set

PACKAGE_PREFIXES = (
    "core",
    "fingerprints",
    "structure",
    "tv_",
    "docpreview",
    "textlite",
    "visualreview",
    "quality",
    "audit",
    "apiguard",
    "gpu",
    "health",
    "devguard",
)

TARGET_PACKAGES = (
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
)


@dataclass(slots=True)
class DependencyGraph:
    edges: Mapping[str, Set[str]]

    def nodes(self) -> Sequence[str]:
        return sorted(self.edges.keys())

    def dependencies_of(self, module: str) -> Set[str]:
        return set(self.edges.get(module, set()))


class _ImportVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.imports: Set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            top = alias.name.split(".", 1)[0]
            self.imports.add(top)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if not node.module:
            return
        top = node.module.split(".", 1)[0]
        self.imports.add(top)


def _iter_python_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*.py"):
        if path.name.startswith("_"):
            continue
        yield path


def _module_name(path: Path, root: Path) -> str:
    relative = path.relative_to(root).with_suffix("")
    parts = list(relative.parts)
    return ".".join(parts)


def build_dependency_graph(root: Path) -> DependencyGraph:
    edges: MutableMapping[str, Set[str]] = {}
    for file_path in _target_python_files(root):
        module = _module_name(file_path, root)
        visitor = _ImportVisitor()
        try:
            source = file_path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        visitor.visit(tree)
        filtered = {imp for imp in visitor.imports if _is_internal(imp)}
        edges.setdefault(module, set()).update(filtered)
    return DependencyGraph(edges=edges)


def _target_python_files(root: Path) -> Iterable[Path]:
    seen: set[Path] = set()
    for package in TARGET_PACKAGES:
        package_path = root / package
        if package_path.is_dir():
            for path in _iter_python_files(package_path):
                if path not in seen:
                    seen.add(path)
                    yield path
        else:
            single = package_path.with_suffix(".py")
            if single.exists() and single not in seen:
                seen.add(single)
                yield single
    for path in root.glob("*.py"):
        if path not in seen and not path.name.startswith("_"):
            yield path


def _is_internal(name: str) -> bool:
    return name.startswith(PACKAGE_PREFIXES)


def find_cycles(graph: DependencyGraph) -> List[List[str]]:
    cycles: List[List[str]] = []
    visited: Set[str] = set()
    stack: List[str] = []

    def visit(node: str) -> None:
        if node in stack:
            cycle = stack[stack.index(node) :] + [node]
            cycles.append(cycle)
            return
        if node in visited:
            return
        visited.add(node)
        stack.append(node)
        for dep in graph.dependencies_of(node):
            visit(dep)
        stack.pop()

    for node in graph.nodes():
        visit(node)
    return cycles
