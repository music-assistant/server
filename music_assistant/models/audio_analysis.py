"""
Data model for audio analysis results stored by Audio Analysis providers.

Stays server-local: the lightweight AudioAnalysisCoverage shape lives upstream
at music_assistant_models.audio_analysis; this fuller model is only needed by the
server-side providers and stream controllers that produce and consume it.

The rhythm/spectral fields are plain float lists, not numpy arrays, so importing
this model never pulls in numpy. The compute code that needs array math (smart
fades, sonic analysis) converts to numpy at the point of use — keeping numpy off
every install that only does e.g. loudness normalization.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any

from mashumaro import DataClassDictMixin


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


@dataclass(kw_only=True)
class AudioAnalysisData(DataClassDictMixin):
    """Shared audio analysis attributes produced by Audio Analysis providers."""

    # General

    # Track duration in seconds.
    duration: float | None = None

    # Loudness

    # EBU R128 integrated loudness in LUFS (typical range: -70.0 to 0.0).
    loudness_integrated: float | None = None
    # EBU R128 integrated album loudness in LUFS, from provider metadata/tags.
    loudness_album: float | None = None
    # EBU R128 loudness range in LU (typical range: 1.0 to 30.0).
    loudness_range: float | None = None
    # ITU-R BS.1770-4 true peak in dBTP (typical range: -20.0 to +3.0).
    true_peak: float | None = None

    # Rhythm

    # Beats per minute.
    bpm: float | None = None
    # Beat positions in seconds. Convert to a numpy array for array math.
    beats: list[float] | None = None
    # Downbeat (bar start) positions in seconds. Convert to a numpy array for array math.
    downbeats: list[float] | None = None
    # Number of beats in each bar indicating time signature, e.g. 3 for 3/4 waltz, 4 for 4/4 common time.
    beats_per_bar: int | None = None

    # Tonal

    # Pitch class of detected key, e.g. "C", "F#", "Bb".
    key: str | None = None
    # Tonality: "major" or "minor".
    mode: str | None = None

    # Spectral & Energy (fixed 1800 bins covering track duration)

    # RMS energy, normalized 0.0-1.0. Fixed 1800 bins. Convert to a numpy array for array math.
    rms_energy: list[float] | None = None
    # Spectral centroid in Hz. Fixed 1800 bins. Convert to a numpy array for array math.
    spectral_centroid: list[float] | None = None

    # High-Level Descriptors (all normalized 0.0-1.0)

    # Overall perceived energy: 0.0 = very low, 1.0 = very high.
    energy: float | None = None
    # Rhythmic regularity and groove: 0.0 = not danceable, 1.0 = very danceable.
    danceability: float | None = None
    # Musical mood: 0.0 = dark/sad, 1.0 = bright/happy.
    valence: float | None = None
    # Intensity/activation: 0.0 = calm/relaxed, 1.0 = energetic/aggressive.
    arousal: float | None = None
    # Speech presence: 0.0 = pure music, 1.0 = pure speech.
    speechiness: float | None = None
    # Vocal absence: 0.0 = prominent vocals, 1.0 = purely instrumental.
    instrumentalness: float | None = None
    # Acoustic character: 0.0 = electronic/produced, 1.0 = purely acoustic.
    acousticness: float | None = None
    # Tonal brightness: 0.0 = warm/dark, 1.0 = bright/sharp.
    brightness: float | None = None
    # Harmonic variety: 0.0 = simple/repetitive, 1.0 = complex/varied.
    harmonic_complexity: float | None = None
    # Timbral roughness: 0.0 = smooth/clean, 1.0 = rough/distorted.
    roughness: float | None = None
    # Beat consistency: 0.0 = free/rubato, 1.0 = metronomic.
    rhythmic_regularity: float | None = None

    # Visualization

    # Provider-Specific

    # Catch-all dict for provider-specific data
    extra_data: dict[str, Any] | None = None

    def update(self, new_values: AudioAnalysisData) -> AudioAnalysisData:
        """Merge new analysis data (in-place). Latest-write-wins for non-None fields."""
        for fld in fields(self):
            new_val = getattr(new_values, fld.name)
            if new_val is None:
                continue
            setattr(self, fld.name, new_val)
        return self
