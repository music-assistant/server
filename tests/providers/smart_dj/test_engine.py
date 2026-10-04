"""Focused Smart DJ engine tests."""

from typing import Any

from music_assistant.providers.smart_dj.engine import (
    MODES,
    DJControls,
    SignalControl,
    beam_optimize,
    score_candidate,
)


def test_hard_bpm_rule_is_never_violated() -> None:
    """Hard bpm rule is never violated."""
    score, _reasons, violations = score_candidate(
        {"bpm": 120, "camelot": "8A"},
        {"bpm": 160, "camelot": "8A"},
        MODES["ai_dj"],
        DJControls(bpm=SignalControl("hard")),
    )
    assert score == 0.0
    assert "hard BPM compatibility" in violations


def test_disabled_key_does_not_affect_score() -> None:
    """Disabled key does not affect score."""
    # Use a compatible adjacent key so the default hard key_relation does not
    # ban the candidate before soft scoring is consulted.
    current = {"bpm": 120, "camelot": "8A", "energy": 0.5}
    candidate = {"bpm": 120, "camelot": "9A", "energy": 0.5}
    soft, _, _ = score_candidate(current, candidate, MODES["ai_dj"], DJControls())
    disabled, _, _ = score_candidate(
        current, candidate, MODES["ai_dj"], DJControls(key=SignalControl("disabled"))
    )
    assert disabled > 0
    assert soft != disabled


def test_missing_analysis_is_preserved_as_a_barrier() -> None:
    """Missing analysis is preserved as a barrier."""
    tracks: list[dict[str, Any]] = [
        {
            "queue_item_id": "a",
            "artist": "A",
            "analysis": {"bpm": 120, "camelot": "8A", "energy": 0.5},
        },
        {"queue_item_id": "b", "artist": "B", "analysis": None},
        {
            "queue_item_id": "c",
            "artist": "C",
            "analysis": {"bpm": 121, "camelot": "8A", "energy": 0.5},
        },
    ]
    result = beam_optimize(tracks, tracks[0]["analysis"], MODES["ai_dj"], controls=DJControls())
    assert [x["queue_item_id"] for x in result] == ["a", "b", "c"]
    assert "analysis unavailable; preserved" in result[1]["reasons"]


def test_required_and_excluded_are_enforced() -> None:
    """Required and excluded are enforced."""
    tracks: list[dict[str, Any]] = [
        {
            "queue_item_id": "a",
            "artist": "A",
            "analysis": {"bpm": 120, "camelot": "8A", "energy": 0.5},
        },
        {
            "queue_item_id": "b",
            "artist": "B",
            "analysis": {"bpm": 121, "camelot": "8A", "energy": 0.5},
        },
        {
            "queue_item_id": "c",
            "artist": "C",
            "analysis": {"bpm": 122, "camelot": "8A", "energy": 0.5},
        },
    ]
    result = beam_optimize(
        tracks[1:],
        tracks[0]["analysis"],
        MODES["ai_dj"],
        controls=DJControls(
            required_ids=frozenset({"c"}),
            excluded_ids=frozenset({"b"}),
        ),
    )
    assert [x["queue_item_id"] for x in result] == ["c"]


def test_fixed_position_is_preserved() -> None:
    """Fixed position is preserved."""
    tracks: list[dict[str, Any]] = [
        {
            "queue_item_id": "a",
            "artist": "A",
            "analysis": {"bpm": 120, "camelot": "8A", "energy": 0.5},
        },
        {
            "queue_item_id": "b",
            "artist": "B",
            "analysis": {"bpm": 121, "camelot": "8A", "energy": 0.5},
        },
        {
            "queue_item_id": "c",
            "artist": "C",
            "analysis": {"bpm": 122, "camelot": "8A", "energy": 0.5},
        },
    ]
    result = beam_optimize(
        tracks,
        tracks[0]["analysis"],
        MODES["ai_dj"],
        controls=DJControls(fixed_ids=frozenset({"b"})),
    )
    assert [x["queue_item_id"] for x in result][1] == "b"


def test_artist_repeat_limit_is_applied() -> None:
    """Artist repeat limit is applied."""
    tracks: list[dict[str, Any]] = [
        {
            "queue_item_id": "a",
            "artist": "A",
            "analysis": {"bpm": 120, "camelot": "8A", "energy": 0.5},
        },
        {
            "queue_item_id": "b",
            "artist": "A",
            "analysis": {"bpm": 121, "camelot": "8A", "energy": 0.5},
        },
        {
            "queue_item_id": "c",
            "artist": "B",
            "analysis": {"bpm": 122, "camelot": "8A", "energy": 0.5},
        },
    ]
    anchor = {**tracks[0]["analysis"], "artist": tracks[0]["artist"]}
    result = beam_optimize(
        tracks[1:],
        anchor,
        MODES["ai_dj"],
        controls=DJControls(max_artist_repeat=1),
    )
    assert [x["queue_item_id"] for x in result] == ["c", "b"]


def test_excluded_track_does_not_consume_fixed_interval() -> None:
    """Excluded track does not consume fixed interval."""
    tracks: list[dict[str, Any]] = [
        {
            "queue_item_id": "a",
            "artist": "A",
            "analysis": {"bpm": 120, "camelot": "8A", "energy": 0.5},
        },
        {
            "queue_item_id": "x",
            "artist": "X",
            "analysis": {"bpm": 121, "camelot": "8A", "energy": 0.5},
        },
        {
            "queue_item_id": "b",
            "artist": "B",
            "analysis": {"bpm": 122, "camelot": "8A", "energy": 0.5},
        },
    ]
    result = beam_optimize(
        tracks,
        tracks[0]["analysis"],
        MODES["ai_dj"],
        controls=DJControls(
            fixed_ids=frozenset({"b"}),
            excluded_ids=frozenset({"x"}),
        ),
    )
    assert [x["queue_item_id"] for x in result] == ["a", "b"]
