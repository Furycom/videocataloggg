# videocataloggg

Utilities for scanning large removable media libraries and keeping a SQLite-based catalog.

## Disk Scanner GUI

`DiskScannerGUI.py` provides a Tk-based interface for launching scans and reviewing job history.

### Recent updates

- Optional multi-threaded metadata extraction to speed up large scans while keeping full detail.
- Live log viewer embedded in the GUI so you can observe progress without opening the log file.
- Automatic shard schema migration that ensures legacy shard databases gain the `is_av` column and other metadata fields.
