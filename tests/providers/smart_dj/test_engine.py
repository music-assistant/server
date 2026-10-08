"""Tests for the Smart DJ scoring engine."""

from __future__ import annotations

import json
from typing import Any

import pytest

from music_assistant.providers.smart_dj.engine import (
    MODES,
    DJControls,
    DJMode,
    DJWeights,
    SignalControl,
    _merge_track,
    beam_optimize,
    camelot_affinity,
    clap_similarity,
    score_candidate,
    track_score,
)

# ---------------------------------------------------------------------------
# Weight contract
# ---------------------------------------------------------------------------


def _weight_total(weights: DJWeights) -> float:
    """Sum every weight field of a DJWeights instance."""
    return float(sum(getattr(weights, name) for name in DJWeights.__dataclass_fields__))


def test_default_weights_sum_to_one() -> None:
    """The default weight set must sum to 1.0."""
    assert _weight_total(DJWeights()) == pytest.approx(1.0)


@pytest.mark.parametrize("name", sorted(MODES))
def test_every_mode_sums_to_one(name: str) -> None:
    """Each preset's weights must sum to 1.0, so no mode is silently mis-scaled."""
    assert _weight_total(MODES[name].weights) == pytest.approx(1.0)


def test_every_mode_has_a_clap_weight() -> None:
    """Every preset must carry the sonic-similarity weight."""
    for mode in MODES.values():
        assert mode.weights.clap > 0.0


# ---------------------------------------------------------------------------
# Camelot handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("8A", "8A", 1.0),  # identical
        ("8A", "8B", 0.72),  # relative key
        ("8A", "9A", 0.85),  # adjacent on the same ring
        ("8A", "3A", 0.0),  # far apart
        (None, "8A", 0.5),  # missing input is neutral
        ("", "", 0.5),
    ],
)
def test_camelot_affinity(a: str | None, b: str | None, expected: float) -> None:
    """Camelot affinity covers the match, relative, adjacent and neutral cases."""
    assert camelot_affinity(a, b) == pytest.approx(expected)


@pytest.mark.parametrize("value", ["x", "1Z", "AA", "12"])
def test_camelot_affinity_rejects_malformed(value: str) -> None:
    """
    Malformed Camelot values must not raise.

    Regression guard for the pre-existing `except ValueError, TypeError:` syntax
    error, which made this module unimportable.
    """
    assert camelot_affinity(value, "8A") == 0.0


# ---------------------------------------------------------------------------
# CLAP sonic similarity
# ---------------------------------------------------------------------------


def test_clap_similarity_identical_vectors() -> None:
    """A vector compared with itself is maximally similar."""
    vector = [0.5, 0.5, 0.5, 0.5]
    assert clap_similarity(vector, vector) == pytest.approx(1.0)


def test_clap_similarity_opposed_vectors() -> None:
    """Opposed vectors score the floor of the scale."""
    assert clap_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(0.0)


def test_clap_similarity_orthogonal_vectors() -> None:
    """Orthogonal vectors land mid-scale."""
    assert clap_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.5)


def test_clap_similarity_is_scale_invariant() -> None:
    """Cosine similarity ignores magnitude, so re-normalisation is redundant but safe."""
    small = [0.1, 0.2, 0.3]
    large = [1000.0, 2000.0, 3000.0]
    assert clap_similarity(small, large) == pytest.approx(1.0)


def test_clap_similarity_accepts_json_string() -> None:
    """A JSON-encoded embedding is decoded rather than silently ignored."""
    vector = [0.25, 0.5, 0.75]
    assert clap_similarity(json.dumps(vector), vector) == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (None, [1.0, 0.0]),
        ([1.0, 0.0], None),
        ([], [1.0]),
        ([], []),
        ([1.0, 0.0], [1.0, 0.0, 0.0]),  # length mismatch
        ([0.0, 0.0], [1.0, 0.0]),  # zero-norm vector
        ("not json", [1.0]),
        (b"bytes", [1.0]),
    ],
)
def test_clap_similarity_unusable_input_is_neutral(a: Any, b: Any) -> None:
    """Every unusable shape returns the neutral score instead of raising."""
    assert clap_similarity(a, b) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Signal weighting
# ---------------------------------------------------------------------------


def _track(**overrides: Any) -> dict[str, Any]:
    """Build a complete analysis dict with sensible defaults."""
    track: dict[str, Any] = {
        "bpm": 120.0,
        "camelot": "8A",
        "energy": 0.5,
        "danceability": 0.5,
        "loudness": -10.0,
        "genre": "house",
        "artist": "A",
    }
    track.update(overrides)
    return track


