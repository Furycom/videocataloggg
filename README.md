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

## Performance Profiles

`scan_drive.py` and the GUI automatically benchmark the selected mount point to choose one of four performance profiles: **SSD**, **HDD**, **USB**, or **NETWORK**. The profile controls worker-thread counts, hashing chunk sizes, FFmpeg parallelism, and whether gentle I/O backoff is enabled. NETWORK scans always include a small per-file pause so SMB/CIFS shares stay responsive, while slower USB media lower concurrency and shrink read chunks to avoid thrashing.

Default targets:

- **SSD** – up to 32 workers (2× CPU), 1 MB hash chunks, FFmpeg parallelism up to 4.
- **HDD** – up to 16 workers, 512 KB hash chunks, FFmpeg parallelism up to 2.
- **USB** – up to 8 workers, 256 KB hash chunks, gentle I/O enabled.
- **NETWORK** – fixed 6 workers, 256 KB hash chunks, FFmpeg single-threaded with gentle I/O always on.

Manual overrides can be supplied in `settings.json` or per run:

```json
{
  "performance": {
    "profile": "SSD",
    "worker_threads": 12,
    "hash_chunk_bytes": 1048576,
    "ffmpeg_parallel": 2,
    "gentle_io": true
  }
}
```

CLI flags override both detection and persisted settings when needed:

- `--perf-profile AUTO|SSD|HDD|USB|NETWORK`
- `--perf-threads N`
- `--perf-chunk BYTES`
- `--perf-ffmpeg N`
- `--perf-gentle-io` / `--no-perf-gentle-io`

The GUI shows the active profile above the progress bars so you can confirm how the scan is tuned in real time.

## Robust enumeration & filters

Scanning massive directory trees or slow network shares now uses a fully iterative walker with bounded backpressure so memory stays stable even with millions of entries. The CLI batches database commits (default: 1,000 files or 2 seconds) and pauses enumeration automatically when the hashing queue hits its limit. Operations are retried with exponential backoff on transient SMB/CIFS errors and per-operation timeouts (default: 30 seconds) prevent the scanner from hanging indefinitely when a share stalls.

`settings.json` can persist these knobs under a new `"robust"` section:

```json
{
  "robust": {
    "batch_files": 1000,
    "batch_seconds": 2,
    "queue_max": 10000,
    "skip_hidden": false,
    "ignore": ["Thumbs.db", "*.tmp"],
    "skip_globs": [],
    "follow_symlinks": false,
    "long_paths": "auto",
    "op_timeout_s": 30
  }
}
```

CLI overrides mirror these settings:

- `--batch-files N` / `--batch-seconds N` – tweak commit batching.
- `--queue-max N` – bound the hashing backlog.
- `--skip-hidden` – ignore hidden/system files.
- `--skip-glob PATTERN` (repeatable) – skip glob patterns; e.g. `--skip-glob "*.bak" --skip-glob "Cache/*"`.
- `--follow-symlinks` – opt-in to traversing symlinks/junctions (cycles are detected and logged once).
- `--long-paths auto|force|off` – control `\\?\` extended-length prefixes on Windows.
- `--op-timeout N` – per-operation timeout before retries.

UNC and network shares remain first-class citizens: `scan_drive.py --label NAS --mount \\\\media-server\\videos --queue-max 2000 --batch-seconds 1` gently paces enumeration to keep remote paths responsive. Long Windows paths are handled transparently when `long_paths` is `auto` or `force`; if a share still refuses the prefix the path is skipped, counted, and reported without aborting the scan.

Symlinks are skipped by default to avoid junction loops. When following links is enabled, the scanner tracks `(st_dev, st_ino)` pairs so cycles are broken with a warning and the run continues.

Both the CLI and GUI surface new skip counters (`perm`, `long`, `ignored`) alongside the existing heartbeat so you can see permission denials, long-path fallbacks, and glob filters at a glance.

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

## Database Maintenance

- Open **Tools ▸ Maintenance…** in the GUI to review the catalog database and every shard. The dialog lists file sizes, highlights when a scan is active, and exposes one-click actions:
  - **Quick Optimize** — creates a lightweight backup then runs `REINDEX`, `ANALYZE`, and `PRAGMA optimize`.
  - **Check Integrity** — runs `PRAGMA quick_check`/`integrity_check` and reports any issues.
  - **Full Maintenance** — backup → reindex/analyze/optimize → size reclaim via `VACUUM` when thresholds are met (or immediately when forced).
  - **VACUUM (force)** — backup then forced `VACUUM`, useful after large deletions.
  - **Backup Now** — stores a compact copy under `<working_dir>/backups/sqlite/`.
- Maintenance runs in a background thread with a cancellable progress window so the GUI stays responsive. Status updates land in the live log and the status bar heartbeat remains active.
- The dialog disables actions when the scanner is working on the same database and shows a tooltip explaining why.
- The CLI mirrors these capabilities via `scan_drive.py --maint-action ACTION [--maint-target catalog|shard:<label>|all-shards] [--maint-force]`. It prints a summary per database and exits non-zero when an integrity check fails.
- Every destructive action (reindex or VACUUM) writes a `.bak` into `<working_dir>/backups/sqlite/` before touching the database. Manual VACUUM from the Database menu now follows the same safety steps.
- After a successful scan the app automatically runs a light `PRAGMA optimize` on the affected shard and, when `settings.json` sets `"maintenance": { "auto_vacuum_after_scan": true }`, also triggers a checkpointed VACUUM with a fresh backup.

## Troubleshooting

- A yellow banner warns when MediaInfo or FFmpeg is missing. Use **Tools ▸ Diagnostics…** to install via winget, configure a portable copy, or point the app to an existing executable before launching a scan.
- When the status ticker says “I am still working — no new items in the last few seconds — please wait”, the scan is still alive but no new files were discovered. This is expected during long directory walks.
- The indeterminate activity indicator below the scan buttons stays visible while the worker is busy so you always know the app has not frozen.
