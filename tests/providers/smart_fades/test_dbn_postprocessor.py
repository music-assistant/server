"""Tests for the pure numpy DBN postprocessor."""

from __future__ import annotations

import numpy as np
import pytest

from music_assistant.providers.smart_fades.dbn_postprocessor import DBNDownBeatTracker


def test_state_space_sizes() -> None:
    """Test that the bar state space has correct dimensions."""
    positions, intervals = DBNDownBeatTracker._build_bar_state_space(
        num_beats=4, min_interval=14, max_interval=55
    )
    # Each interval i contributes i states, repeated for each beat
    expected_states = sum(range(14, 56)) * 4
    assert len(positions) == expected_states
    assert len(intervals) == expected_states
    # Positions for a 4-beat bar range from [0, 4)
    assert positions.min() >= 0.0
    assert positions.max() < 4.0


def test_state_space_small() -> None:
    """Test state space with small interval range for easy verification."""
    positions, _intervals = DBNDownBeatTracker._build_bar_state_space(
        num_beats=2, min_interval=3, max_interval=4
    )
    # interval=3: 3 states, interval=4: 4 states, x 2 beats = 14 states
    assert len(positions) == 14
    # Beat 0 positions: [0/3, 1/3, 2/3, 0/4, 1/4, 2/4, 3/4]
    # Beat 1 positions: [1+0/3, 1+1/3, 1+2/3, 1+0/4, 1+1/4, 1+2/4, 1+3/4]
    assert positions[0] == pytest.approx(0.0)
    assert positions[7] == pytest.approx(1.0)


def test_transition_model_structure() -> None:
    """Test that the transition model is a valid sparse matrix."""
    positions, intervals = DBNDownBeatTracker._build_bar_state_space(
        num_beats=4, min_interval=14, max_interval=55
    )
    tm_states, tm_pointers, _tm_log_probs = DBNDownBeatTracker._build_transition_model(
        positions, intervals, num_beats=4, transition_lambda=100
    )
    num_states = len(positions)
    # pointers has num_states + 1 entries (CSR format)
    assert len(tm_pointers) == num_states + 1
    # Every state must have at least one predecessor
    for s in range(num_states):
        assert tm_pointers[s + 1] > tm_pointers[s]
    # All source state indices must be valid
    assert tm_states.max() < num_states


def test_dbn_tracker_constant_tempo() -> None:
    """Test that the DBN produces regular beats for a constant-tempo signal."""
    fps = 50
    bpm = 120.0
    duration = 10.0  # seconds
    num_frames = int(duration * fps)

    # Synthesize activations: strong peaks at expected beat positions
    interval = 60.0 / bpm * fps  # frames per beat
    beat_act = np.full(num_frames, 0.05)
    downbeat_act = np.full(num_frames, 0.02)
    for i in range(int(duration * bpm / 60)):
        frame = int(i * interval)
        if frame < num_frames:
            beat_act[frame] = 0.95
            if i % 4 == 0:
                downbeat_act[frame] = 0.95

    combined = np.column_stack(
        [
            np.maximum(beat_act - downbeat_act, 1e-5),
            downbeat_act,
        ]
    )

    tracker = DBNDownBeatTracker(beats_per_bar=[4], min_bpm=55, max_bpm=215, fps=fps)
    result, _num_beats = tracker(combined)

    # Should detect ~20 beats in 10s at 120 BPM
    beat_times = result[:, 0]
    assert 18 <= len(beat_times) <= 22

    # Inter-beat intervals should be close to 0.5s
    ibis = np.diff(beat_times)
    assert np.all(np.abs(ibis - 0.5) < 0.06)

    # Should have downbeats (beat_position == 1)
    downbeat_mask = result[:, 1] == 1
    assert downbeat_mask.sum() >= 2


def test_dbn_tracker_fills_intro_gaps() -> None:
    """Test that the DBN fills in regular beats even when the intro has no peaks."""
    fps = 50
    bpm = 120.0
    duration = 10.0
    num_frames = int(duration * fps)

    interval = 60.0 / bpm * fps
    beat_act = np.full(num_frames, 0.05)
    downbeat_act = np.full(num_frames, 0.02)

    # Only place peaks after 5s (second half)
    for i in range(int(5.0 * bpm / 60), int(duration * bpm / 60)):
        frame = int(i * interval)
        if frame < num_frames:
            beat_act[frame] = 0.95
            if i % 4 == 0:
                downbeat_act[frame] = 0.95

    combined = np.column_stack(
        [
            np.maximum(beat_act - downbeat_act, 1e-5),
            downbeat_act,
        ]
    )

    tracker = DBNDownBeatTracker(beats_per_bar=[4], min_bpm=55, max_bpm=215, fps=fps, threshold=0.0)
    result, _num_beats = tracker(combined)

    beat_times = result[:, 0]
    # DBN should still find beats in the first 5s via tempo continuity
    early_beats = beat_times[beat_times < 5.0]
    assert len(early_beats) >= 5, f"Expected beats in intro region, got {len(early_beats)}"


def test_winning_meter_is_returned() -> None:
    """A 4/4 activation pattern reports beats_per_bar=4 alongside the beats."""
    fps = 50
    bpm = 120.0
    duration = 20.0
    num_frames = int(duration * fps)

    interval = 60.0 / bpm * fps
    beat_act = np.full(num_frames, 0.05)
    downbeat_act = np.full(num_frames, 0.02)
    for i in range(int(duration * bpm / 60)):
        frame = int(i * interval)
        if frame < num_frames:
            beat_act[frame] = 0.95
            if i % 4 == 0:
                downbeat_act[frame] = 0.95

    combined = np.column_stack(
        [
            np.maximum(beat_act - downbeat_act, 1e-5),
            downbeat_act,
        ]
    )

    tracker = DBNDownBeatTracker(beats_per_bar=[3, 4], min_bpm=55, max_bpm=215, fps=fps)
    result, num_beats = tracker(combined)

    assert num_beats == 4
    assert len(result) > 0


def test_dbn_tracker_output_format() -> None:
    """Test that output matches the madmom interface: (M, 2) with [time, beat_pos]."""
    fps = 50
    num_frames = 500  # 10s
    rng = np.random.default_rng(42)
    combined = rng.uniform(0.01, 0.1, (num_frames, 2)).astype(np.float64)
    # Place a few clear peaks
    for i in range(0, num_frames, 25):  # 120 BPM
        combined[i, 0] = 0.9
        if (i // 25) % 4 == 0:
            combined[i, 1] = 0.9

    tracker = DBNDownBeatTracker(beats_per_bar=[4], min_bpm=55, max_bpm=215, fps=fps)
    result, _num_beats = tracker(combined)

    assert result.ndim == 2
    assert result.shape[1] == 2
    # Column 0: times in seconds, should be positive and sorted
    assert np.all(result[:, 0] >= 0)
    assert np.all(np.diff(result[:, 0]) > 0)
    # Column 1: beat positions, integers 1..num_beats
    assert np.all(result[:, 1] >= 1)
    assert np.all(result[:, 1] <= 4)
