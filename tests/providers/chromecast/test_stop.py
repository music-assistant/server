"""
Tests for how ChromecastPlayer releases a Cast device on stop.

Quitting the receiver app ends the Cast session, and the device plays its 'cast
connected' chime whenever a new one is started. Stopping must therefore leave the
session in place for a while, so a follow-up command (an announcement stops playback
before it plays) does not have to start a new one.
"""

from __future__ import annotations

from functools import partial
from typing import cast
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import PlayerType

from music_assistant.providers.chromecast.constants import (
    APP_MEDIA_RECEIVER,
    APP_QUIT_DELAY,
    MASS_APP_ID,
    SENDSPIN_CAST_APP_ID,
)
from music_assistant.providers.chromecast.player import ChromecastPlayer


async def _stop(fake: MagicMock) -> None:
    await ChromecastPlayer.stop(cast("ChromecastPlayer", fake))


async def _quit_app_when_unused(fake: MagicMock) -> None:
    await ChromecastPlayer._quit_app_when_unused(cast("ChromecastPlayer", fake))


def _fake_cast(
    *,
    player_type: PlayerType = PlayerType.PLAYER,
    running_app_id: str | None = MASS_APP_ID,
    player_state: str = "PLAYING",
    media_session_id: int | None = 1,
) -> MagicMock:
    """
    Build a MagicMock Cast player.

    :param player_type: Type of the player.
    :param running_app_id: App id the receiver is running, if any.
    :param player_state: Media player state the receiver reports.
    :param media_session_id: Media session the receiver reports, if any.
    """
    fake = MagicMock()
    fake.type = player_type
    fake.available = True
    fake.cc.app_id = running_app_id
    fake._app_quit_task_id = "cast_quit_app_test"
    fake.cancel_pending_app_quit = partial(
        ChromecastPlayer.cancel_pending_app_quit, cast("ChromecastPlayer", fake)
    )
    fake._schedule_app_release = partial(
        ChromecastPlayer._schedule_app_release, cast("ChromecastPlayer", fake)
    )
    status = fake.cc.media_controller.status
    status.player_is_playing = player_state in ("PLAYING", "BUFFERING")
    status.player_is_paused = player_state == "PAUSED"
    status.media_session_id = media_session_id
    return fake


async def test_stop_keeps_the_cast_session_alive() -> None:
    """Stopping a player must not end the Cast session right away."""
    fake = _fake_cast()

    await _stop(fake)

    fake.cc.media_controller.stop.assert_called_once()
    fake.cc.quit_app.assert_not_called()


async def test_stop_schedules_the_device_release() -> None:
    """The device is released a little later, so a follow-up command can reuse it."""
    fake = _fake_cast()

    await _stop(fake)

    fake.mass.call_later.assert_called_once_with(
        APP_QUIT_DELAY, fake._quit_app_when_unused, task_id=fake._app_quit_task_id
    )


@pytest.mark.parametrize("app_id", [SENDSPIN_CAST_APP_ID, "CC32E753", None])
async def test_stop_ends_a_foreign_cast_session_right_away(app_id: str | None) -> None:
    """A session of another app is not ours to keep around."""
    fake = _fake_cast(running_app_id=app_id)

    await _stop(fake)

    fake.cc.quit_app.assert_called_once()
    fake.mass.call_later.assert_not_called()


async def test_stop_on_a_group_leaves_the_app_alone() -> None:
    """A cast group is released by its power command, not by stop."""
    fake = _fake_cast(player_type=PlayerType.GROUP)

    await _stop(fake)

    fake.cc.media_controller.stop.assert_called_once()
    fake.cc.quit_app.assert_not_called()
    fake.mass.call_later.assert_not_called()


async def test_stop_without_a_media_session_is_not_sent() -> None:
    """Nothing was ever loaded, so there is no playback to stop."""
    fake = _fake_cast(media_session_id=None)

    await _stop(fake)

    fake.cc.media_controller.stop.assert_not_called()
    fake.cc.quit_app.assert_not_called()
    fake.mass.call_later.assert_called_once()


async def test_launching_an_app_cancels_a_pending_release() -> None:
    """The device is claimed again within the grace period, so it must not be released."""
    fake = _fake_cast(running_app_id=MASS_APP_ID)

    await ChromecastPlayer._launch_app(cast("ChromecastPlayer", fake))

    fake.mass.cancel_timer.assert_called_once_with(fake._app_quit_task_id)


@pytest.mark.parametrize("app_id", [MASS_APP_ID, APP_MEDIA_RECEIVER])
async def test_unused_receiver_app_is_quit(app_id: str) -> None:
    """Nothing came along within the grace period, so the device is released."""
    fake = _fake_cast(running_app_id=app_id, player_state="IDLE")

    await _quit_app_when_unused(fake)

    fake.cc.quit_app.assert_called_once()


async def test_unreachable_device_is_not_quit() -> None:
    """The device dropped off within the grace period, so there is nothing to send to."""
    fake = _fake_cast(player_state="IDLE")
    fake.available = False

    await _quit_app_when_unused(fake)

    fake.cc.quit_app.assert_not_called()


async def test_app_of_another_sender_is_not_quit() -> None:
    """Another app took over the device, so it is no longer ours to release."""
    fake = _fake_cast(running_app_id=SENDSPIN_CAST_APP_ID, player_state="IDLE")

    await _quit_app_when_unused(fake)

    fake.cc.quit_app.assert_not_called()


@pytest.mark.parametrize("player_state", ["PLAYING", "BUFFERING", "PAUSED"])
async def test_receiver_app_in_use_is_not_quit(player_state: str) -> None:
    """The receiver app got used again (such as by a dashboard), so it is left running."""
    fake = _fake_cast(player_state=player_state)

    await _quit_app_when_unused(fake)

    fake.cc.quit_app.assert_not_called()
