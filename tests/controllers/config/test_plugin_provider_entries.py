"""Unit tests for the per-player plugin toggle entries a player renders."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, PlayerType

from music_assistant import constants as _constants
from music_assistant.constants import CONF_ENABLED, CONF_PLUGIN_KEY_SPLITTER, CONF_PROVIDERS
from music_assistant.helpers.config_entries import CONF_CONNECTED_PLAYERS
from music_assistant.mass import MusicAssistant
from music_assistant.models.plugin import PluginProvider

# the common strings live next to the constants module, so this path holds from anywhere
_STRINGS_PATH = Path(_constants.__file__).resolve().parent / "strings.json"

_PLAYER_ID = "test_player"
_PLUGIN_INSTANCE_ID = "spotify_connect--aabbcc"
_PLUGIN_KEY = f"{_PLUGIN_INSTANCE_ID}{CONF_PLUGIN_KEY_SPLITTER}{CONF_ENABLED}"
_CONNECTED_PLAYERS_KEY = f"{CONF_PROVIDERS}/{_PLUGIN_INSTANCE_ID}/values/{CONF_CONNECTED_PLAYERS}"


def _make_plugin_provider(*, player_bound: bool = True) -> MagicMock:
    """Return a plugin provider mock that (optionally) binds its sources to players."""
    provider = MagicMock(spec=PluginProvider)
    # instance_id and name are properties on the spec, so set them as plain attributes
    provider.instance_id = _PLUGIN_INSTANCE_ID
    provider.name = "Spotify Connect"
    # a player-bound plugin returns a list for any player id, unbound returns None
    provider.get_player_audio_sources.return_value = [] if player_bound else None
    return provider


def _make_player(player_type: PlayerType = PlayerType.PLAYER) -> MagicMock:
    """Return a player mock of the given type."""
    player = MagicMock()
    player.player_id = _PLAYER_ID
    player.state.type = player_type
    return player


def _plugin_entries(
    mass: MusicAssistant, providers: list[MagicMock], player: MagicMock | None = None
) -> list[ConfigEntry]:
    """Build the plugin toggle entries for a player with the given loaded providers."""
    # mass_minimal loads no providers, so serve the mocks through the providers property
    with patch.object(type(mass), "providers", new_callable=PropertyMock, return_value=providers):
        return mass.config._create_plugin_provider_config_entries(player or _make_player())


async def test_player_bound_plugin_renders_a_toggle(mass_minimal: MusicAssistant) -> None:
    """A plugin that binds its sources to players renders a single boolean toggle."""
    mass_minimal.config.set(_CONNECTED_PLAYERS_KEY, [_PLAYER_ID])
    entries = _plugin_entries(mass_minimal, [_make_plugin_provider()])
    assert len(entries) == 1
    entry = entries[0]
    assert entry.key == _PLUGIN_KEY
    assert entry.type == ConfigEntryType.BOOLEAN
    assert entry.category == "plugins"
    # the key is per-plugin (dynamic), so the entry pins a static catalog key
    assert entry.translation_key == "plugin_enable"
    assert entry.translation_params == ["Spotify Connect"]
    assert entry.value is True
    assert entry.default_value is False


async def test_toggle_reads_off_for_an_unconnected_player(mass_minimal: MusicAssistant) -> None:
    """The toggle reads off when the player is not in the plugin's connected players."""
    mass_minimal.config.set(_CONNECTED_PLAYERS_KEY, ["other_player"])
    entries = _plugin_entries(mass_minimal, [_make_plugin_provider()])
    assert len(entries) == 1
    assert entries[0].value is False


async def test_unbound_plugin_and_non_plugin_provider_yield_no_entries(
    mass_minimal: MusicAssistant,
) -> None:
    """A plugin without player-bound sources and a non-plugin provider render no toggle."""
    unbound_plugin = _make_plugin_provider(player_bound=False)
    music_provider = MagicMock()
    music_provider.name = "Some Music Service"
    entries = _plugin_entries(mass_minimal, [unbound_plugin, music_provider])
    assert entries == []


async def test_non_playback_player_gets_no_toggles(mass_minimal: MusicAssistant) -> None:
    """A player that is not a playback target renders no plugin toggles."""
    entries = _plugin_entries(
        mass_minimal, [_make_plugin_provider()], _make_player(PlayerType.PROTOCOL)
    )
    assert entries == []


def test_referenced_strings_exist() -> None:
    """The toggle's translation key and its category are authored in the common strings."""
    strings = json.loads(_STRINGS_PATH.read_text(encoding="utf-8"))
    assert "plugin_enable" in strings["config_entries"]
    assert "plugins" in strings["config_categories"]


async def test_toggle_on_adds_the_player_to_the_plugin(mass_minimal: MusicAssistant) -> None:
    """Enabling the toggle appends the player to the plugin's connected players."""
    mass_minimal.config.set(_CONNECTED_PLAYERS_KEY, ["other_player"])
    values: dict[str, ConfigValueType] = {_PLUGIN_KEY: True, "some_setting": 5}
    with patch.object(
        mass_minimal.config, "_update_provider_config", new_callable=AsyncMock
    ) as update_call:
        result = await mass_minimal.config._update_plugin_provider_config(_PLAYER_ID, values)
    update_call.assert_awaited_once_with(
        _PLUGIN_INSTANCE_ID, {CONF_CONNECTED_PLAYERS: ["other_player", _PLAYER_ID]}
    )
    # the plugin provider is the canonical store, so the toggle never reaches the player values
    assert result == {"some_setting": 5}


async def test_toggle_off_removes_the_player_from_the_plugin(
    mass_minimal: MusicAssistant,
) -> None:
    """Disabling the toggle removes the player from the plugin's connected players."""
    mass_minimal.config.set(_CONNECTED_PLAYERS_KEY, ["other_player", _PLAYER_ID])
    values: dict[str, ConfigValueType] = {_PLUGIN_KEY: False}
    with patch.object(
        mass_minimal.config, "_update_provider_config", new_callable=AsyncMock
    ) as update_call:
        result = await mass_minimal.config._update_plugin_provider_config(_PLAYER_ID, values)
    update_call.assert_awaited_once_with(
        _PLUGIN_INSTANCE_ID, {CONF_CONNECTED_PLAYERS: ["other_player"]}
    )
    assert result == {}


async def test_unchanged_toggle_makes_no_provider_call(mass_minimal: MusicAssistant) -> None:
    """A toggle value that matches the current membership does not touch the plugin config."""
    mass_minimal.config.set(_CONNECTED_PLAYERS_KEY, [_PLAYER_ID])
    values: dict[str, ConfigValueType] = {_PLUGIN_KEY: True, "some_setting": 5}
    with patch.object(
        mass_minimal.config, "_update_provider_config", new_callable=AsyncMock
    ) as update_call:
        result = await mass_minimal.config._update_plugin_provider_config(_PLAYER_ID, values)
    update_call.assert_not_awaited()
    # the toggle key is still stripped so it can never end up in the player's stored values
    assert result == {"some_setting": 5}


async def test_plugin_values_are_extracted_from_the_entries(
    mass_minimal: MusicAssistant,
) -> None:
    """Only the plugin toggle entries contribute to the derived config values."""
    entries = [
        ConfigEntry(key=_PLUGIN_KEY, type=ConfigEntryType.BOOLEAN, default_value=False, value=True),
        ConfigEntry(key="some_setting", type=ConfigEntryType.INTEGER, default_value=5, value=3),
    ]
    values = mass_minimal.config._get_plugin_provider_config_values(entries)
    assert values == {_PLUGIN_KEY: True}
