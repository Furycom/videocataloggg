# videocataloggg

Utilities for scanning large removable media libraries and keeping a SQLite-based catalog.

## Disk Scanner GUI

`DiskScannerGUI.py` provides a Tk-based interface for launching scans and reviewing job history.

## Core modules

- Foundational helpers now live under `core/` (`core.paths`, `core.db`, `core.settings`) and are shared by the CLI and GUI. The path utilities add Windows long-path/UNC handling, database helpers enable WAL mode with sane busy timeouts, and settings management merges defaults with `settings.json` while preserving legacy layouts.

## Quick Search

- Open the **Search** tab (or click the toolbar Search button) and pick a drive/shard to query.
- Enter at least three characters; the query is normalized to lower-case and matches the shard inventory by basename and full path.
- Results return the latest 1,000 matches with name, category, size, modified time, drive label, and full path. Double-click opens the file's folder in Explorer, the context menu can copy the full path, and exports land as CSV/JSONL under `<working_dir>/exports`.
- The first search against an older shard performs a lightweight migration that backfills the lowercase `inventory.name` column and index; if migration fails the UI falls back to path-only matching and surfaces the error.

## Semantic indexing & search

- The CLI can now drive the semantic pipeline directly from the working directory. Use `scan_drive.py --semantic-index build` for an incremental pass that walks every shard and emits deterministic embeddings plus FTS5 entries, or `--semantic-index rebuild` to clear and repopulate the standalone semantic database. Both modes honour the `semantic.index_phase` toggle inside `settings.json`; when the phase is set to `off` the command aborts with a clear error.
- Run ad-hoc lookups without scanning by calling `scan_drive.py --semantic-query "what to find"`. Passing `--hybrid` blends ANN scores with the FTS hits; omitting it favours ANN-only queries unless `semantic.hybrid_weight` in `settings.json` forces hybrid behaviour. Provide `--label <drive>` to scope searches to a single shard and the results print as ranked lines in the console.
- Populate placeholder transcripts with `scan_drive.py --transcribe`. The helper respects `semantic.transcribe_phase` and updates metadata in place, allowing API callers to surface snippets even before real transcripts land.
- Tuning lives under the `"semantic"` section of `settings.json`: `index_phase`, `search_phase`, and `transcribe_phase` gate each operation; `vector_dim` and `hybrid_weight` control embedding size and scoring balance; `rebuild_chunk` bounds SQLite transactions. CLI commands and API routes honour these switches automatically.
- Semantic helpers never rewrite the working directory layout—databases are created under the resolved VideoCatalog home via `core.paths.resolve_working_dir`, just like the rest of the scanner.

## Reports

- Switch to the **Reports** tab to generate read-only summaries for any catalogued drive. Pick a drive, adjust the *Top N* limit (used for top extensions, heaviest folders, and recents), set the folder depth and recency window, then press **Run**. Queries are executed on background threads so the GUI stays responsive.
- The tab renders five sections: an overview (totals, average size, and per-category counts), top extensions (by count and total bytes), largest files, heaviest folders (aggregated to the selected depth), and recent changes (files modified in the last *X* days). Column headers are clickable to sort in place.
- Use **Export CSV…** or **Export JSON…** to dump the current result sets. CSV exports create one file per section and JSON bundles everything into a single structured document. Files land under `<working_dir>/exports/reports/` with timestamped names.

## Local read-only API

