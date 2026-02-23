"""Tests for player grouping logic (independent of protocols).

This module tests the core grouping behavior including:
- can_group_with filtering logic
- Group member inclusion/exclusion
- Sync leader behavior
- Group state transitions
- Cache invalidation
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import PlaybackState, PlayerFeature

from music_assistant.controllers.players import PlayerController
from tests.common import MockPlayer, MockProvider


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance."""
    mass = MagicMock()
    mass.closing = False
    mass.config = MagicMock()
    mass.config.get = MagicMock(return_value=[])
    mass.config.get_raw_player_config_value = MagicMock(return_value="auto")
    # Return "GLOBAL" for log level config (standard default)
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    mass.config.set = MagicMock()
    mass.signal_event = MagicMock()
    mass.get_providers = MagicMock(return_value=[])
    return mass


@pytest.fixture
def controller(mock_mass: MagicMock) -> PlayerController:
    """Create a PlayerController instance."""
    return PlayerController(mock_mass)


class TestCanGroupWithBasics:
    """Test basic can_group_with filtering logic."""

    def test_ungrouped_players_can_group(self, mock_mass: MagicMock) -> None:
        """Test that two ungrouped players can group with each other."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        player_a = MockPlayer(provider, "player_a", "Player A")
        player_a._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        # Use explicit player IDs instead of provider instance ID for simpler test
        player_a._attr_can_group_with = {"player_b"}

        player_b = MockPlayer(provider, "player_b", "Player B")
        player_b._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        player_b._attr_can_group_with = {"player_a"}

        controller._players = {"player_a": player_a, "player_b": player_b}
        mock_mass.players = controller

        # Trigger state calculation
        player_a.update_state(signal_event=False)
        player_b.update_state(signal_event=False)

        # Both players should be able to group with each other
        assert "player_b" in player_a.state.can_group_with
        assert "player_a" in player_b.state.can_group_with

    def test_unavailable_players_excluded(self, mock_mass: MagicMock) -> None:
        """Test that unavailable players are excluded from can_group_with."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        player_a = MockPlayer(provider, "player_a", "Player A")
        player_a._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        player_a._attr_can_group_with = {"player_b"}

        player_b = MockPlayer(provider, "player_b", "Player B")
        player_b._attr_available = False  # Mark as unavailable

        controller._players = {"player_a": player_a, "player_b": player_b}
        mock_mass.players = controller

        # Trigger state calculation
        player_a.update_state(signal_event=False)
        player_b.update_state(signal_event=False)

        # Unavailable player should be excluded
        assert "player_b" not in player_a.state.can_group_with

    def test_playing_players_with_different_source_excluded(self, mock_mass: MagicMock) -> None:
        """Test that players playing different sources are NOT excluded (behavior changed).

        Note: Previously, players with different active sources were excluded from grouping,
        but this was removed as it was difficult to track reliably.
        """
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        player_a = MockPlayer(provider, "player_a", "Player A")
        player_a._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        player_a._attr_can_group_with = {"player_b"}
        player_a._attr_playback_state = PlaybackState.PLAYING
        player_a._attr_active_source = "player_a"

        player_b = MockPlayer(provider, "player_b", "Player B")
        player_b._attr_playback_state = PlaybackState.PLAYING
        player_b._attr_active_source = "player_b"  # Different source

        controller._players = {"player_a": player_a, "player_b": player_b}
        mock_mass.players = controller

        # Trigger state calculation
        player_a.update_state(signal_event=False)
        player_b.update_state(signal_event=False)

        # Player with different active source is now ALLOWED (behavior changed)
        assert "player_b" in player_a.state.can_group_with


