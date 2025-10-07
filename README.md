# videocataloggg

Utilities for scanning large removable media libraries and keeping a SQLite-based catalog.

## Disk Scanner GUI

`DiskScannerGUI.py` provides a Tk-based interface for launching scans and reviewing job history.

### Recent updates

- Optional multi-threaded metadata extraction to speed up large scans while keeping full detail.
- Live log viewer embedded in the GUI so you can observe progress without opening the log file.
- Automatic shard schema migration that ensures legacy shard databases gain the `is_av` column and other metadata fields.

## Troubleshooting

- A red banner across the top of the window means a required media tool is missing. Install `mediainfo` and `ffmpeg` before launching a new scan. An installer will take care of these dependencies in a future release.
- When the status ticker says “I am still working — no new items in the last few seconds — please wait”, the scan is still alive but no new files were discovered. This is expected during long directory walks.
- The indeterminate activity indicator below the scan buttons stays visible while the worker is busy so you always know the app has not frozen.
