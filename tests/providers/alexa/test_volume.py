"""Tests for how volume changes go to an Echo and what MA shows."""

from __future__ import annotations

import asyncio
import math
from functools import partial
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from alexapy.errors import AlexapyError
from music_assistant_models.enums import PlaybackState

from music_assistant.providers.alexa import AlexaPlayer, AlexaProvider
from tests.common import MockProvider, create_mock_config


def _player() -> tuple[AlexaPlayer, Any]:
    """Return a player on an Echo at volume 20, and its provider."""
    provider = cast("Any", MockProvider("alexa", instance_id="alexa--1"))
    provider.mass.config.get_base_player_config.return_value = create_mock_config("Echo")
    provider.mass.loop.time.return_value = 100.0
    provider.get_volumes = AsyncMock(return_value={"SERIAL": 20})
    provider.step_volume = AsyncMock()
    provider.login = MagicMock()
    player = AlexaPlayer(provider, "Echo", MagicMock(device_serial_number="SERIAL"))
    return player, provider


async def test_slider_sends_one_step_once_it_stops() -> None:
    """A burst of changes sends one step, from the Echo's volume to the last value."""
    player, provider = _player()
    provider.step_volume.return_value = 41
    with patch.object(player, "update_state"):
        await player.volume_set(30)
        await player.volume_set(40)
        delay, start_send = provider.mass.call_later.call_args.args[:2]
        assert delay == 0.5
        provider.step_volume.assert_not_awaited()
        # the send runs as its own task, so the next timer for this player cannot cancel it
        start_send()
        send = provider.mass.create_task.call_args.args[0]
        assert "task_id" not in provider.mass.create_task.call_args.kwargs
        send.close()

        await player._send_volume()
    provider.step_volume.assert_awaited_once_with(player.device, 20)
    # the Echo's own volume steps can land 1 off
    assert player.volume_level == 41


async def test_change_during_a_step_sends_one_more() -> None:
    """A change made while a step is sent goes out from the volume that step reached."""
    player, provider = _player()

    async def step(_device: object, amount: int) -> int:
        if provider.step_volume.await_count == 1:
            await player.volume_set(50)
            # the debounced send that change schedules leaves it to the running one
            await player._send_volume()
            return 40
        return 40 + amount

    provider.step_volume.side_effect = step
    with patch.object(player, "update_state"):
        await player.volume_set(40)
        await player._send_volume()
    assert [c.args[1] for c in provider.step_volume.await_args_list] == [20, 10]
    assert player.volume_level == 50


async def test_poll_shows_a_change_on_the_echo_unless_a_change_is_on_its_way() -> None:
    """The readback is shown, but not while MA's own change is being sent."""
    player, provider = _player()
    provider.step_volume.return_value = 40
    with patch.object(player, "update_state"):
        await player.volume_set(40)
        provider.mass.loop.time.return_value = 101.0
        await player.poll()
        provider.get_volumes.assert_not_awaited()
        await player._send_volume()

        provider.get_volumes.return_value = {"SERIAL": 35}
        provider.mass.loop.time.return_value = 103.0
        await player.poll()
    assert player.volume_level == 35


async def test_failed_step_leaves_the_volume_to_the_next_poll() -> None:
    """A failed step is logged, and the next poll shows the Echo's real volume."""
    player, provider = _player()
    provider.step_volume.side_effect = AlexapyError("volume step failed: TIME_OUT")
    with patch.object(player, "update_state"):
        await player.volume_set(40)
        await player._send_volume()
        assert player._echo_volume is None
        provider.mass.loop.time.return_value = 103.0
        await player.poll()
    assert player.volume_level == 20


async def test_echo_missing_from_the_readback_gets_a_volume_routine() -> None:
    """A device the readback leaves out, like a speaker group, still gets its volume."""
    player, provider = _player()
    provider.get_volumes.return_value = {}
    with (
        patch.object(player, "update_state"),
        patch("music_assistant.providers.alexa.AlexaAPI") as api,
    ):
        api.return_value.set_volume = AsyncMock()
        await player.volume_set(40)
        await player._send_volume()
    provider.step_volume.assert_not_awaited()
    api.return_value.set_volume.assert_awaited_once_with(0.4)
    assert player.volume_level == 40


