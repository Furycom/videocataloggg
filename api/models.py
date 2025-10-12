
"""Pydantic schemas for the VideoCatalog local API."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Health response summarising server readiness and realtime status."""

    ok: bool = Field(True, description="Indicates the API server is reachable.")
    version: str = Field(..., description="Application version string.")
    time_utc: str = Field(..., description="Current UTC timestamp in ISO8601 format.")
    gpu_ready: bool = Field(..., description="True when GPU prerequisites for AI workloads are satisfied.")
    ws_clients: int = Field(..., description="Active WebSocket subscriber count.")
    sse_clients: int = Field(..., description="Active Server-Sent Event subscriber count.")
    tool_budget_remaining: int = Field(..., ge=0, description="Assistant tool call budget remaining for the active session.")
    tool_budget_total: Optional[int] = Field(
        None, ge=0, description="Configured assistant tool call budget when known."
    )
    last_event_age_ms: Optional[float] = Field(
        None, description="Milliseconds since the last realtime catalog event was published."
    )


class DriveInfo(BaseModel):
    """Metadata describing a catalogued drive."""

    label: str = Field(..., description="Drive label as registered in the catalog database.")
    type: Optional[str] = Field(None, description="Optional drive type preset.")
    last_scan_utc: Optional[str] = Field(
        None, description="Timestamp (UTC) of the last completed scan for this drive."
    )
    shard_path: str = Field(..., description="Absolute path to the shard SQLite database file.")
    volume_guid: Optional[str] = Field(
        None,
        description="Volume GUID path (\\\\?\\Volume{GUID}\\) bound to the drive when known.",
    )
    volume_serial_hex: Optional[str] = Field(
        None,
        description="Volume serial number in hexadecimal when reported by Windows.",
    )
    filesystem: Optional[str] = Field(
        None, description="Filesystem reported for the volume during the last scan."
    )
    marker_seen: bool = Field(
        False,
        description="True when a root Disk Marker was observed during the last scan.",
    )
    marker_last_scan_utc: Optional[str] = Field(
        None,
        description="Timestamp captured from the Disk Marker for the last completed scan.",
    )
    last_scan_usn: Optional[int] = Field(
        None,
        description="NTFS USN Change Journal checkpoint persisted after the last scan.",
    )


class PaginatedResponse(BaseModel):
    """Base schema for paginated list endpoints."""

    limit: int = Field(..., description="Maximum number of rows returned in this page.")
    offset: int = Field(..., description="Zero-based offset that produced this page.")
    next_offset: Optional[int] = Field(
        None,
        description=(
            "Offset to request the next page, or null when no more data is immediately available."
        ),
    )
    total_estimate: Optional[int] = Field(
        None,
        description=(
            "Estimated total rows matching the filter when cheaply available; null when omitted."
        ),
    )



class SemanticSearchHit(BaseModel):
    """Single semantic search hit."""

    rank: int = Field(..., description="Rank within the current page starting at 1.")
    path: str = Field(..., description="Inventory path returned by the semantic engine.")
    drive_label: str = Field(..., description="Drive label owning this path.")
    score: float = Field(..., description="Combined similarity score (0..1).")
    mode: str = Field(..., description="Search mode used for this hit (ann/text/hybrid).")
    snippet: Optional[str] = Field(
        None, description="Optional snippet produced by the FTS query when available."
    )
    metadata: Dict[str, object] = Field(
        default_factory=dict,
        description="Additional metadata captured in the semantic index.",
    )


class SemanticSearchResponse(PaginatedResponse):
    """Paginated semantic search payload."""

    query: str = Field(..., description="Original query string that was executed.")
    mode: str = Field(..., description="Mode requested: ann, text, or hybrid.")
    hybrid: bool = Field(..., description="True when hybrid scoring blended ANN and FTS results.")
    results: List[SemanticSearchHit] = Field(
        ..., description="Semantic search hits ordered by descending score."
    )


class SemanticIndexRequest(BaseModel):
    """Request body for semantic index operations."""

    mode: str = Field(
        "build",
        description="Operation to execute: build (incremental) or rebuild (clear and rebuild).",
    )


class SemanticOperationResponse(BaseModel):
    """Simple acknowledgement for semantic maintenance operations."""

    ok: bool = Field(True, description="Indicates the operation completed without raising errors.")
    action: str = Field(..., description="Operation that was performed.")
    stats: Dict[str, int] = Field(
        default_factory=dict,
        description="Optional counters returned by the semantic pipeline.",
    )


