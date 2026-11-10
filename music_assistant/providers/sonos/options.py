"""
Player options (EQ, home theater and speaker settings) of a Sonos player.

The websocket api reports most settings in one call but sets only the home theater
options; everything else is set (and partly read) over UPnP. The speaker sends no event
when a setting changes, so the options are polled.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple

from aiosonos.api.upnp import EqType, IrRepeaterState
from aiosonos.exceptions import CommandTimeout, SonosException
from music_assistant_models.player import PlayerOption, PlayerOptionType, PlayerOptionValueType

from .const import (
    AMP_MODEL,
    AUDIO_DELAY_RANGE,
    BALANCE_RANGE,
    DIALOG_LEVEL_RANGE,
    EQ_RANGE,
    SUB_CROSSOVER_RANGE,
    SUB_GAIN_RANGE,
    SURROUND_LEVEL_RANGE,
    ULTRA_SOUNDBAR_MODELS,
    UPNP_OPTION_TIMEOUT_LIMIT,
    OptionRange,
)

if TYPE_CHECKING:
    import logging

    from aiosonos.client import SonosLocalApiClient

OptionReader = Callable[["SonosDevice"], Awaitable[PlayerOptionValueType | None]]
OptionWriter = Callable[["SonosDevice", Any], Awaitable[None]]


@dataclass(frozen=True)
class SonosDevice:
    """The speaker a player option is read from and written to."""

    client: SonosLocalApiClient
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

    @property
    def has_surrounds(self) -> bool:
        """Return if surround speakers are bonded to the speaker."""
        return self.client.player.has_surrounds


@dataclass(frozen=True, kw_only=True)
class SonosOption:
    """
    A player option: how to read and write it, and whether a speaker has it.

    An option is offered when available() passes and read() returns a value; one without
    write is read-only.
    """

    key: str
    name: str
    option_type: PlayerOptionType
    read: OptionReader
    write: OptionWriter | None = None
    available: Callable[[SonosDevice], bool] = lambda _: True
    value_range: OptionRange | None = None
    read_over_upnp: bool = True


def _setting(*path: str) -> OptionReader:
    async def _read(device: SonosDevice) -> PlayerOptionValueType | None:
        value: PlayerOptionValueType | None = device.setting(*path)
        return value

    return _read


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
    """Return an option stored as UPnP EQ type (booleans as 0/1)."""

    async def _read(device: SonosDevice) -> PlayerOptionValueType:
        value = await device.client.upnp.get_eq(eq_type)
        return bool(value) if option_type == PlayerOptionType.BOOLEAN else value

    async def _write(device: SonosDevice, value: PlayerOptionValueType) -> None:
        await device.client.upnp.set_eq(eq_type, int(value))

    return SonosOption(
        key=key,
        name=name,
        option_type=option_type,
        read=_read,
        write=_write,
        available=available,
        value_range=value_range,
    )


async def _read_balance(device: SonosDevice) -> int:
    # Sonos keeps a volume per side; 0 is centered, -100 only left, +100 only right
    left = await device.client.upnp.get_channel_volume("LF")
    right = await device.client.upnp.get_channel_volume("RF")
    return (right - left) * 100 // max(left, right, 1)


async def _write_balance(device: SonosDevice, value: PlayerOptionValueType) -> None:
    balance = int(value)
    await device.client.upnp.set_channel_volume("LF", min(100, 100 - balance))
    await device.client.upnp.set_channel_volume("RF", min(100, 100 + balance))


async def _read_trueplay(device: SonosDevice) -> bool | None:
    status = await device.client.upnp.get_room_calibration_status()
    return status.enabled if status.available else None


async def _read_ir_repeater(device: SonosDevice) -> bool | None:
    state = await device.client.upnp.get_ir_repeater_state()
    return None if state == IrRepeaterState.DISABLED else state == IrRepeaterState.ON


async def _write_ir_repeater(device: SonosDevice, value: bool) -> None:
    state = IrRepeaterState.ON if value else IrRepeaterState.OFF
    await device.client.upnp.set_ir_repeater_state(state)


async def _read_touch_controls(device: SonosDevice) -> bool:
    return not await device.client.upnp.get_button_lock_state()


async def _write_touch_controls(device: SonosDevice, value: bool) -> None:
    await device.client.upnp.set_button_lock_state(not value)


SONOS_OPTIONS: tuple[SonosOption, ...] = (
    SonosOption(
        key="bass",
        name="Bass",
        option_type=PlayerOptionType.INTEGER,
        read=_setting("eq", "bass"),
        read_over_upnp=False,
        write=lambda device, value: device.client.upnp.set_bass(int(value)),
        value_range=EQ_RANGE,
    ),
    SonosOption(
        key="treble",
        name="Treble",
        option_type=PlayerOptionType.INTEGER,
        read=_setting("eq", "treble"),
        read_over_upnp=False,
        write=lambda device, value: device.client.upnp.set_treble(int(value)),
        value_range=EQ_RANGE,
    ),
    SonosOption(
        key="loudness",
        name="Loudness",
        option_type=PlayerOptionType.BOOLEAN,
        read=_setting("eq", "loudness"),
        read_over_upnp=False,
        write=lambda device, value: device.client.upnp.set_loudness(bool(value)),
    ),
    SonosOption(
        key="balance",
        name="Balance",
        option_type=PlayerOptionType.INTEGER,
        read=_read_balance,
        write=_write_balance,
        value_range=BALANCE_RANGE,
    ),
    SonosOption(
        key="status_light",
        name="Status light",
        option_type=PlayerOptionType.BOOLEAN,
        read=lambda device: device.client.upnp.get_led_state(),
        write=lambda device, value: device.client.upnp.set_led_state(bool(value)),
    ),
    SonosOption(
        key="touch_controls",
        name="Touch controls",
        option_type=PlayerOptionType.BOOLEAN,
        read=_read_touch_controls,
        write=_write_touch_controls,
    ),
    SonosOption(
        key="trueplay",
        name="Trueplay",
        option_type=PlayerOptionType.BOOLEAN,
        read=_read_trueplay,
        write=lambda device, value: device.client.upnp.set_room_calibration_enabled(bool(value)),
    ),
    SonosOption(
        key="mono_mode",
        name="Mono",
        option_type=PlayerOptionType.BOOLEAN,
        read=_setting("monoMode"),
        read_over_upnp=False,
    ),
    SonosOption(
        key="spatial_audio",
        name="Spatial audio",
        option_type=PlayerOptionType.BOOLEAN,
        read=_setting("enableSpatialAudio"),
        read_over_upnp=False,
    ),
    SonosOption(
        key="night_mode",
        name="Night mode",
        option_type=PlayerOptionType.BOOLEAN,
        read=_setting("homeTheater", "nightMode"),
        read_over_upnp=False,
        write=_home_theater_option("nightMode"),
    ),
    SonosOption(
        key="speech_enhancement",
        name="Speech enhancement",
        option_type=PlayerOptionType.BOOLEAN,
        read=_setting("homeTheater", "enhanceDialog"),
        read_over_upnp=False,
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
    ),
    SonosOption(
        key="remote_led_feedback",
        name="Status light on remote control",
        option_type=PlayerOptionType.BOOLEAN,
        read=lambda device: device.client.upnp.get_led_feedback_state(),
        write=lambda device, value: device.client.upnp.set_led_feedback_state(bool(value)),
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
        available=lambda device: device.has_surrounds,
    ),
    _eq_option(
        "surround_mode",
        "Full-volume surround for music",
        EqType.SURROUND_MODE,
        PlayerOptionType.BOOLEAN,
        available=lambda device: device.has_surrounds,
    ),
    _eq_option(
        "surround_level",
        "Surround level (TV)",
        EqType.SURROUND_LEVEL,
        PlayerOptionType.INTEGER,
        available=lambda device: device.has_surrounds,
        value_range=SURROUND_LEVEL_RANGE,
    ),
    _eq_option(
        "music_surround_level",
        "Surround level (music)",
        EqType.MUSIC_SURROUND_LEVEL,
        PlayerOptionType.INTEGER,
        available=lambda device: device.has_surrounds,
        value_range=SURROUND_LEVEL_RANGE,
    ),
)


class PlayerOptionsRead(NamedTuple):
    """The player options read from a speaker, and its UPnP timeouts in a row since."""

    options: list[PlayerOption]
    upnp_timeouts: int


async def read_player_options(
    device: SonosDevice, logger: logging.Logger, upnp_timeouts: int
) -> PlayerOptionsRead:
    """
    Read the player options the speaker has.

    UPnP reads are skipped once the speaker timed out UPNP_OPTION_TIMEOUT_LIMIT times in a row.

    :param device: The speaker to read the options from.
    :param logger: Logger for the options a speaker did not answer for.
    :param upnp_timeouts: UPnP timeouts in a row of the earlier reads.
    """
    await device.client.player.refresh_settings()
    options: list[PlayerOption] = []
    for option in SONOS_OPTIONS:
        if not option.available(device):
            continue
        if option.read_over_upnp and upnp_timeouts >= UPNP_OPTION_TIMEOUT_LIMIT:
            continue
        try:
            value = await option.read(device)
        except CommandTimeout as err:
            upnp_timeouts += 1
            logger.debug("Could not read player option %s: %s", option.key, err)
            if upnp_timeouts == UPNP_OPTION_TIMEOUT_LIMIT:
                logger.warning(
                    "UPnP does not answer, options read over UPnP are left out until reconnect"
                )
            continue
        except SonosException as err:
            logger.debug("Could not read player option %s: %s", option.key, err)
            continue
        if option.read_over_upnp:
            upnp_timeouts = 0
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
    return PlayerOptionsRead(options, upnp_timeouts)
