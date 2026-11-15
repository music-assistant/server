"""Tests for the Sonos player options."""

import logging
from collections.abc import Mapping
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from async_upnp_client.exceptions import UpnpActionError, UpnpConnectionTimeoutError
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.providers.sonos.const import (
    DEVICE_PROPERTIES_SERVICE,
    HT_CONTROL_SERVICE,
    EqType,
    IrRepeaterState,
)
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
SUB_KEYS = {"sub_enabled", "subwoofer_volume"}
SURROUND_KEYS = {"surround_enabled", "surround_mode", "surround_level", "music_surround_level"}


def _upnp(trueplay_available: bool = False, error: Exception | None = None) -> MagicMock:
    """Return a speaker's UPnP answering the Get actions like a Ray, or failing with error."""

    def _answer(action: str, **arguments: Any) -> Mapping[str, Any]:
        if error is not None:
            raise error
        answers: dict[str, Mapping[str, Any]] = {
            "GetEQ": {"CurrentValue": 1},
            "GetVolume": {"CurrentVolume": 100 if arguments.get("Channel") == "LF" else 80},
            "GetRoomCalibrationStatus": {
                "RoomCalibrationEnabled": True,
                "RoomCalibrationAvailable": trueplay_available,
            },
            "GetLEDState": {"CurrentLEDState": "Off"},
            "GetButtonLockState": {"CurrentButtonLockState": "Off"},
            "GetIRRepeaterState": {"CurrentIRRepeaterState": "On"},
            "GetLEDFeedbackState": {"LEDFeedbackState": "On"},
        }
        return answers.get(action, {})

    upnp = MagicMock(answers=True)
    upnp.rendering_control = AsyncMock(side_effect=_answer)
    upnp.call = AsyncMock(
        side_effect=lambda _service, action, **arguments: _answer(action, **arguments)
    )
    return upnp


def _device(
    model: str = "Ray",
    settings: dict[str, Any] | None = None,
    has_subwoofer: bool = False,
    has_surrounds: bool = False,
    upnp: MagicMock | None = None,
) -> SonosDevice:
    client = MagicMock()
    client.player.settings = RAY_SETTINGS if settings is None else settings
    client.player.has_subwoofer = has_subwoofer
    client.player.has_surrounds = has_surrounds
    client.player.refresh_settings = AsyncMock()
    client.player.set_home_theater_options = AsyncMock()
    return SonosDevice(client, upnp or _upnp(), model)


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
            _device(
                model="Era 100", settings=SPEAKER_SETTINGS, upnp=_upnp(trueplay_available=True)
            ),
            BASE_KEYS | {"trueplay"},
        ),
    ],
    ids=["ray", "ray_with_sub_and_surrounds", "arc_ultra", "amp", "era_100"],
)
async def test_options_offered_per_speaker(device: SonosDevice, expected_keys: set[str]) -> None:
    """A speaker gets the options its settings, model and bonded speakers allow."""
    options = await read_player_options(device, LOGGER, {})

    assert {option.key for option in options} == expected_keys


async def test_option_values_and_failed_reads() -> None:
    """A refused option is left out; one that timed out keeps its previous value."""
    upnp = _upnp()
    answer = upnp.call.side_effect

    def _call(service: str, action: str, **arguments: Any) -> Mapping[str, Any]:
        if action == "GetLEDState":
            raise UpnpActionError(error_code=401)
        if action == "GetButtonLockState":
            raise UpnpConnectionTimeoutError
        return answer(service, action, **arguments)  # type: ignore[no-any-return]

    upnp.call.side_effect = _call
    device = _device(upnp=upnp)
    previous = {option.key: option for option in await read_player_options(_device(), LOGGER, {})}

    options = {option.key: option for option in await read_player_options(device, LOGGER, previous)}

    assert "status_light" not in options
    assert options["touch_controls"] is previous["touch_controls"]
    assert options["bass"].value == 3
    assert (options["bass"].min_value, options["bass"].max_value) == (-10, 10)
    assert options["night_mode"].value is True
    assert options["balance"].value == -20
    assert options["remote_led_feedback"].value is True
    assert options["mono_mode"].read_only
    assert not options["bass"].read_only


