"""Tests for auto-derivation of PlayerFeature.SELECT_SOURCE."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import PlayerFeature

from music_assistant.models.plugin import PluginSource
from tests.common import MockPlayer, MockProvider


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance."""
    mass = MagicMock()
    mass.config.get_raw_player_config_value = MagicMock(return_value="auto")
    return mass


@pytest.fixture
def provider(mock_mass: MagicMock) -> MockProvider:
    """Create a mock provider."""
    return MockProvider("test_provider", mass=mock_mass)


class TestSelectSourceAutoDerivation:
    """SELECT_SOURCE is auto-set when the FINAL source list has 2+ non-passive entries."""

    def test_plugin_source_grants_bit(self, mock_mass: MagicMock, provider: MockProvider) -> None:
        """MA Queue + one plugin source → SELECT_SOURCE."""
        mock_mass.players.get_plugin_sources = MagicMock(
            return_value=[PluginSource(id="airplay_receiver", name="AirPlay Receiver")]
        )
        player = MockPlayer(provider, "player_1", "Player")

        features = player._Player__final_supported_features  # type: ignore[attr-defined]
        assert PlayerFeature.SELECT_SOURCE in features

    def test_no_extra_sources_does_not_grant_bit(
        self, mock_mass: MagicMock, provider: MockProvider
    ) -> None:
        """MA Queue alone → SELECT_SOURCE not set."""
        mock_mass.players.get_plugin_sources = MagicMock(return_value=[])
        player = MockPlayer(provider, "player_2", "Player")

        features = player._Player__final_supported_features  # type: ignore[attr-defined]
        assert PlayerFeature.SELECT_SOURCE not in features
