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
    provider._volumes_lock = asyncio.Lock()
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
        start_send()
        send = provider.mass.create_task.call_args.args[0]
        assert "task_id" not in provider.mass.create_task.call_args.kwargs
        send.close()

        await player._send_volume()
    provider.step_volume.assert_awaited_once_with(player.device, 20)
    assert player.volume_level == 41


async def test_change_during_a_step_sends_one_more() -> None:
    """A change made while a step is sent goes out from the volume that step reached."""
    player, provider = _player()

    async def step(_device: object, amount: int) -> int:
        if provider.step_volume.await_count == 1:
            await player.volume_set(50)
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


async def test_a_failed_send_goes_out_once_more() -> None:
    """A failed send is tried once more."""
    player, provider = _player()
    provider.get_volumes.side_effect = [
        AlexapyError("volume read failed"),
        {"SERIAL": 20},
    ]
    provider.step_volume.return_value = 40
    with patch.object(player, "update_state"):
        await player.volume_set(40)
        await player._send_volume()
        assert provider.mass.call_later.call_count == 2
        assert provider.mass.call_later.call_args.args[0] == 2
        await player._send_volume()
    provider.step_volume.assert_awaited_once_with(player.device, 20)
    assert player.volume_level == 40


async def test_a_send_that_keeps_failing_is_not_retried_again() -> None:
    """The retry of a failed send is not retried itself."""
    player, provider = _player()
    provider.step_volume.side_effect = AlexapyError("volume step failed: TIME_OUT")
    with patch.object(player, "update_state"):
        await player.volume_set(40)
        await player._send_volume()
        await player._send_volume()
    assert provider.step_volume.await_count == 2
    assert provider.mass.call_later.call_count == 2


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


async def test_poll_waits_after_a_step_reply() -> None:
    """No poll for 2 s after a step's reply."""
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
        await asyncio.sleep(0)
        return response

    with patch(
        "music_assistant.providers.alexa.AlexaAPI._static_request",
        AsyncMock(side_effect=slow_request),
    ) as request:
        assert list(await asyncio.gather(get_volumes(), get_volumes())) == [{"A": 30}] * 2
        provider.mass.loop.time.return_value = 101.0
        assert await get_volumes() == {"A": 30}
        assert request.await_count == 1

        request.side_effect = TimeoutError
        provider.mass.loop.time.return_value = 103.0
        with pytest.raises(TimeoutError):
            await get_volumes()
        with pytest.raises(AlexapyError):
            await get_volumes()

        request.side_effect = None
        request.return_value = None
        provider.mass.loop.time.return_value = 106.0
        with pytest.raises(AlexapyError):
            await get_volumes()


def _echo_follows_steps(player: AlexaPlayer, provider: Any) -> None:
    """Make each step land exactly on the Echo's volume plus the step."""
    provider.step_volume.side_effect = lambda _device, amount: player._echo_volume + amount


async def test_a_change_steps_from_the_echo_volume_now() -> None:
    """A step goes from the Echo's volume read just before it."""
    player, provider = _player()
    _echo_follows_steps(player, provider)
    with patch.object(player, "update_state"):
        await player.poll()
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


async def test_changes_in_a_row_step_from_the_last_reply() -> None:
    """A second change within 2 s of the shared read starts from where the first one left the Echo."""
    player, provider = _player()
    provider._volumes = {}
    provider._volumes_read_at = -math.inf
    provider._volumes_lock = asyncio.Lock()
    provider.get_volumes = partial(AlexaProvider.get_volumes, provider)
    provider.step_volume = partial(AlexaProvider.step_volume, provider)
    echo = {"volume": 20}

    async def request(method: str, *_args: object, **kwargs: Any) -> MagicMock:
        if method == "put":
            echo["volume"] = max(0, min(100, echo["volume"] + kwargs["data"]["amount"]))
            body: dict[str, Any] = {"speakerVolume": echo["volume"]}
        else:
            body = {"volumes": [{"dsn": "SERIAL", "speakerVolume": echo["volume"]}]}
        return MagicMock(json=AsyncMock(return_value=body))

    with (
        patch.object(player, "update_state"),
        patch("music_assistant.providers.alexa.AlexaAPI._static_request", side_effect=request),
    ):
        await player.volume_set(40)
        await player._send_volume()
        await player.volume_set(50)
        await player._send_volume()
        assert echo["volume"] == 50
        await player.volume_set(0)
        await player._send_volume()
    assert echo["volume"] == 0


async def test_poll_skips_while_a_volume_call_is_out() -> None:
    """A poll doesn't wait on another Echo's step, as MA polls every player in one loop."""
    player, provider = _player()
    async with provider._volumes_lock:
        await player.poll()
    provider.get_volumes.assert_not_awaited()


@pytest.mark.parametrize("readback", [20, 0])
async def test_volume_zero_steps_fully_down(readback: int) -> None:
    """Volume 0 is always a full step down."""
    player, provider = _player()
    provider.get_volumes.return_value = {"SERIAL": readback}
    echo = {"volume": 20}

    async def step(_device: object, amount: int) -> int:
        echo["volume"] = max(0, min(100, echo["volume"] + amount))
        return echo["volume"]

    provider.step_volume.side_effect = step
    with patch.object(player, "update_state"):
        await player.volume_set(0)
        await player._send_volume()
    provider.step_volume.assert_awaited_once_with(player.device, -100)
    assert echo["volume"] == 0
    assert player.volume_level == 0


def test_poll_is_slower_when_not_playing() -> None:
    """The volume is read every 5 s while playing, and every 30 s otherwise."""
    player, _ = _player()
    assert player.poll_interval == 30
    player._attr_playback_state = PlaybackState.PLAYING
    assert player.poll_interval == 5


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
    provider = SimpleNamespace(login=MagicMock(), _volumes={}, _volumes_lock=asyncio.Lock())
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
