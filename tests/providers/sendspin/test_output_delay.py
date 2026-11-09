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
from music_assistant_models.enums import ConfigEntryType, PlayerType

from music_assistant.providers.sendspin.constants import CONF_SENDSPIN_STATIC_DELAY
from music_assistant.providers.sendspin.player import SendspinBasePlayer, SendspinPlayer

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


def _player(configured_delay: int) -> MagicMock:
    player = MagicMock()
    player.player_id = "p1"
    player.static_delay_default_ms = 0
    player.config.get_value.return_value = configured_delay
    return player


def test_device_reported_delay_is_stored() -> None:
    """A delay the device reports becomes the player's stored delay setting."""
    player = _player(configured_delay=0)

    SendspinPlayer.event_cb(player, MagicMock(), OutputDelayChangedEvent(output_delay_ms=120))

    player.mass.config.set_raw_player_config_value.assert_called_once_with(
        "p1", CONF_SENDSPIN_STATIC_DELAY, 120
    )


async def test_delay_reported_before_registration_is_stored_and_applied(
    mass_minimal: MusicAssistant,
) -> None:
    """A delay reported before the player config exists is stored and pushed on registration."""
    player = object.__new__(SendspinPlayer)
    player.mass = mass_minimal
    player.logger = logging.getLogger("test.sendspin")
    player._player_id = "p1"
    player._config = mass_minimal.config.get_base_player_config("p1", "sendspin")
    mass_minimal.players = MagicMock()
    mass_minimal.players.get_player.return_value = player
    player_role = MagicMock()

    SendspinPlayer.event_cb(player, MagicMock(), OutputDelayChangedEvent(output_delay_ms=120))
    assert mass_minimal.config.get("players/p1") is None

    # registration: create the config, load it with its entries, then the config hook
    mass_minimal.config.create_default_player_config("p1", "sendspin", PlayerType.PROTOCOL)
    player._config = cast(
        "PlayerConfig",
        PlayerConfig.parse(
            [
                ConfigEntry(
                    key=CONF_SENDSPIN_STATIC_DELAY,
                    type=ConfigEntryType.INTEGER,
                    default_value=player.static_delay_default_ms,
                )
            ],
            mass_minimal.config.get("players/p1"),
        ),
    )
    with (
        patch.object(SendspinPlayer, "_apply_preferred_format", AsyncMock()),
        patch.object(SendspinPlayer, "_player_role", new_callable=PropertyMock) as role,
    ):
        role.return_value = player_role
        await player.on_config_updated()

    assert mass_minimal.config.get_raw_player_config_value("p1", CONF_SENDSPIN_STATIC_DELAY) == 120
    player_role.set_output_delay.assert_called_once_with(120)


def test_report_with_a_config_drops_an_older_kept_report() -> None:
    """A report once the config exists supersedes one kept from before it existed."""
    player = _player(configured_delay=0)
    player._reported_static_delay = 80

    SendspinPlayer.event_cb(player, MagicMock(), OutputDelayChangedEvent(output_delay_ms=120))

    assert player._reported_static_delay is None


def test_unchanged_delay_is_not_stored_again() -> None:
    """A report matching the stored setting writes nothing."""
    player = _player(configured_delay=120)

    SendspinPlayer.event_cb(player, MagicMock(), OutputDelayChangedEvent(output_delay_ms=120))

    player.mass.config.set_raw_player_config_value.assert_not_called()


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
    player = object.__new__(SendspinPlayer)
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
    player = object.__new__(SendspinPlayer)
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
