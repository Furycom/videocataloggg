from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import socket
import sqlite3
import ssl
import string
import struct
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

from core.paths import get_catalog_db_path, resolve_working_dir
from core.settings import load_settings

DEFAULT_BASE_URL = "http://127.0.0.1:27182"


def _resolve_api_key(override: Optional[str], settings: Dict[str, object]) -> Optional[str]:
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


def _headers(api_key: Optional[str]) -> Dict[str, str]:
    headers: Dict[str, str] = {"Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    return headers


def _random_client_id(prefix: str = "smoke") -> str:
    alphabet = string.ascii_lowercase + string.digits
    return f"{prefix}-" + "".join(random.choice(alphabet) for _ in range(8))


def _open_websocket(url: str, headers: Dict[str, str], timeout: float = 5.0) -> Tuple[socket.socket, str]:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    scheme = parsed.scheme or "ws"
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if scheme == "wss" else 80)
    resource = parsed.path or "/"
    if parsed.query:
        resource += f"?{parsed.query}"
    raw_sock = socket.create_connection((host, port), timeout=timeout)
    if scheme == "wss":
        context = ssl.create_default_context()
        sock = context.wrap_socket(raw_sock, server_hostname=host)
    else:
        sock = raw_sock
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    handshake_lines = [
        f"GET {resource} HTTP/1.1",
        f"Host: {host}:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        "Sec-WebSocket-Version: 13",
        f"Sec-WebSocket-Key: {key}",
    ]
    for header, value in headers.items():
        handshake_lines.append(f"{header}: {value}")
    handshake_lines.append("\r\n")
    request_bytes = "\r\n".join(handshake_lines).encode("ascii")
    sock.sendall(request_bytes)
    response = sock.recv(4096)
    if b"101" not in response.split(b"\r\n", 1)[0]:
        sock.close()
        raise RuntimeError(f"WebSocket handshake failed: {response.decode(errors='ignore')}")
    accept_expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
    if accept_expected not in response:
        sock.close()
        raise RuntimeError("WebSocket handshake mismatch")
    sock.settimeout(timeout)
    return sock, resource


def _read_ws_message(sock: socket.socket, timeout: float = 5.0) -> Optional[str]:
    end_time = time.time() + timeout
    while time.time() < end_time:
        try:
            header = sock.recv(2)
            if len(header) < 2:
                continue
            b1, b2 = header
            opcode = b1 & 0x0F
            masked = b2 & 0x80
            length = b2 & 0x7F
            if length == 126:
                extended = sock.recv(2)
                length = struct.unpack("!H", extended)[0]
            elif length == 127:
                extended = sock.recv(8)
                length = struct.unpack("!Q", extended)[0]
            mask_key = b""
            if masked:
                mask_key = sock.recv(4)
            payload = b""
            while len(payload) < length:
                chunk = sock.recv(length - len(payload))
                if not chunk:
                    break
                payload += chunk
            if masked and mask_key:
                payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
            if opcode in (0x1, 0x2):  # text or binary
                return payload.decode("utf-8")
            if opcode == 0x8:  # close frame
                return None
        except socket.timeout:
            break
    return None


def _cleanup_event(seq: Optional[int], catalog_db: Path) -> None:
    if not seq:
        return
    try:
        conn = sqlite3.connect(catalog_db)
        try:
            conn.execute("DELETE FROM events_queue WHERE seq = ?", (int(seq),))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        pass


def _insert_event(catalog_db: Path, kind: str) -> Optional[int]:
    payload = json.dumps({"kind": kind, "ts": time.time()})
    conn = sqlite3.connect(catalog_db)
    seq: Optional[int] = None
    try:
        cursor = conn.execute(
            "INSERT INTO events_queue (kind, payload_json) VALUES (?, ?)",
            (kind, payload),
        )
        seq = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()
    return seq


