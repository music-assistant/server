"""
Player options (EQ, home theater and speaker settings) of a Sonos player.

The websocket api reports most settings in one call but sets only the home theater
options; everything else is set (and partly read) over UPnP.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp
from async_upnp_client.exceptions import UpnpActionError, UpnpError
from music_assistant_models.player import PlayerOption, PlayerOptionType, PlayerOptionValueType

from .const import (
    AMP_MODEL,
    AUDIO_DELAY_RANGE,
    BALANCE_RANGE,
    DEVICE_PROPERTIES_SERVICE,
    DIALOG_LEVEL_RANGE,
    EQ_RANGE,
    HT_CONTROL_SERVICE,
    SUB_CROSSOVER_RANGE,
    SUB_GAIN_RANGE,
    SURROUND_LEVEL_RANGE,
    ULTRA_SOUNDBAR_MODELS,
    EqType,
    IrRepeaterState,
    OptionRange,
)

if TYPE_CHECKING:
    import logging

    from aiosonos.client import SonosLocalApiClient

    from .upnp import SonosSpeakerUpnp

OptionReader = Callable[["SonosDevice"], Awaitable[PlayerOptionValueType | None]]
OptionWriter = Callable[["SonosDevice", Any], Awaitable[Any]]


@dataclass(frozen=True)
class SonosDevice:
    """The speaker a player option is read from and written to."""

    client: SonosLocalApiClient
    upnp: SonosSpeakerUpnp
    model: str

    def setting(self, *path: str) -> Any:
        """Return a value from the player's settings, None when the speaker does not report it."""
        value: Any = self.client.player.settings
        for key in path:
            if not isinstance(value, dict):
                return None
            value = value.get(key)
        return value

    @property
    def is_home_theater(self) -> bool:
        """Return if the speaker reports home theater options."""
        return self.setting("homeTheater") is not None

    @property
    def has_subwoofer(self) -> bool:
        """Return if the speaker drives a subwoofer."""
        return self.model == AMP_MODEL or self.client.player.has_subwoofer


@dataclass(frozen=True, kw_only=True)
class SonosOption:
    """A player option: offered when available() passes and read() returns a value."""

    key: str
    name: str
    option_type: PlayerOptionType
    read: OptionReader
    write: OptionWriter | None = None
    available: Callable[[SonosDevice], bool] = lambda _: True
    value_range: OptionRange | None = None
    read_over_upnp: bool = True
    # a change of it comes with a RenderingControl event (or the settings read with one)
    sends_events: bool = True

    @property
    def polled(self) -> bool:
        """Return if a change of the option is only seen by polling the speaker."""
        return self.read_over_upnp and not self.sends_events


def _setting_option(
    key: str,
    name: str,
    option_type: PlayerOptionType,
    path: tuple[str, ...],
    write: OptionWriter | None = None,
    value_range: OptionRange | None = None,
) -> SonosOption:
    """Return an option read from the player's (websocket) settings at path."""

    async def _read(device: SonosDevice) -> PlayerOptionValueType | None:
        value: PlayerOptionValueType | None = device.setting(*path)
        return value

    return SonosOption(
        key=key,
        name=name,
        option_type=option_type,
        read=_read,
        write=write,
        value_range=value_range,
        read_over_upnp=False,
    )


def _home_theater_option(name: str) -> OptionWriter:
    async def _write(device: SonosDevice, value: bool) -> None:
        await device.client.player.set_home_theater_options({name: value})

    return _write


def _eq_option(
    key: str,
    name: str,
    eq_type: EqType,
    option_type: PlayerOptionType,
    available: Callable[[SonosDevice], bool],
    value_range: OptionRange | None = None,
) -> SonosOption:
    """Return an option stored as RenderingControl EQ type (booleans as 0/1)."""

    async def _read(device: SonosDevice) -> PlayerOptionValueType:
        result = await device.upnp.rendering_control("GetEQ", EQType=eq_type)
        value = int(result["CurrentValue"])
        return bool(value) if option_type == PlayerOptionType.BOOLEAN else value

    async def _write(device: SonosDevice, value: PlayerOptionValueType) -> None:
        await device.upnp.rendering_control("SetEQ", EQType=eq_type, DesiredValue=int(value))

    return SonosOption(
        key=key,
        name=name,
        option_type=option_type,
        read=_read,
        write=_write,
        available=available,
        value_range=value_range,
    )


