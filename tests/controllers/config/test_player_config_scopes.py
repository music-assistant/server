"""Tests for the scoped access to the stored config of a player and of a protocol."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, PlayerFeature, PlayerType, ProviderType

from music_assistant.constants import (
    CONF_CACHED_ARP_MAC,
    CONF_ENABLED,
    CONF_HIDE_IN_UI,
    CONF_OUTPUT_CHANNELS,
    CONF_PLAYERS,
    CONF_PREFERRED_OUTPUT_PROTOCOL,
)
from music_assistant.controllers.config.constants import PLAYER_VALUE_KEYS
from music_assistant.models.player import DeviceInfo, Player

if TYPE_CHECKING:
    from music_assistant.controllers.config import ConfigController
    from music_assistant.mass import MusicAssistant

PLAYER_ID = "player_1"
PROTOCOL_ID = "protocol_1"
PROVIDER = "test_instance"
# an entry the protocol object of a player declares
PROTOCOL_SETTING = "protocol_setting"
# the tree a test stores the protocol configs in, apart from the player configs
PROTOCOL_TREE = "test_player_protocols"


@pytest.fixture
def config(mass_minimal: MusicAssistant) -> ConfigController:
    """Return the config controller with a stored config for a player and for a protocol."""
    mass_minimal.players = MagicMock()
    mass_minimal.players.get_player.return_value = None
    mass_minimal.players.get_protocol.return_value = None
    mass_minimal.config.create_default_player_config(PLAYER_ID, PROVIDER, PlayerType.PLAYER)
    mass_minimal.config.create_default_player_config(PROTOCOL_ID, PROVIDER, PlayerType.PROTOCOL)
    return mass_minimal.config


@pytest.fixture
def protocol_tree(monkeypatch: pytest.MonkeyPatch) -> str:
    """Store the protocol configs in a tree of their own; returns the name of that tree."""
    monkeypatch.setattr(
        "music_assistant.controllers.config.players.CONF_PLAYER_PROTOCOLS", PROTOCOL_TREE
    )
    return PROTOCOL_TREE


def test_a_value_is_stored_with_the_id_it_was_set_for(config: ConfigController) -> None:
    """A player value and a protocol value are read back under the id they were stored for."""
    config.set_player_value(PLAYER_ID, CONF_HIDE_IN_UI, True)
    config.set_protocol_value(PROTOCOL_ID, CONF_OUTPUT_CHANNELS, "left")

    assert config.get_player_value(PLAYER_ID, CONF_HIDE_IN_UI) is True
    assert config.get_protocol_value(PROTOCOL_ID, CONF_OUTPUT_CHANNELS) == "left"
    assert config.get_player_value(PLAYER_ID, CONF_OUTPUT_CHANNELS) is None
    assert config.get_protocol_value(PROTOCOL_ID, CONF_HIDE_IN_UI) is None


def test_a_missing_value_reads_as_the_default(config: ConfigController) -> None:
    """Nothing stored, a stored None and an unknown id all read as the given default."""
    config.set_protocol_value(PROTOCOL_ID, CONF_OUTPUT_CHANNELS, None)

    assert config.get_protocol_value(PROTOCOL_ID, CONF_OUTPUT_CHANNELS, "stereo") == "stereo"
    assert config.get_player_value(PLAYER_ID, CONF_HIDE_IN_UI, False) is False
    assert config.get_player_value("unknown", CONF_HIDE_IN_UI, False) is False
    assert config.get_protocol_value("unknown", CONF_ENABLED, True) is True


def test_a_base_key_is_read_and_stored_next_to_the_values(config: ConfigController) -> None:
    """A base key such as enabled is no value: it is stored on the config itself."""
    assert config.get_protocol_value(PROTOCOL_ID, "provider") == PROVIDER
    assert config.get_player_value(PLAYER_ID, CONF_ENABLED) is True

    config.set_protocol_value(PROTOCOL_ID, CONF_ENABLED, False)
    config.set_player_value(PLAYER_ID, "name", "Kitchen")

    assert config.get(f"{CONF_PLAYERS}/{PROTOCOL_ID}/enabled") is False
    assert config.get(f"{CONF_PLAYERS}/{PLAYER_ID}/name") == "Kitchen"
    assert config.get(f"{CONF_PLAYERS}/{PROTOCOL_ID}/values") == {}
    assert config.get_protocol_value(PROTOCOL_ID, CONF_ENABLED, True) is False
    assert config.get_player_value(PLAYER_ID, "name") == "Kitchen"


def test_a_value_is_only_stored_for_a_stored_config(config: ConfigController) -> None:
    """Storing a value for an id without a stored config raises and creates nothing."""
    with pytest.raises(KeyError):
        config.set_player_value("unknown", CONF_HIDE_IN_UI, True)
    with pytest.raises(KeyError):
        config.set_protocol_value("unknown", CONF_OUTPUT_CHANNELS, "left")

    assert config.get(f"{CONF_PLAYERS}/unknown") is None


def test_a_player_and_a_protocol_with_one_id_each_have_their_own_values(
    config: ConfigController, protocol_tree: str
) -> None:
    """With the protocol configs in their own tree, one id holds a config in each scope."""
    config.set(f"{protocol_tree}/{PLAYER_ID}", {"provider": PROVIDER, "enabled": False})

    config.set_player_value(PLAYER_ID, CONF_HIDE_IN_UI, True)
    config.set_protocol_value(PLAYER_ID, CONF_OUTPUT_CHANNELS, "left")

    assert config.get(f"{CONF_PLAYERS}/{PLAYER_ID}/values") == {CONF_HIDE_IN_UI: True}
    assert config.get(f"{protocol_tree}/{PLAYER_ID}/values") == {CONF_OUTPUT_CHANNELS: "left"}
    assert config.get_player_value(PLAYER_ID, CONF_OUTPUT_CHANNELS) is None
    assert config.get_protocol_value(PLAYER_ID, CONF_HIDE_IN_UI) is None
    # the base keys are apart as well: the protocol is switched off, the player is not
    assert config.get_player_value(PLAYER_ID, CONF_ENABLED) is True
    assert config.get_protocol_value(PLAYER_ID, CONF_ENABLED, True) is False
    # a protocol without a config of its own takes no value, whatever the players hold
    with pytest.raises(KeyError):
        config.set_protocol_value(PROTOCOL_ID, CONF_OUTPUT_CHANNELS, "left")


class _TestProvider:
    """Minimal PlayerProvider stand-in backed by the real MusicAssistant."""

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize the test provider."""
        self.mass = mass
        self.domain = self.instance_id = PROVIDER
        self.translation_owner = f"provider.{PROVIDER}"
        self.name = "Test Provider"
        self.available = True
        self.logger = logging.getLogger("test.provider")
        self.manifest = MagicMock()
        self.manifest.domain = PROVIDER
        self.manifest.name = self.name
        self.type = self.manifest.type = ProviderType.PLAYER
        self.protocols: list[Player] = []

    async def unload(self, is_removed: bool = False) -> None:
        """Unload the provider (nothing to clean up)."""


