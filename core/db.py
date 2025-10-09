from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional

__all__ = [
    "DEFAULT_BUSY_TIMEOUT_MS",
    "backup_sqlite",
    "connect",
    "configure_connection",
    "pragma_optimize",
    "transaction",
]

DEFAULT_BUSY_TIMEOUT_MS = 5000


def connect(
    db_path: str | Path,
    *,
    read_only: bool = False,
    timeout: float = 5.0,
    detect_types: int = 0,
    isolation_level: Optional[str] = None,
    check_same_thread: bool = False,
) -> sqlite3.Connection:
    """Return a configured SQLite connection with sane defaults."""

    path = Path(db_path)
    if not read_only:
        path.parent.mkdir(parents=True, exist_ok=True)
    if read_only:
        uri = f"file:{path.resolve().as_posix()}?mode=ro&immutable=0"
        conn = sqlite3.connect(
            uri,
            uri=True,
            timeout=timeout,
            detect_types=detect_types,
            isolation_level=isolation_level,
            check_same_thread=check_same_thread,
        )
    else:
        conn = sqlite3.connect(
            str(path),
            timeout=timeout,
            detect_types=detect_types,
            isolation_level=isolation_level,
            check_same_thread=check_same_thread,
        )
    configure_connection(conn, enable_wal=not read_only)
    return conn


def configure_connection(conn: sqlite3.Connection, *, enable_wal: bool = True) -> None:
    conn.execute(f"PRAGMA busy_timeout={int(DEFAULT_BUSY_TIMEOUT_MS)}")
    if enable_wal:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
    try:
        conn.execute("PRAGMA foreign_keys=ON")
    except sqlite3.DatabaseError:
        pass


def pragma_optimize(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA analysis_limit=400")
    conn.execute("PRAGMA optimize")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def backup_sqlite(
    source: sqlite3.Connection | str | Path,
    destination: sqlite3.Connection | str | Path,
    *,
    pages: int = 0,
    progress: Optional[Callable[[int, int, int], None]] = None,
) -> None:
    """Perform a SQLite backup using the built-in backup API."""

    own_source = False
    own_destination = False
    if isinstance(source, (str, Path)):
        source_conn = connect(source, read_only=True)
        own_source = True
    else:
        source_conn = source
    if isinstance(destination, (str, Path)):
        destination_conn = connect(destination, read_only=False)
        own_destination = True
    else:
        destination_conn = destination
    try:
        source_conn.backup(destination_conn, pages=pages, progress=progress)
    finally:
        if own_destination:
            destination_conn.close()
        if own_source:
            source_conn.close()
