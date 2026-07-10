"""
Data model for audio analysis results stored by Audio Analysis providers.

Stays server-local: the numpy.ndarray fields of AudioAnalysisData would force
numpy as an upstream dep on every consumer of music_assistant_models if this
module moved. The lightweight AudioAnalysisCoverage shape lives upstream at
music_assistant_models.audio_analysis instead.

AudioAnalysisData itself lives in _audio_analysis_data and is re-exported
lazily (PEP 562): defining it needs numpy plus the mashumaro codegen, and this
module is imported by the base server (AudioAnalysisError), which must not pay
that cost at startup.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime

    from music_assistant.models._audio_analysis_data import AudioAnalysisData

__all__ = ["AudioAnalysisData", "AudioAnalysisError"]


class AudioAnalysisError(Exception):
    """Raised by an Audio Analysis provider to fail the current analysis."""

    def __init__(self, reason: str, retry_at: datetime | None = None) -> None:
        """
        Initialize the error.

        :param reason: Human-readable failure reason.
        :param retry_at: Timezone-aware datetime when a retry is allowed; None (default)
            means do not retry.
        """
        if retry_at is not None and retry_at.tzinfo is None:
            raise ValueError("retry_at must be timezone-aware")
        super().__init__(reason)
        self.reason = reason
        self.retry_at = retry_at


def __getattr__(name: str) -> Any:
    if name == "AudioAnalysisData":
        from music_assistant.models._audio_analysis_data import (  # noqa: PLC0415
            AudioAnalysisData,
        )

        # cache in the module namespace so subsequent imports bypass __getattr__
        globals()["AudioAnalysisData"] = AudioAnalysisData
        return AudioAnalysisData
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
