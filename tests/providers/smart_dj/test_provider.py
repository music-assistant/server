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