def _on_off_option(
    key: str,
    name: str,
    service_type: str,
    state: str,
    available: Callable[[SonosDevice], bool] = lambda _: True,
    inverted: bool = False,
) -> SonosOption:
    """
    Return an option stored as On/Off state with Get<state>/Set<state> actions; it sends no events.

    :param state: The state name, e.g. LEDState for GetLEDState/SetLEDState.
    :param inverted: The option is on while the state is Off (e.g. touch controls/button lock).
    """

    async def _read(device: SonosDevice) -> bool:
        result = await device.upnp.call(service_type, f"Get{state}")
        # DeviceProperties names the output Current<state>, HTControl just <state>
        value: str | None = result.get(f"Current{state}", result.get(state))
        return (value == "On") != inverted

    async def _write(device: SonosDevice, value: PlayerOptionValueType) -> None:
        # DeviceProperties names the input Desired<state>, HTControl just <state>
        argument = state if service_type == HT_CONTROL_SERVICE else f"Desired{state}"
        on = bool(value) != inverted
        await device.upnp.call(service_type, f"Set{state}", **{argument: "On" if on else "Off"})

    return SonosOption(
        key=key,
        name=name,
        option_type=PlayerOptionType.BOOLEAN,
        read=_read,
        write=_write,
        available=available,
        sends_events=False,
    )


