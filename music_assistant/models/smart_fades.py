"""Data models for Smart Fades analysis and configuration."""

from dataclasses import dataclass, field
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
    FULL_SONG = 3


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


@dataclass
class MusicalKey(DataClassDictMixin):
    """Musical key estimation result from chroma analysis."""

    root: str  # "C", "C#", "D", etc. (from chroma bin index)
    mode: str  # "major" or "minor"
    confidence: float  # Correlation coefficient [0, 1]


@dataclass
class TimeSignature(DataClassDictMixin):
    """Time signature estimation result from accent pattern analysis."""

    beats_per_bar: int  # Number of beats per bar (3, 4, 6, etc.)
    confidence: float  # Estimation confidence [0, 1]


@dataclass
class PhraseBoundary(DataClassDictMixin):
    """Phrase/section boundary detected in the audio."""

    time: float  # Position in seconds
    confidence: float  # Detection confidence [0, 1]
    boundary_type: str  # "phrase" or "section"


@dataclass
class ExtendedSmartFadesAnalysis(DataClassDictMixin):
    """Extended beat tracking analysis with full-song features.

    This extends the basic SmartFadesAnalysis with additional features computed
    from whole-song streaming analysis: musical key, phrase boundaries, and
    energy/brightness curves for improved crossfade timing.
    """

    # Core fields (aligned with librosa beat_track output)
    bpm: float  # From tempo array, converted to float
    beats: npt.NDArray[np.float64]  # Beat times in seconds
    downbeats: npt.NDArray[np.float64]  # Downbeat times in seconds
    confidence: float  # Beat interval consistency
    duration: float  # Total track duration

    # Extended fields
    musical_key: MusicalKey | None = None
    time_signature: TimeSignature | None = None
    phrase_boundaries: list[PhraseBoundary] = field(default_factory=list)

    # Per-second energy/brightness curves (downsampled from per-frame librosa output)
    # Per-second is sufficient for crossfade timing decisions (~10KB for 10-min song)
    energy_curve: npt.NDArray[np.float32] | None = None  # RMS energy per second
    spectral_centroid_curve: npt.NDArray[np.float32] | None = None  # Spectral centroid (Hz)

    full_song_analysis: bool = False
    analysis_version: int = 2

    # Storage identifiers
    item_id: str = ""
    provider: str = ""

    class Config(BaseConfig):  # noqa: D106
        serialization_strategy = {
            np.ndarray: {"serialize": lambda x: x.tolist(), "deserialize": lambda x: np.array(x)}
        }

    def to_fragment_analysis(
        self, fragment: SmartFadesAnalysisFragment, fragment_duration: float = 45.0
    ) -> SmartFadesAnalysis:
        """Convert to legacy SmartFadesAnalysis for backward compatibility.

        :param fragment: The fragment type (INTRO or OUTRO) to extract.
        :param fragment_duration: Duration in seconds to extract for the fragment.
        """
        if fragment == SmartFadesAnalysisFragment.INTRO:
            # Extract beats/downbeats from the beginning
            beats_mask = self.beats < fragment_duration
            beats = self.beats[beats_mask]
            downbeats_mask = self.downbeats < fragment_duration
            downbeats = self.downbeats[downbeats_mask]
        elif fragment == SmartFadesAnalysisFragment.OUTRO:
            # Extract beats/downbeats from the end, shifted to start at 0
            outro_start = max(0, self.duration - fragment_duration)
            beats_mask = self.beats >= outro_start
            beats = self.beats[beats_mask] - outro_start
            downbeats_mask = self.downbeats >= outro_start
            downbeats = self.downbeats[downbeats_mask] - outro_start
        else:
            # FULL_SONG - return all beats
            beats = self.beats
            downbeats = self.downbeats

        return SmartFadesAnalysis(
            fragment=fragment,
            bpm=self.bpm,
            beats=beats,
            downbeats=downbeats,
            confidence=self.confidence,
            duration=min(fragment_duration, self.duration),
        )
