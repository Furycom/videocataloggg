from pathlib import Path
import sqlite3

from textlite.store import PreviewRow, PreviewStore, ensure_tables


def test_textlite_store_upsert(tmp_path: Path) -> None:
    db_path = tmp_path / "textlite.db"
    conn = sqlite3.connect(db_path)
    ensure_tables(conn)
    store = PreviewStore(conn, batch_size=1)
    row = PreviewRow(
        path="sample.txt",
        kind="txt",
        bytes_sampled=1024,
        lines_sampled=120,
        summary="Example summary",
        keywords=["example", "text"],
        schema_json='{"csv_headers": ["a", "b"]}',
    )
    store.add(row)
    store.flush()
    cur = conn.cursor()
    stored = cur.execute(
        "SELECT summary, keywords, schema_json FROM textlite_preview WHERE path=?",
        ("sample.txt",),
    ).fetchone()
    assert stored is not None
    assert stored[0] == "Example summary"
    assert "example" in stored[1]
    assert stored[2] == '{"csv_headers": ["a", "b"]}'
    fts = cur.execute(
        "SELECT summary, keywords FROM textlite_fts WHERE path=?",
        ("sample.txt",),
    ).fetchone()
    assert fts is not None
    assert "example" in fts[0] or "Example" in fts[0]
    conn.close()
