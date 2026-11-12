"""Tests for the Sendspin player's output delay handling."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from aiosendspin.models.types import PlayerCommand
from aiosendspin.server.roles.player.events import OutputDelayChangedEvent
from aiosendspin.server.roles.player.v1 import PlayerPersistentState
from music_assistant_models.config_entries import ConfigEntry, PlayerConfig
from music_assistant_models.enums import ConfigEntryType, ProtocolRole

from music_assistant.providers.sendspin.constants import CONF_SENDSPIN_STATIC_DELAY
from music_assistant.providers.sendspin.player import SendspinBasePlayer, SendspinPlayer

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


def _player(configured_delay: int) -> MagicMock:
    player = MagicMock()
    player.protocol_id = "p1"
    player.static_delay_default_ms = 0
    player.config.values = {CONF_SENDSPIN_STATIC_DELAY: MagicMock()}
    player.config.get_value.return_value = configured_delay
    return player


def _unregistered_player(mass: MusicAssistant) -> SendspinPlayer:
    """
    Return a player that holds the base config it is constructed with.

    :param mass: The Music Assistant instance the player belongs to.
    """
    player = object.__new__(SendspinPlayer)
    player.mass = mass
    player.logger = logging.getLogger("test.sendspin")
    player._protocol_id = "p1"
    player._player = None
    player._config = mass.config.get_base_protocol_config("p1", "sendspin")
    mass.players = MagicMock()
    mass.players.get_protocol.return_value = player
    return player


def _load_config(mass: MusicAssistant, player: SendspinPlayer) -> PlayerConfig:
    """
    Return the player's config with its static delay entry, as registration loads it.

    :param mass: The Music Assistant instance holding the stored config.
    :param player: The player to load the config for.
    """
    entry = ConfigEntry(
        key=CONF_SENDSPIN_STATIC_DELAY,
        type=ConfigEntryType.INTEGER,
        default_value=player.static_delay_default_ms,
    )
    return cast(
        "PlayerConfig",
        PlayerConfig.parse([entry], mass.config._get_raw_protocol_config("p1", "sendspin")),
    )


def _create_record(mass: MusicAssistant) -> None:
    """
    Store the config of the protocol, as its first attach does.

    :param mass: The Music Assistant instance to store it in.
    """
    mass.config.create_protocol_record(
        "p1", provider="sendspin", player_id=None, role=ProtocolRole.AUDIO, default_name=None
    )


async def _run_config_hook(player: SendspinPlayer) -> MagicMock:
    """
    Run the hook registration calls once the config is loaded, returning the player role.

    :param player: The player to run the hook on.
    """
    player_role = MagicMock()
    with (
        patch.object(SendspinPlayer, "_apply_preferred_format", AsyncMock()),
        patch.object(SendspinPlayer, "_player_role", new_callable=PropertyMock) as role,
    ):
        role.return_value = player_role
        await player.on_config_updated()
    return player_role


def _entries_player() -> SendspinPlayer:
    """Return a player of a regular client with only what its config entries read."""
    player = object.__new__(SendspinPlayer)
    player._protocol_id = "p1"
    player._provider = MagicMock(is_virtual_player=lambda _client_id: False)
    return player


def test_device_reported_delay_is_stored() -> None:
    """A delay the device reports becomes the player's stored delay setting."""
    player = _player(configured_delay=0)

    SendspinPlayer.event_cb(player, MagicMock(), OutputDelayChangedEvent(output_delay_ms=120))

    player.mass.config.set_protocol_value.assert_called_once_with(
        "p1", CONF_SENDSPIN_STATIC_DELAY, 120
    )


async def test_delay_reported_before_registration_is_stored_and_applied(
    mass_minimal: MusicAssistant,
) -> None:
    """A delay reported before the protocol config exists is stored and pushed on attach."""
    player = _unregistered_player(mass_minimal)

    SendspinPlayer.event_cb(player, MagicMock(), OutputDelayChangedEvent(output_delay_ms=120))
    assert mass_minimal.config.get_protocol_record("p1") is None

    _create_record(mass_minimal)
    player._config = _load_config(mass_minimal, player)
    player_role = await _run_config_hook(player)

    assert mass_minimal.config.get_protocol_value("p1", CONF_SENDSPIN_STATIC_DELAY) == 120
    player_role.set_output_delay.assert_called_once_with(120)


