"""Tests for how a Sendspin session decides that its media plays as a live source."""

from __future__ import annotations

from unittest.mock import MagicMock

from music_assistant_models.enums import MediaType

from music_assistant.providers.sendspin.playback import SendspinPlaybackSession


def test_radio_is_live() -> None:
    """Radio plays as a live source."""
    session, _queues = _session_mock()
    assert session._is_live_source(_media(MediaType.RADIO)) is True


def test_a_realtime_track_is_buffered() -> None:
    """A track whose source hands over its audio just-in-time keeps the buffered send-ahead."""
    session, queues = _session_mock()
    queues.get_item.return_value = MagicMock(streamdetails=MagicMock(is_realtime=True))
    assert session._is_live_source(_media(MediaType.TRACK)) is False


def _session_mock() -> tuple[SendspinPlaybackSession, MagicMock]:
    """Return a bare playback session plus the player_queues mock it could consult."""
    session = object.__new__(SendspinPlaybackSession)
    session.player = MagicMock()
    return session, session.player.mass.player_queues


def _media(media_type: MediaType) -> MagicMock:
    """Return PlayerMedia for a queue item of the given media type."""
    return MagicMock(media_type=media_type, source_id="player1", queue_item_id="qi-1")