class _TestPlayer(Player):
    """A player with audio output, declaring one config entry of its own."""

    def __init__(self, provider: _TestProvider, player_type: PlayerType) -> None:
        """Initialize the test player."""
        super().__init__(provider, PLAYER_ID)  # type: ignore[arg-type]
        self._attr_name = "Test Player"
        self._attr_type = player_type
        self._attr_available = True
        self._attr_supported_features = {PlayerFeature.VOLUME_SET, PlayerFeature.PLAY_MEDIA}
        self._attr_device_info = DeviceInfo(model="Test Model", manufacturer="Test")
        self._cache.clear()
        self.update_state(signal_event=False)

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return the entry this player declares."""
        return [
            ConfigEntry(
                key=PROTOCOL_SETTING,
                type=ConfigEntryType.STRING,
                default_value="off",
                required=False,
            )
        ]

    async def stop(self) -> None:
        """Stop playback - required abstract method."""


def _register_player(mass: MusicAssistant, player_type: PlayerType = PlayerType.PLAYER) -> Player:
    """Put a player in the registry with the config that registration creates."""
    provider = _TestProvider(mass)
    mass._providers[provider.instance_id] = provider  # type: ignore[assignment]
    mass._provider_manifests[provider.domain] = provider.manifest
    player = _TestPlayer(provider, player_type)
    mass.config.create_default_player_config(PLAYER_ID, PROVIDER, player_type, player.name)
    mass.players._players[PLAYER_ID] = player
    # avoid driving the full player-manager reload machinery
    mass.players.on_player_config_change = AsyncMock()  # type: ignore[method-assign]
    return player


def _stored_values(mass: MusicAssistant, tree: str, config_id: str) -> dict[str, Any]:
    """Return the stored values of one config."""
    values: dict[str, Any] = mass.config.get(f"{tree}/{config_id}/values")
    return values


SAVED_VALUES: dict[str, ConfigValueType] = {
    CONF_HIDE_IN_UI: True,
    PROTOCOL_SETTING: "on",
    CONF_OUTPUT_CHANNELS: "left",
}


async def test_saving_stores_the_values_of_a_native_protocol_with_its_own_id_on_its_config(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A value is stored with whoever declared its entry: the player or its native protocol."""
    _register_player(mass)
    monkeypatch.setattr(mass.config, "_get_native_protocol_id", lambda _player_id: PROTOCOL_ID)
    mass.config.create_default_player_config(PROTOCOL_ID, PROVIDER, PlayerType.PLAYER)
    # what the provider stores for its protocol is no entry, and stays where it is
    mass.config.set_protocol_value(PROTOCOL_ID, CONF_CACHED_ARP_MAC, "AA:BB:CC:DD:EE:FF")

    saved = await mass.config.save_player_config(PLAYER_ID, dict(SAVED_VALUES))

    assert _stored_values(mass, CONF_PLAYERS, PLAYER_ID) == {CONF_HIDE_IN_UI: True}
    assert _stored_values(mass, CONF_PLAYERS, PROTOCOL_ID) == {
        PROTOCOL_SETTING: "on",
        CONF_OUTPUT_CHANNELS: "left",
        CONF_CACHED_ARP_MAC: "AA:BB:CC:DD:EE:FF",
    }
    # the player's config still holds all of them, as one config
    assert {key: saved.get_value(key) for key in SAVED_VALUES} == SAVED_VALUES
    assert mass.config.get_player_value(PLAYER_ID, PROTOCOL_SETTING) is None
    assert mass.config.get_protocol_value(PROTOCOL_ID, PROTOCOL_SETTING) == "on"

    # a value set back to its default leaves the config it was stored on
    await mass.config.save_player_config(PLAYER_ID, {PROTOCOL_SETTING: "off"})

    assert _stored_values(mass, CONF_PLAYERS, PLAYER_ID) == {CONF_HIDE_IN_UI: True}
    assert _stored_values(mass, CONF_PLAYERS, PROTOCOL_ID) == {
        CONF_OUTPUT_CHANNELS: "left",
        CONF_CACHED_ARP_MAC: "AA:BB:CC:DD:EE:FF",
    }


