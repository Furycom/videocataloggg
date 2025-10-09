import sqlite3

from docpreview.store import DiagRow, PreviewRow, PreviewStore, ensure_tables


def test_preview_store_roundtrip(tmp_path):
    db_path = tmp_path / "preview.db"
    conn = sqlite3.connect(db_path)
    try:
        ensure_tables(conn)
        store = PreviewStore(conn, batch_size=1)
        preview = PreviewRow(
            path="C:/docs/report.pdf",
            doc_type="pdf",
            lang="en",
            pages_sampled=3,
            chars_used=1200,
            summary="Short summary",
            keywords=["alpha", "beta", "gamma"],
        )
        diag = DiagRow(path=preview.path, is_scanned=False, had_ocr=False, notes="ok")
        store.add(preview, diag)
        store.flush()
        cur = conn.cursor()
        row = cur.execute(
            "SELECT summary, keywords, pages_sampled FROM docs_preview WHERE path=?",
            (preview.path,),
        ).fetchone()
        assert row is not None
        assert row[0] == "Short summary"
        assert "alpha" in row[1]
        assert row[2] == 3
        fts_row = cur.execute(
            "SELECT summary FROM docs_fts WHERE path=?",
            (preview.path,),
        ).fetchone()
        assert fts_row is not None
        assert fts_row[0] == "Short summary"
    finally:
        conn.close()
