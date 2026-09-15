"""
Tests for deferring a Cast volume_set command received while the device is idle.

Some Cast receivers accept a volume change while no receiver app is running, report it
back as applied, then keep playing at their previous level - and de-dupe a later repeat
of that same value. Sending nothing while idle and applying the deferred value right
after the receiver app launches (at playback start) works around this: the value then
differs from what the idle device is still reporting, so it actually takes effect.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import MagicMock, patch
from uuid import uuid4

from pychromecast import IDLE_APP_ID

from music_assistant.providers.chromecast.constants import MASS_APP_ID
from music_assistant.providers.chromecast.player import ChromecastPlayer


def _make_player(app_id: str | None) -> ChromecastPlayer:
    """Build a ChromecastPlayer whose device currently reports the given app id."""
    info = MagicMock()
    info.manufacturer = "Terris"
    info.model_name = "Terris CCM283"
    info.friendly_name = "Arbeitszimmer"
    info.is_audio_group = False
    info.is_multichannel_group = False
    info.host = "10.0.0.10"
    info.mac_address = "00:11:22:33:44:55"
    info.uuid = uuid4()
    provider = MagicMock()
    provider.mass.closing = False
    provider.mz_mgr = MagicMock()
    chromecast = MagicMock()
    chromecast.app_id = app_id
    with patch("music_assistant.providers.chromecast.player.CastStatusListener"):
        return ChromecastPlayer(provider, str(info.uuid), info, chromecast)


def _sent_volumes(player: ChromecastPlayer) -> list[float]:
    """Return the volumes handed to the cast device, in order."""
    return [call.args[0] for call in cast("MagicMock", player.cc).set_volume.call_args_list]


async def test_volume_set_while_idle_with_no_app_defers() -> None:
    """A volume_set while no app has ever run (app_id None) is stored, not sent."""
    player = _make_player(app_id=None)

    await player.volume_set(42)

    assert _sent_volumes(player) == []
    assert player._pending_volume == 42
    assert player.volume_level == 42


async def test_volume_set_while_idle_defers() -> None:
    """A volume_set while the receiver reports the idle app is stored, not sent."""
    player = _make_player(app_id=IDLE_APP_ID)

    await player.volume_set(55)

    assert _sent_volumes(player) == []
    assert player._pending_volume == 55
    assert player.volume_level == 55


async def test_volume_set_while_active_sends_immediately() -> None:
    """A volume_set while our app is running is sent right away, rounded to cast's scale."""
    player = _make_player(app_id=MASS_APP_ID)

    await player.volume_set(50)

    assert _sent_volumes(player) == [0.5]
    assert player._pending_volume is None


async def test_volume_set_while_active_clears_a_stale_pending_value() -> None:
    """A send while active also clears any volume that was previously deferred."""
    player = _make_player(app_id=MASS_APP_ID)
    player._pending_volume = 20

    await player.volume_set(50)

    assert player._pending_volume is None


def _fake_launch(*, pending_volume: int | None, launch_result: bool = True) -> MagicMock:
    """Build a MagicMock Cast whose receiver accepts the launch, for use with _launch_app."""
    fake = MagicMock()
    fake.mass.loop = asyncio.get_running_loop()
    fake.display_name = "Fake Cast"
    fake.cc.app_id = None
    fake.app_quit_sent = False
    fake.config.get_value = MagicMock(return_value=True)
    fake._pending_volume = pending_volume
    fake.volume_set = MagicMock(
        side_effect=lambda level: ChromecastPlayer.volume_set(cast("ChromecastPlayer", fake), level)
    )

    def launch_app(
        app_id: str,
        *,
        force_launch: bool = False,  # noqa: ARG001
        callback_function: Any = None,
    ) -> None:
        fake.cc.app_id = app_id
        callback_function(launch_result, None)

    fake.cc.socket_client.receiver_controller.launch_app = launch_app
    return fake


async def _launch_app(fake: MagicMock) -> None:
    await ChromecastPlayer._launch_app(cast("ChromecastPlayer", fake))


async def test_launch_app_applies_a_pending_volume_after_launch() -> None:
    """Once the app is confirmed running, a deferred volume is sent and cleared."""
    fake = _fake_launch(pending_volume=33)

    await _launch_app(fake)

    assert fake._pending_volume is None
    fake.cc.set_volume.assert_called_once_with(round(33 / 100, 2))


async def test_launch_app_with_no_pending_volume_sends_nothing_extra() -> None:
    """A launch with no deferred volume does not send any volume command."""
    fake = _fake_launch(pending_volume=None)

    await _launch_app(fake)

    fake.cc.set_volume.assert_not_called()


def _cast_status(*, volume_level: float, volume_muted: bool = False) -> MagicMock:
    """Build a CastStatus as the receiver reports it."""
    status = MagicMock()
    status.app_id = None
    status.volume_level = volume_level
    status.volume_muted = volume_muted
    return status


def _fake_status_player(app_id: str | None, pending_volume: int | None) -> MagicMock:
    """Build a MagicMock standing in for a ChromecastPlayer, for use with _handle_cast_status."""
    fake = MagicMock()
    fake.mass.closing = False
    fake.cc.app_id = app_id
    fake.cast_info.is_multichannel_group = False
    fake.cast_info.is_audio_group = False
    fake.on_app_status_changed = None
    fake._pending_volume = pending_volume
    return fake


def _handle_cast_status(fake: MagicMock, status: MagicMock) -> None:
    ChromecastPlayer._handle_cast_status(cast("ChromecastPlayer", fake), status)


def test_cast_status_keeps_pending_volume_while_idle() -> None:
    """The device's stale reported level does not overwrite a value still deferred."""
    fake = _fake_status_player(app_id=IDLE_APP_ID, pending_volume=77)

    _handle_cast_status(fake, _cast_status(volume_level=0.10))

    assert fake._attr_volume_level == 77


def test_cast_status_reflects_device_when_not_pending() -> None:
    """With nothing deferred, the reported level is used as-is."""
    fake = _fake_status_player(app_id=IDLE_APP_ID, pending_volume=None)

    _handle_cast_status(fake, _cast_status(volume_level=0.42))

    assert fake._attr_volume_level == 42


def test_cast_status_reflects_device_when_active_even_with_pending() -> None:
    """A pending volume only overrides the report while idle; once active it is stale."""
    fake = _fake_status_player(app_id=MASS_APP_ID, pending_volume=77)

    _handle_cast_status(fake, _cast_status(volume_level=0.42))

    assert fake._attr_volume_level == 42
