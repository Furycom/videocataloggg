# videocataloggg

Utilities for scanning large removable media libraries and keeping a SQLite-based catalog.

## Disk Scanner GUI

`DiskScannerGUI.py` provides a Tk-based interface for launching scans and reviewing job history.

## Rescan modes

The scanner now supports two rescan strategies when revisiting a drive:

- **Delta rescan (default).** Enumerates the drive, detects new, modified, and missing files, and only re-processes the items that changed. Files removed from the disk are soft-marked with a deleted flag so they can still be referenced in exports when requested.
- **Full rescan.** Forces every file to be hashed and re-processed regardless of whether the metadata changed. Use this when you need to rebuild a shard from scratch or verify every asset end-to-end. Full rescans can take significantly longer than the delta mode.

Both the CLI (`scan_drive.py`) and the GUI offer a toggle between these modes. A *Resume interrupted scan* option is also enabled by default; it writes lightweight checkpoints every few seconds so a cancelled scan can restart from the last completed file instead of redoing the entire drive.

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

## Tools & Diagnostics

- Open **Tools ▸ Diagnostics…** in the GUI to review the availability, version, and resolved path for MediaInfo, FFmpeg, and smartctl.
- The **Install…** action tries winget packages first. When winget is unavailable or fails, use the portable setup option to copy binaries into `<working_dir>/bin/<tool>`; the app adds that folder to the current session `PATH` automatically and remembers the location for next launch.
- The **Locate…** button lets you select an existing executable manually. The absolute path is stored in `settings.json` so follow-up launches reuse the same binary without additional prompts.

## Exports

- All exports land under `<working_dir>/exports` by default with timestamped names such as `export_drive_20240101_120000Z.csv` or `.jsonl`.
- Run `scan_drive.py --export-csv` or `--export-jsonl` after a scan to stream results directly to disk. Combine with `--include-deleted`, `--av-only`, and `--since 2024-01-01T00:00:00Z` to filter rows before they are written. Provide a path (e.g. `--export-csv C:/tmp/files.csv`) to override the default location.
- The GUI adds an **Exports** menu and menubutton with quick actions for CSV/JSONL along with checkboxes for “Include deleted” and “AV only”. Completed exports raise a toast and the post-scan banner offers an *Open exports folder* shortcut when new files were produced.

## Troubleshooting

- A yellow banner warns when MediaInfo or FFmpeg is missing. Use **Tools ▸ Diagnostics…** to install via winget, configure a portable copy, or point the app to an existing executable before launching a scan.
- When the status ticker says “I am still working — no new items in the last few seconds — please wait”, the scan is still alive but no new files were discovered. This is expected during long directory walks.
- The indeterminate activity indicator below the scan buttons stays visible while the worker is busy so you always know the app has not frozen.
