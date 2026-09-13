"""
Tests for deferring a Cast volume_set received while the device is idle.

Some receivers store a volume set while no audio is flowing but keep playing at their
previous level, then de-duplicate a plain resend of the value they report. So a volume
set while idle is deferred, and once the device is actually playing it is re-asserted
with a 1/255 nudge (target - 1/255, then target) that the device cannot de-duplicate.
"""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from pychromecast import IDLE_APP_ID
from pychromecast.error import NotConnected, PyChromecastError, RequestFailed, RequestTimeout

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
    """A volume_set while no app has ever run (app_id None) is stored and armed, not sent."""
    player = _make_player(app_id=None)

    await player.volume_set(42)

    assert _sent_volumes(player) == []
    assert player._pending_volume == 42
    assert player._reassert_volume is True
    assert player.volume_level == 42


async def test_volume_set_while_idle_defers() -> None:
    """A volume_set while the receiver reports the idle app is stored and armed, not sent."""
    player = _make_player(app_id=IDLE_APP_ID)

    await player.volume_set(55)

    assert _sent_volumes(player) == []
    assert player._pending_volume == 55
    assert player._reassert_volume is True
    assert player.volume_level == 55


async def test_volume_set_while_active_sends_immediately() -> None:
    """A volume_set while our app is running is sent right away, rounded to cast's scale."""
    player = _make_player(app_id=MASS_APP_ID)

    await player.volume_set(50)

    assert _sent_volumes(player) == [0.5]
    assert player._pending_volume is None
    assert player._reassert_volume is False


async def test_volume_set_while_active_clears_a_stale_deferral() -> None:
    """A send while active clears both a value and its arm left over from an earlier defer."""
    player = _make_player(app_id=IDLE_APP_ID)
    await player.volume_set(20)  # idle: defers and arms
    cast("MagicMock", player.cc).app_id = MASS_APP_ID  # the app is running now

    await player.volume_set(50)

    assert player._pending_volume is None
    assert player._reassert_volume is False


async def test_reassert_sends_the_target_directly_when_not_wedged() -> None:
    """The device still reports the old level, so a single plain send applies the target."""
    player = _make_player(app_id=MASS_APP_ID)
    player._pending_volume = 36
    cast("MagicMock", player.cc).status.volume_level = 0.06  # the old, pre-idle level

    await player._reassert_pending_volume()

    assert _sent_volumes(player) == [0.36]
    assert player._pending_volume is None


async def test_reassert_nudges_when_the_device_is_wedged_at_the_target() -> None:
    """When the device already reports the target, a 1/255 nudge is needed to escape de-dup."""
    player = _make_player(app_id=MASS_APP_ID)
    player._pending_volume = 36
    cast("MagicMock", player.cc).status.volume_level = 0.36  # reports target, plays old

    with patch("music_assistant.providers.chromecast.player.VOLUME_REASSERT_GAP", 0):
        await player._reassert_pending_volume()

    sent = _sent_volumes(player)
    assert len(sent) == 2
    assert sent[0] == pytest.approx(round(0.36, 2) - 1 / 255)
    assert sent[1] == 0.36
    assert player._pending_volume is None


async def test_reassert_pending_volume_with_nothing_pending_is_a_noop() -> None:
    """With no deferred value there is nothing to re-assert."""
    player = _make_player(app_id=MASS_APP_ID)
    player._pending_volume = None

    await player._reassert_pending_volume()

    assert _sent_volumes(player) == []


@pytest.mark.parametrize(
    "error",
    [RequestFailed("volume"), RequestTimeout("volume", 10.0), NotConnected("down")],
)
async def test_reassert_pending_volume_survives_a_failed_send(error: PyChromecastError) -> None:
    """A failed re-assert is swallowed; it is fire-and-forget from the status handler."""
    player = _make_player(app_id=MASS_APP_ID)
    player._pending_volume = 36
    cast("MagicMock", player.cc).status.volume_level = 0.06
    cast("MagicMock", player.cc).set_volume.side_effect = error

    with patch("music_assistant.providers.chromecast.player.VOLUME_REASSERT_GAP", 0):
        await player._reassert_pending_volume()  # must not raise

    assert player._pending_volume is None


async def test_reassert_pending_volume_propagates_an_unexpected_error() -> None:
    """Only pychromecast send failures are swallowed; a real bug still surfaces."""
    player = _make_player(app_id=MASS_APP_ID)
    player._pending_volume = 36
    cast("MagicMock", player.cc).status.volume_level = 0.06
    cast("MagicMock", player.cc).set_volume.side_effect = RuntimeError("bug")

    with (
        patch("music_assistant.providers.chromecast.player.VOLUME_REASSERT_GAP", 0),
        pytest.raises(RuntimeError),
    ):
        await player._reassert_pending_volume()


def _fake_play_media_player(*, reassert: bool) -> MagicMock:
    """Build a MagicMock standing in for a ChromecastPlayer, for use with play_media."""
    fake = MagicMock()
    fake.player_id = "cast-1"
    fake._reassert_volume = reassert
    fake.provider.mass.streams.resolve_stream_url = AsyncMock(return_value="http://stream")
    fake._create_cc_media_item = MagicMock(return_value={})
    fake._launch_app = AsyncMock()
    fake._reassert_pending_volume = AsyncMock()
    return fake


async def _play_media(fake: MagicMock) -> None:
    await ChromecastPlayer.play_media(cast("ChromecastPlayer", fake), MagicMock())


async def test_play_media_reasserts_a_volume_deferred_while_idle() -> None:
    """Playback start (media loaded, no audio yet) is where a deferred volume is applied."""
    fake = _fake_play_media_player(reassert=True)

    await _play_media(fake)

    assert fake._reassert_volume is False
    fake._reassert_pending_volume.assert_awaited_once()


async def test_play_media_without_a_deferred_volume_does_not_reassert() -> None:
    """A normal playback start (nothing deferred) never nudges the volume."""
    fake = _fake_play_media_player(reassert=False)

    await _play_media(fake)

    fake._reassert_pending_volume.assert_not_called()


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