class SemanticStatusResponse(BaseModel):
    """Status summary for the semantic index."""

    documents: int = Field(..., description="Total documents currently stored in the index.")
    latest_updated_utc: Optional[str] = Field(
        None, description="Timestamp of the most recently updated entry when available."
    )
    vector_dim: int = Field(..., description="Vector dimensionality configured for ANN search.")


class InventoryRow(BaseModel):
    """Single file entry coming from a drive inventory shard."""

    path: str = Field(..., description="Full path relative to the scanned drive.")
    name: str = Field(..., description="Basename of the file for convenience.")
    category: Optional[str] = Field(None, description="Categorised type, e.g. image/audio/video.")
    size_bytes: int = Field(..., description="File size in bytes.")
    mtime_utc: str = Field(..., description="Last modification timestamp in UTC (ISO8601).")
    drive_label: Optional[str] = Field(
        None, description="Drive label owning this record (mirrors drive_label column)."
    )
    ext: Optional[str] = Field(None, description="File extension cached during the scan.")
    mime: Optional[str] = Field(None, description="MIME guess captured during the scan.")


class InventoryResponse(PaginatedResponse):
    """Paginated inventory listing."""

    results: List[InventoryRow] = Field(..., description="Inventory rows for this page.")


class FileResponse(BaseModel):
    """Single inventory row fetched by path."""

    path: str
    size_bytes: int
    mtime_utc: str
    ext: Optional[str]
    mime: Optional[str]
    category: Optional[str]
    drive_label: Optional[str]
    drive_type: Optional[str]
    indexed_utc: Optional[str]


class StatsTotals(BaseModel):
    """Category breakdown for a drive inventory."""

    total_files: int = Field(..., description="Number of inventory rows tallied.")
    by_category: Dict[str, int] = Field(
        ..., description="Mapping of category name → count for the latest completed scan."
    )
    scanned_at_utc: Optional[str] = Field(
        None, description="UTC timestamp of the stats snapshot when available."
    )


class DriveStatsResponse(BaseModel):
    """Drive level statistics response."""

    drive_label: str = Field(..., description="Drive label referenced by the stats.")
    totals: StatsTotals = Field(..., description="Totals and category breakdown.")



class FeatureMetadata(BaseModel):
    """Metadata row describing a lightweight feature vector entry."""

    path: str = Field(..., description="Inventory path associated with the vector.")
    kind: str = Field(..., description="Feature kind: image or video.")
    dim: int = Field(..., description="Vector dimensionality.")
    frames_used: int = Field(..., description="Number of frames averaged to build the vector.")
    updated_utc: str = Field(..., description="Last update timestamp in UTC (ISO8601).")


class FeaturesResponse(PaginatedResponse):
    """Paginated listing of lightweight feature metadata."""

    results: List[FeatureMetadata] = Field(..., description="Feature metadata rows for this page.")


class CatalogMovieRow(BaseModel):
    id: str = Field(..., description="Opaque identifier for the movie folder.")
    path: str = Field(..., description="Folder path relative to the drive.")
    title: Optional[str] = Field(None, description="Resolved title when available.")
    year: Optional[int] = Field(None, description="Release year when detected.")
    poster_thumb: Optional[str] = Field(
        None, description="Media token used to fetch the cached thumbnail via /thumb."
    )
    contact_sheet: Optional[str] = Field(
        None, description="Media token for a contact sheet when available."
    )
    confidence: float = Field(..., description="Confidence score assigned by the structure pipeline.")
    quality: Optional[int] = Field(None, description="Quality score when evaluated by the quality pipeline.")
    langs_audio: List[str] = Field(default_factory=list, description="Audio languages detected in the main video.")
    langs_subs: List[str] = Field(default_factory=list, description="Subtitle languages detected in the main video.")
    drive: Optional[str] = Field(None, description="Drive label owning this movie folder.")


class CatalogMoviesResponse(PaginatedResponse):
    results: List[CatalogMovieRow] = Field(..., description="Movie rows for the requested page.")


class CatalogSeriesRow(BaseModel):
    id: str = Field(..., description="Series identifier used across the catalog endpoints.")
    series_root: str = Field(..., description="Series root folder path relative to the drive.")
    title: Optional[str] = Field(None, description="Series title when resolved.")
    year: Optional[int] = Field(None, description="First air year when available.")
    confidence: float = Field(..., description="Series level confidence score.")
    seasons_found: int = Field(..., description="Number of seasons detected for this series.")
    poster_thumb: Optional[str] = Field(
        None, description="Media token that can be passed to /thumb for a cached series thumbnail."
    )
    drive: Optional[str] = Field(None, description="Drive label owning the series root folder.")


class CatalogSeriesResponse(PaginatedResponse):
    results: List[CatalogSeriesRow] = Field(..., description="TV series rows for the requested page.")


