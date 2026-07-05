"""Tests for the pure numpy DBN postprocessor."""

from __future__ import annotations

import numpy as np
import pytest

from music_assistant.providers.smart_fades.dbn_postprocessor import (
    DBNDownBeatTracker,
    _viterbi_numba,
)


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
    result = tracker(combined)

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
    result = tracker(combined)

    beat_times = result[:, 0]
    # DBN should still find beats in the first 5s via tempo continuity
    early_beats = beat_times[beat_times < 5.0]
    assert len(early_beats) >= 5, f"Expected beats in intro region, got {len(early_beats)}"


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
    result = tracker(combined)

    assert result.ndim == 2
    assert result.shape[1] == 2
    # Column 0: times in seconds, should be positive and sorted
    assert np.all(result[:, 0] >= 0)
    assert np.all(np.diff(result[:, 0]) > 0)
    # Column 1: beat positions, integers 1..num_beats
    assert np.all(result[:, 1] >= 1)
    assert np.all(result[:, 1] <= 4)


# ---------------------------------------------------------------------------
# Decoder-optimization correctness
#
# The DBN decode runs either the numba-jitted path or the numpy fallback. Both
# must produce bit-identical beats/downbeats, and both must match a plain
# textbook Viterbi. These tests guard against any of the optimizations (the
# within-beat shift, the precomputed decode plan, the numba kernel) silently
# changing the detected beats.
# ---------------------------------------------------------------------------


def _synth_activations(seed: int, num_frames: int, bpm: float, beats_per_bar: int) -> np.ndarray:
    """Build a (T, 2) activation array with tempo drift and noise for a given meter."""
    rng = np.random.default_rng(seed)
    fps = 50
    beat_act = np.full(num_frames, 0.04)
    downbeat_act = np.full(num_frames, 0.02)
    pos = 0.0
    beat_idx = 0
    while pos < num_frames:
        frame = round(pos)
        if frame < num_frames:
            if beat_idx % beats_per_bar == 0:
                downbeat_act[frame] = 0.9
                beat_act[frame] = 0.5
            else:
                beat_act[frame] = 0.85
        # drift the tempo a little so beat spacing is not perfectly regular
        pos += (60.0 / bpm * fps) * (1.0 + 0.03 * (rng.random() - 0.5))
        beat_idx += 1
    beat_act = np.clip(beat_act + 0.04 * rng.random(num_frames), 1e-5, 0.999)
    downbeat_act = np.clip(downbeat_act + 0.02 * rng.random(num_frames), 1e-5, 0.999)
    return np.column_stack([np.maximum(beat_act - downbeat_act, 1e-5), downbeat_act])


def _naive_viterbi(
    log_dens: np.ndarray,
    om_pointers: np.ndarray,
    tm_states: np.ndarray,
    tm_pointers: np.ndarray,
    tm_log_probs: np.ndarray,
    num_states: int,
) -> np.ndarray:
    """
    Textbook Viterbi decode used as an independent reference.

    Mirrors the model semantics of the optimized decoders (a within-beat single
    predecessor carries log_prob 0; beat boundaries take the max over tempo
    predecessors, first max wins on ties) but uses a dense per-frame backpointer
    matrix and no vectorization tricks, so a divergence flags an optimization bug.
    """
    num_frames = len(log_dens)
    prev = np.full(num_states, np.float32(-np.log(num_states)), dtype=np.float32)
    backptr = np.full((num_frames, num_states), -1, dtype=np.int64)
    for t in range(num_frames):
        cur = np.full(num_states, -np.inf, dtype=np.float32)
        row = log_dens[t]
        for s in range(num_states):
            start, end = tm_pointers[s], tm_pointers[s + 1]
            obs = row[om_pointers[s]]
            if end - start == 1:
                src = tm_states[start]
                cur[s] = prev[src] + obs
                backptr[t, s] = src
            else:
                best, best_src = np.float32(-np.inf), -1
                for k in range(start, end):
                    score = prev[tm_states[k]] + tm_log_probs[k]
                    if score > best:
                        best, best_src = score, tm_states[k]
                cur[s] = best + obs
                backptr[t, s] = best_src
        prev = cur
    state = int(np.argmax(prev))
    path = np.empty(num_frames, dtype=np.int64)
    for t in range(num_frames - 1, -1, -1):
        path[t] = state
        state = backptr[t, state]
    return path


@pytest.mark.skipif(_viterbi_numba is None, reason="numba unavailable on this platform")
@pytest.mark.parametrize(
    ("seed", "num_frames", "bpm", "meter"),
    [
        (1, 1500, 120.0, 4),
        (2, 2200, 90.0, 3),
        (3, 1800, 174.0, 4),
        (4, 1200, 60.0, 4),
    ],
)
def test_numba_and_numpy_paths_agree(seed: int, num_frames: int, bpm: float, meter: int) -> None:
    """The numba-jitted decode must return bit-identical beats to the numpy fallback."""
    combined = _synth_activations(seed, num_frames, bpm, meter)
    tracker = DBNDownBeatTracker(beats_per_bar=[3, 4], min_bpm=55, max_bpm=215, fps=50)

    tracker._use_numba = True
    numba_out = tracker(combined)
    tracker._use_numba = False
    numpy_out = tracker(combined)

    assert numba_out.shape == numpy_out.shape
    assert np.array_equal(numba_out, numpy_out)


def test_numba_unavailable_uses_numpy_fallback() -> None:
    """When numba is disabled the tracker still decodes correctly via the numpy path."""
    combined = _synth_activations(5, 1500, 128.0, 4)
    tracker = DBNDownBeatTracker(beats_per_bar=[3, 4], min_bpm=55, max_bpm=215, fps=50)
    tracker._use_numba = False
    result = tracker(combined)

    assert result.ndim == 2
    assert result.shape[1] == 2
    assert np.all(np.diff(result[:, 0]) > 0)
    assert np.all((result[:, 1] >= 1) & (result[:, 1] <= 4))


def test_decoders_match_naive_reference() -> None:
    """Both optimized decode paths must match a plain textbook Viterbi.

    Uses a small tempo range so the naive O(T*S*P) reference is cheap to run.
    """
    # Narrow BPM range -> few tempo states, keeping the naive reference fast.
    tracker = DBNDownBeatTracker(beats_per_bar=[2], min_bpm=100, max_bpm=120, fps=50)
    combined = _synth_activations(11, 300, 110.0, 2)
    log_dens = tracker._compute_log_densities(combined, tracker.observation_lambda)

    hmm = tracker._hmms[0]
    plan = hmm["plan"]
    reference = _naive_viterbi(
        log_dens,
        hmm["om_pointers"],
        hmm["tm_states"],
        hmm["tm_pointers"],
        hmm["tm_log_probs"],
        plan["num_states"],
    )

    numpy_path, _ = tracker._viterbi(log_dens, plan)
    assert np.array_equal(numpy_path, reference)

    if _viterbi_numba is not None:
        numba_path, _ = _viterbi_numba(
            log_dens,
            hmm["om_pointers"],
            hmm["tm_states"],
            hmm["tm_pointers"],
            hmm["tm_log_probs"],
            plan["num_states"],
            plan["multi_lookup"],
            plan["single_sources"],
            len(plan["multi_states"]),
        )
        assert np.array_equal(numba_path, reference)
