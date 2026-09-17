"""Tests for the Sendspin player's output delay handling."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from aiosendspin.models.types import PlayerCommand
from aiosendspin.server.roles.player.events import OutputDelayChangedEvent

from music_assistant.providers.sendspin.constants import CONF_SENDSPIN_STATIC_DELAY
from music_assistant.providers.sendspin.player import SendspinBasePlayer, SendspinPlayer


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


def test_unchanged_delay_is_not_stored_again() -> None:
    """A report matching the stored setting writes nothing."""
    player = _player(configured_delay=120)

    SendspinPlayer.event_cb(player, MagicMock(), OutputDelayChangedEvent(output_delay_ms=120))

    player.mass.config.set_raw_player_config_value.assert_not_called()


async def test_configured_delay_is_sent_as_output_delay() -> None:
    """The stored delay setting reaches the device through set_output_delay."""
    player = _player(configured_delay=80)

    await SendspinPlayer._apply_static_delay(player)

    player._player_role.set_output_delay.assert_called_once_with(80)


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
