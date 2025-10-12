"""Tests for assistant.model_cache error aggregation."""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


def _install_hf_stub(monkeypatch):
    stub = types.ModuleType("huggingface_hub")

    class _DummyApi:
        def model_info(self, repo_id: str, timeout: int = 10):  # noqa: D401 - simple stub
            raise RuntimeError("metadata disabled for tests")

    stub.HfApi = lambda: _DummyApi()

    def _dummy_download(*args, **kwargs):  # noqa: D401 - simple stub
        return str(kwargs.get("local_dir") or "")

    stub.hf_hub_download = _dummy_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", stub)


def test_ensure_models_continues_after_failure(tmp_path, monkeypatch):
    _install_hf_stub(monkeypatch)
    sys.modules.pop("assistant", None)
    sys.modules.pop("assistant.model_cache", None)

    package = types.ModuleType("assistant")
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "assistant")]
    monkeypatch.setitem(sys.modules, "assistant", package)

    spec = importlib.util.spec_from_file_location(
        "assistant.model_cache",
        Path(__file__).resolve().parents[1] / "assistant" / "model_cache.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "assistant.model_cache", module)
    spec.loader.exec_module(module)
    model_cache = module

    monkeypatch.setattr(model_cache, "resolve_working_dir", lambda: tmp_path)

    failing = model_cache.ModelSpec(
        alias="fail",
        repo_id="repo/fail",
        filename="fail.bin",
        description="Expected failure for test",
    )
    ok = model_cache.ModelSpec(
        alias="ok",
        repo_id="repo/ok",
        filename="ok.bin",
        description="Successful download",
    )
    monkeypatch.setattr(model_cache, "_MODEL_SPECS", [failing, ok])

    def _fake_ensure(spec, **kwargs):  # type: ignore[override]
        if spec.alias == "fail":
            raise RuntimeError("boom")
        return model_cache.ModelSummary(
            alias=spec.alias,
            repo_id=spec.repo_id,
            filename=spec.filename,
            path="models/ok.bin",
            size_bytes=123,
            sha256="deadbeef" * 8,
            downloaded=True,
            downloaded_utc="2024-01-01T00:00:00Z",
        )

    monkeypatch.setattr(model_cache, "_ensure_single", _fake_ensure)

    payload = model_cache.ensure_models()

    assert payload["status"] == "partial_failure"
    assert payload["errors"] == [{"alias": "fail", "error": "boom"}]
    assert [entry["alias"] for entry in payload["models"]] == ["ok"]

    manifest_path = tmp_path / "models" / model_cache._MANIFEST_NAME
    assert manifest_path.exists()