async def _read_balance(device: SonosDevice) -> int:
    # Sonos keeps a volume per side; 0 is centered, -100 only left, +100 only right
    left = (await device.upnp.rendering_control("GetVolume", Channel="LF"))["CurrentVolume"]
    right = (await device.upnp.rendering_control("GetVolume", Channel="RF"))["CurrentVolume"]
    return int((right - left) * 100 // max(left, right, 1))


async def _write_balance(device: SonosDevice, value: PlayerOptionValueType) -> None:
    balance = int(value)
    for channel, volume in (("LF", 100 - balance), ("RF", 100 + balance)):
        await device.upnp.rendering_control(
            "SetVolume", Channel=channel, DesiredVolume=min(100, volume)
        )


async def _read_trueplay(device: SonosDevice) -> bool | None:
    result = await device.upnp.rendering_control("GetRoomCalibrationStatus")
    if not bool(int(result["RoomCalibrationAvailable"])):
        return None
    return bool(int(result["RoomCalibrationEnabled"]))


async def _read_ir_repeater(device: SonosDevice) -> bool | None:
    result = await device.upnp.call(HT_CONTROL_SERVICE, "GetIRRepeaterState")
    state = result["CurrentIRRepeaterState"]
    return None if state == IrRepeaterState.DISABLED else state == IrRepeaterState.ON


async def _write_ir_repeater(device: SonosDevice, value: bool) -> None:
    state = IrRepeaterState.ON if value else IrRepeaterState.OFF
    await device.upnp.call(HT_CONTROL_SERVICE, "SetIRRepeaterState", DesiredIRRepeaterState=state)


SONOS_OPTIONS: tuple[SonosOption, ...] = (
    _setting_option(
        "bass",
        "Bass",
        PlayerOptionType.INTEGER,
        ("eq", "bass"),
        write=lambda device, value: device.upnp.rendering_control(
            "SetBass", DesiredBass=int(value)
        ),
        value_range=EQ_RANGE,
    ),
    _setting_option(
        "treble",
        "Treble",
        PlayerOptionType.INTEGER,
        ("eq", "treble"),
        write=lambda device, value: device.upnp.rendering_control(
            "SetTreble", DesiredTreble=int(value)
        ),
        value_range=EQ_RANGE,
    ),
    _setting_option(
        "loudness",
        "Loudness",
        PlayerOptionType.BOOLEAN,
        ("eq", "loudness"),
        write=lambda device, value: device.upnp.rendering_control(
            "SetLoudness", Channel="Master", DesiredLoudness=bool(value)
        ),
    ),
    SonosOption(
        key="balance",
        name="Balance",
        option_type=PlayerOptionType.INTEGER,
        read=_read_balance,
        write=_write_balance,
        value_range=BALANCE_RANGE,
    ),
    _on_off_option("status_light", "Status light", DEVICE_PROPERTIES_SERVICE, "LEDState"),
    _on_off_option(
        "touch_controls",
        "Touch controls",
        DEVICE_PROPERTIES_SERVICE,
        "ButtonLockState",
        inverted=True,
    ),
    SonosOption(
        key="trueplay",
        name="Trueplay",
        option_type=PlayerOptionType.BOOLEAN,
        read=_read_trueplay,
        write=lambda device, value: device.upnp.rendering_control(
            "SetRoomCalibrationStatus", RoomCalibrationEnabled=bool(value)
        ),
        sends_events=False,
    ),
    _setting_option("mono_mode", "Mono", PlayerOptionType.BOOLEAN, ("monoMode",)),
    _setting_option(
        "spatial_audio", "Spatial audio", PlayerOptionType.BOOLEAN, ("enableSpatialAudio",)
    ),
    _setting_option(
        "night_mode",
        "Night mode",
        PlayerOptionType.BOOLEAN,
        ("homeTheater", "nightMode"),
        write=_home_theater_option("nightMode"),
    ),
    _setting_option(
        "speech_enhancement",
        "Speech enhancement",
        PlayerOptionType.BOOLEAN,
        ("homeTheater", "enhanceDialog"),
        write=_home_theater_option("enhanceDialog"),
    ),
    _eq_option(
        "dialogue_level",
        "Dialogue level",
        EqType.DIALOG_LEVEL,
        PlayerOptionType.INTEGER,
        available=lambda device: device.model in ULTRA_SOUNDBAR_MODELS,
        value_range=DIALOG_LEVEL_RANGE,
    ),
    _eq_option(
        "audio_delay",
        "Audio delay",
        EqType.AUDIO_DELAY,
        PlayerOptionType.INTEGER,
        available=lambda device: device.is_home_theater,
        value_range=AUDIO_DELAY_RANGE,
    ),
    SonosOption(
        key="ir_repeater",
        name="IR repeater",
        option_type=PlayerOptionType.BOOLEAN,
        read=_read_ir_repeater,
        write=_write_ir_repeater,
        available=lambda device: device.is_home_theater,
        sends_events=False,
    ),
    _on_off_option(
        "remote_led_feedback",
        "Status light on remote control",
        HT_CONTROL_SERVICE,
        "LEDFeedbackState",
        available=lambda device: device.is_home_theater,
    ),
    _eq_option(
        "sub_enabled",
        "Subwoofer",
        EqType.SUB_ENABLED,
        PlayerOptionType.BOOLEAN,
        available=lambda device: device.has_subwoofer,
    ),
    _eq_option(
        "subwoofer_volume",
        "Subwoofer volume",
        EqType.SUB_GAIN,
        PlayerOptionType.INTEGER,
        available=lambda device: device.has_subwoofer,
        value_range=SUB_GAIN_RANGE,
    ),
    _eq_option(
        "sub_crossover",
        "Subwoofer crossover",
        EqType.SUB_CROSSOVER,
        PlayerOptionType.INTEGER,
        available=lambda device: device.model == AMP_MODEL,
        value_range=SUB_CROSSOVER_RANGE,
    ),
    _eq_option(
        "surround_enabled",
        "Surround speakers",
        EqType.SURROUND_ENABLED,
        PlayerOptionType.BOOLEAN,
        available=lambda device: device.client.player.has_surrounds,
    ),
    _eq_option(
        "surround_mode",
        "Full-volume surround for music",
        EqType.SURROUND_MODE,
        PlayerOptionType.BOOLEAN,
        available=lambda device: device.client.player.has_surrounds,
    ),
    _eq_option(
        "surround_level",
        "Surround level (TV)",
        EqType.SURROUND_LEVEL,
        PlayerOptionType.INTEGER,
        available=lambda device: device.client.player.has_surrounds,
        value_range=SURROUND_LEVEL_RANGE,
    ),
    _eq_option(
        "music_surround_level",
        "Surround level (music)",
        EqType.MUSIC_SURROUND_LEVEL,
        PlayerOptionType.INTEGER,
        available=lambda device: device.client.player.has_surrounds,
        value_range=SURROUND_LEVEL_RANGE,
    ),
)


async def read_player_options(
    device: SonosDevice,
    logger: logging.Logger,
    previous: Mapping[str, PlayerOption],
    options_to_read: Sequence[SonosOption] = SONOS_OPTIONS,
) -> list[PlayerOption]:
    """
    Read the player options the speaker has.

    An option the speaker lacks or refuses is left out; one it failed to answer in time keeps
    its previous value. Over UPnP nothing is read while the speaker does not answer at all.

    :param device: The speaker to read the options from.
    :param logger: Logger for the options a speaker did not answer for.
    :param previous: The options read before, by key.
    :param options_to_read: The options to read.
    """
    if any(not option.read_over_upnp for option in options_to_read):
        await device.client.player.refresh_settings()
    options: list[PlayerOption] = []
    for option in options_to_read:
        if not option.available(device):
            continue
        if option.read_over_upnp and not device.upnp.answers:
            continue
        try:
            value = await option.read(device)
        except UpnpActionError as err:
            logger.debug("Speaker refused player option %s: %r", option.key, err)
            continue
        except (TimeoutError, aiohttp.ClientError, OSError) as err:
            logger.debug("Could not read player option %s: %r", option.key, err)
            if option.key in previous:
                options.append(previous[option.key])
            continue
        except UpnpError as err:
            logger.debug("Speaker lacks player option %s: %r", option.key, err)
            continue
        if value is None:
            continue
        options.append(
            PlayerOption(
                key=option.key,
                name=option.name,
                type=option.option_type,
                value=value,
                read_only=option.write is None,
                min_value=option.value_range.minimum if option.value_range else None,
                max_value=option.value_range.maximum if option.value_range else None,
                step=1 if option.value_range else None,
            )
        )
    return options