class CatalogSeasonRow(BaseModel):
    id: str = Field(..., description="Season identifier for navigation.")
    season_path: str = Field(..., description="Season folder path relative to the drive.")
    season_number: Optional[int] = Field(None, description="Season number (1-based) when parsed.")
    episodes_found: Optional[int] = Field(None, description="Number of episode files detected in the season folder.")
    expected: Optional[int] = Field(None, description="Expected episode count when matched from metadata.")
    confidence: float = Field(..., description="Season level confidence score.")
    drive: Optional[str] = Field(None, description="Drive label owning the season folder.")


class CatalogSeasonsResponse(BaseModel):
    series: Dict[str, Any] = Field(default_factory=dict, description="Series metadata context.")
    seasons: List[CatalogSeasonRow] = Field(
        default_factory=list, description="Season rows associated with the requested series."
    )


class CatalogEpisodeRow(BaseModel):
    id: str = Field(..., description="Episode identifier for the catalog endpoints.")
    episode_path: str = Field(..., description="Episode file path relative to the drive.")
    season_number: Optional[int] = Field(None, description="Season number for the episode.")
    episode_numbers: List[str] = Field(
        default_factory=list,
        description="Episode numbers parsed from filenames or metadata (SxxExx).",
    )
    air_date: Optional[str] = Field(None, description="Air date captured for the episode when available.")
    title: Optional[str] = Field(None, description="Episode title when resolved.")
    confidence: float = Field(..., description="Episode confidence score from the TV pipeline.")
    quality: Optional[int] = Field(None, description="Quality score when evaluated.")
    poster_thumb: Optional[str] = Field(
        None, description="Media token for a cached episode thumbnail."
    )
    drive: Optional[str] = Field(None, description="Drive label owning the episode file.")


class CatalogEpisodesResponse(BaseModel):
    season: Dict[str, Any] = Field(default_factory=dict, description="Season metadata context.")
    episodes: List[CatalogEpisodeRow] = Field(
        default_factory=list, description="Episode rows for the requested series/season."
    )


class CatalogItemCandidate(BaseModel):
    source: Optional[str] = Field(None, description="Metadata provider or signal that produced the candidate.")
    candidate_id: Optional[str] = Field(None, description="External identifier when present.")
    title: Optional[str] = Field(None, description="Candidate title.")
    year: Optional[int] = Field(None, description="Candidate year.")
    score: float = Field(..., description="Score assigned to the candidate by the pipeline.")
    extra: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata for the candidate.")


class CatalogConfidenceComponent(BaseModel):
    label: str = Field(..., description="Human readable component name.")
    score: float = Field(..., description="Contribution score for the component (0..1).")


class CatalogResolution(BaseModel):
    width: Optional[int] = Field(None, description="Video width in pixels when measured.")
    height: Optional[int] = Field(None, description="Video height in pixels when measured.")


class CatalogMovieDetail(BaseModel):
    id: str
    title: Optional[str]
    year: Optional[int]
    drive: Optional[str]
    folder_path: str
    main_video_path: Optional[str]
    runtime_minutes: Optional[int]
    overview: Optional[str]
    confidence: float
    confidence_components: List[CatalogConfidenceComponent] = Field(default_factory=list)
    quality_score: Optional[int]
    quality_reasons: List[str] = Field(default_factory=list)
    audio_langs: List[str] = Field(default_factory=list)
    subs_langs: List[str] = Field(default_factory=list)
    subs_present: bool = False
    video_codec: Optional[str]
    resolution: CatalogResolution
    container: Optional[str] = None
    ids: Dict[str, Any] = Field(default_factory=dict)
    issues: List[str] = Field(default_factory=list)
    candidates: List[CatalogItemCandidate] = Field(default_factory=list)
    review_reasons: List[str] = Field(default_factory=list)
    review_questions: List[str] = Field(default_factory=list)
    assets: Dict[str, Any] = Field(default_factory=dict)
    signals: Dict[str, Any] = Field(default_factory=dict)
    poster_thumb: Optional[str] = None
    contact_sheet: Optional[str] = None
    open_plan: Dict[str, Any]


class CatalogEpisodeDetail(BaseModel):
    id: str
    title: Optional[str]
    drive: Optional[str]
    episode_path: str
    series_root: Optional[str]
    season_number: Optional[int]
    episode_numbers: List[str] = Field(default_factory=list)
    air_date: Optional[str]
    ids: Dict[str, Any] = Field(default_factory=dict)
    confidence: float
    audio_langs: List[str] = Field(default_factory=list)
    subs_langs: List[str] = Field(default_factory=list)
    subs_present: bool = False
    quality_score: Optional[int]
    quality_reasons: List[str] = Field(default_factory=list)
    runtime_minutes: Optional[int]
    video_codec: Optional[str]
    resolution: CatalogResolution
    series: Dict[str, Any] = Field(default_factory=dict)
    season: Dict[str, Any] = Field(default_factory=dict)
    poster_thumb: Optional[str] = None
    open_plan: Dict[str, Any]


