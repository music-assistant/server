"""Tests for the Sendspin calibration chirp signal."""

from __future__ import annotations

import math
import struct
from itertools import pairwise

import pytest

from music_assistant.providers.sendspin.calibration_chirp import (
    BED_PEAK,
    BIT_DEPTH,
    CHANNELS,
    CHIRP_END_HZ,
    CHIRP_FRAMES,
    CHIRP_PEAK,
    CHIRP_START_HZ,
    PERIOD_FRAMES,
    SAMPLE_RATE,
    build_chirp_period,
    chirp_phase,
)

INT16_PEAK = 32767
# comfortably above the noise bed and below the chirp, so the first sample over it
# marks a chirp onset rather than a loud moment in the bed
ONSET_THRESHOLD = 1000


def test_period_is_one_buffer_of_stereo_pcm() -> None:
    """The generated period is exactly 1 second of 48 kHz 16-bit stereo PCM_S16LE."""
    assert (SAMPLE_RATE, BIT_DEPTH, CHANNELS) == (48000, 16, 2)
    assert PERIOD_FRAMES == 48000
    assert CHIRP_FRAMES == 2880
    assert len(build_chirp_period()) == 48000 * 2 * 2


def test_period_is_reproducible() -> None:
    """Two builds produce identical bytes, so the streamed loop is stable."""
    assert build_chirp_period() == build_chirp_period()


def test_both_channels_carry_identical_content() -> None:
    """Left and right hold the same signal, so no player derives a phase difference."""
    samples = struct.unpack(f"<{PERIOD_FRAMES * CHANNELS}h", build_chirp_period())
    assert samples[0::2] == samples[1::2]


def test_chirp_onsets_are_exactly_48000_frames_apart() -> None:
    """Repeating the period spaces consecutive chirp onsets exactly 1 second apart."""
    onsets = _chirp_onsets(_left_channel(build_chirp_period() * 3))
    assert len(onsets) == 3
    assert [later - earlier for earlier, later in pairwise(onsets)] == [48000, 48000]


def test_sweep_starts_and_ends_at_the_intended_frequencies() -> None:
    """The sweep runs from CHIRP_START_HZ to CHIRP_END_HZ over its full length."""
    assert chirp_phase(0) == 0.0
    assert _instantaneous_hz(0) == pytest.approx(CHIRP_START_HZ, rel=0.01)
    assert _instantaneous_hz(CHIRP_FRAMES - 1) == pytest.approx(CHIRP_END_HZ, rel=0.01)


def test_sweep_frequency_rises_monotonically() -> None:
    """The sweep never doubles back, so correlation cannot lock onto a second peak."""
    frequencies = [_instantaneous_hz(frame) for frame in range(0, CHIRP_FRAMES, 32)]
    assert frequencies == sorted(frequencies)


def test_chirp_peaks_at_the_intended_level() -> None:
    """The windowed sweep reaches -6 dBFS, leaving headroom for downstream DSP."""
    chirp = _left_channel(build_chirp_period())[:CHIRP_FRAMES]
    assert max(abs(sample) for sample in chirp) == pytest.approx(CHIRP_PEAK * INT16_PEAK, rel=0.01)


def test_chirp_fades_in_and_out_without_a_click() -> None:
    """The Hann window brings the sweep to zero at both of its edges."""
    chirp = _left_channel(build_chirp_period())[:CHIRP_FRAMES]
    assert chirp[0] == 0
    assert abs(chirp[-1]) <= BED_PEAK * INT16_PEAK


def test_gap_between_chirps_is_a_quiet_bed_not_digital_silence() -> None:
    """The rest of the period is audible-to-hardware but inaudible noise, never zeros."""
    bed = _left_channel(build_chirp_period())[CHIRP_FRAMES:]
    assert max(abs(sample) for sample in bed) <= BED_PEAK * INT16_PEAK
    assert any(bed)


def _left_channel(pcm: bytes) -> tuple[int, ...]:
    """Return the left channel of interleaved stereo PCM_S16LE as signed samples."""
    return struct.unpack(f"<{len(pcm) // 2}h", pcm)[0::2]


def _chirp_onsets(samples: tuple[int, ...]) -> list[int]:
    """Return the frame index of every chirp onset in the given mono samples."""
    onsets: list[int] = []
    for index, sample in enumerate(samples):
        if abs(sample) > ONSET_THRESHOLD and (not onsets or index - onsets[-1] >= CHIRP_FRAMES):
            onsets.append(index)
    return onsets


def _instantaneous_hz(frame: int) -> float:
    """Return the sweep's frequency at the given frame, from the slope of its phase."""
    return (chirp_phase(frame + 1) - chirp_phase(frame)) * SAMPLE_RATE / (2 * math.pi)
