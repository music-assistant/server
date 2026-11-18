"""Tests for the published static delay accessors on the Sendspin provider."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock, Mock

import pytest
from aiosendspin.models.types import PlayerCommand
from music_assistant_models.config_entries import ConfigEntry, PlayerConfig
from music_assistant_models.enums import ConfigEntryType, ProtocolRole
from music_assistant_models.errors import (
    InvalidDataError,
    PlayerUnavailableError,
    UnsupportedFeaturedException,
)

from music_assistant.constants import CONF_UNDERLYING_PLAYER_ID
from music_assistant.models.player_protocol import PlayerProtocol
from music_assistant.providers.sendspin.bridge_manager import hand_static_delay_over
from music_assistant.providers.sendspin.constants import (
    CONF_SENDSPIN_BRIDGE_STATIC_DELAYS,
    CONF_SENDSPIN_STATIC_DELAY,
    MAX_SENDSPIN_STATIC_DELAY,
    MIN_SENDSPIN_STATIC_DELAY,
)
from music_assistant.providers.sendspin.player import SendspinPlayer
from music_assistant.providers.sendspin.provider import SendspinProvider

if TYPE_CHECKING:
    from aiosendspin.server.roles.player.types import PlayerRoleProtocol

    from music_assistant.mass import MusicAssistant

PROTOCOL_ID = "calibration_target"
AIRPLAY_ID = "airplay_carrier"
SQUEEZELITE_ID = "squeezelite_carrier"


class _StaticDelayPlayer(SendspinPlayer):
    """
    A SendspinPlayer with a stubbed player role instead of a live Sendspin client.

    Only the role is stubbed: the config it holds and ``_apply_static_delay`` are the
    real provider code, so a test can follow a write all the way to the role command.
    """

    _role: Mock

    @property
    def _player_role(self) -> PlayerRoleProtocol | None:
        """Return the stubbed player role."""
        return cast("PlayerRoleProtocol", self._role)


def _get_sendspin_provider(mass: MusicAssistant) -> SendspinProvider:
    """Return the loaded Sendspin provider instance."""
    provider = mass.get_provider("sendspin")
    assert provider is not None
    return cast("SendspinProvider", provider)


def _register_carrier(mass: MusicAssistant, carrier_id: str) -> None:
    """
    Attach a stand-in for a protocol a Sendspin bridge rides on.

    :param mass: The Music Assistant instance to attach it to.
    :param carrier_id: Id of the carrying protocol.
    """
    carrier = Mock(spec=PlayerProtocol)
    carrier.player = Mock()
    carrier.available_for_playback = True
    mass.players._protocols[carrier_id] = carrier


def _register_player(
    mass: MusicAssistant,
    *,
    supported: bool = True,
    stored: bool = True,
    carrier_id: str | None = None,
    default_ms: int = 0,
) -> tuple[SendspinProvider, Mock]:
    """
    Attach a Sendspin player stand-in and return its provider and player role.

    :param mass: The Music Assistant instance to attach it to.
    :param supported: Whether its player role accepts a static delay.
    :param stored: Whether a config is stored of it, which a browser tab or app has not.
    :param carrier_id: Id of the protocol its output rides on, None for a client of its own.
    :param default_ms: The static delay it carries while none is set.
    """
    sendspin = _get_sendspin_provider(mass)
    role = Mock()
    role.state_supported_commands = [PlayerCommand.SET_STATIC_DELAY] if supported else []
    player = object.__new__(_StaticDelayPlayer)
    player.mass = mass
    player.logger = logging.getLogger("test.sendspin")
    player._protocol_id = PROTOCOL_ID
    player._player = None
    player._role = role
    player._attr_name = "Calibration Target"
    player.static_delay_default_ms = default_ms
    if carrier_id is not None:
        _register_carrier(mass, carrier_id)
        player._attr_underlying_protocol_id = carrier_id
    if stored:
        mass.config.create_protocol_record(
            PROTOCOL_ID,
            provider=sendspin.instance_id,
            player_id=None,
            role=ProtocolRole.AUDIO,
            default_name=None,
        )
        if carrier_id is not None:
            mass.config.set_protocol_value(PROTOCOL_ID, CONF_UNDERLYING_PLAYER_ID, carrier_id)
    entries = (
        [
            ConfigEntry(
                key=CONF_SENDSPIN_STATIC_DELAY,
                type=ConfigEntryType.INTEGER,
                required=False,
                default_value=default_ms,
                range=(MIN_SENDSPIN_STATIC_DELAY, MAX_SENDSPIN_STATIC_DELAY),
                immediate_apply=True,
            )
        ]
        if supported
        else []
    )
    player._config = cast(
        "PlayerConfig",
        PlayerConfig.parse(
            entries, mass.config._get_raw_protocol_config(PROTOCOL_ID, sendspin.instance_id)
        ),
    )
    mass.players._protocols[PROTOCOL_ID] = player
    return sendspin, role


def _register_foreign_protocol(mass: MusicAssistant) -> None:
    """Attach a protocol of another provider."""
    mass.players._protocols["foreign_protocol"] = MagicMock(spec=PlayerProtocol, available=True)


def _pushed_delays(role: Mock) -> list[int]:
    """Return every static delay pushed to the player role, in order."""
    return [call.args[0] for call in role.set_output_delay.call_args_list]


def _stored_values(mass: MusicAssistant) -> dict[str, object]:
    """Return the values stored of the Sendspin output."""
    record = mass.config.get_protocol_record(PROTOCOL_ID)
    assert record is not None
    return dict(record.get("values") or {})


async def test_static_delay_reaches_the_player_role(mass: MusicAssistant) -> None:
    """A static delay is stored with the output and pushed to the device."""
    sendspin, role = _register_player(mass)

    await sendspin.set_player_static_delay(PROTOCOL_ID, 320)

    assert _pushed_delays(role) == [320]
    assert mass.config.get_protocol_value(PROTOCOL_ID, CONF_SENDSPIN_STATIC_DELAY) == 320


async def test_static_delay_reaches_an_output_nothing_is_stored_of(mass: MusicAssistant) -> None:
    """A browser tab or app has no stored config, and takes a delay all the same."""
    sendspin, role = _register_player(mass, stored=False)

    await sendspin.set_player_static_delay(PROTOCOL_ID, 320)

    assert _pushed_delays(role) == [320]
    assert sendspin.get_player_static_delay(PROTOCOL_ID) == 320
    assert mass.config.get_protocol_record(PROTOCOL_ID) is None


async def test_reapplying_the_same_delay_does_not_push_again(mass: MusicAssistant) -> None:
    """An unchanged value never reaches the role again."""
    sendspin, role = _register_player(mass)
    await sendspin.set_player_static_delay(PROTOCOL_ID, 320)
    role.reset_mock()

    await sendspin.set_player_static_delay(PROTOCOL_ID, 320)

    assert _pushed_delays(role) == []


@pytest.mark.parametrize("delay_ms", [MIN_SENDSPIN_STATIC_DELAY, MAX_SENDSPIN_STATIC_DELAY])
async def test_range_boundaries_are_accepted(mass: MusicAssistant, delay_ms: int) -> None:
    """Both ends of the supported range are applied rather than rejected."""
    sendspin, role = _register_player(mass)
    # 0 is what the output starts out with, so seed another value to change away from
    await sendspin.set_player_static_delay(PROTOCOL_ID, 100)
    role.reset_mock()

    await sendspin.set_player_static_delay(PROTOCOL_ID, delay_ms)

    assert _pushed_delays(role) == [delay_ms]


@pytest.mark.parametrize(
    "delay_ms",
    [MIN_SENDSPIN_STATIC_DELAY - 1, MAX_SENDSPIN_STATIC_DELAY + 1, -500, 10_000],
)
async def test_out_of_range_delay_raises(mass: MusicAssistant, delay_ms: int) -> None:
    """An out-of-range delay is rejected outright, never clamped into range."""
    sendspin, role = _register_player(mass)

    with pytest.raises(InvalidDataError):
        await sendspin.set_player_static_delay(PROTOCOL_ID, delay_ms)

    assert _pushed_delays(role) == []


async def test_unknown_player_raises(mass: MusicAssistant) -> None:
    """An unknown id raises instead of writing config for an output that isn't there."""
    sendspin = _get_sendspin_provider(mass)

    with pytest.raises(PlayerUnavailableError):
        await sendspin.set_player_static_delay("no_such_protocol", 100)


