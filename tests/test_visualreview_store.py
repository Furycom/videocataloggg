import sqlite3

from visualreview.store import VisualReviewStore, VisualReviewStoreConfig


def test_visualreview_store_trims_blob_budget(tmp_path) -> None:
    db_path = tmp_path / "store.db"
    config = VisualReviewStoreConfig(
        max_thumbnail_bytes=1_000_000,
        max_contact_sheet_bytes=1_000_000,
        thumbnail_retention=10,
        sheet_retention=10,
        max_db_blob_mb=1,
    )
    with VisualReviewStore(db_path, config=config) as store:
        for idx in range(3):
            payload = bytes([idx % 256]) * 400_000
            assert store.upsert_contact_sheet(
                item_type="video",
                item_key=f"item-{idx}",
                data=payload,
                format="webp",
                width=1920,
                height=1080,
                frame_count=12,
            )

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT item_key, LENGTH(image_blob) FROM contact_sheets ORDER BY item_key"
        ).fetchall()
    finally:
        conn.close()

    keys = [row[0] for row in rows]
    total_bytes = sum(int(row[1]) for row in rows)

    assert keys == ["item-1", "item-2"]
    assert 0 < total_bytes <= 1_000_000
