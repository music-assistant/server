"""
Tests for how ChromecastPlayer releases a Cast device once playback ends by itself.

Playback that finishes on its own never gets a stop command: the queue simply runs
out, or an announcement ends. Without a release the receiver app stays loaded, which
on a TV or Nest Hub means Music Assistant stays on screen instead of the backdrop.
"""

from __future__ import annotations

from functools import partial
from typing import cast
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import PlaybackState, PlayerType
from pychromecast import IDLE_APP_ID

from music_assistant.providers.chromecast.constants import (
    APP_QUIT_DELAY,
    MASS_APP_ID,
)
from music_assistant.providers.chromecast.player import ChromecastPlayer
from tests.providers.chromecast.helpers import bind_media_status_helpers


def _handle_media_status(fake: MagicMock, status: MagicMock) -> None:
    ChromecastPlayer._handle_media_status(cast("ChromecastPlayer", fake), status)


def _fake_player(
    *,
    prev_state: PlaybackState = PlaybackState.PLAYING,
    player_type: PlayerType = PlayerType.PLAYER,
    active_cast_group: str | None = None,
) -> MagicMock:
    """
    Build a MagicMock Cast player that handles media status like the real one.

    :param prev_state: Playback state the player was in before the status arrives.
    :param player_type: Type of the player.
    :param active_cast_group: Player id of the cast group the player plays through, if any.
    """
    fake = MagicMock()
    fake.type = player_type
    fake.active_cast_group = active_cast_group
    fake.powered = False
    fake.group_members = []
    fake.cc.app_id = MASS_APP_ID
    fake._app_quit_task_id = "cast_quit_app_test"
    fake._attr_playback_state = prev_state
    fake._flow_stream_underrun.return_value = False
    fake._schedule_app_release = partial(
        ChromecastPlayer._schedule_app_release, cast("ChromecastPlayer", fake)
    )
    return bind_media_status_helpers(fake)


def _media_status(
    *,
    playing: bool = False,
    paused: bool = False,
    buffering: bool = False,
    content_id: str = "",
) -> MagicMock:
    """
    Build a MediaStatus as the receiver reports it.

    :param playing: Whether the receiver reports playback.
    :param paused: Whether the receiver reports paused playback.
    :param buffering: Whether the receiver reports buffering, which it counts as playing.
    :param content_id: Content id the receiver has loaded, if any.
    """
    status = MagicMock()
    status.content_id = content_id
    status.player_state = (
        "BUFFERING" if buffering else "PLAYING" if playing else "PAUSED" if paused else "IDLE"
    )
    status.player_is_playing = playing or buffering
    status.player_is_paused = paused
    status.player_is_idle = not playing and not paused and not buffering
    return status


def _assert_release_scheduled(fake: MagicMock) -> None:
    fake.mass.call_later.assert_called_once_with(
        APP_QUIT_DELAY, fake._quit_app_when_unused, task_id=fake._app_quit_task_id
    )


@pytest.mark.parametrize("prev_state", [PlaybackState.PLAYING, PlaybackState.PAUSED])
def test_playback_ending_releases_the_device(prev_state: PlaybackState) -> None:
    """Nothing follows up on playback that ended on its own, so the device is released."""
    fake = _fake_player(prev_state=prev_state)

    _handle_media_status(fake, _media_status())

    assert fake._attr_playback_state == PlaybackState.IDLE
    _assert_release_scheduled(fake)


def test_an_announcement_on_an_idle_player_releases_the_device() -> None:
    """
    An announcement claims an idle device, so it must hand it back afterwards.

    The player is never stopped in this flow (there was nothing to stop), which is
    what used to leave the receiver app loaded for good.
    """
    fake = _fake_player(prev_state=PlaybackState.IDLE)

    _handle_media_status(fake, _media_status(playing=True, content_id="http://mass/announce.mp3"))
    assert fake._attr_playback_state == PlaybackState.PLAYING
    fake.mass.call_later.assert_not_called()

    _handle_media_status(fake, _media_status())

    _assert_release_scheduled(fake)


def test_a_device_that_ran_dry_at_the_end_of_the_flow_stream_releases_it() -> None:
    """A device stuck buffering at flow EOF counts as finished, so it is released too."""
    fake = _fake_player()
    fake._flow_stream_underrun.return_value = True

    _handle_media_status(fake, _media_status(buffering=True, content_id="http://mass/flow.flac"))

    assert fake._attr_playback_state == PlaybackState.IDLE
    _assert_release_scheduled(fake)


@pytest.mark.parametrize("app_id", [None, IDLE_APP_ID])
def test_a_released_device_does_not_become_a_source(app_id: str | None) -> None:
    """A released device sits on its backdrop, which is not something to select."""
    fake = _fake_player()
    fake.cc.app_id = app_id
    fake._attr_source_list = []

    _handle_media_status(fake, _media_status())

    assert fake._attr_active_source is None
    assert fake._attr_source_list == []


def test_pausing_keeps_the_device_claimed() -> None:
    """A paused player is meant to be resumed, so it keeps its Cast session."""
    fake = _fake_player()

    _handle_media_status(fake, _media_status(paused=True, content_id="http://mass/track.flac"))

    assert fake._attr_playback_state == PlaybackState.PAUSED
    fake.mass.call_later.assert_not_called()


def test_an_idle_player_is_not_released_again() -> None:
    """A player that was already idle has no session of its own to release."""
    fake = _fake_player(prev_state=PlaybackState.IDLE)

    _handle_media_status(fake, _media_status())

    fake.mass.call_later.assert_not_called()


@pytest.mark.parametrize(
    ("content_id", "playing", "paused"),
    [
        ("https://cast.music-assistant.io/dashboard-keepalive.mp4", True, False),
        ("https://cast.music-assistant.io/keepalive.png", False, True),
    ],
)
def test_a_dashboard_keepalive_does_not_release_the_device(
    content_id: str, playing: bool, paused: bool
) -> None:
    """The receiver is showing a dashboard, so the device is deliberately kept claimed."""
    fake = _fake_player()

    _handle_media_status(fake, _media_status(playing=playing, paused=paused, content_id=content_id))

    assert fake._attr_playback_state == PlaybackState.IDLE
    fake.mass.call_later.assert_not_called()


def test_a_cast_group_is_not_released_when_playback_ends() -> None:
    """A cast group is released by its power control, not by playback ending."""
    fake = _fake_player(player_type=PlayerType.GROUP)

    _handle_media_status(fake, _media_status())

    assert fake._attr_playback_state == PlaybackState.IDLE
    fake.mass.call_later.assert_not_called()


def test_a_group_member_is_not_released_when_the_group_stops() -> None:
    """A member follows the group's Cast session, so it has none of its own to release."""
    fake = _fake_player(active_cast_group="group_player_1")
    # the handler reads the group's status instead of its own, but only for a real cast player
    group = MagicMock(spec=ChromecastPlayer)
    group.cc = MagicMock()
    group.cc.media_controller.status = _media_status()
    fake.mass.players.get_player.return_value = group

    _handle_media_status(fake, _media_status())

    assert fake._attr_playback_state == PlaybackState.IDLE
    fake.mass.call_later.assert_not_called()
