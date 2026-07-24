"""Tests for get_config_entries (the genuine playback options surface)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from music_assistant.providers.yandex_ynison import get_config_entries
from music_assistant.providers.yandex_ynison.constants import (
    CONF_ALLOW_PLAYER_SWITCH,
    CONF_DEVICE_ID,
    CONF_MASS_PLAYER_ID,
    CONF_OUTPUT_BIT_DEPTH,
    CONF_OUTPUT_SAMPLE_RATE,
    CONF_PUBLISH_NAME,
    PLAYER_ID_AUTO,
)

# auth / account-source keys that moved to the interactive setup flow — none of
# these may reappear on the options surface
_FLOW_ONLY_KEYS = frozenset(
    {
        "auth_qr",
        "clear_auth",
        "remember_session",
        "token",
        "x_token",
        "account_login",
        "ym_instance",
        "label_text",
        "session_id",
    }
)


def _make_mock_mass(players: list[Any] | None = None) -> MagicMock:
    """Build a MusicAssistant stub exposing a player list for the dropdown."""
    mass = MagicMock()
    mass.players.all_players = MagicMock(return_value=players or [])
    return mass


def _entries_by_key(entries: tuple[Any, ...]) -> dict[str, Any]:
    return {entry.key: entry for entry in entries}


async def test_no_auth_or_account_source_entries() -> None:
    """Authentication and the account source moved to the setup flow."""
    entries = await get_config_entries(_make_mock_mass())
    keys = {e.key for e in entries}
    assert keys.isdisjoint(_FLOW_ONLY_KEYS)
    actions = {e.action for e in entries if e.action}
    assert not actions


async def test_genuine_options_present() -> None:
    """The genuine playback options remain on the options surface."""
    entries = await get_config_entries(_make_mock_mass())
    keys = {e.key for e in entries}
    assert {
        CONF_MASS_PLAYER_ID,
        CONF_ALLOW_PLAYER_SWITCH,
        CONF_OUTPUT_SAMPLE_RATE,
        CONF_OUTPUT_BIT_DEPTH,
        CONF_PUBLISH_NAME,
        CONF_DEVICE_ID,
    } == keys


async def test_player_dropdown_lists_players_plus_auto() -> None:
    """The player dropdown lists the Auto sentinel plus every registered player."""
    player = MagicMock()
    player.player_id = "player-1"
    player.display_name = "Living Room"
    entries = await get_config_entries(_make_mock_mass([player]))
    dropdown = _entries_by_key(entries)[CONF_MASS_PLAYER_ID]
    option_values = [opt.value for opt in dropdown.options]
    assert option_values == [PLAYER_ID_AUTO, "player-1"]


async def test_legacy_keys_migrated() -> None:
    """Legacy 'player'/'display_name' keys are migrated to the current names."""
    values: dict[str, Any] = {"player": "player-9", "display_name": "Kitchen"}
    await get_config_entries(_make_mock_mass(), values=values)
    assert values[CONF_MASS_PLAYER_ID] == "player-9"
    assert values[CONF_PUBLISH_NAME] == "Kitchen"
    assert "player" not in values
    assert "display_name" not in values
