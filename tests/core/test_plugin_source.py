"""Tests for plugin source selection and deselection on the PlayerController.

This module tests:
- deselect_source stops the player via _handle_cmd_stop
- deselect_source is a no-op for unknown players
- deselect_source suppresses errors from stop
- in_use_by lifecycle on PluginSource
- select_source(id, None) differs from deselect_source (selects queue vs stops)
- Disconnect flow: clear in_use_by then deselect stops the player
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import PlayerFeature
from music_assistant_models.errors import PlayerCommandFailed, PlayerUnavailableError

from music_assistant.controllers.players import PlayerController
from music_assistant.helpers.throttle_retry import Throttler
from music_assistant.models.plugin import PluginProvider, PluginSource
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
    mass.streams = MagicMock()
    mass.streams.get_plugin_source_url = AsyncMock(return_value="http://localhost/plugin/test")
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


@pytest.fixture
def player(provider: MockProvider, controller: PlayerController) -> MockPlayer:
    """Create and register a mock player."""
    p = MockPlayer(provider, "player_1", "Test Player")
    p._attr_supported_features = {PlayerFeature.VOLUME_SET}
    controller._players = {"player_1": p}
    controller._player_throttlers = {"player_1": Throttler(1, 0.05)}
    return p


@pytest.fixture
def plugin_source() -> PluginSource:
    """Create a PluginSource for testing."""
    return PluginSource(
        id="test_plugin",
        name="Test Plugin Source",
    )


class TestDeselectSource:
    """Test deselect_source stops the player when a plugin source disconnects."""

    @pytest.mark.asyncio
    async def test_deselect_source_calls_stop(
        self, controller: PlayerController, player: MockPlayer
    ) -> None:
        """Deselecting a source should call _handle_cmd_stop."""
        controller._handle_cmd_stop = AsyncMock()  # type: ignore[method-assign]

        await controller.deselect_source("player_1")

        controller._handle_cmd_stop.assert_awaited_once_with("player_1")

    @pytest.mark.asyncio
    async def test_deselect_source_noop_for_unknown_player(
        self, controller: PlayerController
    ) -> None:
        """Deselecting on a non-existent player should not raise."""
        controller._handle_cmd_stop = AsyncMock()  # type: ignore[method-assign]

        await controller.deselect_source("nonexistent_player")

        controller._handle_cmd_stop.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_deselect_source_suppresses_player_command_failed(
        self, controller: PlayerController, player: MockPlayer
    ) -> None:
        """Deselect should not raise PlayerCommandFailed."""
        controller._handle_cmd_stop = AsyncMock(  # type: ignore[method-assign]
            side_effect=PlayerCommandFailed("test error")
        )

        await controller.deselect_source("player_1")

    @pytest.mark.asyncio
    async def test_deselect_source_suppresses_runtime_error(
        self, controller: PlayerController, player: MockPlayer
    ) -> None:
        """Deselect should not raise RuntimeError."""
        controller._handle_cmd_stop = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("test error")
        )

        await controller.deselect_source("player_1")

    @pytest.mark.asyncio
    async def test_deselect_source_suppresses_player_unavailable(
        self, controller: PlayerController, player: MockPlayer
    ) -> None:
        """Deselect should not raise PlayerUnavailableError."""
        controller._handle_cmd_stop = AsyncMock(  # type: ignore[method-assign]
            side_effect=PlayerUnavailableError("player unavailable")
        )

        await controller.deselect_source("player_1")


class TestPluginSourceInUseBy:
    """Test that in_use_by is properly managed during plugin source lifecycle."""

    def test_in_use_by_initially_none(self, plugin_source: PluginSource) -> None:
        """New plugin source should have in_use_by as None."""
        assert plugin_source.in_use_by is None

    def test_set_and_clear_in_use_by(self, plugin_source: PluginSource) -> None:
        """Setting in_use_by to None should clear the source."""
        plugin_source.in_use_by = "player_1"
        assert plugin_source.in_use_by == "player_1"

        plugin_source.in_use_by = None
        assert plugin_source.in_use_by is None

    def test_as_player_source_strips_callbacks(self, plugin_source: PluginSource) -> None:
        """as_player_source should return a basic PlayerSource without plugin-specific fields."""
        plugin_source.on_select = AsyncMock()
        plugin_source.in_use_by = "player_1"

        ps = plugin_source.as_player_source()
        assert ps.id == "test_plugin"
        assert ps.name == "Test Plugin Source"
        # PlayerSource doesn't have in_use_by or on_select
        assert not hasattr(ps, "in_use_by")


class TestSelectSourceVsDeselectSource:
    """Test that select_source(id, None) and deselect_source have different semantics."""

    @pytest.mark.asyncio
    async def test_select_source_none_selects_queue(
        self, controller: PlayerController, player: MockPlayer, mock_mass: MagicMock
    ) -> None:
        """select_source with None should select the queue source (not stop)."""
        mock_mass.player_queues.get = MagicMock(return_value=MagicMock())
        mock_mass.get_provider = MagicMock(return_value=None)

        with patch.object(player, "set_active_mass_source") as mock_set:
            await controller.select_source("player_1", None)
            # source defaults to player_id, which matches the queue
            mock_set.assert_called_once_with("player_1")

    @pytest.mark.asyncio
    async def test_deselect_source_stops_instead_of_selecting(
        self, controller: PlayerController, player: MockPlayer
    ) -> None:
        """deselect_source should stop, not select the queue."""
        controller._handle_cmd_stop = AsyncMock()  # type: ignore[method-assign]

        await controller.deselect_source("player_1")

        controller._handle_cmd_stop.assert_awaited_once_with("player_1")

    @pytest.mark.asyncio
    async def test_select_source_with_plugin_delegates_to_handler(
        self, controller: PlayerController, player: MockPlayer, mock_mass: MagicMock
    ) -> None:
        """Selecting a plugin source should delegate to _handle_select_plugin_source."""
        plugin_source = PluginSource(id="my_plugin", name="My Plugin")
        mock_plugin_prov = MagicMock(spec=PluginProvider)
        mock_plugin_prov.instance_id = "my_plugin"
        mock_plugin_prov.get_source.return_value = plugin_source

        mock_mass.get_provider = MagicMock(return_value=mock_plugin_prov)

        # Mock _handle_select_plugin_source to avoid needing full playback infrastructure
        controller._handle_select_plugin_source = AsyncMock()  # type: ignore[method-assign]

        await controller.select_source("player_1", "my_plugin")

        controller._handle_select_plugin_source.assert_awaited_once()


class TestPluginSourceDisconnectFlow:
    """Test the disconnect flow for plugin sources (AirPlay, Spotify Connect, etc.)."""

    @pytest.mark.asyncio
    async def test_deselect_after_clearing_in_use_by_stops_player(
        self, controller: PlayerController, player: MockPlayer
    ) -> None:
        """Verify that the provider disconnect pattern (clear in_use_by + deselect) stops the player.

        Providers call _clear_active_player() which sets in_use_by=None,
        then deselect_source() to stop the player. This test verifies both
        steps work correctly together.
        """
        controller._handle_cmd_stop = AsyncMock()  # type: ignore[method-assign]

        plugin_source = PluginSource(id="airplay_recv", name="AirPlay")
        plugin_source.in_use_by = "player_1"

        # Provider clears in_use_by (breaks the stream loop in the controller)
        plugin_source.in_use_by = None
        assert plugin_source.in_use_by is None

        # Provider calls deselect_source to stop the player (the fix for #5130)
        await controller.deselect_source("player_1")

        controller._handle_cmd_stop.assert_awaited_once_with("player_1")

    @pytest.mark.asyncio
    async def test_disconnect_with_no_active_player_is_noop(
        self, controller: PlayerController
    ) -> None:
        """If no player was active, deselect should be a no-op."""
        controller._handle_cmd_stop = AsyncMock()  # type: ignore[method-assign]

        await controller.deselect_source("nonexistent")

        controller._handle_cmd_stop.assert_not_awaited()