async def test_change_in_ma_during_a_readback_wins() -> None:
    """A readback that was on its way when MA changed the volume is not shown."""
    player, provider = _player()

    async def read() -> dict[str, int]:
        await player.volume_set(40)
        return {"SERIAL": 20}

    provider.get_volumes.side_effect = read
    with patch.object(player, "update_state"):
        await player.poll()
    assert player.volume_level == 40


async def test_poll_waits_after_a_step_reply() -> None:
    """A readback taken before the step's reply is not shown over it."""
    player, provider = _player()
    provider.step_volume.return_value = 50
    with patch.object(player, "update_state"):
        await player.volume_set(50)
        provider.mass.loop.time.return_value = 103.0
        await player._send_volume()
        provider.get_volumes.reset_mock()
        await player.poll()
    provider.get_volumes.assert_not_awaited()
    assert player.volume_level == 50


async def test_failed_readback_keeps_the_volume() -> None:
    """A readback that fails or misses the Echo leaves MA's volume alone, without a warning."""
    player, provider = _player()
    with patch.object(player, "update_state"):
        await player.poll()
        provider.get_volumes.side_effect = TimeoutError
        await player.poll()
        provider.get_volumes.side_effect = None
        provider.get_volumes.return_value = {}
        await player.poll()
    assert player.volume_level == 20


async def test_get_volumes_reads_all_echos_once_per_round() -> None:
    """One read serves every Echo for 2 s; a failed read serves nothing."""
    response = MagicMock(
        json=AsyncMock(
            return_value={
                "volumes": [
                    {"dsn": "A", "speakerVolume": 30},
                    {"dsn": "B", "speakerVolume": None},
                ]
            }
        )
    )
    provider = SimpleNamespace(
        login=MagicMock(),
        mass=MagicMock(),
        _volumes={},
        _volumes_read_at=-math.inf,
        _volumes_lock=asyncio.Lock(),
    )
    provider.mass.loop.time.return_value = 100.0
    get_volumes = partial(AlexaProvider.get_volumes, cast("Any", provider))

    async def slow_request(*_args: object) -> MagicMock:
        await asyncio.sleep(0)  # lets the other poll start while this read is out
        return response

    with patch(
        "music_assistant.providers.alexa.AlexaAPI._static_request",
        AsyncMock(side_effect=slow_request),
    ) as request:
        # two players polling at once share one read
        assert list(await asyncio.gather(get_volumes(), get_volumes())) == [{"A": 30}] * 2
        provider.mass.loop.time.return_value = 101.0
        assert await get_volumes() == {"A": 30}
        assert request.await_count == 1

        request.side_effect = TimeoutError
        provider.mass.loop.time.return_value = 103.0
        with pytest.raises(TimeoutError):
            await get_volumes()
        assert await get_volumes() == {}


def _echo_follows_steps(player: AlexaPlayer, provider: Any) -> None:
    """Make each step land exactly on the Echo's volume plus the step."""
    provider.step_volume.side_effect = lambda _device, amount: player._echo_volume + amount


async def test_mute_steps_to_zero_and_back() -> None:
    """Mute steps the Echo to 0 and keeps its volume; unmute steps back to it."""
    player, provider = _player()
    _echo_follows_steps(player, provider)
    with patch.object(player, "update_state"):
        await player.poll()
        await player.volume_mute(True)
        await player._send_volume()
        assert (player.volume_level, player.volume_muted) == (20, True)
        provider.get_volumes.return_value = {"SERIAL": 0}

        await player.volume_mute(False)
        await player._send_volume()
    assert [c.args[1] for c in provider.step_volume.await_args_list] == [-20, 20]
    assert (player.volume_level, player.volume_muted) == (20, False)


async def test_a_muted_echo_stays_silent_when_its_volume_changes() -> None:
    """A volume change while muted, as from a group, is kept for the unmute."""
    player, provider = _player()
    _echo_follows_steps(player, provider)
    with patch.object(player, "update_state"):
        await player.poll()
        await player.volume_mute(True)
        await player._send_volume()
        provider.get_volumes.return_value = {"SERIAL": 0}
        await player.volume_set(50)
        await player._send_volume()
        assert (player.volume_level, player.volume_muted) == (50, True)

        await player.volume_mute(False)
        await player._send_volume()
    assert [c.args[1] for c in provider.step_volume.await_args_list] == [-20, 50]
    assert (player.volume_level, player.volume_muted) == (50, False)


