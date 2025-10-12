"""Bootstrap the assistant model cache for Windows deployments."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from core.paths import resolve_working_dir

try:  # pragma: no cover - optional dependency guard
    from huggingface_hub import HfApi, hf_hub_download  # type: ignore
except Exception as exc:  # pragma: no cover - optional dependency guard
    raise RuntimeError(
        "huggingface-hub is required to bootstrap assistant models. Install requirements-windows.txt first."
    ) from exc


LOGGER = logging.getLogger("videocatalog.assistant.model_cache")


@dataclass(slots=True)
class ModelSpec:
    alias: str
    repo_id: str
    filename: str
    description: str


@dataclass(slots=True)
class ModelSummary:
    alias: str
    repo_id: str
    filename: str
    path: str
    size_bytes: int
    sha256: str
    downloaded: bool
    downloaded_utc: str
    remote_size_bytes: Optional[int] = None
    remote_sha256: Optional[str] = None


_MODEL_SPECS: List[ModelSpec] = [
    ModelSpec(
        alias="instruct",
        repo_id="lmstudio-community/Qwen2-0.5B-Instruct-GGUF",
        filename="Qwen2-0.5B-Instruct-Q4_K_M.gguf",
        description="Quantised Qwen2 0.5B instruct GGUF suitable for GPU warmup",
    ),
    ModelSpec(
        alias="embedding",
        repo_id="BAAI/bge-small-en-v1.5",
        filename="model.safetensors",
        description="BGE small English embeddings for RAG",
    ),
]

_MANIFEST_NAME = "model_manifest.json"


def _configure_logging(log_path: Optional[Path]) -> None:
    handlers: List[logging.Handler] = [logging.StreamHandler()]
    formatter = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    handlers[0].setFormatter(formatter)
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
    logging.basicConfig(level=logging.INFO, handlers=handlers)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_path(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


def _load_manifest(path: Path) -> Dict[str, Dict[str, object]]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        LOGGER.warning("Unable to parse existing model manifest at %s; starting fresh", path)
        return {}
    models = payload.get("models") if isinstance(payload, dict) else None
    if isinstance(models, dict):
        return models
    return {}


def _write_manifest(path: Path, summaries: List[ModelSummary]) -> None:
    payload = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "models": {
            summary.alias: {
                "repo_id": summary.repo_id,
                "filename": summary.filename,
                "path": summary.path,
                "size_bytes": summary.size_bytes,
                "sha256": summary.sha256,
                "downloaded_utc": summary.downloaded_utc,
                "remote_size_bytes": summary.remote_size_bytes,
                "remote_sha256": summary.remote_sha256,
            }
            for summary in summaries
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def _fetch_remote_metadata(spec: ModelSpec) -> Dict[str, Optional[str]]:
    try:
        info = HfApi().model_info(spec.repo_id, timeout=10)
    except Exception as exc:  # pragma: no cover - network guard
        LOGGER.debug("Unable to fetch metadata for %s: %s", spec.repo_id, exc)
        return {}
    for sibling in getattr(info, "siblings", []) or []:
        if getattr(sibling, "rfilename", None) != spec.filename:
            continue
        remote_size = getattr(sibling, "size", None)
        remote_sha = getattr(sibling, "sha256", None)
        if remote_sha is None:
            lfs = getattr(sibling, "lfs", None)
            if isinstance(lfs, dict):
                remote_sha = lfs.get("sha256")
        return {
            "remote_size_bytes": int(remote_size) if remote_size is not None else None,
            "remote_sha256": remote_sha,
        }
    return {}


def _ensure_single(
    spec: ModelSpec,
    *,
    cache_dir: Path,
    working_dir: Path,
    manifest: Dict[str, Dict[str, object]],
    refresh: bool,
) -> ModelSummary:
    target_dir = cache_dir / spec.alias
    target_dir.mkdir(parents=True, exist_ok=True)
    expected_path = target_dir / spec.filename
    manifest_entry = manifest.get(spec.alias) if manifest else None
    downloaded = False

    if refresh or not expected_path.exists():
        LOGGER.info("Downloading %s (%s/%s)", spec.alias, spec.repo_id, spec.filename)
        try:
            hf_hub_download(
                repo_id=spec.repo_id,
                filename=spec.filename,
                cache_dir=str(target_dir),
                local_dir=str(target_dir),
                local_dir_use_symlinks=False,
                force_download=refresh,
                resume_download=True,
            )
        except Exception as exc:  # pragma: no cover - network guard
            raise RuntimeError(f"Failed to download {spec.alias} model: {exc}") from exc
        downloaded = True
    else:
        LOGGER.info("Reusing cached %s model from %s", spec.alias, expected_path)

    if not expected_path.exists():
        raise RuntimeError(f"Model file missing after download: {expected_path}")

    size_bytes = expected_path.stat().st_size
    sha_value = _sha256(expected_path)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    downloaded_utc = timestamp if downloaded else (
        str(manifest_entry.get("downloaded_utc"))
        if isinstance(manifest_entry, dict) and manifest_entry.get("downloaded_utc")
        else timestamp
    )

    metadata = _fetch_remote_metadata(spec)

    summary = ModelSummary(
        alias=spec.alias,
        repo_id=spec.repo_id,
        filename=spec.filename,
        path=_relative_path(expected_path, working_dir),
        size_bytes=size_bytes,
        sha256=sha_value,
        downloaded=downloaded,
        downloaded_utc=downloaded_utc,
        remote_size_bytes=metadata.get("remote_size_bytes"),
        remote_sha256=metadata.get("remote_sha256"),
    )
    LOGGER.info(
        "%s model ready (size=%.2f MB, sha256=%s, downloaded=%s)",
        spec.alias,
        summary.size_bytes / (1024 * 1024),
        summary.sha256[:12],
        "yes" if downloaded else "no",
    )
    return summary


def ensure_models(*, log_path: Optional[Path] = None, refresh: bool = False) -> Dict[str, object]:
    """Ensure assistant models are cached locally.

    Returns a JSON-serialisable summary describing the cached assets.
    """

    _configure_logging(log_path)
    working_dir = resolve_working_dir()
    cache_dir = working_dir / "models"
    manifest_path = cache_dir / _MANIFEST_NAME
    manifest = _load_manifest(manifest_path)

    summaries: List[ModelSummary] = []
    errors: List[Dict[str, str]] = []
    for spec in _MODEL_SPECS:
        try:
            summary = _ensure_single(
                spec,
                cache_dir=cache_dir,
                working_dir=working_dir,
                manifest=manifest,
                refresh=refresh,
            )
        except Exception as exc:
            LOGGER.exception("Model %s preparation failed: %s", spec.alias, exc)
            errors.append({"alias": spec.alias, "error": str(exc)})
            continue
        summaries.append(summary)

    _write_manifest(manifest_path, summaries)

    payload = {
        "working_dir": str(working_dir),
        "cache_dir": str(cache_dir),
        "manifest": str(manifest_path),
        "models": [asdict(summary) for summary in summaries],
        "errors": errors,
        "status": "partial_failure" if errors else "ok",
    }
    return payload


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Ensure assistant models are cached locally")
    parser.add_argument("--log", dest="log_path", default=None, help="Optional log file path")
    parser.add_argument("--refresh", action="store_true", help="Force re-download of models")
    args = parser.parse_args(argv)

    log_path = Path(args.log_path).expanduser() if args.log_path else None

    try:
        payload = ensure_models(log_path=log_path, refresh=bool(args.refresh))
    except Exception as exc:
        LOGGER.exception("Model cache preparation failed: %s", exc)
        return 1

    print(json.dumps(payload))
    errors = payload.get("errors") or []
    if errors:
        LOGGER.error(
            "Assistant model cache completed with %d failure(s)", len(errors)
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