async def test_delay_reported_while_registration_loads_the_config_is_applied(
    mass_minimal: MusicAssistant,
) -> None:
    """A delay reported after the config was created but before it is loaded is not lost."""
    player = _unregistered_player(mass_minimal)
    _create_record(mass_minimal)
    # registration read the stored config before the report came in
    loaded_config = _load_config(mass_minimal, player)

    SendspinPlayer.event_cb(player, MagicMock(), OutputDelayChangedEvent(output_delay_ms=120))
    assert mass_minimal.config.get_protocol_value("p1", CONF_SENDSPIN_STATIC_DELAY) is None

    player._config = loaded_config
    player_role = await _run_config_hook(player)

    assert mass_minimal.config.get_protocol_value("p1", CONF_SENDSPIN_STATIC_DELAY) == 120
    player_role.set_output_delay.assert_called_once_with(120)


def test_report_with_a_loaded_config_drops_an_older_kept_report() -> None:
    """A report once the config is loaded supersedes one kept from before."""
    player = _player(configured_delay=0)
    player._reported_static_delay = 80

    SendspinPlayer.event_cb(player, MagicMock(), OutputDelayChangedEvent(output_delay_ms=120))

    assert player._reported_static_delay is None


def test_unchanged_delay_is_not_stored_again() -> None:
    """A report matching the stored setting writes nothing."""
    player = _player(configured_delay=120)

    SendspinPlayer.event_cb(player, MagicMock(), OutputDelayChangedEvent(output_delay_ms=120))

    player.mass.config.set_protocol_value.assert_not_called()


@pytest.mark.parametrize(
    ("commands", "offered"),
    [
        ([PlayerCommand.SET_OUTPUT_DELAY], True),
        ([PlayerCommand.SET_STATIC_DELAY], True),
        ([PlayerCommand.VOLUME], False),
    ],
)
async def test_delay_setting_follows_the_declared_commands(
    commands: list[PlayerCommand], offered: bool
) -> None:
    """The delay setting shows for either delay command a player declares."""
    player = _entries_player()
    player_role = MagicMock()
    player_role.get_supported_formats.return_value = []
    player_role.state_supported_commands = commands

    with (
        patch.object(SendspinBasePlayer, "get_config_entries", AsyncMock(return_value=[])),
        patch.object(SendspinPlayer, "_player_role", new_callable=PropertyMock) as role,
    ):
        role.return_value = player_role
        entries = await player.get_config_entries()

    assert (CONF_SENDSPIN_STATIC_DELAY in {entry.key for entry in entries}) is offered


@pytest.mark.parametrize(
    ("connected", "commands", "noted"),
    [
        (False, [PlayerCommand.SET_STATIC_DELAY], True),
        (False, [PlayerCommand.VOLUME], False),
        (True, [PlayerCommand.SET_STATIC_DELAY], False),
    ],
)
async def test_a_detached_player_says_when_its_delay_setting_returns(
    connected: bool, commands: list[PlayerCommand], noted: bool
) -> None:
    """The note follows the commands the disconnected device last declared."""
    player = _entries_player()
    player.api = MagicMock()
    player.api.is_connected = connected
    player.api.get_role_state.return_value = PlayerPersistentState(
        state_supported_commands=commands
    )

    with (
        patch.object(SendspinBasePlayer, "get_config_entries", AsyncMock(return_value=[])),
        patch.object(SendspinPlayer, "_player_role", new_callable=PropertyMock) as role,
    ):
        role.return_value = None
        entries = await player.get_config_entries()

    assert ("sendspin_static_delay_disconnected" in {entry.key for entry in entries}) is noted
