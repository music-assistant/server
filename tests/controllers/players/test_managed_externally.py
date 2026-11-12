"""Tests for players managed outside of Music Assistant (e.g. an Apple Home grouping)."""

from collections.abc import Callable, Coroutine
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.dsp import DSPConfig
from music_assistant_models.enums import ConfigEntryType, PlayerType
from music_assistant_models.errors import ActionUnavailable

from music_assistant.controllers.config.dsp import DSPConfigMixin
from music_assistant.controllers.config.players import PlayerConfigMixin
from music_assistant.controllers.config.queues import PlayerQueueConfigMixin
from music_assistant.controllers.players.controller import PlayerController
from tests.common import bare_player_controller

PLAYER_ID = "syncgroup_aptest"


def _controller(managed_externally: bool) -> PlayerController:
    """
    Return a players controller holding one group player.

    :param managed_externally: Whether the group is managed outside of Music Assistant.
    """
    players = bare_player_controller()
    player = MagicMock()
    player.player_id = PLAYER_ID
    player.display_name = "Living Room TV"
    player.managed_externally = managed_externally
    player.state.type = PlayerType.GROUP
    player.provider.remove_group_player = AsyncMock()
    players._players[PLAYER_ID] = player
    return players


def test_a_managed_player_is_refused_a_change() -> None:
    """Only a player managed externally is refused; other or unknown players pass."""
    with pytest.raises(ActionUnavailable):
        _controller(True).raise_if_managed_externally(PLAYER_ID)
    _controller(False).raise_if_managed_externally(PLAYER_ID)
    _controller(True).raise_if_managed_externally("unknown_player")


@pytest.mark.parametrize("command", ["remove", "remove_group_player"])
async def test_a_managed_player_can_not_be_removed(command: str) -> None:
    """Removing a player managed externally is refused before its provider is asked."""
    players = _controller(True)
    with pytest.raises(ActionUnavailable):
        await getattr(players, command)(PLAYER_ID)
    players._players[PLAYER_ID].provider.remove_group_player.assert_not_awaited()


def _config(managed_externally: bool) -> MagicMock:
    """
    Return a config controller stand-in whose players controller holds one group player.

    :param managed_externally: Whether the group is managed outside of Music Assistant.
    """
    config = MagicMock()
    config.mass.players = _controller(managed_externally)
    return config


CONFIG_COMMANDS: dict[str, Callable[[MagicMock], Coroutine[Any, Any, Any]]] = {
    "rename": lambda c: PlayerConfigMixin.save_player_config(c, PLAYER_ID, {"name": "Den"}),
    "remove config": lambda c: PlayerConfigMixin.remove_player_config(c, PLAYER_ID),
    "config action": lambda c: PlayerConfigMixin.invoke_player_config_action(c, PLAYER_ID, "x"),
    "queue settings": lambda c: PlayerQueueConfigMixin.save_player_queue_config(c, PLAYER_ID, {}),
    "dsp": lambda c: DSPConfigMixin.save_dsp_config(c, PLAYER_ID, DSPConfig()),
    "dsp preset": lambda c: DSPConfigMixin.apply_dsp_preset(c, PLAYER_ID, "preset"),
}


@pytest.mark.parametrize("command", list(CONFIG_COMMANDS))
async def test_nothing_about_a_managed_player_is_saved(command: str) -> None:
    """A rename, setting, queue or DSP change of a player managed externally is refused."""
    config = _config(True)
    with pytest.raises(ActionUnavailable):
        await CONFIG_COMMANDS[command](config)
    config.set.assert_not_called()


async def test_the_settings_of_a_managed_player_are_read_only() -> None:
    """A player managed externally shows its settings, but none of them can be changed."""
    config = _config(True)
    player = config.mass.players._players[PLAYER_ID]
    player.translation_owner = None
    config.mass.players.get_player = MagicMock(return_value=player)
    config._get_default_player_config_entries = MagicMock(
        return_value=[ConfigEntry(key="hide_in_ui", type=ConfigEntryType.BOOLEAN)]
    )
    config._create_output_protocol_config_entries = AsyncMock(return_value=[])
    config._get_player_config_entries = AsyncMock(
        return_value=[ConfigEntry(key="group_members", type=ConfigEntryType.STRING)]
    )
    config._create_plugin_provider_config_entries = MagicMock(return_value=[])
    entries = await PlayerConfigMixin.get_player_config_entries(config, PLAYER_ID)
    assert [entry.key for entry in entries] == ["hide_in_ui", "group_members"]
    assert all(entry.read_only for entry in entries)