- Launch the service directly with `python videocatalog_api.py --api-key <KEY>` (optional `--host`, `--port`, and repeated `--cors` flags override `settings.json`). On start the CLI prints `API listening on http://<host>:<port>` so other tools can probe it locally.
- The GUI exposes a **Start Local API** toggle under the Database card. It shows host/port plus whether an API key is configured and runs the server in a background process. Disable it from the same button or let it auto-start when `settings.json` sets `"api.enabled_default": true`.
- All endpoints are GET-only, paginate with `limit`/`offset`, and require an `X-API-Key` header. Missing or empty keys return `401 Unauthorized`. Defaults bind to `127.0.0.1:8756`; expanding beyond localhost or exposing the API externally is at your own risk.
- `/v1/reports/*` mirrors the GUI summaries (`overview`, `top-extensions`, `largest-files`, `heaviest-folders`, `recent`) and clamps `limit` parameters to the configured API maximum.
- `/v1/semantic/search` exposes the same ANN/FTS hybrid search used by the CLI. Supply `q`, optional `mode=ann|text|hybrid`, `limit`, `offset`, `drive_label`, and `hybrid=true` to tweak scoring. New maintenance routes—`GET /v1/semantic/index`, `POST /v1/semantic/index` (mode=`build|rebuild`), and `POST /v1/semantic/transcribe`—wrap the underlying pipeline with authentication and respect the `semantic.*_phase` toggles in `settings.json`.
- Example requests:

  ```bash
  curl -H "X-API-Key: $VIDEOCATALOG_API_KEY" http://127.0.0.1:8756/v1/health
  curl -H "X-API-Key: $VIDEOCATALOG_API_KEY" \
       "http://127.0.0.1:8756/v1/inventory?drive_label=MyDrive&limit=25&q=hdr"
  curl -H "X-API-Key: $VIDEOCATALOG_API_KEY" \
       "http://127.0.0.1:8756/v1/features/vector?drive_label=MyDrive&path=movies/clip.mp4&raw=true"
  ```

- Configure behaviour under the `"api"` section of `settings.json` (host, port, API key, allowed CORS origins, default page size). Pagination caps at `max_page_size`, and `/v1/features/vector` enforces a dimensionality guard unless `?raw=true` is supplied to download large vectors explicitly.

## Rescan modes

The scanner now supports two rescan strategies when revisiting a drive:

- **Delta rescan (default).** Enumerates the drive, detects new, modified, and missing files, and only re-processes the items that changed. Files removed from the disk are soft-marked with a deleted flag so they can still be referenced in exports when requested.
- **Full rescan.** Forces every file to be hashed and re-processed regardless of whether the metadata changed. Use this when you need to rebuild a shard from scratch or verify every asset end-to-end. Full rescans can take significantly longer than the delta mode.

Both the CLI (`scan_drive.py`) and the GUI offer a toggle between these modes. A *Resume interrupted scan* option is also enabled by default; it writes lightweight checkpoints every few seconds so a cancelled scan can restart from the last completed file instead of redoing the entire drive.

### Recent updates

- Eco-IO **Inventory Only** mode records every file's path, size, timestamps, extension, MIME guess (libmagic when available, otherwise extension heuristics), and category without hashing or FFmpeg analysis. Results land in the lightweight `inventory` table with per-scan summaries under `inventory_stats` and the GUI offers a dedicated toggle plus a completion dialog with per-category counts.
- Optional multi-threaded metadata extraction to speed up large scans while keeping full detail.
- Live log viewer embedded in the GUI so you can observe progress without opening the log file.
- Automatic shard schema migration that ensures legacy shard databases gain the `is_av` column and other metadata fields.
- Optional **Light Analysis** mode extracts compact MobileNetV3-Small embeddings for images and a couple of sampled video frames. Results are stored in a dedicated `features` table (path → kind → normalized float32 vector) so the primary `files` inventory stays lean. The CLI exposes a `--light-analysis` flag and the GUI adds a “Light Analysis (images & video thumbnails)” toggle; both default to off. Supply a MobileNetV3 ONNX file under `<working_dir>/models/mobilenetv3-small.onnx` (or set `light_analysis.model_path` in `settings.json`) and the app will reuse it across runs. Missing FFmpeg gracefully limits analysis to still images and posts a banner warning.

### Light Analysis mode