class TestSyncedPlayers:
    """Test behavior with synced/grouped players."""

    def test_sync_leader_excludes_itself_from_members_can_group_with(
        self, mock_mass: MagicMock
    ) -> None:
        """Test that sync leader doesn't appear in its members' can_group_with."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        leader = MockPlayer(provider, "leader", "Leader")
        leader._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        leader._attr_can_group_with = {"member"}
        leader._attr_group_members = ["leader", "member"]

        member = MockPlayer(provider, "member", "Member")

        controller._players = {"leader": leader, "member": member}
        mock_mass.players = controller

        # Trigger synced_to calculation
        leader.update_state(signal_event=False)
        member.update_state(signal_event=False)

        # Member is synced, so can_group_with should be empty
        assert member.state.can_group_with == set()

    def test_group_members_included_in_leader_can_group_with(self, mock_mass: MagicMock) -> None:
        """
        Test that group members appear in sync leader's can_group_with.

        This allows ungrouping members from the leader.
        """
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        leader = MockPlayer(provider, "leader", "Leader")
        leader._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        leader._attr_can_group_with = {"member_a", "member_b"}
        leader._attr_group_members = ["leader", "member_a", "member_b"]

        member_a = MockPlayer(provider, "member_a", "Member A")
        member_b = MockPlayer(provider, "member_b", "Member B")

        controller._players = {
            "leader": leader,
            "member_a": member_a,
            "member_b": member_b,
        }
        mock_mass.players = controller

        # Trigger synced_to calculation
        leader.update_state(signal_event=False)
        member_a.update_state(signal_event=False)
        member_b.update_state(signal_event=False)

        # Leader should be able to see its own members (for ungrouping)
        assert "member_a" in leader.state.can_group_with
        assert "member_b" in leader.state.can_group_with


class TestSyncLeaderBehavior:
    """Test sync leader specific behavior."""

    def test_sync_leader_excluded_from_can_group_with(self, mock_mass: MagicMock) -> None:
        """Test that players with group members (sync leaders) are excluded."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        leader = MockPlayer(provider, "leader", "Leader")
        leader._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        leader._attr_can_group_with = {"member", "other"}
        leader._attr_group_members = ["leader", "member"]
        leader._attr_playback_state = PlaybackState.PLAYING  # Make it playing so it gets excluded

        member = MockPlayer(provider, "member", "Member")

        other = MockPlayer(provider, "other", "Other")
        other._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        other._attr_can_group_with = {"leader", "member"}

        controller._players = {"leader": leader, "member": member, "other": other}
        mock_mass.players = controller

        # Trigger synced_to calculation
        leader.update_state(signal_event=False)
        member.update_state(signal_event=False)
        other.update_state(signal_event=False)

        # Leader should NOT appear in other's can_group_with (has group members)
        assert "leader" not in other.state.can_group_with


class TestCircularDependency:
    """Test that circular dependencies are avoided."""

    def test_no_circular_dependency_in_synced_to(self, mock_mass: MagicMock) -> None:
        """
        Test that synced_to calculation doesn't cause circular dependency.

        Regression test for: synced_to calling group_members causing infinite recursion.
        """
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        leader = MockPlayer(provider, "leader", "Leader")
        leader._attr_group_members = ["leader", "member"]

        member = MockPlayer(provider, "member", "Member")

        controller._players = {"leader": leader, "member": member}
        mock_mass.players = controller

        # Trigger synced_to calculation via update_state
        leader.update_state(signal_event=False)
        member.update_state(signal_event=False)

        # This should not cause infinite recursion
        assert member.state.synced_to == "leader"
        assert leader.state.synced_to is None


class TestCacheInvalidation:
    """Test that caches are invalidated correctly."""

    def test_can_group_with_cache_cleared_on_update_state(self, mock_mass: MagicMock) -> None:
        """Test that can_group_with cache is cleared when update_state is called."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        player_a = MockPlayer(provider, "player_a", "Player A")
        player_a._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        player_a._attr_can_group_with = {"player_b"}

        player_b = MockPlayer(provider, "player_b", "Player B")

        controller._players = {"player_a": player_a, "player_b": player_b}
        mock_mass.players = controller

        # Update state after setting attributes and registering with controller
        player_a.update_state(signal_event=False)
        player_b.update_state(signal_event=False)

        # Get can_group_with to populate cache
        initial = player_a.state.can_group_with
        assert "player_b" in initial

        # Modify underlying data
        player_a._attr_can_group_with = set()

        # Cache should still have old value
        assert player_a.state.can_group_with == initial

        # Clear cache via update_state
        player_a.update_state(signal_event=False)

        # Cache should be cleared, new value should be returned
        assert player_a.state.can_group_with == set()


class TestProviderInstanceIdExpansion:
    """Test expansion of provider instance IDs in can_group_with."""

    def test_provider_instance_id_expands_to_all_players(self, mock_mass: MagicMock) -> None:
        """Test that provider instance IDs expand to all available players from that provider."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        player_a = MockPlayer(provider, "player_a", "Player A")
        player_a._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        player_a._attr_can_group_with = {"test"}  # Provider instance ID

        player_b = MockPlayer(provider, "player_b", "Player B")
        player_c = MockPlayer(provider, "player_c", "Player C")

        controller._players = {
            "player_a": player_a,
            "player_b": player_b,
            "player_c": player_c,
        }
        mock_mass.players = controller
        # Set up get_provider to return the provider for instance ID
        mock_mass.get_provider = MagicMock(return_value=provider)

        # Trigger state calculation
        player_a.update_state(signal_event=False)
        player_b.update_state(signal_event=False)
        player_c.update_state(signal_event=False)

        # Provider instance ID should expand to include all players from that provider
        can_group = player_a.state.can_group_with
        assert "player_b" in can_group
        assert "player_c" in can_group


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
