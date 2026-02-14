"""Helper utilities for the Smart Fades provider."""

from __future__ import annotations

from typing import Any

import numpy as np

from music_assistant.models.smart_fades import SmartFadesAnalysis, SmartFadesAnalysisFragment


def build_smart_fades_analysis(result: dict[str, Any]) -> SmartFadesAnalysis | None:
    """Build a SmartFadesAnalysis from raw beat detection results.

    :param result: Raw result dict from BeatThisStreamingProcessor.finalize()
                   with keys "beats", "downbeats", and "duration".
    :return: SmartFadesAnalysis if sufficient beats were detected, None otherwise.
    """
    beats = result.get("beats", [])
    downbeats = result.get("downbeats", [])
    duration = result.get("duration", 0.0)

    if len(beats) < 2:
        return None

    beats_arr = np.array(beats) if not isinstance(beats, np.ndarray) else beats
    downbeats_arr = np.array(downbeats) if not isinstance(downbeats, np.ndarray) else downbeats

    beat_intervals = np.diff(beats_arr)
    bpm = 60.0 / np.mean(beat_intervals)

    # Confidence based on beat interval consistency (coefficient of variation)
    cv = float(np.std(beat_intervals) / np.mean(beat_intervals))
    confidence = max(0.0, min(1.0, 1.0 - cv))

    return SmartFadesAnalysis(
        fragment=SmartFadesAnalysisFragment.FULL_SONG,
        bpm=float(bpm),
        beats=beats_arr,
        downbeats=downbeats_arr,
        confidence=confidence,
        duration=duration,
    )