When light analysis is enabled the scanner loads a small MobileNetV3-Small ONNX model via CPU-only ONNXRuntime, resizes frames with Pillow, and L2-normalizes the resulting embeddings. For videos, two thumbnails (≈5% and 50% of the runtime, capped by `light_analysis.max_video_frames`) are sampled via FFmpeg; the vectors are averaged and normalized before being written. On USB and NETWORK profiles the sampler throttles itself and falls back to a single frame to keep I/O gentle.

Each processed asset writes a single row into the shard-local `features` table:

```
CREATE TABLE features (
  path TEXT PRIMARY KEY,
  kind TEXT CHECK(kind IN ('image','video')),
  vec BLOB NOT NULL,
  dim INTEGER NOT NULL,
  frames_used INTEGER NOT NULL,
  updated_utc TEXT NOT NULL
);
```

Vectors are serialized as float32 arrays so downstream tools can read them directly. A post-scan summary line reports how many images/videos were embedded and the average dimensionality. Disable or override defaults through `settings.json`:

```json
"light_analysis": {
  "enabled_default": false,
  "model_path": "models/mobilenetv3-small.onnx",
  "max_video_frames": 2,
  "prefer_ffmpeg": true
}
```

### Semantic enrichment (experimental)

VideoCatalog now reserves space in `settings.json` for semantic search and transcription workflows. The feature is disabled by default (`"semantic.enabled_default": false`) but the defaults make it easy to wire in local or hosted models later:

- `semantic.phase_mode` mirrors the fingerprint pipeline (`off`, `during-scan`, `post-scan`). The default `post-scan` queues work after hashing so ingest stays responsive.
- `semantic.models` lists pluggable assets: text/vision/video/audio embedders, a Whisper-compatible transcriber, and an optional reranker. Leave entries `null` to auto-discover from `<working_dir>/models` or set absolute/remote identifiers explicitly.
- `semantic.index` captures vector index preferences (provider backend, on-disk path, metric, normalization, batch sizing) so background jobs can hydrate a FAISS/SQLite hybrid without extra flags.
- `semantic.video` controls how many frames to sample, the stride in seconds, thumbnail resolution, and whether to prefer FFmpeg for decoding.
- `semantic.transcribe` documents the speech pipeline (engine, default model size, optional language override, sampling temperature, timestamp hints, batching window).

Saving settings through the GUI/CLI backfills missing keys so older `settings.json` files automatically pick up the semantic block without manual edits.

If the ONNX model or runtime cannot be loaded the scanner posts a red banner, skips feature extraction, and continues hashing/metadata work as usual.

## GPU acceleration

VideoCatalog automatically probes NVIDIA hardware on Windows 11 via NVML (`pynvml`), sanity-checks the ONNX Runtime CUDA execution provider, and falls back to DirectML or the CPU-only backend whenever CUDA cannot be brought online safely. Detection also queries FFmpeg (`ffmpeg -hwaccels`) so NVDEC decoding is enabled whenever the binary advertises `cuda` support. Capabilities are cached for the current process and surfaced through both the CLI and GUI so you can confirm the active backend at a glance.

- `settings.json` ships with a `"gpu"` block controlling the default policy, whether FFmpeg hardware acceleration is allowed, the minimum free VRAM required before a GPU provider is selected, and the maximum number of concurrent GPU workers.
- CLI overrides: `--gpu-policy AUTO|FORCE_GPU|CPU_ONLY` and `--gpu-hwaccel` / `--no-gpu-hwaccel`. `AUTO` always attempts CUDA first, then DirectML, then CPU. `FORCE_GPU` emits a warning when neither CUDA nor DirectML can be created but still downgrades gracefully to keep the scan running.
- The GUI’s **GPU** card shows the detected adapter (name, VRAM, driver), the provider that will be used with the current policy, and whether FFmpeg hardware acceleration is active. In addition to Auto/Force/CPU radio buttons and the FFmpeg checkbox, a new **Troubleshoot…** button opens a provisioning assistant.
- The provisioning assistant parses the CUDA error returned by ONNX Runtime, highlights missing dependencies (driver, CUDA Toolkit, cuDNN, MSVC), offers a one-click `winget install -e --id Nvidia.CUDA`, links to the official ONNX Runtime CUDA requirements, and exposes buttons to retry CUDA or fall back to DirectML immediately.
- If CUDA starts but free VRAM dips below the configured threshold, the run logs a warning and automatically continues on the CPU to stay stable. When CUDA cannot be loaded, DirectML is selected transparently (when available); otherwise the CPU provider is used. TMK+PDQF and Chromaprint fingerprinting always remain CPU-bound so disk I/O pacing and hashing behaviour stay unchanged.
- FFmpeg automatically receives `-hwaccel cuda` when hardware decoding is allowed and NVDEC support is detected. Clearing the checkbox (or passing `--no-gpu-hwaccel`) forces software decode without affecting ONNX inference.

