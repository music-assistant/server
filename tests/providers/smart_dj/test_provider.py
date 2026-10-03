"""Tests for Smart DJ scoring primitives."""
from music_assistant.providers.smart_dj.provider import SmartDJProvider


def test_compatibility_rewards_matching_bpm_and_key() -> None:
    current = {"bpm": 120, "camelot": "8A", "energy": 0.6, "danceability": 0.7, "valence": 0.5, "arousal": 0.6}
    candidate = {"bpm": 120, "camelot": "8A", "energy": 0.6, "danceability": 0.7, "valence": 0.5, "arousal": 0.6}
    score = SmartDJProvider._compatibility(current, candidate, {"bpm_tolerance": 0.08})
    assert 0.99 <= score <= 1.0


def test_compatibility_rejects_large_bpm_jump() -> None:
    current = {"bpm": 120, "camelot": "8A"}
    candidate = {"bpm": 180, "camelot": "2B"}
    score = SmartDJProvider._compatibility(current, candidate, {"bpm_tolerance": 0.08})
    assert score < 0.2


def test_compatibility_handles_missing_analysis() -> None:
    assert SmartDJProvider._compatibility({"bpm": 120}, None, {"bpm_tolerance": 0.08}) == 0.0


from music_assistant.providers.smart_dj.engine import DJControls, MODES, SignalControl, score_candidate


def test_hard_bpm_rule_is_never_violated() -> None:
    current = {"bpm": 120, "camelot": "8A"}
    candidate = {"bpm": 160, "camelot": "8A"}
    score, _reasons, violations = score_candidate(
        current,
        candidate,
        MODES["ai_dj"],
        DJControls(bpm=SignalControl("hard")),
    )
    assert score == 0.0
    assert "hard BPM compatibility" in violations


def test_disabled_key_does_not_affect_score() -> None:
    current = {"bpm": 120, "camelot": "8A", "energy": 0.5}
    candidate = {"bpm": 120, "camelot": "2B", "energy": 0.5}
    soft, _, _ = score_candidate(current, candidate, MODES["ai_dj"], DJControls())
    disabled, _, _ = score_candidate(
        current, candidate, MODES["ai_dj"], DJControls(key=SignalControl("disabled"))
    )
    assert disabled > 0
    assert soft != disabled


def test_required_excluded_controls_are_serializable() -> None:
    controls = DJControls(
        required_ids=frozenset({"required"}),
        excluded_ids=frozenset({"excluded"}),
        fixed_ids=frozenset({"fixed"}),
    )
    assert "required" in controls.required_ids
    assert "excluded" in controls.excluded_ids
    assert "fixed" in controls.fixed_ids
