"""Tests for the MusicCast player."""

from unittest.mock import MagicMock

from music_assistant.providers.musiccast.player import MusicCastPlayer


def test_a_musiccast_player_gets_the_gapless_burst() -> None:
    """A MusicCast device only plays a track gapless once it holds a whole opening chunk of it."""
    player = MusicCastPlayer(MagicMock(), "A___main", MagicMock(), MagicMock())

    assert player.needs_gapless_burst is True
