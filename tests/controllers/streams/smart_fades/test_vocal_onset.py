"""Tests for reading a track's vocal onset off its stored vocal-activity timeline."""

from __future__ import annotations

import pytest

from music_assistant.controllers.streams.smart_fades.vocal import (
    VOCAL_ACTIVITY_BINS,
    VOCAL_LEFT_PADDING,
    first_vocal_onset,
)
from music_assistant.models.audio_analysis import AudioAnalysisData

# 180 s over 1800 bins: every bin is a tenth of a second
DURATION = 180.0


def _analysis(vocal_activity: list[float] | None) -> AudioAnalysisData:
    return AudioAnalysisData(duration=DURATION, bpm=120.0, vocal_activity=vocal_activity)


def test_first_vocal_onset_returns_the_padded_start_of_the_first_vocal_run() -> None:
    """The onset is where the first confident vocal run opens, less the detector's lag."""
    timeline = [0.0] * VOCAL_ACTIVITY_BINS
    # a short blip stays below the window gate, the real phrase starts at 30 s
    timeline[100:102] = [0.9, 0.9]
    timeline[300:400] = [0.9] * 100
    timeline[600:700] = [0.9] * 100

    onset = first_vocal_onset(_analysis(timeline))

    assert onset == pytest.approx(30.0 - VOCAL_LEFT_PADDING)


def test_first_vocal_onset_is_none_without_a_timeline() -> None:
    """A track analysed without vocal activity has no known onset."""
    assert first_vocal_onset(_analysis(None)) is None


def test_first_vocal_onset_is_none_without_a_vocal_run() -> None:
    """An instrumental timeline has no onset."""
    assert first_vocal_onset(_analysis([0.1] * VOCAL_ACTIVITY_BINS)) is None