@pytest.mark.parametrize(("before", "unmuted_at"), [(20, 20), (None, 10)])
async def test_mute_on_the_echo_shows_as_muted(before: int | None, unmuted_at: int) -> None:
    """A voice mute sets 0, which shows as muted; unmute goes back to the volume before."""
    player, provider = _player()
    _echo_follows_steps(player, provider)
    with patch.object(player, "update_state"):
        if before is not None:
            await player.poll()
        provider.get_volumes.return_value = {"SERIAL": 0}
        await player.poll()
        assert (player.volume_level, player.volume_muted) == (before, True)

        await player.volume_mute(False)
        await player._send_volume()
    provider.step_volume.assert_awaited_once_with(player.device, unmuted_at)
    assert (player.volume_level, player.volume_muted) == (unmuted_at, False)


async def test_a_change_steps_from_the_echo_volume_now() -> None:
    """A change made on the Echo since the last readback is not stepped over."""
    player, provider = _player()
    _echo_follows_steps(player, provider)
    with patch.object(player, "update_state"):
        await player.poll()
        # turned up by voice after that readback
        provider.get_volumes.return_value = {"SERIAL": 50}
        await player.volume_set(40)
        await player._send_volume()
    provider.step_volume.assert_awaited_once_with(player.device, -10)
    assert player.volume_level == 40


async def test_unload_drops_a_volume_change_on_its_way() -> None:
    """A removed Echo gets no volume change that was still waiting or being sent."""
    player, provider = _player()
    with patch.object(player, "update_state"):
        await player.volume_set(40)
        provider.mass.call_later.call_args.args[1]()
        provider.mass.create_task.call_args.args[0].close()
        await player.on_unload()
    provider.mass.cancel_timer.assert_any_call("alexa_volume_Echo")
    provider.mass.create_task.return_value.cancel.assert_called_once()


def test_poll_is_slower_when_not_playing() -> None:
    """The volume is read every 5 s while playing, and every 30 s otherwise."""
    player, _ = _player()
    assert player.poll_interval == 30
    player._attr_playback_state = PlaybackState.PLAYING
    assert player.poll_interval == 5


async def test_volume_up_on_the_echo_unmutes() -> None:
    """A muted Echo turned up by voice or button shows as unmuted."""
    player, provider = _player()
    provider.step_volume.return_value = 0
    with patch.object(player, "update_state"):
        await player.volume_mute(True)
        await player._send_volume()
        provider.get_volumes.return_value = {"SERIAL": 30}
        provider.mass.loop.time.return_value = 103.0
        await player.poll()
        assert player.volume_muted is False
        # unmute now leaves the volume set on the Echo alone
        await player.volume_mute(False)
    assert player.volume_level == 30


@pytest.mark.parametrize(
    ("body", "result"),
    [
        ({"error": None, "speakerVolume": 27}, 27),
        ({"error": "TIME_OUT", "speakerVolume": None}, None),
    ],
)
async def test_step_volume_returns_the_new_volume(body: dict[str, Any], result: int | None) -> None:
    """The step's reply carries the Echo's new volume, or an error."""
    response = MagicMock(json=AsyncMock(return_value=body))
    provider = SimpleNamespace(login=MagicMock())
    device = SimpleNamespace(device_serial_number="SERIAL", _device_type="TYPE")
    with patch(
        "music_assistant.providers.alexa.AlexaAPI._static_request",
        AsyncMock(return_value=response),
    ) as request:
        if result is None:
            with pytest.raises(AlexapyError, match="TIME_OUT"):
                await AlexaProvider.step_volume(cast("Any", provider), cast("Any", device), 5)
        else:
            assert (
                await AlexaProvider.step_volume(cast("Any", provider), cast("Any", device), 5)
                == result
            )
    assert request.await_args is not None
    method, _, uri = request.await_args.args
    assert (method, uri) == ("put", "/api/devices/TYPE/SERIAL/audio/v2/speakerVolume")
    assert request.await_args.kwargs["data"]["amount"] == 5
