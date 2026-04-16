"""Tests for PluginSource elapsed_time override in __final_playback_state.

Verifies that PlayerState.elapsed_time prefers PluginSource metadata
over the physical player's (or protocol player's) byte-based clock.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import PlaybackState, PlayerType
from music_assistant_models.streamdetails import StreamMetadata

from music_assistant.controllers.players import PlayerController
from music_assistant.helpers.throttle_retry import Throttler
from music_assistant.models.plugin import PluginSource
from tests.common import MockPlayer, MockProvider


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance."""
    mass = MagicMock()
    mass.closing = False
    mass.loop = None
    mass.config = MagicMock()
    mass.config.get = MagicMock(return_value=[])
    mass.config.get_raw_player_config_value = MagicMock(return_value="auto")
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    mass.config.set = MagicMock()
    mass.signal_event = MagicMock()
    mass.get_providers = MagicMock(return_value=[])
    mass.player_queues = MagicMock()
    mass.player_queues.get = MagicMock(return_value=None)
    return mass


@pytest.fixture
def controller(mock_mass: MagicMock) -> PlayerController:
    """Create a PlayerController instance."""
    ctrl = PlayerController(mock_mass)
    mock_mass.players = ctrl
    return ctrl


@pytest.fixture
def provider(mock_mass: MagicMock) -> MockProvider:
    """Create a mock provider."""
    return MockProvider("test_provider", instance_id="test_prov", mass=mock_mass)


def _make_plugin_source(
    source_id: str = "ynison_source",
    elapsed_time: int | None = 42,
    elapsed_time_last_updated: float | None = None,
    in_use_by: str | None = "player_1",
) -> PluginSource:
    """Create a PluginSource with metadata."""
    source = PluginSource(id=source_id, name="Test Plugin Source")
    source.metadata = StreamMetadata(title="Test Track")
    source.metadata.elapsed_time = elapsed_time
    source.metadata.elapsed_time_last_updated = elapsed_time_last_updated or time.time()
    source.in_use_by = in_use_by
    return source


class TestPluginSourceElapsedTime:
    """Test that PlayerState.elapsed_time prefers PluginSource metadata."""

    def test_plugin_source_elapsed_time_preferred_over_player(
        self,
        provider: MockProvider,
        controller: PlayerController,
        mock_mass: MagicMock,
    ) -> None:
        """PluginSource elapsed_time overrides the physical player's elapsed_time."""
        player = MockPlayer(provider, "player_1", "Test Player")
        player._attr_playback_state = PlaybackState.PLAYING
        player._attr_elapsed_time = 10.0  # physical player reports 10s
        player._attr_elapsed_time_last_updated = time.time() - 5

        controller._players = {"player_1": player}
        controller._player_throttlers = {"player_1": Throttler(1, 0.05)}

        source = _make_plugin_source(elapsed_time=42)
        mock_mass.get_providers.return_value = []

        # Mock get_plugin_source to return our source
        controller.get_plugin_source = MagicMock(return_value=source)  # type: ignore[method-assign]

        # Mock get_plugin_sources so __final_active_source finds our source
        controller.get_plugin_sources = MagicMock(return_value=[source])  # type: ignore[method-assign]

        player.update_state(signal_event=False)

        # elapsed_time should come from PluginSource, not physical player
        assert player.state.elapsed_time == 42

    def test_plugin_source_no_elapsed_time_falls_through(
        self,
        provider: MockProvider,
        controller: PlayerController,
        mock_mass: MagicMock,
    ) -> None:
        """Without PluginSource elapsed_time, use the player's own elapsed_time."""
        player = MockPlayer(provider, "player_1", "Test Player")
        player._attr_playback_state = PlaybackState.PLAYING
        player._attr_elapsed_time = 10.0
        player._attr_elapsed_time_last_updated = time.time()

        controller._players = {"player_1": player}
        controller._player_throttlers = {"player_1": Throttler(1, 0.05)}

        # Source without elapsed_time
        source = _make_plugin_source(elapsed_time=None)
        controller.get_plugin_source = MagicMock(return_value=source)  # type: ignore[method-assign]
        controller.get_plugin_sources = MagicMock(return_value=[source])  # type: ignore[method-assign]

        player.update_state(signal_event=False)

        assert player.state.elapsed_time == 10.0

    def test_plugin_source_elapsed_time_with_output_protocol(
        self,
        provider: MockProvider,
        controller: PlayerController,
        mock_mass: MagicMock,
    ) -> None:
        """PluginSource elapsed_time overrides even when an output protocol is active."""
        # Create protocol player (e.g., AirPlay)
        protocol_player = MockPlayer(
            provider, "airplay_1", "AirPlay", player_type=PlayerType.PROTOCOL
        )
        protocol_player._attr_playback_state = PlaybackState.PLAYING
        protocol_player._attr_elapsed_time = 5.0  # protocol player reports 5s
        protocol_player._attr_elapsed_time_last_updated = time.time()

        # Create main player with active output protocol
        player = MockPlayer(provider, "player_1", "Test Player")
        player._attr_playback_state = PlaybackState.PLAYING
        player._attr_elapsed_time = 8.0
        player._attr_elapsed_time_last_updated = time.time()
        # Set active output protocol via public setter
        player.set_active_output_protocol("airplay_1")

        controller._players = {"player_1": player, "airplay_1": protocol_player}
        controller._player_throttlers = {
            "player_1": Throttler(1, 0.05),
            "airplay_1": Throttler(1, 0.05),
        }

        source = _make_plugin_source(elapsed_time=42, in_use_by="player_1")
        controller.get_plugin_source = MagicMock(return_value=source)  # type: ignore[method-assign]
        controller.get_plugin_sources = MagicMock(return_value=[source])  # type: ignore[method-assign]

        # Need protocol player state to be available
        protocol_player.update_state(signal_event=False)
        player.update_state(signal_event=False)

        # PluginSource elapsed_time should override protocol player's elapsed_time
        assert player.state.elapsed_time == 42

    def test_plugin_source_elapsed_time_last_updated_fallback(
        self,
        provider: MockProvider,
        controller: PlayerController,
        mock_mass: MagicMock,
    ) -> None:
        """When PluginSource has no elapsed_time_last_updated, fall back to time.time()."""
        player = MockPlayer(provider, "player_1", "Test Player")
        player._attr_playback_state = PlaybackState.PLAYING
        player._attr_elapsed_time = 10.0
        old_timestamp = time.time() - 100
        player._attr_elapsed_time_last_updated = old_timestamp

        controller._players = {"player_1": player}
        controller._player_throttlers = {"player_1": Throttler(1, 0.05)}

        source = _make_plugin_source(elapsed_time=42, elapsed_time_last_updated=None)
        assert source.metadata is not None
        source.metadata.elapsed_time_last_updated = None  # explicitly None
        controller.get_plugin_source = MagicMock(return_value=source)  # type: ignore[method-assign]
        controller.get_plugin_sources = MagicMock(return_value=[source])  # type: ignore[method-assign]

        before = time.time()
        player.update_state(signal_event=False)
        after = time.time()

        assert player.state.elapsed_time == 42
        # Should use time.time(), not the old player timestamp
        assert player.state.elapsed_time_last_updated is not None
        assert player.state.elapsed_time_last_updated >= before
        assert player.state.elapsed_time_last_updated <= after
