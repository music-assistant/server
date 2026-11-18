"""
Tests for how ChromecastPlayer has the queue restart a stream the receiver gave up on.

Nothing is queued on the receiver after a flow stream, so a receiver that reports a
playback error on it stays idle until the queue restarts it.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import PlaybackState

from music_assistant.providers.chromecast.player import ChromecastPlayer
from tests.providers.chromecast.test_idle_release import _fake_player, _media_status

FLOW_URL = "http://192.168.1.10:8097/flow/session-1/queue-1/item-1/player-1.flac"


def _error_status(content_id: str = FLOW_URL, idle_reason: str = "ERROR") -> MagicMock:
    status = _media_status(content_id=content_id)
    status.idle_reason = idle_reason
    return status


def test_a_playback_error_on_a_stream_that_played_restarts_it() -> None:
    """The queue is asked to restart the stream the receiver reported the error for."""
    fake = _fake_player(prev_state=PlaybackState.PLAYING)

    fake._handle_media_status(_error_status())

    fake.mass.streams.restart_lost_stream.assert_called_once_with(FLOW_URL)
    assert fake._attr_playback_state == PlaybackState.IDLE


def test_a_repeated_error_report_restarts_once() -> None:
    """The receiver can repeat its idle status; only the end of playback counts."""
    fake = _fake_player(prev_state=PlaybackState.PLAYING)

    fake._handle_media_status(_error_status())
    fake._handle_media_status(_error_status())

    fake.mass.streams.restart_lost_stream.assert_called_once_with(FLOW_URL)


@pytest.mark.parametrize("prev_state", [PlaybackState.IDLE, PlaybackState.PAUSED])
def test_an_error_on_a_stream_that_did_not_play_restarts_nothing(
    prev_state: PlaybackState,
) -> None:
    """A stream that fails to start, or fails while paused, is not lost while playing."""
    fake = _fake_player(prev_state=prev_state)

    fake._handle_media_status(_error_status())

    fake.mass.streams.restart_lost_stream.assert_not_called()


@pytest.mark.parametrize("idle_reason", ["FINISHED", "INTERRUPTED", "CANCELLED"])
def test_an_end_without_an_error_restarts_nothing(idle_reason: str) -> None:
    """A stream that ended, was replaced or was stopped is no lost stream."""
    fake = _fake_player(prev_state=PlaybackState.PLAYING)

    fake._handle_media_status(_error_status(idle_reason=idle_reason))

    fake.mass.streams.restart_lost_stream.assert_not_called()


def test_a_group_member_leaves_the_restart_to_its_group() -> None:
    """A group forwards its status to every member; the group player restarts its own queue."""
    fake = _fake_player(prev_state=PlaybackState.PLAYING, active_cast_group="group-1")
    # the handler reads the group's status instead of its own, but only for a real cast player
    group = MagicMock(spec=ChromecastPlayer)
    group.cc = MagicMock()
    group.cc.media_controller.status = _error_status()
    fake.mass.players.get_protocol.return_value = group

    fake._handle_media_status(_error_status())

    fake.mass.streams.restart_lost_stream.assert_not_called()
