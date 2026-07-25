"""Tests for the Bose SoundTouch player config-action helpers."""

from __future__ import annotations

from music_assistant.providers.bose_soundtouch.player import _parse_preset_action


def test_parse_preset_action_search() -> None:
    """A preset search action maps to its preset id with is_copy False."""
    assert _parse_preset_action("preset_3_search_media") == (3, False)
    assert _parse_preset_action("preset_1_search_media") == (1, False)
    assert _parse_preset_action("preset_6_search_media") == (6, False)


def test_parse_preset_action_copy() -> None:
    """A preset copy action maps to its preset id with is_copy True."""
    assert _parse_preset_action("preset_2_copy_media") == (2, True)
    assert _parse_preset_action("preset_6_copy_media") == (6, True)


def test_parse_preset_action_unknown() -> None:
    """Non-action ids (incl. the do_search entry key and out-of-range presets) don't match."""
    assert _parse_preset_action("preset_3_do_search") == (None, False)
    assert _parse_preset_action("preset_7_search_media") == (None, False)
    assert _parse_preset_action("preset_0_copy_media") == (None, False)
    assert _parse_preset_action("some_other_action") == (None, False)
    assert _parse_preset_action("") == (None, False)
