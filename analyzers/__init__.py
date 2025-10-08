"""Lightweight analysis helpers for optional image/video embeddings."""

from .image_embed import ImageEmbedder, ImageEmbedderConfig, LightAnalysisModelError
from .video_thumb import VideoThumbnailAnalyzer, VideoFrameSample
from .persist import FeatureRecord, FeatureWriter, ensure_features_table

__all__ = [
    "ImageEmbedder",
    "ImageEmbedderConfig",
    "LightAnalysisModelError",
    "VideoThumbnailAnalyzer",
    "VideoFrameSample",
    "FeatureRecord",
    "FeatureWriter",
    "ensure_features_table",
]
