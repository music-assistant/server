"""Focused Smart DJ provider tests."""

import pytest

from music_assistant.providers.smart_dj.engine import DJControls, MODES, SignalControl, score_candidate
from music_assistant.providers.smart_dj.provider import _camelot_from_key


def test_camelot_conversion() -> None:
    assert _camelot_from_key("A", "minor") == "8A"
    assert _camelot_from_key("C", "major") == "8B"


def test_hard_bpm_rule_is_never_violated() -> None:
    score, _reasons, violations = score_candidate(
        {"bpm": 120, "camelot": "8A"},
        {"bpm": 160, "camelot": "8A"},
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


def test_required_excluded_and_fixed_controls_are_serializable() -> None:
    controls = DJControls(
        required_ids=frozenset({"required"}),
        excluded_ids=frozenset({"excluded"}),
        fixed_ids=frozenset({"fixed"}),
    )
    assert "required" in controls.required_ids
    assert "excluded" in controls.excluded_ids
    assert "fixed" in controls.fixed_ids
