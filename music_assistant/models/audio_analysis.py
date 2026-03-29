"""Data model for audio analysis results stored by Audio Analysis providers."""

from __future__ import annotations

from dataclasses import dataclass, fields

import numpy as np
import numpy.typing as npt
from mashumaro import DataClassDictMixin
from mashumaro.config import BaseConfig


@dataclass(kw_only=True)
class AudioAnalysisData(DataClassDictMixin):
    """Audio analysis data using standard MIR (Music Information Retrieval) concepts.

    All fields are optional. Each Audio Analysis provider fills in what it can
    produce. Multiple providers' data is stored in separate rows and can be
    aggregated for consumers via latest-write-wins merge.
    """

    # Rhythm
    bpm: float | None = None
    beats: npt.NDArray[np.float64] | None = None
    downbeats: npt.NDArray[np.float64] | None = None
    # Tonal
    key: str | None = None  # e.g. "C", "F#", "Bb"
    key_scale: str | None = None  # e.g. "MAJOR", "MINOR"
    # General
    duration: float | None = None

    class Config(BaseConfig):  # noqa: D106
        serialization_strategy = {
            np.ndarray: {
                "serialize": lambda x: x.tolist(),
                "deserialize": lambda x: np.array(x),
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
