from __future__ import annotations

import argparse
import sys
from pathlib import Path

from assistant_webmon import load_recent_metrics
from core.paths import resolve_working_dir
from core.settings import load_settings

from web_preflight import DEFAULT_BASE_URL, run_preflight as run_web_preflight
from web_smoke import run_smoke as run_web_smoke


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VideoCatalog web tooling")
    parser.add_argument("--api-base", default=None, help="Base URL for the API")
    parser.add_argument("--api-key", default=None, help="API key override")
    parser.add_argument("--timeout", type=float, default=5.0, help="HTTP timeout in seconds")
    parser.add_argument("--web-preflight", action="store_true", help="Run HTTP preflight checks")
    parser.add_argument("--web-smoke", action="store_true", help="Run web smoke tests")
    parser.add_argument(
        "--webmon-snapshot",
        action="store_true",
        help="Print a snapshot of realtime metrics from the last minute",
    )
    parser.add_argument(
        "--lookback",
        type=int,
        default=60,
        help="Lookback window in seconds for --webmon-snapshot",
    )
    args = parser.parse_args(argv)

    actions = [args.web_preflight, args.web_smoke, args.webmon_snapshot]
    if sum(1 for flag in actions if flag) != 1:
        parser.error("Please select exactly one action")

    working_dir = resolve_working_dir()
    settings = load_settings(working_dir)
    api_settings = settings.get("api") if isinstance(settings.get("api"), dict) else {}
    if isinstance(api_settings, dict):
        host = api_settings.get("host") or "127.0.0.1"
        port = api_settings.get("port") or 8756
    else:
        host, port = "127.0.0.1", 8756
    base_default = args.api_base or f"http://{host}:{port}"
    api_base = (args.api_base or base_default or DEFAULT_BASE_URL).rstrip("/")

    if args.webmon_snapshot:
        return _run_snapshot(args.lookback)

    if args.web_preflight:
        return run_web_preflight(api_base, args.api_key, timeout=float(args.timeout))

    if args.web_smoke:
        return run_web_smoke(api_base, args.api_key, timeout=float(args.timeout))

    return 0


def _run_snapshot(lookback: int) -> int:
    working_dir = resolve_working_dir()
    db_path = Path(working_dir) / "data" / "web_metrics.db"
    metrics = load_recent_metrics(db_path, lookback_seconds=max(1, int(lookback)))
    if not metrics:
        print(f"No metrics recorded in the last {lookback} seconds.")
        return 1
    print(f"web_metrics snapshot ({lookback}s)")
    for key, value in sorted(metrics.items()):
        print(f" - {key}: {value:.3f}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