class CatalogItemDetailResponse(BaseModel):
    kind: str = Field(..., description="Item kind: movie, episode, or series.")
    movie: Optional[CatalogMovieDetail] = None
    episode: Optional[CatalogEpisodeDetail] = None
    series: Optional[Dict[str, Any]] = None


class CatalogSummaryQueue(BaseModel):
    movies: List[Dict[str, Any]] = Field(default_factory=list)
    episodes: List[Dict[str, Any]] = Field(default_factory=list)


class CatalogSummaryResponse(BaseModel):
    totals: Dict[str, int] = Field(..., description="Aggregate counts for movies, series, and episodes.")
    review_queue: CatalogSummaryQueue = Field(
        ..., description="Low confidence buckets for quick review continuation."
    )


class CatalogOpenFolderRequest(BaseModel):
    path: str = Field(..., description="Path that should be opened by the caller.")


class CatalogOpenFolderResponse(BaseModel):
    plan: str = Field(..., description="Plan identifier the client should execute.")
    path: str = Field(..., description="Path that should be opened.")


class PlaylistCandidate(BaseModel):
    id: str = Field(..., description="Identifier returned by catalog listings.")
    kind: str = Field(..., description="Item kind (movie or episode).")
    drive: Optional[str] = Field(None, description="Drive label owning the media path.")
    path: str = Field(..., description="Absolute path to the media file for export.")
    masked_path: str = Field(..., description="Masked path safe for UI display.")
    title: Optional[str] = Field(None, description="Display title for the candidate.")
    year: Optional[int] = Field(None, description="Release or air year when available.")
    duration_min: Optional[int] = Field(None, description="Runtime in whole minutes when available.")
    quality: Optional[int] = Field(None, description="Quality score when evaluated.")
    confidence: float = Field(..., description="Confidence score from catalog pipelines.")
    langs_audio: List[str] = Field(default_factory=list, description="Detected audio language codes.")
    langs_subs: List[str] = Field(default_factory=list, description="Detected subtitle language codes.")
    subs_present: bool = Field(False, description="True when local subtitles were detected.")
    genres: List[str] = Field(default_factory=list, description="Best-effort genres extracted from metadata.")


class PlaylistSuggestResponse(BaseModel):
    limit: int = Field(..., description="Maximum number of candidates returned.")
    candidates: List[PlaylistCandidate] = Field(
        default_factory=list, description="Candidate rows ordered by descending quality/confidence."
    )


class PlaylistBuildRequest(BaseModel):
    items: List[str] = Field(..., description="Item identifiers that should compose the playlist.")
    mode: str = Field(
        "weighted",
        description="Ordering strategy: weighted, sort_quality, or sort_confidence.",
    )
    target_minutes: Optional[int] = Field(
        None, description="Target total runtime in minutes; used as a soft limit when ordering."
    )


class PlaylistBuildItem(BaseModel):
    id: str = Field(..., description="Identifier of the playlist item.")
    kind: str = Field(..., description="Item kind (movie or episode).")
    drive: Optional[str] = Field(None, description="Drive label owning the media path.")
    title: Optional[str] = Field(None, description="Display title for the playlist row.")
    year: Optional[int] = Field(None, description="Release or air year when available.")
    duration_min: Optional[int] = Field(None, description="Runtime in minutes.")
    cumulative_minutes: int = Field(..., description="Cumulative runtime including this item.")
    path: str = Field(..., description="Absolute media path used for exports.")
    masked_path: str = Field(..., description="Masked path for user display.")
    quality: Optional[int] = Field(None, description="Quality score when available.")
    confidence: float = Field(..., description="Confidence score from catalog pipelines.")
    langs_audio: List[str] = Field(default_factory=list, description="Detected audio languages.")
    langs_subs: List[str] = Field(default_factory=list, description="Detected subtitle languages.")
    subs_present: bool = Field(False, description="True when local subtitles were detected.")
    open_plan: Dict[str, Any] = Field(..., description="Plan payload for opening the containing folder.")


