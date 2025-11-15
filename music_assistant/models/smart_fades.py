"""Data models for Smart Fades analysis and configuration."""

from dataclasses import dataclass
from enum import IntEnum, StrEnum

import numpy as np
import numpy.typing as npt
from mashumaro import DataClassDictMixin
from mashumaro.config import BaseConfig


class SmartFadesMode(StrEnum):
    """Smart fades modes."""

    SMART_CROSSFADE = "smart_crossfade"  # Use smart crossfade with beat matching and EQ filters
    STANDARD_CROSSFADE = "standard_crossfade"  # Use standard crossfade only
    DISABLED = "disabled"  # No crossfade


class SmartFadesAnalysisFragment(IntEnum):
    """Smart fades analysis fragment types."""

    INTRO = 1
    OUTRO = 2


@dataclass
class SmartFadesAnalysis(DataClassDictMixin):
    """Beat tracking analysis data for BPM matching crossfade."""

    fragment: SmartFadesAnalysisFragment
    bpm: float
    beats: npt.NDArray[np.float64]  # Beat positions
    downbeats: npt.NDArray[np.float64]  # Downbeat positions
    confidence: float  # Analysis confidence score 0-1
    duration: float = 0.0  # Duration of the track in seconds

    class Config(BaseConfig):  # noqa: D106
        serialization_strategy = {
            np.ndarray: {"serialize": lambda x: x.tolist(), "deserialize": lambda x: np.array(x)}
        }
