"""
Tests for keeping back a Cast volume set received while a non-Google device is idle.

Some non-Google receivers accept a volume set while no app is running but keep playing
at their old level once one starts. So on those devices a volume set while idle is kept
back, shown right away, and sent once the media is loaded at playback start.
"""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import uuid4

import pytest
from pychromecast import IDLE_APP_ID
from pychromecast.error import NotConnected, PyChromecastError, RequestFailed, RequestTimeout

from music_assistant.providers.chromecast.constants import MASS_APP_ID
from music_assistant.providers.chromecast.player import ChromecastPlayer


def _make_player(app_id: str | None, manufacturer: str = "Terris") -> ChromecastPlayer:
    """Build a ChromecastPlayer whose device currently reports the given app id."""
    info = MagicMock()
    info.manufacturer = manufacturer
    info.model_name = "CCM283"
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


@pytest.mark.parametrize("app_id", [None, IDLE_APP_ID])
async def test_volume_set_while_idle_is_kept_back(app_id: str | None) -> None:
    """A volume_set while no app runs is stored and shown, not sent to the device."""
    player = _make_player(app_id)

    await player.volume_set(42)

    assert _sent_volumes(player) == []
    assert player._pending_volume == 42
    assert player.volume_level == 42


async def test_volume_set_while_active_is_sent() -> None:
    """A volume_set while our app is running is sent right away, rounded to cast's scale."""
    player = _make_player(MASS_APP_ID)

    await player.volume_set(50)

    assert _sent_volumes(player) == [0.5]
    assert player._pending_volume is None


async def test_volume_set_while_active_drops_a_kept_back_volume() -> None:
    """A volume sent while active makes an earlier kept-back one obsolete."""
    player = _make_player(IDLE_APP_ID)
    await player.volume_set(20)
    cast("MagicMock", player.cc).app_id = MASS_APP_ID

    await player.volume_set(50)

    assert _sent_volumes(player) == [0.5]
    assert player._pending_volume is None


async def test_google_device_volume_set_while_idle_is_sent() -> None:
    """A Google device honours a volume set while idle, so it is sent right away."""
    player = _make_player(IDLE_APP_ID, manufacturer="Google Inc.")

    await player.volume_set(42)

    assert _sent_volumes(player) == [0.42]
    assert player._pending_volume is None


def _fake_play_media_player(pending_volume: int | None) -> MagicMock:
    """Build a MagicMock standing in for a ChromecastPlayer, for use with play_media."""
    fake = MagicMock()
    fake.player_id = "cast-1"
    fake._pending_volume = pending_volume
    fake.provider.mass.streams.resolve_stream_url = AsyncMock(return_value="http://stream")
    fake._create_cc_media_item = MagicMock(return_value={})
    fake._launch_app = AsyncMock()
    return fake


async def _play_media(fake: MagicMock) -> None:
    await ChromecastPlayer.play_media(cast("ChromecastPlayer", fake), MagicMock())


async def test_play_media_sends_a_kept_back_volume_after_the_load() -> None:
    """The kept-back volume goes out once the media is loaded, before any audio plays."""
    fake = _fake_play_media_player(pending_volume=36)

    await _play_media(fake)

    assert fake._pending_volume is None
    load_index = fake.cc.mock_calls.index(
        call.media_controller.send_message(data={"type": "LOAD", "media": {}}, inc_session_id=True)
    )
    assert fake.cc.mock_calls.index(call.set_volume(0.36)) > load_index


async def test_play_media_without_a_kept_back_volume_sends_none() -> None:
    """A normal playback start never touches the volume."""
    fake = _fake_play_media_player(pending_volume=None)

    await _play_media(fake)

    fake.cc.set_volume.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [RequestFailed("volume"), RequestTimeout("volume", 10.0), NotConnected("down")],
)
async def test_play_media_survives_a_failed_volume_send(error: PyChromecastError) -> None:
    """A failed volume send is logged, so it cannot fail a play command that already loaded."""
    fake = _fake_play_media_player(pending_volume=36)
    fake.cc.set_volume.side_effect = error

    await _play_media(fake)  # must not raise

    assert fake._pending_volume is None
    fake.logger.warning.assert_called_once()


async def test_play_media_propagates_an_unexpected_volume_error() -> None:
    """Only cast send failures are swallowed; a real bug still surfaces."""
    fake = _fake_play_media_player(pending_volume=36)
    fake.cc.set_volume.side_effect = RuntimeError("bug")

    with pytest.raises(RuntimeError):
        await _play_media(fake)


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


def test_cast_status_shows_the_kept_back_volume_while_idle() -> None:
    """The device's old reported level does not overwrite a volume still kept back."""
    fake = _fake_status_player(app_id=IDLE_APP_ID, pending_volume=77)

    _handle_cast_status(fake, _cast_status(volume_level=0.10))

    assert fake._attr_volume_level == 77


def test_cast_status_reflects_the_device_when_nothing_is_kept_back() -> None:
    """With nothing kept back, the reported level is used as-is."""
    fake = _fake_status_player(app_id=IDLE_APP_ID, pending_volume=None)

    _handle_cast_status(fake, _cast_status(volume_level=0.42))

    assert fake._attr_volume_level == 42


def test_cast_status_reflects_the_device_once_active() -> None:
    """A kept-back volume only overrides the report while idle; once active it is stale."""
    fake = _fake_status_player(app_id=MASS_APP_ID, pending_volume=77)

    _handle_cast_status(fake, _cast_status(volume_level=0.42))

    assert fake._attr_volume_level == 42
