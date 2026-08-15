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

from typing import Any, cast
from unittest.mock import MagicMock

from music_assistant_models.enums import PlaybackState

from music_assistant.providers.chromecast.constants import MASS_APP_ID
from music_assistant.providers.chromecast.player import ChromecastPlayer


def _fake_player(*, flow_underrun: bool = False) -> Any:
    """
    Build an idle, ungrouped Cast player on mocked collaborators.

    :param flow_underrun: Whether the queue's flow stream has been fully consumed.
    """
    # __init__ is skipped: it needs a provider, cast info and a live Chromecast connection.
    # Typed as Any because the collaborators below are read back as the mocks they are.
    fake = cast("Any", ChromecastPlayer.__new__(ChromecastPlayer))
    fake.mass = MagicMock()
    fake.logger = MagicMock()
    fake.cc = MagicMock(app_id=MASS_APP_ID)
    fake.update_state = MagicMock()
    fake._flow_stream_underrun = MagicMock(return_value=flow_underrun)
    fake._media_error_reported = False
    fake._app_quit_task_id = "cast_quit_app_test"
    fake.active_cast_group = None
    # display_name is a cached property, normally built from the config in __init__
    fake._cache = {"display_name": "Test Cast"}
    # playback_state is a read-only property, so it is seeded through its backing field
    fake._attr_playback_state = PlaybackState.IDLE
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

    fake._handle_media_status(_error_status())

    fake.logger.warning.assert_called_once()
    assert "track.flac" in str(fake.logger.warning.call_args)


def test_repeated_error_status_is_logged_once() -> None:
    """The receiver echoes the error status several times; only the first is logged."""
    fake = _fake_player()

    fake._handle_media_status(_error_status())
    fake._handle_media_status(_error_status())
    fake._handle_media_status(_error_status())

    fake.logger.warning.assert_called_once()


def test_error_logged_again_after_recovery() -> None:
    """A new error after successful playback is a new incident and is logged again."""
    fake = _fake_player()

    fake._handle_media_status(_error_status())
    fake._handle_media_status(_playing_status())
    fake._handle_media_status(_error_status())

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

    member._handle_media_status(_error_status())

    member.logger.warning.assert_not_called()
    # assert the group status was really processed, so the check above cannot pass
    # just because the member bailed out before reaching the error handling
    member.mass.players.get_player.assert_called_once_with("group-uuid")
    member.update_state.assert_called_once()


def test_flow_stream_underrun_is_not_an_error() -> None:
    """An idle ERROR at the end of a fully consumed flow stream is expected, not logged."""
    fake = _fake_player(flow_underrun=True)

    fake._handle_media_status(_error_status())

    fake.logger.warning.assert_not_called()


def test_load_media_failed_logs_the_error_code() -> None:
    """A LOAD_FAILED with a detailed error code is logged with its meaning."""
    fake = _fake_player()

    fake._handle_load_media_failed(1, 104)

    fake.logger.warning.assert_called_once()
    assert "104" in str(fake.logger.warning.call_args)
    assert fake._media_error_reported is True
