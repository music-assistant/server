"""Tests for the Sonos player options."""

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from aiosonos.api.upnp import EqType, IrRepeaterState, RoomCalibrationStatus
from aiosonos.exceptions import CommandTimeout, FailedCommand
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.providers.sonos.const import UPNP_OPTION_TIMEOUT_LIMIT
from music_assistant.providers.sonos.options import (
    SONOS_OPTIONS,
    SonosDevice,
    read_player_options,
)
from tests.providers.sonos.test_player import _make_named_player

LOGGER = logging.getLogger("test.sonos.options")

# getPlayerSettings of a Sonos Ray (firmware 98.0)
RAY_SETTINGS: dict[str, Any] = {
    "monoMode": False,
    "enableSpatialAudio": True,
    "homeTheater": {"nightMode": True, "enhanceDialog": False, "enhanceDialogLevel": 0},
    "eq": {"treble": -3, "bass": 3, "loudness": True},
}
SPEAKER_SETTINGS: dict[str, Any] = {
    "monoMode": False,
    "eq": {"treble": 0, "bass": 0, "loudness": True},
}

BASE_KEYS = {
    "bass",
    "treble",
    "loudness",
    "balance",
    "status_light",
    "touch_controls",
    "mono_mode",
}
HOME_THEATER_KEYS = {
    "spatial_audio",
    "night_mode",
    "speech_enhancement",
    "audio_delay",
    "ir_repeater",
    "remote_led_feedback",
}
SETTINGS_KEYS = {
    "bass",
    "treble",
    "loudness",
    "mono_mode",
    "spatial_audio",
    "night_mode",
    "speech_enhancement",
}
UPNP_GETTERS = (
    "get_eq",
    "get_channel_volume",
    "get_led_state",
    "get_button_lock_state",
    "get_room_calibration_status",
    "get_ir_repeater_state",
    "get_led_feedback_state",
)
SUB_KEYS = {"sub_enabled", "subwoofer_volume"}
SURROUND_KEYS = {"surround_enabled", "surround_mode", "surround_level", "music_surround_level"}


def _device(
    model: str = "Ray",
    settings: dict[str, Any] | None = None,
    has_subwoofer: bool = False,
    has_surrounds: bool = False,
    trueplay_available: bool = False,
) -> SonosDevice:
    client = MagicMock()
    client.player.settings = RAY_SETTINGS if settings is None else settings
    client.player.has_subwoofer = has_subwoofer
    client.player.has_surrounds = has_surrounds
    client.player.refresh_settings = AsyncMock()
    client.player.set_home_theater_options = AsyncMock()
    client.upnp = upnp = AsyncMock()
    upnp.get_eq = AsyncMock(return_value=1)
    upnp.get_channel_volume = AsyncMock(side_effect=lambda channel: 100 if channel == "LF" else 80)
    upnp.get_led_state = AsyncMock(return_value=False)
    upnp.get_button_lock_state = AsyncMock(return_value=False)
    upnp.get_led_feedback_state = AsyncMock(return_value=True)
    upnp.get_ir_repeater_state = AsyncMock(return_value=IrRepeaterState.ON)
    upnp.get_room_calibration_status = AsyncMock(
        return_value=RoomCalibrationStatus(enabled=True, available=trueplay_available)
    )
    return SonosDevice(client, model)


@pytest.mark.parametrize(
    ("device", "expected_keys"),
    [
        (_device(), BASE_KEYS | HOME_THEATER_KEYS),
        (
            _device(has_subwoofer=True, has_surrounds=True),
            BASE_KEYS | HOME_THEATER_KEYS | SUB_KEYS | SURROUND_KEYS,
        ),
        (_device(model="Arc Ultra"), BASE_KEYS | HOME_THEATER_KEYS | {"dialogue_level"}),
        (_device(model="Amp"), BASE_KEYS | HOME_THEATER_KEYS | SUB_KEYS | {"sub_crossover"}),
        (
            _device(model="Era 100", settings=SPEAKER_SETTINGS, trueplay_available=True),
            BASE_KEYS | {"trueplay"},
        ),
    ],
    ids=["ray", "ray_with_sub_and_surrounds", "arc_ultra", "amp", "era_100"],
)
async def test_options_offered_per_speaker(device: SonosDevice, expected_keys: set[str]) -> None:
    """A speaker gets the options its settings, model and bonded speakers allow."""
    result = await read_player_options(device, LOGGER, 0)

    assert {option.key for option in result.options} == expected_keys