class PlaylistBuildResponse(BaseModel):
    mode: str = Field(..., description="Ordering strategy that was applied.")
    target_minutes: Optional[int] = Field(
        None, description="Target total runtime requested by the caller."
    )
    total_minutes: int = Field(..., description="Cumulative runtime for the returned playlist.")
    items: List[PlaylistBuildItem] = Field(..., description="Ordered playlist items with cumulative runtime.")


class PlaylistExportItem(BaseModel):
    path: str = Field(..., description="Absolute media path to include in the export.")
    title: Optional[str] = Field(None, description="Friendly title stored in CSV exports.")


class PlaylistExportRequest(BaseModel):
    name: str = Field(..., description="Human friendly name used for the exported filename.")
    format: str = Field(..., regex="^(m3u|csv)$", description="Export format: m3u or csv.")
    items: List[PlaylistExportItem] = Field(..., description="Ordered items to include in the export.")


class PlaylistExportResponse(BaseModel):
    ok: bool = Field(True, description="True when the export completed successfully.")
    format: str = Field(..., description="Export format that was written.")
    path: str = Field(..., description="Absolute path to the exported playlist file.")
    count: int = Field(..., description="Number of entries written to the export file.")


class PlaylistAiConstraints(BaseModel):
    dur_min: Optional[int] = Field(None, description="Minimum runtime for generated suggestions.")
    dur_max: Optional[int] = Field(None, description="Maximum runtime for generated suggestions.")
    conf_min: Optional[float] = Field(None, description="Minimum confidence threshold.")
    qual_min: Optional[int] = Field(None, description="Minimum quality threshold.")
    langs_audio: List[str] = Field(default_factory=list, description="Preferred audio language codes.")
    langs_subs: List[str] = Field(default_factory=list, description="Preferred subtitle language codes.")
    genres: List[str] = Field(default_factory=list, description="Preferred genres to bias suggestions.")
    target_minutes: Optional[int] = Field(None, description="Target runtime window for the evening playlist.")


class PlaylistAiRequest(BaseModel):
    question: str = Field(..., description="High level preference question posed to the assistant.")
    constraints: PlaylistAiConstraints = Field(default_factory=PlaylistAiConstraints)


class PlaylistAiPlanItem(BaseModel):
    id: str = Field(..., description="Identifier proposed by the assistant.")
    reason: Optional[str] = Field(None, description="Short justification for including the item.")
    confidence: Optional[float] = Field(None, description="Catalog confidence score when referenced.")
    quality: Optional[int] = Field(None, description="Quality score when referenced.")


class PlaylistAiResponse(BaseModel):
    mode: str = Field(..., description="Assistant strategy used for the plan.")
    items: List[PlaylistAiPlanItem] = Field(default_factory=list, description="Ordered playlist items.")
    reasoning: str = Field(..., description="Narrative explanation of the selection.")
    sources: List[str] = Field(default_factory=list, description="Data sources consulted while planning.")


class CatalogSearchHit(BaseModel):
    id: str = Field(..., description="Identifier returned by catalog listings.")
    kind: str = Field(..., description="Item kind associated with the hit.")
    title: Optional[str] = Field(None, description="Display title for the hit.")
    drive: Optional[str] = Field(None, description="Drive label owning the hit.")
    confidence: Optional[float] = Field(None, description="Confidence score associated with the item.")
    context: Dict[str, Any] = Field(default_factory=dict, description="Additional context payload.")


class CatalogSearchResponse(BaseModel):
    mode: str = Field(..., description="Search mode that was executed (fts or semantic).")
    query: str = Field(..., description="Original query string submitted by the client.")
    results: List[CatalogSearchHit] = Field(
        default_factory=list, description="Combined movie/episode hits ordered by relevance."
    )


class AssistantAskSource(BaseModel):
    type: str
    ref: str


class AssistantToolCall(BaseModel):
    tool: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)


class AssistantAskRequest(BaseModel):
    item_id: str = Field(..., description="Catalog item identifier." )
    mode: str = Field("context", description="Only 'context' mode is supported.")
    question: str = Field(..., min_length=1, description="Question the assistant should answer.")
    tool_budget: Optional[int] = Field(
        None,
        ge=1,
        le=50,
        description="Optional override for the maximum number of tool calls allowed.",
    )
    rag: Optional[bool] = Field(
        True,
        description="Enable retrieval-augmented generation snippets when answering.",
    )


class AssistantAskResponse(BaseModel):
    answer_markdown: str
    sources: List[AssistantAskSource]
    tool_calls: List[AssistantToolCall]
    elapsed_ms: int
    status: Dict[str, Any]


class AssistantStatusResponse(BaseModel):
    requested: bool
    gpu_ready: bool
    enabled: bool
    disabled_by_gpu: bool
    message: str
    gpu: Dict[str, Any]
    runtime: Optional[Dict[str, Any]] = None


