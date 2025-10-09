"""CLI entry-point to launch the VideoCatalog read-only HTTP API."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import List, Optional, Sequence

import uvicorn

from api import __version__ as API_VERSION
from api.db import DataAccess
from api.server import APIServerConfig, create_app
from paths import load_settings, resolve_working_dir

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8756
DEFAULT_CORS = ["http://localhost", "http://127.0.0.1"]


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start the local VideoCatalog API service.")
    parser.add_argument("--host", default=None, help="Bind host (default from settings.json)")
    parser.add_argument("--port", type=int, default=None, help="Bind port (default from settings.json)")
    parser.add_argument("--api-key", dest="api_key", default=None, help="Override the API key for this session")
    parser.add_argument(
        "--cors",
        action="append",
        dest="cors",
        default=None,
        help="Additional allowed CORS origin (repeatable).",
    )
    return parser.parse_args(argv)


def resolve_api_settings(args: argparse.Namespace) -> tuple[str, int, Optional[str], List[str], DataAccess]:
    working_dir = resolve_working_dir()
    settings = load_settings(working_dir)
    api_settings = settings.get("api") if isinstance(settings.get("api"), dict) else {}

    host = args.host or api_settings.get("host") or DEFAULT_HOST
    port = args.port or api_settings.get("port") or DEFAULT_PORT
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = DEFAULT_PORT

    api_key = args.api_key if args.api_key else api_settings.get("api_key")

    if args.cors:
        cors = list(args.cors)
    else:
        cors = list(api_settings.get("cors_origins") or DEFAULT_CORS)

    data_access = DataAccess(working_dir=Path(working_dir), settings=settings)
    return str(host), int(port), api_key, cors, data_access


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    args = parse_args(argv)
    host, port, api_key, cors, data_access = resolve_api_settings(args)

    if not api_key:
        logging.warning("API key is not configured; all requests will be rejected with 401.")

    config = APIServerConfig(
        data_access=data_access,
        api_key=api_key,
        cors_origins=cors,
        app_version=API_VERSION,
    )
    app = create_app(config)

    print(f"API listening on http://{host}:{port}", flush=True)

    uvicorn_config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(uvicorn_config)
    return 0 if server.run() else 1


if __name__ == "__main__":
    sys.exit(main())
