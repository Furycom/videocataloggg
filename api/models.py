
"""Pydantic schemas for the VideoCatalog local API."""
from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Simple health response with application metadata."""

    ok: bool = Field(True, description="Indicates the API server is reachable.")
    version: str = Field(..., description="Application version string.")
    time_utc: str = Field(..., description="Current UTC timestamp in ISO8601 format.")


class DriveInfo(BaseModel):
    """Metadata describing a catalogued drive."""

    label: str = Field(..., description="Drive label as registered in the catalog database.")
    type: Optional[str] = Field(None, description="Optional drive type preset.")
    last_scan_utc: Optional[str] = Field(
        None, description="Timestamp (UTC) of the last completed scan for this drive."
    )
    shard_path: str = Field(..., description="Absolute path to the shard SQLite database file.")


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