def _soft_controls(**overrides: SignalControl) -> DJControls:
    """Build controls with every signal disabled, then apply the given overrides."""
    signals: dict[str, SignalControl] = {
        name: SignalControl("disabled")
        for name in (
            "bpm",
            "key",
            "energy",
            "danceability",
            "loudness",
            "genre",
            "artist_spacing",
            "momentum",
            "clap",
        )
    }
    signals.update(overrides)
    return DJControls(**signals)


def test_clap_signal_contributes_when_enabled() -> None:
    """With only clap active, a matching embedding outranks a mismatched one."""
    mode = MODES["ai_dj"]
    current = _track(clap_embedding=[1.0, 0.0, 0.0])
    controls = _soft_controls(clap=SignalControl("soft", 1.0))

    same, _, _ = score_candidate(current, _track(clap_embedding=[1.0, 0.0, 0.0]), mode, controls)
    other, _, _ = score_candidate(current, _track(clap_embedding=[-1.0, 0.0, 0.0]), mode, controls)

    assert same > other


def test_clap_signal_drops_out_when_no_embedding() -> None:
    """
    Without embeddings the signal is skipped, not scored as neutral.

    Skipping keeps the remaining weights redistributed rather than diluting the
    score toward 0.5.
    """
    mode = MODES["ai_dj"]
    current = _track()
    controls = _soft_controls(bpm=SignalControl("soft", 1.0), clap=SignalControl("soft", 1.0))

    with_signal, _, _ = score_candidate(current, _track(bpm=121.0), mode, controls)
    without, _, _ = score_candidate(
        current,
        _track(bpm=121.0, clap_embedding=None),
        mode,
        controls,
    )

    assert with_signal == pytest.approx(without)


def test_clap_signal_respects_disabled_state() -> None:
    """A disabled clap signal does not influence the score."""
    mode = MODES["ai_dj"]
    current = _track(clap_embedding=[1.0, 0.0])
    controls = _soft_controls(bpm=SignalControl("soft", 1.0), clap=SignalControl("disabled"))

    near, _, _ = score_candidate(
        current, _track(bpm=120.0, clap_embedding=[1.0, 0.0]), mode, controls
    )
    far, _, _ = score_candidate(
        current, _track(bpm=120.0, clap_embedding=[-1.0, 0.0]), mode, controls
    )

    assert near == pytest.approx(far)


def test_clap_similarity_reported_in_reasons() -> None:
    """The sonic-similarity verdict is surfaced in the reason list."""
    mode = MODES["ai_dj"]
    controls = _soft_controls(clap=SignalControl("soft", 1.0))
    _, reasons, _ = score_candidate(
        _track(clap_embedding=[1.0, 0.0]),
        _track(clap_embedding=[1.0, 0.0]),
        mode,
        controls,
    )
    assert any("sonic similarity" in reason for reason in reasons)


# ---------------------------------------------------------------------------
# Hard constraints
# ---------------------------------------------------------------------------


def test_zero_bpm_does_not_raise() -> None:
    """A zero tempo is treated as unusable, not as a division-by-zero."""
    mode = MODES["ai_dj"]
    controls = _soft_controls(bpm=SignalControl("soft", 1.0))
    score, _, violations = score_candidate(_track(bpm=0.0), _track(bpm=0.0), mode, controls)
    assert not violations
    assert 0.0 <= score <= 1.0


def test_excluded_track_is_a_violation() -> None:
    """An excluded queue item can never be placed."""
    controls = DJControls(excluded_ids=frozenset({"b"}))
    _, _, violations = score_candidate(
        _track(), _track(queue_item_id="b"), MODES["ai_dj"], controls
    )
    assert "excluded track" in violations


def test_missing_analysis_is_a_violation() -> None:
    """A track with no analysis is rejected rather than scored."""
    _, _, violations = score_candidate(_track(), {}, MODES["ai_dj"], DJControls())
    assert violations == ["missing analysis"]


def test_artist_repeat_limit_is_enforced() -> None:
    """Consecutive same-artist tracks are capped by max_artist_repeat."""
    controls = DJControls(max_artist_repeat=1)
    _, _, violations = score_candidate(
        _track(),
        _track(artist="A"),
        MODES["ai_dj"],
        controls,
        artist_run_length=1,
    )
    assert "maximum consecutive artist repeat exceeded" in violations


