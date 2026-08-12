"""Tests for auto-derivation of PlayerFeature.SELECT_SOURCE."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import PlayerFeature
from music_assistant_models.player import PlayerSource

from tests.common import MockPlayer, MockProvider


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance."""
    mass = MagicMock()
    mass.config.get_raw_player_config_value = MagicMock(
        side_effect=lambda _player_id, _key, default=None: default
    )
    return mass


@pytest.fixture
def provider(mock_mass: MagicMock) -> MockProvider:
    """Create a mock provider."""
    return MockProvider("test_provider", mass=mock_mass)


class TestSelectSourceAutoDerivation:
    """SELECT_SOURCE is auto-set when the FINAL source list has 2+ non-passive entries."""

    def test_native_source_plus_queue_grants_bit(
        self, mock_mass: MagicMock, provider: MockProvider
    ) -> None:
        """MA Queue + one native source (e.g. line-in) → SELECT_SOURCE."""
        player = MockPlayer(provider, "player_1", "Player")
        player._attr_source_list = [PlayerSource(id="line_in", name="Line-in", passive=False)]

        features = player._Player__final_supported_features  # type: ignore[attr-defined]
        assert PlayerFeature.SELECT_SOURCE in features

    def test_no_extra_sources_does_not_grant_bit(
        self, mock_mass: MagicMock, provider: MockProvider
    ) -> None:
        """MA Queue alone (no native sources) → SELECT_SOURCE not set."""
        player = MockPlayer(provider, "player_2", "Player")
        # No source_list mutation — only the implicit MA queue gets added

        features = player._Player__final_supported_features  # type: ignore[attr-defined]
        assert PlayerFeature.SELECT_SOURCE not in features
