"""Tests for config resolution against the player's active audio output path."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from music_assistant_models.config_entries import ConfigEntry, PlayerConfig
from music_assistant_models.enums import ConfigEntryType, PlayerType

from music_assistant.constants import CONF_HTTP_PROFILE
from tests.common import MockPlayer, MockProvider


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance."""
    mass = MagicMock()
    mass.closing = False
    mass.config.get = MagicMock(return_value=[])
    mass.config.get_raw_player_config_value = MagicMock(return_value=None)
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    mass.config.set = MagicMock()
    return mass


def _http_profile_entry(default_value: str) -> ConfigEntry:
    return ConfigEntry(
        key=CONF_HTTP_PROFILE,
        type=ConfigEntryType.STRING,
        default_value=default_value,
    )


def _player_config(
    player_id: str,
    entries: list[ConfigEntry] | None = None,
    values: dict[str, Any] | None = None,
) -> PlayerConfig:
    raw: dict[str, Any] = {"player_id": player_id, "provider": "test"}
    if values:
        raw["values"] = values
    return cast("PlayerConfig", PlayerConfig.parse(entries or [], raw))


def _make_parent_and_child(mock_mass: MagicMock) -> tuple[MockPlayer, MockPlayer]:
    """Create a parent player and a protocol child wired into mass.players."""
    provider = MockProvider("test", mass=mock_mass)
    parent = MockPlayer(provider, "parent", "Parent")
    child = MockPlayer(provider, "child", "Child", player_type=PlayerType.PROTOCOL)
    players = {"parent": parent, "child": child}
    mock_mass.players.get_player = MagicMock(side_effect=lambda pid: players.get(pid))
    return parent, child


class TestResolveOutputPlayer:
    """Tests for Player.resolve_output_player."""

    def test_returns_active_protocol_player(self, mock_mass: MagicMock) -> None:
        """An active (non-native) output protocol resolves to the protocol player."""
        parent, child = _make_parent_and_child(mock_mass)
        parent.set_active_output_protocol("child")
        assert parent.resolve_output_player() is child

    def test_returns_self_for_native_output(self, mock_mass: MagicMock) -> None:
        """Native output resolves to the player itself."""
        parent, _child = _make_parent_and_child(mock_mass)
        parent.set_active_output_protocol("native")
        assert parent.resolve_output_player() is parent

    def test_returns_self_without_active_protocol(self, mock_mass: MagicMock) -> None:
        """No active output protocol resolves to the player itself."""
        parent, _child = _make_parent_and_child(mock_mass)
        assert parent.resolve_output_player() is parent

    def test_returns_self_when_protocol_player_missing(self, mock_mass: MagicMock) -> None:
        """A dangling active protocol id falls back to the player itself."""
        parent, _child = _make_parent_and_child(mock_mass)
        parent.set_active_output_protocol("ghost")
        assert parent.resolve_output_player() is parent


class TestGetOutputConfigValue:
    """Tests for Player.get_output_config_value."""

    def test_protocol_entry_default_beats_parent_value(self, mock_mass: MagicMock) -> None:
        """The output player's provider entry default wins over the parent's stored value."""
        parent, child = _make_parent_and_child(mock_mass)
        parent.set_config(
            _player_config(
                "parent",
                [_http_profile_entry("default")],
                {CONF_HTTP_PROFILE: "chunked"},
            )
        )
        child.set_config(_player_config("child", [_http_profile_entry("forced_content_length")]))
        parent.set_active_output_protocol("child")

        assert parent.get_output_config_value(CONF_HTTP_PROFILE, "default") == (
            "forced_content_length"
        )

    def test_protocol_stored_value_wins(self, mock_mass: MagicMock) -> None:
        """A value stored on the output player wins over its entry default."""
        parent, child = _make_parent_and_child(mock_mass)
        child.set_config(
            _player_config(
                "child",
                [_http_profile_entry("forced_content_length")],
                {CONF_HTTP_PROFILE: "no_content_length"},
            )
        )
        parent.set_active_output_protocol("child")

        assert parent.get_output_config_value(CONF_HTTP_PROFILE, "default") == "no_content_length"

    def test_own_value_used_without_active_protocol(self, mock_mass: MagicMock) -> None:
        """Without an active output protocol the player's own value applies."""
        parent, _child = _make_parent_and_child(mock_mass)
        parent.set_config(
            _player_config(
                "parent",
                [_http_profile_entry("default")],
                {CONF_HTTP_PROFILE: "chunked"},
            )
        )

        assert parent.get_output_config_value(CONF_HTTP_PROFILE, "default") == "chunked"

    def test_falls_back_to_own_value_when_protocol_lacks_entry(self, mock_mass: MagicMock) -> None:
        """A key the output player has no entry for falls back to the player's own value."""
        parent, child = _make_parent_and_child(mock_mass)
        parent.set_config(
            _player_config(
                "parent",
                [_http_profile_entry("default")],
                {CONF_HTTP_PROFILE: "chunked"},
            )
        )
        child.set_config(_player_config("child"))
        parent.set_active_output_protocol("child")

        assert parent.get_output_config_value(CONF_HTTP_PROFILE, "default") == "chunked"

    def test_default_for_unknown_key(self, mock_mass: MagicMock) -> None:
        """A key present on neither player resolves to the given default."""
        parent, child = _make_parent_and_child(mock_mass)
        parent.set_config(_player_config("parent"))
        child.set_config(_player_config("child"))
        parent.set_active_output_protocol("child")

        assert parent.get_output_config_value(CONF_HTTP_PROFILE, "default") == "default"