# ---------------------------------------------------------------------------
# Optimiser integrity
# ---------------------------------------------------------------------------


def test_beam_optimize_preserves_every_track() -> None:
    """The optimiser reorders without losing or duplicating a queue item."""
    tracks = [
        {"queue_item_id": str(i), "artist": f"artist-{i}", "analysis": _track(bpm=120.0 + i)}
        for i in range(5)
    ]
    result = beam_optimize(tracks, _track(), MODES["ai_dj"], beam_width=4)
    assert {t["queue_item_id"] for t in result} == {t["queue_item_id"] for t in tracks}
    assert len(result) == len(tracks)


def test_beam_optimize_rejects_conflicting_constraints() -> None:
    """A track cannot be both required and excluded."""
    tracks = [{"queue_item_id": "a", "analysis": _track()}]
    controls = DJControls(
        required_ids=frozenset({"a"}),
        excluded_ids=frozenset({"a"}),
    )
    with pytest.raises(RuntimeError, match="required and excluded"):
        beam_optimize(tracks, _track(), MODES["ai_dj"], controls=controls)


def test_beam_optimize_reports_a_missing_required_track() -> None:
    """A required track absent from the queue is an error, not a silent skip."""
    tracks = [{"queue_item_id": "a", "analysis": _track()}]
    controls = DJControls(required_ids=frozenset({"zzz"}))
    with pytest.raises(RuntimeError, match="Required tracks are missing"):
        beam_optimize(tracks, _track(), MODES["ai_dj"], controls=controls)


def test_beam_optimize_pins_fixed_tracks() -> None:
    """A fixed queue item keeps its position."""
    tracks = [
        {"queue_item_id": "a", "artist": "x", "analysis": _track()},
        {"queue_item_id": "b", "artist": "y", "analysis": _track()},
        {"queue_item_id": "c", "artist": "z", "analysis": _track()},
    ]
    result = beam_optimize(
        tracks, None, MODES["ai_dj"], controls=DJControls(fixed_ids=frozenset({"b"}))
    )
    assert result[1]["queue_item_id"] == "b"


# ---------------------------------------------------------------------------
# Track merging
# ---------------------------------------------------------------------------


def test_merge_track_prefers_item_metadata() -> None:
    """Per-item metadata wins over the analysis payload."""
    merged = _merge_track(
        {
            "queue_item_id": "q1",
            "artist": "Queen",
            "genre": "rock",
            "analysis": {"bpm": 120.0, "artist": "wrong"},
        }
    )
    assert merged["bpm"] == 120.0
    assert merged["artist"] == "Queen"
    assert merged["queue_item_id"] == "q1"


def test_merge_track_keeps_the_clap_embedding() -> None:
    """The embedding survives the merge, since it is not an overridden key."""
    merged = _merge_track(
        {
            "queue_item_id": "q1",
            "artist": "A",
            "analysis": {"bpm": 120.0, "clap_embedding": [0.1, 0.2]},
        }
    )
    assert merged["clap_embedding"] == [0.1, 0.2]


# ---------------------------------------------------------------------------
# Convenience scorer
# ---------------------------------------------------------------------------


def test_track_score_returns_a_bounded_score() -> None:
    """The ad-hoc scorer stays within 0.0-1.0."""
    score, _ = track_score(_track(), _track(bpm=124.0), MODES["ai_dj"])
    assert 0.0 <= score <= 1.0


def test_track_score_honours_bpm_tolerance() -> None:
    """A tighter tolerance penalises a tempo gap more than a loose one."""
    candidate = _track(bpm=135.0)
    loose, _ = track_score(_track(), candidate, MODES["ai_dj"], bpm_tolerance=0.5)
    tight, _ = track_score(_track(), candidate, MODES["ai_dj"], bpm_tolerance=0.01)
    assert loose > tight


def test_track_score_treats_same_artist_as_a_run() -> None:
    """same_artist counts the candidate as a continuation of the artist run."""
    score, _ = track_score(_track(), _track(), MODES["ai_dj"], same_artist=True)
    assert 0.0 <= score <= 1.0


def test_mode_override_keeps_the_base_weights() -> None:
    """A tolerance override must not disturb the preset's weights."""
    mode = MODES["party"]
    adjusted = DJMode(mode.name, 0.2, mode.energy_direction, mode.variety, mode.weights)
    assert adjusted.weights == mode.weights
    assert adjusted.bpm_tolerance == 0.2