async def test_nothing_is_read_over_upnp_while_the_speaker_does_not_answer() -> None:
    """Only the settings options are read once the speaker stopped answering UPnP."""
    upnp = _upnp()
    upnp.answers = False

    options = await read_player_options(_device(upnp=upnp), LOGGER, {})

    assert {option.key for option in options} == SETTINGS_KEYS
    upnp.call.assert_not_awaited()
    upnp.rendering_control.assert_not_awaited()


@pytest.mark.parametrize(
    ("key", "value", "method", "expected_calls"),
    [
        ("bass", 4, "upnp.rendering_control", [call("SetBass", DesiredBass=4)]),
        (
            "night_mode",
            False,
            "client.player.set_home_theater_options",
            [call({"nightMode": False})],
        ),
        (
            "touch_controls",
            False,
            "upnp.call",
            [call(DEVICE_PROPERTIES_SERVICE, "SetButtonLockState", DesiredButtonLockState="On")],
        ),
        (
            "remote_led_feedback",
            False,
            "upnp.call",
            [call(HT_CONTROL_SERVICE, "SetLEDFeedbackState", LEDFeedbackState="Off")],
        ),
        (
            "balance",
            -20.0,
            "upnp.rendering_control",
            [
                call("SetVolume", Channel="LF", DesiredVolume=100),
                call("SetVolume", Channel="RF", DesiredVolume=80),
            ],
        ),
        (
            "surround_enabled",
            True,
            "upnp.rendering_control",
            [call("SetEQ", EQType=EqType.SURROUND_ENABLED, DesiredValue=1)],
        ),
        (
            "ir_repeater",
            False,
            "upnp.call",
            [
                call(
                    HT_CONTROL_SERVICE,
                    "SetIRRepeaterState",
                    DesiredIRRepeaterState=IrRepeaterState.OFF,
                )
            ],
        ),
    ],
)
async def test_option_writes(key: str, value: Any, method: str, expected_calls: list[Any]) -> None:
    """Each option is written through the api that can set it."""
    device = _device()
    option = next(option for option in SONOS_OPTIONS if option.key == key)
    assert option.write is not None

    await option.write(device, value)

    target: Any = device
    for attribute in method.split("."):
        target = getattr(target, attribute)
    assert target.await_args_list == expected_calls


async def test_polled_options_are_merged_into_the_options_read_before() -> None:
    """Polling some options keeps the others, in the order of the table."""
    player = _make_named_player("Ray")
    player.discovery_info = {"device": {"modelDisplayName": "Ray"}}  # type: ignore[typeddict-item]
    device = _device()
    player.client = device.client
    player.upnp = device.upnp
    await player._update_options()
    status_light = next(option for option in SONOS_OPTIONS if option.key == "status_light")
    upnp: Any = device.upnp
    upnp.call.side_effect = lambda _service, _action: {"CurrentLEDState": "On"}

    keys_before = [option.key for option in player._attr_options]
    await player._update_options([status_light])

    assert [option.key for option in player._attr_options] == keys_before
    values = {option.key: option.value for option in player._attr_options}
    assert values["status_light"] is True
    assert values["bass"] == 3


@pytest.mark.parametrize(
    ("key", "write_error", "fails"),
    [
        ("bass", None, False),
        ("mono_mode", None, True),
        ("unknown", None, True),
        ("bass", UpnpActionError(error_code=402), True),
    ],
    ids=["set", "read_only", "unknown", "rejected"],
)
async def test_set_option(key: str, write_error: Exception | None, fails: bool) -> None:
    """A set option is read back; read-only, unknown and rejected ones raise."""
    player = _make_named_player("Ray")
    player.discovery_info = {"device": {"modelDisplayName": "Ray"}}  # type: ignore[typeddict-item]
    device = _device(upnp=_upnp(error=write_error))
    player.client = device.client
    player.upnp = device.upnp

    if fails:
        with pytest.raises(PlayerCommandFailed):
            await player.set_option(key, 4)
        return
    await player.set_option(key, 4)

    upnp: Any = device.upnp
    assert call("SetBass", DesiredBass=4) in upnp.rendering_control.await_args_list
    assert any(option.key == "bass" for option in player._attr_options)
    update_state: Any = player.update_state
    update_state.assert_called_once()
