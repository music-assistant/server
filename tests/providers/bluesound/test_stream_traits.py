"""Tests for what a Bluesound player tells about the streams it fetches."""

from __future__ import annotations

from music_assistant.providers.bluesound.player import BluesoundPlayer


def test_a_player_requires_a_content_length() -> None:
    """BluOS loops the audio without a content length, so every stream declares one."""
    player = BluesoundPlayer.__new__(BluesoundPlayer)

    assert player.requires_content_length is True
