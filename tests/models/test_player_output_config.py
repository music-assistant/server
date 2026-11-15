"""Tests for config resolution against the player's active audio output path."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from music_assistant_models.config_entries import ConfigEntry, PlayerConfig
from music_assistant_models.enums import ConfigEntryType, ProtocolRole

from music_assistant.constants import CONF_OUTPUT_CODEC
from music_assistant.models.player import Player
from tests.common import MockProtocol, MockProvider, attach_mock, mock_player_mass


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance."""
    mass = mock_player_mass()
    mass.config.get_player_value = MagicMock(return_value=None)
    return mass


def _output_codec_entry(default_value: str) -> ConfigEntry:
    return ConfigEntry(
        key=CONF_OUTPUT_CODEC,
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


def _make_player(mock_mass: MagicMock) -> tuple[Player, MockProtocol, MockProtocol]:
    """Create a player with its native protocol ("parent") and an audio protocol ("child")."""
    provider = MockProvider("test", mass=mock_mass)
    player, native = attach_mock(provider, "parent", "Parent")
    _, audio = attach_mock(provider, "child", "Child", role=ProtocolRole.AUDIO, player=player)
    return player, native, audio


class TestResolveOutputPlayer:
    """Tests for Player.resolve_output_protocol."""

    def test_returns_active_protocol_player(self, mock_mass: MagicMock) -> None:
        """An active (non-native) output protocol resolves to that protocol."""
        player, _, audio = _make_player(mock_mass)
        player.set_active_output_protocol("child")
        assert player.resolve_output_protocol() is audio

    def test_returns_self_for_native_output(self, mock_mass: MagicMock) -> None:
        """Native output resolves to the player itself."""
        player, native, _audio = _make_player(mock_mass)
        player.set_active_output_protocol("native")
        assert player.resolve_output_protocol() is native

    def test_returns_self_without_active_protocol(self, mock_mass: MagicMock) -> None:
        """No active output protocol resolves to the player itself."""
        player, native, _audio = _make_player(mock_mass)
        assert player.resolve_output_protocol() is native

    def test_returns_self_when_protocol_player_missing(self, mock_mass: MagicMock) -> None:
        """A dangling active protocol id falls back to the player itself."""
        player, native, _audio = _make_player(mock_mass)
        player.set_active_output_protocol("ghost")
        assert player.resolve_output_protocol() is native


class TestGetOutputConfigValue:
    """Tests for Player.get_output_config_value."""

    def test_protocol_entry_default_beats_parent_value(self, mock_mass: MagicMock) -> None:
        """The output protocol's provider entry default wins over the native protocol's value."""
        player, native, audio = _make_player(mock_mass)
        native.set_config(
            _player_config(
                "parent",
                [_output_codec_entry("default")],
                {CONF_OUTPUT_CODEC: "mp3"},
            )
        )
        audio.set_config(_player_config("child", [_output_codec_entry("wav")]))
        player.set_active_output_protocol("child")

        assert player.get_output_config_value(CONF_OUTPUT_CODEC, "default") == "wav"

    def test_protocol_stored_value_wins(self, mock_mass: MagicMock) -> None:
        """A value stored on the output protocol wins over its entry default."""
        player, _, audio = _make_player(mock_mass)
        audio.set_config(
            _player_config(
                "child",
                [_output_codec_entry("wav")],
                {CONF_OUTPUT_CODEC: "aac"},
            )
        )
        player.set_active_output_protocol("child")

        assert player.get_output_config_value(CONF_OUTPUT_CODEC, "default") == "aac"

    def test_own_value_used_without_active_protocol(self, mock_mass: MagicMock) -> None:
        """Without an active output protocol the player's own value applies."""
        player, native, _audio = _make_player(mock_mass)
        native.set_config(
            _player_config(
                "parent",
                [_output_codec_entry("default")],
                {CONF_OUTPUT_CODEC: "mp3"},
            )
        )

        assert player.get_output_config_value(CONF_OUTPUT_CODEC, "default") == "mp3"

    def test_falls_back_to_own_value_when_protocol_lacks_entry(self, mock_mass: MagicMock) -> None:
        """A key the output protocol has no entry for falls back to the native protocol's value."""
        player, native, audio = _make_player(mock_mass)
        native.set_config(
            _player_config(
                "parent",
                [_output_codec_entry("default")],
                {CONF_OUTPUT_CODEC: "mp3"},
            )
        )
        audio.set_config(_player_config("child"))
        player.set_active_output_protocol("child")

        assert player.get_output_config_value(CONF_OUTPUT_CODEC, "default") == "mp3"

    def test_default_for_unknown_key(self, mock_mass: MagicMock) -> None:
        """A key present on neither player resolves to the given default."""
        player, native, audio = _make_player(mock_mass)
        native.set_config(_player_config("parent"))
        audio.set_config(_player_config("child"))
        player.set_active_output_protocol("child")

        assert player.get_output_config_value(CONF_OUTPUT_CODEC, "default") == "default"
