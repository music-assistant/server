"""
Tests for deferring a Cast volume_set received while the device is idle.

Some receivers store a volume set while no audio is flowing but keep playing at their
previous level, then de-duplicate a plain resend of the value they report. So a volume
set while idle is deferred and re-asserted at playback start, with a 1/255 nudge first
when the device already reports the target, so it cannot de-duplicate the resend.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, cast
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


async def test_reassert_nudges_upward_when_wedged_at_zero() -> None:
    """At a target of 0 a downward nudge clamps back to 0, so the nudge must go up instead."""
    player = _make_player(app_id=MASS_APP_ID)
    player._pending_volume = 0
    cast("MagicMock", player.cc).status.volume_level = 0.0

    with patch("music_assistant.providers.chromecast.player.VOLUME_REASSERT_GAP", 0):
        await player._reassert_pending_volume()

    assert _sent_volumes(player) == [pytest.approx(1 / 255), 0.0]


async def test_reassert_yields_to_a_volume_set_during_the_nudge_gap() -> None:
    """A volume the user sets while the nudge waits must not be overwritten by the old target."""
    player = _make_player(app_id=MASS_APP_ID)
    player._pending_volume = 30
    cast("MagicMock", player.cc).status.volume_level = 0.30

    async def user_sets_volume_during_gap(_delay: float) -> None:
        await player.volume_set(50)

    with patch(
        "music_assistant.providers.chromecast.player.asyncio.sleep", user_sets_volume_during_gap
    ):
        await player._reassert_pending_volume()

    sent = _sent_volumes(player)
    assert sent[-1] == 0.5
    assert 0.3 not in sent


def _hold_first_send() -> tuple[Callable[..., Any], asyncio.Event, asyncio.Event]:
    """
    Build a to_thread stand-in that holds the first send in flight until released.

    Every later send goes through at once, so a send that is not held back by the
    player overtakes the held one.
    """
    started = asyncio.Event()
    release = asyncio.Event()
    sends = 0

    async def to_thread(func: Callable[..., Any], *args: Any) -> Any:
        nonlocal sends
        sends += 1
        if sends == 1:
            started.set()
            await release.wait()
        return func(*args)

    return to_thread, started, release


async def _let_other_tasks_run() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_volume_set_waits_for_the_reassert_send_in_flight() -> None:
    """A volume_set made while the re-assert's send is on its way must land after it."""
    player = _make_player(app_id=MASS_APP_ID)
    player._pending_volume = 36
    cast("MagicMock", player.cc).status.volume_level = 0.06
    to_thread, started, release = _hold_first_send()

    with patch.object(asyncio, "to_thread", to_thread):
        reassert = asyncio.create_task(player._reassert_pending_volume())
        await started.wait()
        user = asyncio.create_task(player.volume_set(50))
        await _let_other_tasks_run()
        release.set()
        await asyncio.gather(reassert, user)

    assert _sent_volumes(player) == [0.36, 0.5]


async def test_volume_set_waits_for_the_nudge_send_in_flight() -> None:
    """A volume_set made while the nudge is on its way must land after it, not under it."""
    player = _make_player(app_id=MASS_APP_ID)
    player._pending_volume = 36
    cast("MagicMock", player.cc).status.volume_level = 0.36
    to_thread, started, release = _hold_first_send()

    with (
        patch.object(asyncio, "to_thread", to_thread),
        patch("music_assistant.providers.chromecast.player.VOLUME_REASSERT_GAP", 0),
    ):
        reassert = asyncio.create_task(player._reassert_pending_volume())
        await started.wait()
        user = asyncio.create_task(player.volume_set(50))
        await _let_other_tasks_run()
        release.set()
        await asyncio.gather(reassert, user)

    assert _sent_volumes(player) == [pytest.approx(0.36 - 1 / 255), 0.5]


async def test_reassert_skips_its_send_when_overtaken_while_waiting_to_send() -> None:
    """A volume_set made while the re-assert waits its turn makes the old target obsolete."""
    player = _make_player(app_id=MASS_APP_ID)
    player._pending_volume = 36
    cast("MagicMock", player.cc).status.volume_level = 0.06

    async with player._volume_lock:
        reassert = asyncio.create_task(player._reassert_pending_volume())
        await _let_other_tasks_run()
        user = asyncio.create_task(player.volume_set(50))
        await _let_other_tasks_run()
    await asyncio.gather(reassert, user)

    assert _sent_volumes(player) == [0.5]


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
    """A failed re-assert is swallowed, so it cannot abort the playback start that called it."""
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
    """
    Playback start (media loaded, no audio yet) is where a deferred volume is applied.

    It runs as a task, so a receiver slow to answer the volume sends cannot hold up playback.
    """
    fake = _fake_play_media_player(reassert=True)

    await _play_media(fake)

    assert fake._reassert_volume is False
    fake.mass.create_task.assert_called_once_with(
        fake._reassert_pending_volume, task_id=fake._reassert_task_id, abort_existing=True
    )


async def test_play_media_without_a_deferred_volume_does_not_reassert() -> None:
    """A normal playback start (nothing deferred) never nudges the volume."""
    fake = _fake_play_media_player(reassert=False)

    await _play_media(fake)

    fake.mass.create_task.assert_not_called()


async def test_unload_cancels_a_running_reassert() -> None:
    """An unloaded player must not keep sending volumes to the device."""
    player = _make_player(app_id=MASS_APP_ID)

    with (
        patch("music_assistant.providers.chromecast.player.Player.on_unload", AsyncMock()),
        patch("music_assistant.providers.chromecast.player.disconnect_cast"),
    ):
        await player.on_unload()

    cast("MagicMock", player.mass).cancel_task.assert_any_call(player._reassert_task_id)


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