class RealtimeClientStatus(BaseModel):
    client_id: str
    last_seen_utc: Optional[str]
    stale: bool


class RealtimeStatusResponse(BaseModel):
    ts_utc: str
    ws_connected: int
    sse_connected: int
    events_pushed_total: float
    events_dropped_total: float
    event_lag_ms_p50: Optional[float]
    event_lag_ms_p95: Optional[float]
    last_event_utc: Optional[str]
    last_event_age_ms: Optional[float]
    ai_requests_total: float
    ai_errors_total: float
    client: Optional[RealtimeClientStatus] = None


class RealtimeHeartbeatRequest(BaseModel):
    client_id: str = Field(..., min_length=4, max_length=128)
    transport: str = Field(..., regex="^(ws|sse)$")


class RealtimeHeartbeatResponse(BaseModel):
    acknowledged: bool
    ts_utc: str
class FeatureVectorResponse(BaseModel):
    """Single feature vector payload."""

    path: str = Field(..., description="Inventory path associated with the vector.")
    dim: int = Field(..., description="Vector dimensionality.")
    kind: Optional[str] = Field(None, description="Feature kind if available.")
    vector: List[float] = Field(..., description="Float32 vector serialised as JSON array.")


class DocPreviewRow(BaseModel):
    """Row describing a lightweight document preview."""

    path: str = Field(..., description="Inventory path processed for preview.")
    doc_type: Optional[str] = Field(None, description="Detected document type (pdf/epub).")
    lang: Optional[str] = Field(None, description="Detected language code when available.")
    pages_sampled: int = Field(..., description="Number of pages sampled for the preview.")
    chars_used: int = Field(..., description="Characters analysed for the summary.")
    summary: Optional[str] = Field(None, description="Short summary generated for the document.")
    keywords: Optional[str] = Field(None, description="Top keywords extracted for the document.")
    updated_utc: Optional[str] = Field(None, description="Timestamp of the preview generation.")


class DocPreviewResponse(PaginatedResponse):
    """Paginated response for document previews."""

    results: List[DocPreviewRow] = Field(..., description="Preview rows for this page.")


class TextLiteRow(BaseModel):
    """Row describing a TextLite plaintext preview."""

    path: str = Field(..., description="Inventory path processed for TextLite.")
    kind: Optional[str] = Field(None, description="Detected text kind (txt/md/log/…).")
    bytes_sampled: int = Field(..., description="Approximate bytes sampled from the source file.")
    lines_sampled: int = Field(..., description="Number of lines captured in the snippet.")
    summary: Optional[str] = Field(None, description="Generated summary for the text sample.")
    keywords: Optional[str] = Field(None, description="Keywords extracted from the sample.")
    schema_json: Optional[str] = Field(None, description="Serialized schema hints (CSV headers, JSON keys…).")
    updated_utc: Optional[str] = Field(None, description="Timestamp of the preview generation.")


class TextLiteResponse(PaginatedResponse):
    """Paginated response for TextLite previews."""

    results: List[TextLiteRow] = Field(..., description="TextLite previews for this page.")


class TextVerifyRow(BaseModel):
    """Row describing a subtitle/plot cross-check summary."""

    path: str = Field(..., description="Relative media path associated with the cross-check.")
    has_local_subs: bool = Field(..., description="True when local subtitles were sampled.")
    subs_lang: Optional[str] = Field(None, description="Detected subtitle language code.")
    subs_lines_used: Optional[int] = Field(None, description="Number of subtitle lines sampled.")
    summary: Optional[str] = Field(None, description="Short summary produced from the subtitles.")
    keywords: List[str] = Field(default_factory=list, description="Keywords extracted from the subtitles.")
    plot_source: Optional[str] = Field(None, description="Source of the official plot synopsis.")
    plot_excerpt: Optional[str] = Field(None, description="Excerpt of the official plot used for comparison.")
    semantic_sim: float = Field(..., description="Semantic similarity score (0..1).")
    ner_overlap: float = Field(..., description="Named entity overlap score (0..1).")
    keyword_overlap: float = Field(..., description="Keyword overlap score (0..1).")
    aggregated_score: float = Field(..., description="Weighted aggregate score (0..1).")
    updated_utc: Optional[str] = Field(None, description="Last update timestamp in UTC.")


class TextVerifyResponse(PaginatedResponse):
    """Paginated subtitle/plot cross-check listing."""

    results: List[TextVerifyRow] = Field(..., description="Cross-check rows for this page.")


class TextVerifyDetailsResponse(TextVerifyRow):
    """Detailed subtitle/plot cross-check payload."""

    pass