async def test_non_sendspin_player_raises(mass: MusicAssistant) -> None:
    """A protocol belonging to another provider is refused."""
    sendspin = _get_sendspin_provider(mass)
    _register_foreign_protocol(mass)

    with pytest.raises(UnsupportedFeaturedException):
        await sendspin.set_player_static_delay("foreign_protocol", 100)


async def test_player_without_static_delay_support_raises(mass: MusicAssistant) -> None:
    """
    A player whose role won't take a static delay is refused, not silently accepted.

    Without the support check the caller would get a clean return while the device
    never hears about the new delay.
    """
    sendspin, role = _register_player(mass, supported=False)

    with pytest.raises(UnsupportedFeaturedException):
        await sendspin.set_player_static_delay(PROTOCOL_ID, 100)

    assert _pushed_delays(role) == []


async def test_static_delay_support_is_reported_without_raising(mass: MusicAssistant) -> None:
    """
    The support predicate answers rather than raising, so a caller can filter on it.

    It is what keeps a speaker that could never accept a correction out of a calibration
    session, instead of the session failing at the point it tries to apply one.
    """
    sendspin, _role = _register_player(mass)

    assert sendspin.supports_player_static_delay(PROTOCOL_ID) is True


@pytest.mark.parametrize("protocol_id", [PROTOCOL_ID, "no_such_protocol", "foreign_protocol"])
async def test_unsupported_players_report_no_static_delay_support(
    mass: MusicAssistant, protocol_id: str
) -> None:
    """An unknown id, another provider's protocol, and an unsupporting role all say no."""
    sendspin, _role = _register_player(mass, supported=False)
    _register_foreign_protocol(mass)

    assert sendspin.supports_player_static_delay(protocol_id) is False


