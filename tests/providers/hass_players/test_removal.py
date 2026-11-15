"""Tests for what the Home Assistant MediaPlayers provider cleans up for a removed player."""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

from music_assistant.providers.hass_players.constants import CONF_PLAYERS
from music_assistant.providers.hass_players.provider import HomeAssistantPlayerProvider

from .conftest import PLAYER_ID


def _provider(selected: list[str]) -> HomeAssistantPlayerProvider:
    """Return a provider set up with the given entities, holding a player for each."""
    provider = HomeAssistantPlayerProvider.__new__(HomeAssistantPlayerProvider)
    provider.mass = MagicMock()
    provider.config = MagicMock(instance_id="hass_players")
    provider.config.get_value.return_value = selected
    provider._entity_players = {entity_id: MagicMock() for entity_id in selected}
    return provider


def test_a_forgotten_entity_leaves_the_entities_of_the_provider() -> None:
    """A removed player is taken out of the entities the provider is set up with."""
    provider = _provider([PLAYER_ID, "media_player.kitchen"])

    provider.forget_entity(PLAYER_ID)

    cast("MagicMock", provider.config).get_value.assert_called_once_with(CONF_PLAYERS)
    provider.mass.config.set_raw_provider_config_value.assert_called_once_with(  # type: ignore[attr-defined]
        "hass_players", CONF_PLAYERS, ["media_player.kitchen"]
    )
    assert list(provider._entity_players) == ["media_player.kitchen"]


def test_an_entity_that_was_not_selected_changes_nothing() -> None:
    """A removed player the provider was not set up with leaves its config alone."""
    provider = _provider(["media_player.kitchen"])

    provider.forget_entity(PLAYER_ID)

    provider.mass.config.set_raw_provider_config_value.assert_not_called()  # type: ignore[attr-defined]
