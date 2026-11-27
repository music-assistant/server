"""Tests for what a Samsung WAM player tells about the streams it fetches."""

from __future__ import annotations

from music_assistant.providers.samsung_wam.player import WamPlayer


def test_a_speaker_requires_a_content_length() -> None:
    """A WAM speaker gets a content length on every stream."""
    player = WamPlayer.__new__(WamPlayer)

    assert player.requires_content_length is True