async def test_saving_stores_the_values_of_a_native_protocol_with_the_players_id_in_its_tree(
    mass: MusicAssistant, protocol_tree: str
) -> None:
    """With the protocol configs in their own tree, a save splits the values over both trees."""
    _register_player(mass)
    mass.config.set(f"{protocol_tree}/{PLAYER_ID}", {"provider": PROVIDER, "values": {}})

    saved = await mass.config.save_player_config(PLAYER_ID, dict(SAVED_VALUES))

    assert _stored_values(mass, CONF_PLAYERS, PLAYER_ID) == {CONF_HIDE_IN_UI: True}
    assert _stored_values(mass, protocol_tree, PLAYER_ID) == {
        PROTOCOL_SETTING: "on",
        CONF_OUTPUT_CHANNELS: "left",
    }
    assert {key: saved.get_value(key) for key in SAVED_VALUES} == SAVED_VALUES


async def test_saving_stores_all_values_of_a_player_on_the_config_it_shares_with_its_protocol(
    mass: MusicAssistant,
) -> None:
    """A player and its native protocol under one id in one tree share a stored config."""
    _register_player(mass)

    await mass.config.save_player_config(PLAYER_ID, dict(SAVED_VALUES))

    assert _stored_values(mass, CONF_PLAYERS, PLAYER_ID) == SAVED_VALUES


async def test_a_rejected_save_restores_the_config_of_the_player_and_of_its_protocol(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A save the players controller rejects leaves both stored configs as they were."""
    _register_player(mass)
    monkeypatch.setattr(mass.config, "_get_native_protocol_id", lambda _player_id: PROTOCOL_ID)
    mass.config.create_default_player_config(PROTOCOL_ID, PROVIDER, PlayerType.PLAYER)
    await mass.config.save_player_config(PLAYER_ID, dict(SAVED_VALUES))
    mass.players.on_player_config_change = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("rejected")
    )

    with pytest.raises(RuntimeError, match="rejected"):
        await mass.config.save_player_config(
            PLAYER_ID, {CONF_HIDE_IN_UI: False, PROTOCOL_SETTING: "other"}
        )

    assert _stored_values(mass, CONF_PLAYERS, PLAYER_ID) == {CONF_HIDE_IN_UI: True}
    assert _stored_values(mass, CONF_PLAYERS, PROTOCOL_ID) == {
        PROTOCOL_SETTING: "on",
        CONF_OUTPUT_CHANNELS: "left",
    }


@pytest.mark.parametrize("player_type", [PlayerType.PLAYER, PlayerType.GROUP])
async def test_the_entries_every_player_gets_are_values_of_the_player_itself(
    mass: MusicAssistant, player_type: PlayerType
) -> None:
    """The entries the players controller adds to every player are stored with the player."""
    player = _register_player(mass, player_type)

    own_keys = {entry.key for entry in mass.config._get_default_player_config_entries(player)}
    protocol_keys = {entry.key for entry in await mass.config._get_player_config_entries(player)}

    assert own_keys
    assert own_keys | {CONF_PREFERRED_OUTPUT_PROTOCOL} <= PLAYER_VALUE_KEYS
    assert PROTOCOL_SETTING in protocol_keys
    assert not protocol_keys & PLAYER_VALUE_KEYS
