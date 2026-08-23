"""Tests for how a Sendspin session decides that its media is fed at playback pace."""

from __future__ import annotations

from unittest.mock import MagicMock

from music_assistant_models.enums import MediaType

from music_assistant.providers.sendspin.playback import SendspinPlaybackSession


def test_radio_is_live_without_looking_at_the_queue() -> None:
    """Radio is live by media type; no queue lookup is needed."""
    session, queues = _session_mock()
    assert session._is_live_source(_media(MediaType.RADIO)) is True
    queues.get_item.assert_not_called()


def test_a_realtime_track_is_live() -> None:
    """A track whose source hands over its audio just-in-time counts as live."""
    session, queues = _session_mock()
    queues.get_item.return_value = MagicMock(streamdetails=MagicMock(is_realtime=True))
    assert session._is_live_source(_media(MediaType.TRACK)) is True
    queues.get_item.assert_called_once_with("player1", "qi-1")


def test_an_ordinary_track_is_buffered() -> None:
    """A track that can be read ahead keeps the buffered send-ahead."""
    session, queues = _session_mock()
    queues.get_item.return_value = MagicMock(streamdetails=MagicMock(is_realtime=False))
    assert session._is_live_source(_media(MediaType.TRACK)) is False


def test_an_unresolved_item_is_treated_as_buffered() -> None:
    """Without streamdetails to consult there is no reason to assume a live source."""
    session, queues = _session_mock()
    queues.get_item.return_value = MagicMock(streamdetails=None)
    assert session._is_live_source(_media(MediaType.TRACK)) is False
    queues.get_item.return_value = None
    assert session._is_live_source(_media(MediaType.TRACK)) is False


def test_media_without_a_queue_reference_is_treated_as_buffered() -> None:
    """Media that is not a queue item cannot be looked up, so it stays buffered."""
    session, _queues = _session_mock()
    media = _media(MediaType.TRACK)
    media.queue_item_id = None
    assert session._is_live_source(media) is False


def _session_mock() -> tuple[SendspinPlaybackSession, MagicMock]:
    """Return a bare playback session plus the player_queues mock it consults."""
    session = object.__new__(SendspinPlaybackSession)
    session.player = MagicMock()
    return session, session.player.mass.player_queues


def _media(media_type: MediaType) -> MagicMock:
    """Return PlayerMedia for a queue item of the given media type."""
    return MagicMock(media_type=media_type, source_id="player1", queue_item_id="qi-1")
