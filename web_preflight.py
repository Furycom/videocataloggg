from __future__ import annotations

import argparse
import os
import random
import sqlite3
import string
import sys
from typing import Dict, List

import requests

from core.paths import get_catalog_db_path, resolve_working_dir
from core.settings import load_settings

DEFAULT_BASE_URL = "http://127.0.0.1:27182"


def _resolve_api_key(override: str | None, settings: Dict[str, object]) -> str | None:
    if override:
        return override
    env_key = os.environ.get("VIDEOCATALOG_API_KEY")
    if env_key:
        return env_key
    api_settings = settings.get("api") if isinstance(settings.get("api"), dict) else {}
    if isinstance(api_settings, dict):
        candidate = api_settings.get("api_key")
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _headers(api_key: str | None) -> Dict[str, str]:
    headers: Dict[str, str] = {"Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    return headers


def _random_client_id() -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "preflight-" + "".join(random.choice(alphabet) for _ in range(8))


def run_preflight(api_base: str, api_key: str | None, timeout: float = 5.0) -> int:
    results: List[str] = []
    headers = _headers(api_key)
    status_ok = True

    try:
        movies = requests.get(
            f"{api_base}/v1/catalog/movies",
            params={"limit": 1},
            headers=headers,
            timeout=timeout,
        )
        if movies.status_code == 200:
            results.append("PASS catalog/movies reachable")
        else:
            status_ok = False
            results.append(f"FAIL catalog/movies -> {movies.status_code}")
    except requests.RequestException as exc:
        status_ok = False
        results.append(f"FAIL catalog/movies error: {exc}")

    try:
        client_id = _random_client_id()
        stream = requests.get(
            f"{api_base}/v1/catalog/subscribe",
            params={"last_seq": 0, "client_id": client_id},
            headers=headers,
            stream=True,
            timeout=timeout,
        )
        if stream.status_code == 200:
            results.append("PASS catalog/subscribe reachable")
        else:
            status_ok = False
            results.append(f"FAIL catalog/subscribe -> {stream.status_code}")
    except requests.RequestException as exc:
        status_ok = False
        results.append(f"FAIL catalog/subscribe error: {exc}")

    assistant_payload: Dict[str, object] | None = None
    try:
        assistant_resp = requests.get(
            f"{api_base}/v1/assistant/status",
            headers=headers,
            timeout=timeout,
        )
        if assistant_resp.status_code == 200:
            assistant_payload = assistant_resp.json()
            results.append("PASS assistant/status reachable")
        else:
            status_ok = False
            results.append(f"FAIL assistant/status -> {assistant_resp.status_code}")
    except requests.RequestException as exc:
        status_ok = False
        results.append(f"FAIL assistant/status error: {exc}")

    if assistant_payload and assistant_payload.get("disabled_by_gpu"):
        try:
            probe = requests.post(
                f"{api_base}/v1/assistant/ask",
                headers=headers,
                json={
                    "item_id": "__preflight__",
                    "mode": "context",
                    "question": "status check",
                },
                timeout=timeout,
            )
            text = probe.text.strip()
            if probe.status_code in (400, 409) and "GPU" in text:
                results.append("PASS GPU gate enforced for assistant")
            else:
                status_ok = False
                results.append(
                    f"FAIL GPU gate expected 4xx with message, got {probe.status_code}: {text or 'no message'}"
                )
        except requests.RequestException as exc:
            status_ok = False
            results.append(f"FAIL GPU gate check error: {exc}")
    elif assistant_payload:
        results.append("SKIP GPU gate (assistant enabled)")

    working_dir = resolve_working_dir()
    catalog_db = get_catalog_db_path(working_dir)
    try:
        conn = sqlite3.connect(f"file:{catalog_db}?mode=ro", uri=True)
        try:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND sql LIKE '%events_queue%'"
            )
            trigger_rows = cursor.fetchall()
        finally:
            conn.close()
        if trigger_rows:
            trigger_names = ", ".join(row[0] for row in trigger_rows)
            results.append(f"PASS events_queue triggers present ({trigger_names})")
        else:
            status_ok = False
            results.append("FAIL events_queue triggers missing (run migrations)")
    except sqlite3.DatabaseError as exc:
        status_ok = False
        results.append(f"FAIL unable to inspect catalog triggers: {exc}")

    for line in results:
        print(line)
    return 0 if status_ok else 1


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VideoCatalog web preflight checks")
    parser.add_argument("--api-base", default=None, help="Base URL for the API (default from settings)")
    parser.add_argument("--api-key", default=None, help="API key override")
    parser.add_argument("--timeout", type=float, default=5.0, help="HTTP timeout in seconds")
    args = parser.parse_args(argv)

    working_dir = resolve_working_dir()
    settings = load_settings(working_dir)
    api_settings = settings.get("api") if isinstance(settings.get("api"), dict) else {}
    if isinstance(api_settings, dict):
        host = api_settings.get("host") or "127.0.0.1"
        port = api_settings.get("port") or 27182
    else:
        host, port = "127.0.0.1", 27182
    base_default = args.api_base or f"http://{host}:{port}"
    api_base = args.api_base or base_default or DEFAULT_BASE_URL
    api_key = _resolve_api_key(args.api_key, settings)

    return run_preflight(api_base.rstrip("/"), api_key, timeout=float(args.timeout))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
