"""
Tests for surfacing receiver media errors from ChromecastPlayer.

Regression tests for https://github.com/music-assistant/support/issues/5981, where a
receiver answered every LOAD with LOAD_FAILED and a media status carrying
``idleReason: ERROR``, and Music Assistant logged nothing: the player silently
returned to idle. A media error reported by the receiver must be visible in the log.

The LOAD_FAILED message itself only reaches the ``load_media_failed`` listener when
the receiver includes a detailed error code, so the media status path is the one
that must catch the general case.
"""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

from music_assistant.providers.chromecast.player import ChromecastPlayer


def _handle_media_status(fake: MagicMock, status: MagicMock) -> None:
    ChromecastPlayer._handle_media_status(cast("ChromecastPlayer", fake), status)


def _fake_player() -> MagicMock:
    """Build a MagicMock standing in for an idle, ungrouped ChromecastPlayer."""
    fake = MagicMock()
    fake.active_cast_group = None
    fake._media_error_reported = False
    fake._flow_stream_underrun = MagicMock(return_value=False)
    return fake


def _error_status() -> MagicMock:
    status = MagicMock()
    status.content_id = "http://192.168.1.58:8097/flow/abc/track.flac"
    status.player_is_playing = False
    status.player_is_paused = False
    status.player_is_idle = True
    status.idle_reason = "ERROR"
    return status


def _playing_status() -> MagicMock:
    status = MagicMock()
    status.content_id = "http://192.168.1.58:8097/flow/abc/track.flac"
    status.player_is_playing = True
    status.player_is_paused = False
    status.player_is_idle = False
    status.idle_reason = None
    return status


def test_idle_error_is_logged() -> None:
    """A media status with idleReason ERROR produces a warning naming the media."""
    fake = _fake_player()

    _handle_media_status(fake, _error_status())

    fake.logger.warning.assert_called_once()
    assert "track.flac" in str(fake.logger.warning.call_args)


def test_repeated_error_status_is_logged_once() -> None:
    """The receiver echoes the error status several times; only the first is logged."""
    fake = _fake_player()

    _handle_media_status(fake, _error_status())
    _handle_media_status(fake, _error_status())
    _handle_media_status(fake, _error_status())

    fake.logger.warning.assert_called_once()


def test_error_logged_again_after_recovery() -> None:
    """A new error after successful playback is a new incident and is logged again."""
    fake = _fake_player()

    _handle_media_status(fake, _error_status())
    _handle_media_status(fake, _playing_status())
    _handle_media_status(fake, _error_status())

    assert fake.logger.warning.call_count == 2


def test_group_error_is_not_logged_by_every_member() -> None:
    """A group's error reaches all its members, but only the group itself reports it."""
    group = MagicMock()
    # a plain spec= mock has no 'cc', which is set in __init__
    group.__class__ = ChromecastPlayer  # type: ignore[assignment]
    group.cc.media_controller.status = _error_status()
    member = _fake_player()
    member.active_cast_group = "group-uuid"
    member.mass.players.get_player = MagicMock(return_value=group)

    _handle_media_status(member, _error_status())

    member.logger.warning.assert_not_called()
    # assert the group status was really processed, so the check above cannot pass
    # just because the member bailed out before reaching the error handling
    member.mass.players.get_player.assert_called_once_with("group-uuid")
    member.update_state.assert_called_once()


def test_flow_stream_underrun_is_not_an_error() -> None:
    """An idle ERROR at the end of a fully consumed flow stream is expected, not logged."""
    fake = _fake_player()
    fake._flow_stream_underrun = MagicMock(return_value=True)

    _handle_media_status(fake, _error_status())

    fake.logger.warning.assert_not_called()


def test_load_media_failed_logs_the_error_code() -> None:
    """A LOAD_FAILED with a detailed error code is logged with its meaning."""
    fake = _fake_player()

    ChromecastPlayer._handle_load_media_failed(cast("ChromecastPlayer", fake), 1, 104)

    fake.logger.warning.assert_called_once()
    assert "104" in str(fake.logger.warning.call_args)
    assert fake._media_error_reported is True
