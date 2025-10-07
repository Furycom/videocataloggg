# videocataloggg

Utilities for scanning large removable media libraries and keeping a SQLite-based catalog.

## Disk Scanner GUI

`DiskScannerGUI.py` provides a Tk-based interface for launching scans and reviewing job history.

### Recent updates

- Optional multi-threaded metadata extraction to speed up large scans while keeping full detail.
- Live log viewer embedded in the GUI so you can observe progress without opening the log file.
- Automatic shard schema migration that ensures legacy shard databases gain the `is_av` column and other metadata fields.

## Paths & working directory

The scanner now resolves its working directory deterministically:

1. `VIDEOCATALOG_HOME` (if set and writable).
2. `settings.json` located under the working directory itself, `%ProgramData%\VideoCatalog`, or the project root (legacy, read-only).
3. Default fallback: `%ProgramData%\VideoCatalog` when writable, otherwise `%LOCALAPPDATA%\VideoCatalog`.

All runtime assets live beneath `<working_dir>/data` by default, including `catalog.db` and the per-drive shard databases under `<working_dir>/data/shards/`.

PowerShell entry points (`launch-gui.ps1` and `scan-drive.ps1`) automatically locate the repository via `$PSScriptRoot`, honor local virtual environments, and forward arguments safely so UNC paths and spaces are supported out of the box.

## Troubleshooting

- A red banner across the top of the window means a required media tool is missing. Install `mediainfo` and `ffmpeg` before launching a new scan. An installer will take care of these dependencies in a future release.
- When the status ticker says “I am still working — no new items in the last few seconds — please wait”, the scan is still alive but no new files were discovered. This is expected during long directory walks.
- The indeterminate activity indicator below the scan buttons stays visible while the worker is busy so you always know the app has not frozen.

## Rescan modes

The scanner now supports two rescan strategies:

- **Delta rescan (recommended)** compares the current drive contents with the last catalog snapshot and only processes files that are new, modified, or restored. Files that disappeared are marked as soft-deleted with a timestamp so they can be tracked without losing history.
- **Full rescan** forces a complete re-hash of every file regardless of previous results. Use this when you need to rebuild the shard from scratch, but be aware it can take significantly longer on large drives.

Scans automatically write periodic checkpoints to each shard. If a scan stops unexpectedly, leave the “Resume interrupted scan” option enabled and the next run will continue from the most recent consistent point instead of restarting from zero.