class MusicRow(BaseModel):
    """Single inferred music metadata row."""

    path: str = Field(..., description="Inventory path for the audio file.")
    drive_label: str = Field(..., description="Drive label owning the shard entry.")
    ext: Optional[str] = Field(None, description="Lowercase file extension cached during the scan.")
    artist: Optional[str] = Field(None, description="Best-effort artist parsed from the filename or folders.")
    title: Optional[str] = Field(None, description="Best-effort track title parsed from the filename or folders.")
    album: Optional[str] = Field(None, description="Album inferred from surrounding folders when available.")
    track_no: Optional[str] = Field(None, description="Track number token detected in the filename or folders.")
    confidence: float = Field(..., description="Confidence score produced by the music parser (0..1).")
    reasons: List[str] = Field(
        default_factory=list,
        description="Reasons contributing to the confidence score, already sorted by relevance.",
    )
    suggestions: List[str] = Field(
        default_factory=list,
        description="Parser follow-up notes such as alternate splits or missing metadata.",
    )
    parsed_utc: Optional[str] = Field(
        None, description="Timestamp (UTC) when the metadata row was generated."
    )


class MusicResponse(PaginatedResponse):
    """Paginated response containing inferred music metadata."""

    drive_label: str = Field(..., description="Drive label referenced by the query.")
    results: List[MusicRow] = Field(
        ..., description="Music metadata rows ordered by confidence then path."
    )


class MusicReviewEntry(BaseModel):
    """Row queued for manual music metadata review."""

    path: str = Field(..., description="Inventory path pending manual review.")
    drive_label: str = Field(..., description="Drive label owning this queued row.")
    ext: Optional[str] = Field(None, description="Lowercase file extension cached during the scan.")
    confidence: float = Field(..., description="Current parser confidence score (0..1).")
    reasons: List[str] = Field(
        default_factory=list, description="Reasons explaining why the entry needs review."
    )
    suggestions: List[str] = Field(
        default_factory=list,
        description="Actionable suggestions to resolve the review entry.",
    )
    queued_utc: Optional[str] = Field(
        None, description="Timestamp (UTC) when the entry was enqueued for review."
    )


class MusicReviewResponse(PaginatedResponse):
    """Paginated response exposing the manual music review queue."""

    drive_label: str = Field(..., description="Drive label referenced by the review query.")
    results: List[MusicReviewEntry] = Field(
        ..., description="Queued entries ordered by ascending confidence and enqueue time."
    )


class DrivesResponse(BaseModel):
    """Wrapper around drive list payload."""

    results: List[DriveInfo] = Field(..., description="Known drive entries in the catalog.")


class OverviewCategoryModel(BaseModel):
    category: str = Field(..., description="Category key (video/audio/image/etc.)")
    files: int = Field(..., description="File count within this category.")
    bytes: int = Field(..., description="Total bytes across the category.")


class OverviewReport(BaseModel):
    drive_label: str = Field(..., description="Drive label referenced by the report.")
    total_files: int = Field(..., description="Total number of files analysed.")
    total_size: int = Field(..., description="Aggregate size in bytes across the drive.")
    average_size: int = Field(..., description="Average file size in bytes.")
    source: str = Field(..., description="Source table used to build the report.")
    categories: List[OverviewCategoryModel] = Field(
        ..., description="Category-level breakdown rows."
    )


class StructureCandidateModel(BaseModel):
    source: str = Field(..., description="Signal source identifier (tmdb/imdb/opensubs/name).")
    candidate_id: Optional[str] = Field(
        None, description="Source-specific identifier such as TMDb or IMDb ID."
    )
    title: Optional[str] = Field(None, description="Candidate title returned by the source.")
    year: Optional[int] = Field(None, description="Candidate release year when available.")
    score: float = Field(..., description="Normalized score in the 0..1 range.")
    extra: Dict[str, object] = Field(
        default_factory=dict,
        description="Additional metadata returned by the lookup source.",
    )


class StructureFolderProfile(BaseModel):
    folder_path: str = Field(..., description="Folder path relative to the scanned drive.")
    kind: Optional[str] = Field(None, description="Detected content kind: movie/series/other.")
    main_video_path: Optional[str] = Field(
        None, description="Relative path to the primary video file when detected."
    )
    parsed_title: Optional[str] = Field(None, description="Best-effort parsed title.")
    parsed_year: Optional[int] = Field(None, description="Best-effort parsed year.")
    assets: Dict[str, object] = Field(
        default_factory=dict, description="Detected local assets such as poster/subtitles." 
    )
    issues: List[str] = Field(default_factory=list, description="Recorded anomalies for the folder.")
    confidence: float = Field(..., description="Combined confidence score (0..1).")
    source_signals: Dict[str, float] = Field(
        default_factory=dict, description="Contribution of each signal to the confidence score."
    )
    updated_utc: Optional[str] = Field(
        None, description="Last time the profile was updated (UTC ISO format)."
    )
    candidates: List[StructureCandidateModel] = Field(
        default_factory=list,
        description="Sorted verification candidates associated with this folder.",
    )