def run_smoke(api_base: str, api_key: Optional[str], timeout: float = 5.0) -> int:
    headers = _headers(api_key)
    results: List[str] = []
    status_ok = True

    try:
        resp = requests.get(
            f"{api_base}/v1/catalog/movies",
            params={"limit": 5},
            headers=headers,
            timeout=timeout,
        )
        if resp.status_code == 200 and "results" in resp.json():
            results.append("PASS movies list (limit 5)")
        else:
            status_ok = False
            results.append(f"FAIL movies list -> {resp.status_code}")
    except requests.RequestException as exc:
        status_ok = False
        results.append(f"FAIL movies list error: {exc}")

    working_dir = resolve_working_dir()
    catalog_db = get_catalog_db_path(working_dir)

    ws_seq: Optional[int] = None
    try:
        client_id = _random_client_id("ws")
        ws_url = (api_base.replace("http", "ws") + "/v1/catalog/subscribe" + f"?client_id={client_id}&last_seq=0")
        sock, _ = _open_websocket(ws_url, headers)
        try:
            ws_seq = _insert_event(catalog_db, "smoke.ws")
            message = _read_ws_message(sock, timeout=timeout)
            if message:
                payload = json.loads(message)
                if payload.get("kind") == "smoke.ws":
                    results.append("PASS websocket delivered smoke event")
                else:
                    status_ok = False
                    results.append("FAIL websocket event kind mismatch")
            else:
                status_ok = False
                results.append("FAIL websocket timeout (WS_TIMEOUT_TRY_SSE)")
        finally:
            try:
                sock.close()
            except Exception:
                pass
    except Exception as exc:
        status_ok = False
        results.append(f"FAIL websocket subscribe error: {exc}")
    finally:
        _cleanup_event(ws_seq, catalog_db)

    sse_seq: Optional[int] = None
    try:
        client_id = _random_client_id("sse")
        with requests.get(
            f"{api_base}/v1/catalog/subscribe",
            params={"client_id": client_id, "last_seq": 0},
            headers=headers,
            stream=True,
            timeout=timeout,
        ) as resp:
            if resp.status_code != 200:
                raise RuntimeError(f"status {resp.status_code}")
            sse_seq = _insert_event(catalog_db, "smoke.sse")
            payload = None
            start = time.time()
            for line in resp.iter_lines():
                if time.time() - start > timeout:
                    break
                if line and line.startswith(b"data:"):
                    payload = json.loads(line.decode("utf-8")[5:].strip())
                    break
            if payload and payload.get("kind") == "smoke.sse":
                results.append("PASS SSE delivered smoke event")
            else:
                status_ok = False
                results.append("FAIL SSE did not deliver event")
    except Exception as exc:
        status_ok = False
        results.append(f"FAIL SSE subscribe error: {exc}")
    finally:
        _cleanup_event(sse_seq, catalog_db)

    assistant_payload: Optional[Dict[str, object]] = None
    try:
        resp = requests.get(
            f"{api_base}/v1/assistant/status",
            headers=headers,
            timeout=timeout,
        )
        if resp.status_code == 200:
            assistant_payload = resp.json()
        else:
            results.append(f"SKIP assistant status unavailable ({resp.status_code})")
    except requests.RequestException as exc:
        results.append(f"SKIP assistant status error: {exc}")

    if assistant_payload and assistant_payload.get("enabled"):
        try:
            probe = requests.post(
                f"{api_base}/v1/assistant/ask",
                headers=headers,
                json={
                    "item_id": "__smoke__",
                    "mode": "context",
                    "question": "quick summary?",
                    "tool_budget": 1,
                },
                timeout=timeout,
            )
            if probe.status_code == 404:
                results.append("PASS assistant reachable (catalog item required)")
            elif probe.status_code == 200:
                results.append("PASS assistant answered smoke request")
            else:
                status_ok = False
                results.append(f"FAIL assistant ask -> {probe.status_code}")
        except requests.RequestException as exc:
            status_ok = False
            results.append(f"FAIL assistant ask error: {exc}")
    else:
        results.append("SKIP assistant ask (GPU disabled or assistant off)")

    for line in results:
        print(line)
    return 0 if status_ok else 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="VideoCatalog web smoke tests")
    parser.add_argument("--api-base", default=None, help="Base URL for the API (default from settings)")
    parser.add_argument("--api-key", default=None, help="API key override")
    parser.add_argument("--timeout", type=float, default=5.0, help="Timeout per step in seconds")
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

    return run_smoke(api_base.rstrip("/"), api_key, timeout=float(args.timeout))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
