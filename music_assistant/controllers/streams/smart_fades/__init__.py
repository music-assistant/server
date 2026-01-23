"""Smart Fades - Audio analyzer and mixer."""

from __future__ import annotations

from .analyzer import SmartFadesAnalyzer
from .extended_analysis import ExtendedAnalysisProcessor
from .feature_accumulator import FeatureAccumulator
from .mixer import SmartFadesMixer
from .streaming_extractor import StreamingFeatureExtractor

__all__ = [
    "ExtendedAnalysisProcessor",
    "FeatureAccumulator",
    "SmartFadesAnalyzer",
    "SmartFadesMixer",
    "StreamingFeatureExtractor",
]