async def test_the_delay_reads_back_as_it_was_applied(mass: MusicAssistant) -> None:
    """The getter reports what the setter stored, so a correction can build on it."""
    sendspin, _role = _register_player(mass)
    await sendspin.set_player_static_delay(PROTOCOL_ID, 275)

    assert sendspin.get_player_static_delay(PROTOCOL_ID) == 275


async def test_an_unset_delay_reads_back_as_the_player_default(mass: MusicAssistant) -> None:
    """
    With no value stored for the output, the getter reports its model-specific default.

    This is the same expression the push to the device reads, so a caller folding the
    current delay into a correction works from what the client was last told rather than
    from a bare 0.
    """
    sendspin, _role = _register_player(mass, default_ms=180)

    assert sendspin.get_player_static_delay(PROTOCOL_ID) == 180
    assert CONF_SENDSPIN_STATIC_DELAY not in _stored_values(mass)


async def test_reading_an_unknown_player_raises(mass: MusicAssistant) -> None:
    """The getter refuses an output that is not there rather than reporting a delay."""
    sendspin = _get_sendspin_provider(mass)

    with pytest.raises(PlayerUnavailableError):
        sendspin.get_player_static_delay("no_such_protocol")


async def test_reading_a_non_sendspin_player_raises(mass: MusicAssistant) -> None:
    """A protocol belonging to another provider carries no Sendspin static delay."""
    sendspin = _get_sendspin_provider(mass)
    _register_foreign_protocol(mass)

    with pytest.raises(UnsupportedFeaturedException):
        sendspin.get_player_static_delay("foreign_protocol")


async def test_reading_a_player_without_static_delay_support_raises(mass: MusicAssistant) -> None:
    """
    A player that cannot take a static delay raises instead of reporting 0.

    A 0 there is indistinguishable from a real one, so a correction computed against it
    would look applicable while the device could never accept it.
    """
    sendspin, _role = _register_player(mass, supported=False)

    with pytest.raises(UnsupportedFeaturedException):
        sendspin.get_player_static_delay(PROTOCOL_ID)


async def test_a_client_of_its_own_has_no_carrier(mass: MusicAssistant) -> None:
    """A Sendspin speaker, browser tab or app rides on no other protocol."""
    sendspin, _role = _register_player(mass)

    assert sendspin.get_player_static_delay_carrier(PROTOCOL_ID) is None


async def test_a_bridged_output_names_the_protocol_that_carries_it(mass: MusicAssistant) -> None:
    """A bridged speaker reports its carrier, which is what its static delay belongs to."""
    sendspin, _role = _register_player(mass, carrier_id=AIRPLAY_ID)

    assert sendspin.get_player_static_delay_carrier(PROTOCOL_ID) == AIRPLAY_ID


async def test_a_delay_named_by_the_current_carrier_reaches_the_device(
    mass: MusicAssistant,
) -> None:
    """Naming the protocol that carries the output now changes nothing about the write."""
    sendspin, role = _register_player(mass, carrier_id=AIRPLAY_ID)

    await sendspin.set_player_static_delay(PROTOCOL_ID, 320, carrier_id=AIRPLAY_ID)

    assert _pushed_delays(role) == [320]
    assert _stored_values(mass)[CONF_SENDSPIN_STATIC_DELAY] == 320


async def test_a_delay_of_another_carrier_is_put_aside(mass: MusicAssistant) -> None:
    """
    A delay of a protocol that does not carry the output now waits for when it does.

    The device plays through the current carrier, so nothing is pushed and the delay of
    that carrier stays as it is.
    """
    sendspin, role = _register_player(mass, carrier_id=AIRPLAY_ID)
    await sendspin.set_player_static_delay(PROTOCOL_ID, 40)
    role.reset_mock()

    await sendspin.set_player_static_delay(PROTOCOL_ID, 320, carrier_id=SQUEEZELITE_ID)

    assert _pushed_delays(role) == []
    assert sendspin.get_player_static_delay(PROTOCOL_ID) == 40
    assert _stored_values(mass)[CONF_SENDSPIN_BRIDGE_STATIC_DELAYS] == {SQUEEZELITE_ID: 320}


async def test_a_delay_put_aside_is_back_once_its_carrier_has_the_output(
    mass: MusicAssistant,
) -> None:
    """The hand-over to a carrier brings the delay that was put aside for it."""
    sendspin, _role = _register_player(mass, carrier_id=AIRPLAY_ID)
    await sendspin.set_player_static_delay(PROTOCOL_ID, 40)
    await sendspin.set_player_static_delay(PROTOCOL_ID, 320, carrier_id=SQUEEZELITE_ID)

    hand_static_delay_over(mass, PROTOCOL_ID, SQUEEZELITE_ID)

    values = _stored_values(mass)
    assert values[CONF_SENDSPIN_STATIC_DELAY] == 320
    assert values[CONF_SENDSPIN_BRIDGE_STATIC_DELAYS] == {AIRPLAY_ID: 40}