async def test_option_values_and_unanswered_read() -> None:
    """Values come from settings and UPnP; an option the speaker does not answer is left out."""
    device = _device()
    client: Any = device.client
    client.upnp.get_led_state = AsyncMock(side_effect=FailedCommand("401"))

    result = await read_player_options(device, LOGGER, 3)
    options = {option.key: option for option in result.options}

    # an answered UPnP read ends the timeouts in a row
    assert result.upnp_timeouts == 0
    assert "status_light" not in options
    assert options["bass"].value == 3
    assert (options["bass"].min_value, options["bass"].max_value) == (-10, 10)
    assert options["night_mode"].value is True
    assert options["balance"].value == -20
    assert options["touch_controls"].value is True
    assert options["mono_mode"].read_only
    assert not options["bass"].read_only
    client.player.refresh_settings.assert_awaited_once()


async def test_upnp_reads_stop_after_timeouts_in_a_row() -> None:
    """Once UPnP timed out often enough in a row, only the settings options are read."""
    device = _device()
    client: Any = device.client
    for method in UPNP_GETTERS:
        setattr(client.upnp, method, AsyncMock(side_effect=CommandTimeout("upnp", 10)))

    first = await read_player_options(device, LOGGER, 0)
    second = await read_player_options(device, LOGGER, first.upnp_timeouts)

    assert first.upnp_timeouts >= UPNP_OPTION_TIMEOUT_LIMIT
    assert {option.key for option in second.options} == SETTINGS_KEYS
    assert client.upnp.get_led_state.await_count == 1


@pytest.mark.parametrize(
    ("key", "value", "method", "expected_calls"),
    [
        ("bass", 4, "upnp.set_bass", [call(4)]),
        ("loudness", False, "upnp.set_loudness", [call(False)]),
        ("night_mode", False, "player.set_home_theater_options", [call({"nightMode": False})]),
        ("touch_controls", False, "upnp.set_button_lock_state", [call(True)]),
        ("balance", -20.0, "upnp.set_channel_volume", [call("LF", 100), call("RF", 80)]),
        ("subwoofer_volume", 5, "upnp.set_eq", [call(EqType.SUB_GAIN, 5)]),
        ("surround_enabled", True, "upnp.set_eq", [call(EqType.SURROUND_ENABLED, 1)]),
        ("ir_repeater", False, "upnp.set_ir_repeater_state", [call(IrRepeaterState.OFF)]),
    ],
)
async def test_option_writes(key: str, value: Any, method: str, expected_calls: list[Any]) -> None:
    """Each option is written through the api that can set it."""
    device = _device()
    option = next(option for option in SONOS_OPTIONS if option.key == key)
    assert option.write is not None

    await option.write(device, value)

    target: Any = device.client
    for attribute in method.split("."):
        target = getattr(target, attribute)
    assert target.await_args_list == expected_calls


@pytest.mark.parametrize(
    ("key", "write_error", "fails"),
    [
        ("bass", None, False),
        ("mono_mode", None, True),
        ("unknown", None, True),
        ("bass", FailedCommand("402"), True),
    ],
    ids=["set", "read_only", "unknown", "rejected"],
)
async def test_set_option(key: str, write_error: Exception | None, fails: bool) -> None:
    """A set option is read back; read-only, unknown and rejected ones raise."""
    player = _make_named_player("Ray")
    player.discovery_info = {"device": {"modelDisplayName": "Ray"}}  # type: ignore[typeddict-item]
    client: Any = _device().client
    player.client = client
    client.upnp.set_bass = AsyncMock(side_effect=write_error)

    if fails:
        with pytest.raises(PlayerCommandFailed):
            await player.set_option(key, 4)
        return
    await player.set_option(key, 4)

    client.upnp.set_bass.assert_awaited_once_with(4)
    assert any(option.key == "bass" for option in player._attr_options)
    update_state: Any = player.update_state
    update_state.assert_called_once()
