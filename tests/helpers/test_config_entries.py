"""Tests for the shared player pickers of plugin config entries."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

from music_assistant_models.enums import PlayerType

from music_assistant.helpers.config_entries import (
    create_connected_players_entry,
    create_player_selector,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


def _mass() -> MusicAssistant:
    """Return a server with a player of a device and two browser players."""
    players = [
        SimpleNamespace(
            player_id=player_id, name=name, type=PlayerType.PLAYER, session_only=session
        )
        for player_id, name, session in (
            ("pl_a", "Player A", False),
            ("web_b", "Browser B", True),
            ("web_c", "Browser C", True),
        )
    ]
    mass = MagicMock()
    mass.players.all_players.return_value = players
    return cast("MusicAssistant", mass)


def test_a_player_selector_offers_no_session_only_player() -> None:
    """A browser or app player is no choice for a plugin."""
    entry = create_player_selector(_mass(), "player")

    assert [option.value for option in entry.options] == ["pl_a"]


def test_a_player_selector_keeps_a_session_only_player_that_is_selected() -> None:
    """A selected browser player stays the selection of the form."""
    entry = create_player_selector(_mass(), "player", "web_b")

    assert [option.value for option in entry.options] == ["web_b", "pl_a"]
    assert entry.value == "web_b"


def test_connected_players_offer_no_session_only_player_but_keep_the_selected() -> None:
    """Of the browser players only the connected one is listed, by its name."""
    entry = create_connected_players_entry(_mass(), ["web_c"])

    assert [(option.value, option.title) for option in entry.options] == [
        ("web_c", "Browser C"),
        ("pl_a", "Player A"),
    ]
    assert entry.value == ["web_c"]
