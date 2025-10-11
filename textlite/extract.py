"""Schema extraction helpers for TextLite."""
from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

LOGGER = logging.getLogger("videocatalog.textlite.extract")


@dataclass(slots=True)
class SchemaHints:
    csv_headers: Optional[List[str]] = None
    json_keys: Optional[List[str]] = None
    yaml_keys: Optional[List[str]] = None

    def as_dict(self) -> Dict[str, List[str]]:
        payload: Dict[str, List[str]] = {}
        if self.csv_headers:
            payload["csv_headers"] = self.csv_headers
        if self.json_keys:
            payload["json_keys"] = self.json_keys
        if self.yaml_keys:
            payload["yaml_keys"] = self.yaml_keys
        return payload


def _clean_fields(values: List[str], *, max_len: int = 120, limit: int = 40) -> List[str]:
    cleaned: List[str] = []
    for value in values[:limit]:
        text = value.strip()
        if not text:
            continue
        if len(text) > max_len:
            text = text[:max_len]
        cleaned.append(text)
    return cleaned


def extract_csv_headers(path: Path, *, delimiter: str = ",", max_bytes: int = 32768) -> Optional[List[str]]:
    try:
        with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            sample = handle.read(max_bytes)
    except OSError as exc:
        LOGGER.debug("Failed to read CSV header from %s: %s", path, exc)
        return None
    if not sample:
        return None
    reader = csv.reader(sample.splitlines(), delimiter=delimiter)
    try:
        header = next(reader)
    except StopIteration:
        return None
    except csv.Error as exc:
        LOGGER.debug("CSV header parse failed for %s: %s", path, exc)
        return None
    return _clean_fields(header)


def extract_json_keys(path: Path, *, max_bytes: int = 32768, kind: str = "json") -> Optional[List[str]]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(max_bytes)
    except OSError as exc:
        LOGGER.debug("Failed to read JSON snippet from %s: %s", path, exc)
        return None
    if not text:
        return None
    text = text.strip()
    if not text:
        return None
    try:
        if kind == "ndjson":
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                if isinstance(obj, dict):
                    return _clean_fields(list(obj.keys()))
            return None
        data = json.loads(text)
    except json.JSONDecodeError:
        # Attempt to locate the first JSON object in the snippet.
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start : end + 1])
            except Exception:
                return None
        else:
            return None
    except Exception:
        return None
    if isinstance(data, dict):
        return _clean_fields(list(data.keys()))
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return _clean_fields(list(data[0].keys()))
    return None


def extract_yaml_keys(path: Path, *, max_bytes: int = 32768) -> Optional[List[str]]:
    try:
        import yaml  # type: ignore
    except Exception:  # pragma: no cover - optional dependency
        LOGGER.debug("PyYAML not available; skipping YAML keys for %s", path)
        return None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(max_bytes)
    except OSError as exc:
        LOGGER.debug("Failed to read YAML snippet from %s: %s", path, exc)
        return None
    if not text.strip():
        return None
    try:
        data = yaml.safe_load(text)
    except Exception as exc:  # pragma: no cover - runtime dependent
        LOGGER.debug("YAML parse failed for %s: %s", path, exc)
        return None
    if isinstance(data, dict):
        return _clean_fields(list(data.keys()))
    return None


def build_schema(path: Path, kind: str, *, max_bytes: int = 32768) -> SchemaHints:
    hints = SchemaHints()
    if kind in {"csv", "tsv"}:
        delimiter = "\t" if kind == "tsv" else ","
        hints.csv_headers = extract_csv_headers(path, delimiter=delimiter, max_bytes=max_bytes)
    if kind in {"json", "ndjson"}:
        hints.json_keys = extract_json_keys(path, max_bytes=max_bytes, kind=kind)
    if kind == "yaml":
        hints.yaml_keys = extract_yaml_keys(path, max_bytes=max_bytes)
    return hints


__all__ = ["SchemaHints", "build_schema"]
