"""Tests for the scoped access to the stored config of a player and of a protocol."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import PlayerType

from music_assistant.constants import (
    CONF_ENABLED,
    CONF_HIDE_IN_UI,
    CONF_OUTPUT_CHANNELS,
    CONF_PLAYERS,
)

if TYPE_CHECKING:
    from music_assistant.controllers.config import ConfigController
    from music_assistant.mass import MusicAssistant

PLAYER_ID = "player_1"
PROTOCOL_ID = "protocol_1"
PROVIDER = "test_instance"
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