class StructureSummaryResponse(BaseModel):
    drive_label: str = Field(..., description="Drive label referenced by the summary.")
    total: int = Field(..., description="Total profiled folders in this shard.")
    confident: int = Field(..., description="Folders with confidence >= high threshold.")
    medium: int = Field(..., description="Folders with confidence between low/high thresholds.")
    low: int = Field(..., description="Folders with confidence below the low threshold.")
    updated_utc: Optional[str] = Field(
        None, description="Most recent update timestamp across profiled folders."
    )


class StructureReviewEntry(BaseModel):
    folder_path: str = Field(..., description="Folder requiring manual review.")
    confidence: float = Field(..., description="Current confidence score (0..1).")
    reasons: List[str] = Field(
        default_factory=list, description="Reasons that led to manual review."
    )
    questions: List[Dict[str, object]] = Field(
        default_factory=list, description="Suggested follow-up actions/questions."
    )
    kind: Optional[str] = Field(None, description="Detected folder kind when available.")
    parsed_title: Optional[str] = Field(None, description="Best-effort parsed title.")
    parsed_year: Optional[int] = Field(None, description="Best-effort parsed year.")


class StructureReviewResponse(PaginatedResponse):
    drive_label: str = Field(..., description="Drive label referenced by the review queue.")
    results: List[StructureReviewEntry] = Field(
        ..., description="Manual review entries ordered by ascending confidence."
    )


class StructureDetailsResponse(BaseModel):
    drive_label: str = Field(..., description="Drive label referenced by the profile.")
    profile: StructureFolderProfile = Field(
        ..., description="Detailed folder profile including verification candidates."
    )


class TopExtensionEntryModel(BaseModel):
    extension: str = Field(..., description="File extension (lowercase, dotted).")
    files: int = Field(..., description="File count for this extension.")
    bytes: int = Field(..., description="Total bytes contributed by this extension.")
    rank_count: Optional[int] = Field(None, description="Rank based on file count.")
    rank_size: Optional[int] = Field(None, description="Rank based on total bytes.")


class TopExtensionsReport(BaseModel):
    drive_label: str = Field(..., description="Drive label referenced by the report.")
    limit: int = Field(..., description="Maximum number of rows returned.")
    entries: List[TopExtensionEntryModel] = Field(..., description="Top extension rows.")


class LargestFileModel(BaseModel):
    path: str = Field(..., description="Inventory path for the file.")
    size_bytes: int = Field(..., description="File size in bytes.")
    mtime_utc: Optional[str] = Field(None, description="Modification timestamp in UTC.")
    category: Optional[str] = Field(None, description="Category classification if available.")


class LargestFilesReport(BaseModel):
    drive_label: str = Field(..., description="Drive label referenced by the report.")
    limit: int = Field(..., description="Maximum number of rows returned.")
    results: List[LargestFileModel] = Field(..., description="Largest file rows.")


class HeaviestFolderModel(BaseModel):
    folder: str = Field(..., description="Folder path aggregated to the configured depth.")
    files: int = Field(..., description="File count within this aggregated folder.")
    bytes: int = Field(..., description="Total bytes across the folder tree.")


class HeaviestFoldersReport(BaseModel):
    drive_label: str = Field(..., description="Drive label referenced by the report.")
    depth: int = Field(..., description="Depth used to aggregate folders.")
    limit: int = Field(..., description="Maximum number of rows returned.")
    results: List[HeaviestFolderModel] = Field(..., description="Heaviest folder rows.")


class RecentChangeModel(BaseModel):
    path: str = Field(..., description="Inventory path for the file.")
    size_bytes: int = Field(..., description="File size in bytes.")
    mtime_utc: Optional[str] = Field(None, description="Modification timestamp in UTC.")
    category: Optional[str] = Field(None, description="Category classification if available.")


class RecentChangesReport(BaseModel):
    drive_label: str = Field(..., description="Drive label referenced by the report.")
    days: int = Field(..., description="Window in days used for the report.")
    limit: int = Field(..., description="Maximum number of rows returned.")
    total: int = Field(..., description="Total files matching the window across the inventory.")
    results: List[RecentChangeModel] = Field(..., description="Recent change rows.")
