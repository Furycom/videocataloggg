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
from catalog.exporter import export_catalog
from core.paths import resolve_working_dir
from core.settings import load_settings

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 27182
DEFAULT_CORS = ["http://localhost", "http://127.0.0.1"]


def _resolve_bind_host(candidate: Optional[str]) -> str:
    host = (candidate or DEFAULT_HOST).strip()
    if not host:
        host = DEFAULT_HOST
    norm = host.lower()
    if norm == "localhost":
        return "127.0.0.1"
    if norm.startswith("::ffff:"):
        norm = norm.split("::ffff:")[-1]
    if norm == "::1":
        return "127.0.0.1"
    if norm.startswith("127."):
        return host if host.startswith("127.") else "127.0.0.1"
    raise ValueError(
        f"Refusing to bind API server to non-loopback host '{candidate}'. VideoCatalog only serves on localhost."
    )


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
    parser.add_argument(
        "--export-catalog",
        action="store_true",
        help="Export catalog data instead of starting the API server.",
    )
    parser.add_argument(
        "--export-format",
        default="jsonl",
        choices=["jsonl", "csv", "nfozip"],
        help="Export format when --export-catalog is used.",
    )
    parser.add_argument(
        "--export-scope",
        default="all",
        choices=["movies", "tv", "all"],
        help="Catalog scope to export when --export-catalog is used.",
    )
    return parser.parse_args(argv)


def resolve_api_settings(
    args: argparse.Namespace,
) -> tuple[str, int, Optional[str], List[str], DataAccess, bool]:
    working_dir = resolve_working_dir()
    settings = load_settings(working_dir)
    api_settings = settings.get("api") if isinstance(settings.get("api"), dict) else {}
    server_settings = settings.get("server") if isinstance(settings.get("server"), dict) else {}

    host_candidate = args.host or server_settings.get("host") or api_settings.get("host") or DEFAULT_HOST
    host = _resolve_bind_host(host_candidate)
    port = args.port or server_settings.get("port") or api_settings.get("port") or DEFAULT_PORT
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
    lan_refuse = bool(server_settings.get("lan_refuse", True))
    return str(host), int(port), api_key, cors, data_access, lan_refuse


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    args = parse_args(argv)
    try:
        host, port, api_key, cors, data_access, lan_refuse = resolve_api_settings(args)
    except ValueError as exc:
        logging.error("%s", exc)
        return 2

    if args.export_catalog:
        logging.info(
            "Exporting catalog in %s format (scope=%s)...",
            args.export_format,
            args.export_scope,
        )
        paths = export_catalog(
            data_access,
            format=str(args.export_format),
            scope=str(args.export_scope),
        )
        for path in paths:
            logging.info("Wrote %s", path)
        if not paths:
            logging.warning("No catalog entries were exported (library may be empty).")
        return 0

    if not api_key:
        logging.warning("API key is not configured; all requests will be rejected with 401.")

    config = APIServerConfig(
        data_access=data_access,
        api_key=api_key,
        cors_origins=cors,
        app_version=API_VERSION,
        lan_only=lan_refuse,
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
