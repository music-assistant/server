"""Tests for what the Home Assistant MediaPlayers provider cleans up for a removed player."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant.providers.hass_players.constants import CONF_PLAYERS

from .conftest import PLAYER_ID

if TYPE_CHECKING:
    from unittest.mock import MagicMock

    from music_assistant.providers.hass_players.player import HomeAssistantPlayer


async def test_removed_player_leaves_the_players_of_the_provider(
    player: HomeAssistantPlayer,
) -> None:
    """A removed player is taken out of the entities the provider is set up with."""
    provider = cast("MagicMock", player.provider)
    provider.instance_id = "hass_players"
    provider.config.get_value.return_value = [PLAYER_ID, "media_player.kitchen"]

    await player.on_remove()

    provider.config.get_value.assert_called_once_with(CONF_PLAYERS)
    cast("MagicMock", player.mass).config.set_raw_provider_config_value.assert_called_once_with(
        "hass_players", CONF_PLAYERS, ["media_player.kitchen"]
    )


async def test_removed_player_that_was_not_selected_changes_nothing(
    player: HomeAssistantPlayer,
) -> None:
    """A removed player the provider was not set up with leaves its config alone."""
    cast("MagicMock", player.provider).config.get_value.return_value = ["media_player.kitchen"]

    await player.on_remove()

    cast("MagicMock", player.mass).config.set_raw_provider_config_value.assert_not_called()
