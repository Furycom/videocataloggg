
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


class DrivesResponse(BaseModel):
    """Wrapper around drive list payload."""

    results: List[DriveInfo] = Field(..., description="Known drive entries in the catalog.")


class OverviewCategoryModel(BaseModel):
    category: str = Field(..., description="Category key (video/audio/image/etc.)")
    files: int = Field(..., description="File count within this category.")
    bytes: int = Field(..., description="Total bytes across the category.")


class OverviewReport(BaseModel):
    drive_label: str = Field(..., description="Drive label referenced by the report.")
    total_files: int = Field(..., description="Total files discovered in the inventory.")
    total_size: int = Field(..., description="Aggregate size in bytes.")
    average_size: int = Field(..., description="Average file size in bytes.")
    source: str = Field(..., description="Data source used (inventory or inventory_stats).")
    categories: List[OverviewCategoryModel] = Field(
        ..., description="Category-level breakdown rows."
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
