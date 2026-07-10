"""
Implementation module for AudioAnalysisData.

Importing this module pulls in numpy (field types and serialization strategy)
and triggers the mashumaro codegen for the dataclass. Import the class through
music_assistant.models.audio_analysis, which resolves it lazily so the base
server import graph stays numpy-free.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

import numpy as np
import numpy.typing as npt
from mashumaro import DataClassDictMixin
from mashumaro.config import BaseConfig


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
    # Array of beat positions in seconds.
    beats: npt.NDArray[np.float32] | None = None
    # Array of downbeat (bar start) positions in seconds.
    downbeats: npt.NDArray[np.float32] | None = None
    # Number of beats in each bar indicating time signature, e.g. 3 for 3/4 waltz, 4 for 4/4 common time.
    beats_per_bar: int | None = None

    # Tonal

    # Pitch class of detected key, e.g. "C", "F#", "Bb".
    key: str | None = None
    # Tonality: "major" or "minor".
    mode: str | None = None

    # Spectral & Energy (fixed 1800 bins covering track duration)

    # RMS energy, normalized 0.0-1.0. Fixed 1800 bins.
    rms_energy: npt.NDArray[np.float32] | None = None
    # Spectral centroid in Hz. Fixed 1800 bins.
    spectral_centroid: npt.NDArray[np.float32] | None = None

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

    class Config(BaseConfig):
        serialization_strategy = {  # noqa: RUF012  # mashumaro Config attr; ClassVar conflicts with BaseConfig
            np.ndarray: {
                "serialize": lambda x: x.tolist(),
                "deserialize": lambda x: np.asarray(x, dtype=np.float32),
            }
        }

    def update(self, new_values: AudioAnalysisData) -> AudioAnalysisData:
        """Merge new analysis data (in-place). Latest-write-wins for non-None fields."""
        for fld in fields(self):
            new_val = getattr(new_values, fld.name)
            if new_val is None:
                continue
            setattr(self, fld.name, new_val)
        return self