**Troubleshooting tips**

- Verify `nvidia-smi` works and that the NVIDIA driver is current. The GPU status line falls back to `provider: CPU` when GPU backends cannot be loaded.
- Run `winget install -e --id Nvidia.CUDA` (or use the GUI button) to install the CUDA Toolkit and Visual C++ dependencies that ONNX Runtime expects. Download cuDNN from NVIDIA if the checklist reports it missing.
- Inspect `scanner.log` for messages such as `GPU: CUDA EP unavailable — falling back to DirectML` or `GPU: FORCE_GPU requested but no compatible provider found — using CPU` to confirm which backend is active.
- If FFmpeg lacks NVDEC support, run `ffmpeg -hwaccels` manually. The GUI refresh button reruns detection once a new build is installed.

## Duplicate detection (TMK+PDQF & Chromaprint)

- VideoCatalog can now capture robust perceptual fingerprints for both video and audio assets. Enable them from the CLI (`scan_drive.py --fingerprint-video-tmk` and/or `--fingerprint-audio-chroma`) or from the GUI's scan dialog under the new **Fingerprints** panel.
- Fingerprints are stored in shard-local tables (`video_fp_tmk`, `audio_fp_chroma`, and optional `video_vhash`) so the primary inventory remains lightweight. Each record includes tool version metadata and timestamps to support incremental updates.
- TMK+PDQF signatures rely on Meta's ThreatExchange utilities (`tmk-hash-video`, `tmk-compare-post`). Point the scanner at a portable build with `--tmk-bin <PATH>` (or `settings.json → fingerprints.tmk_bin_path`); missing tools gracefully disable video fingerprinting with a banner warning.
- Audio fingerprints leverage Chromaprint's `fpcalc`. Provide a path with `--fpcalc <PATH>` or configure it once in `settings.json`. When the executable is absent the run continues without audio fingerprints.
- Optional videohash prefiltering (`videohash` Python package) quickly spots obvious duplicates and reduces expensive TMK comparisons. Toggle it via `--fingerprint-prefilter-vhash` / `--no-prefilter-vhash` or `fingerprints.enable_video_vhash_prefilter` in settings.
- Fingerprinting now defaults to the gentler post-scan phase with two worker threads. Work is chunked in batches of 50 files with a configurable sleep (`fingerprints.io_gentle_ms`, override via `--fingerprint-io-ms` and `--fingerprint-batch-size`) and the NETWORK/USB performance profiles automatically widen those delays to keep remote media responsive.
- Missing tools raise clear “Tool missing: …” banners in the GUI and warnings in the CLI; counts and warnings are surfaced in the completion summary so you can act on them immediately.
- Consensus scoring blends TMK similarity with Chromaprint distance (defaults: 0.7 weight on video, 0.3 on audio). Adjust thresholds and weights in `settings.json` (`fingerprints.tmk_similarity_threshold`, `fingerprints.chroma_match_threshold`, `fingerprints.consensus_video_weight`).
- Completion summaries include counts for video signatures, audio fingerprints, and prefilter hashes so you can monitor coverage per run. Detailed status (including missing tool hints) is also surfaced to the GUI progress feed.

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
