"""
Tests for player grouping logic (independent of protocols).

This module tests the core grouping behavior including:
- can_group_with filtering logic
- Group member inclusion/exclusion
- Sync leader behavior
- Group state transitions
- Cache invalidation
"""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.player_queue import PlayerQueue

from music_assistant.controllers.player_queues.helpers import handle_play_action
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.players.constants import PlayerLockPurpose
from music_assistant.models.player import LinkedOutputProtocol
from tests.common import MockPlayer, MockProvider, use_real_create_task

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from music_assistant import MusicAssistant
    from music_assistant.controllers.player_queues import PlayerQueuesController
    from music_assistant.models.player import Player


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance."""
    mass = MagicMock()
    mass.closing = False
    mass.config = MagicMock()
    mass.config.get = MagicMock(return_value=[])

    def _get_raw_player_config_value(
        _player_id: str, key: str, default: str | int | None = None
    ) -> str | int | None:
        """Return appropriate defaults for player config values."""
        if key == "min_volume":
            return 0
        if key == "max_volume":
            return 100
        return default

    mass.config.get_raw_player_config_value = MagicMock(side_effect=_get_raw_player_config_value)
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


class SessionBoundMockPlayer(MockPlayer):
    """Mock player whose native members ride its own stream session (e.g. AirPlay)."""

    @property
    def native_grouping_requires_own_stream(self) -> bool:
        """Return True: native members are attached to this player's own stream session."""
        return True


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
        """
        Test that players playing different sources are NOT excluded (behavior changed).

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

    def test_solo_raw_group_members_does_not_exclude_a_playing_candidate(
        self, mock_mass: MagicMock
    ) -> None:
        """Test that a playing player whose raw group_members is only itself can still be grouped."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        # A detached client (e.g. a Sendspin client in a solo group) reports itself
        # as its only raw group member - that must not make it a group leader
        solo_candidate = MockPlayer(provider, "solo", "Solo")
        solo_candidate._attr_group_members = ["solo"]
        solo_candidate._attr_playback_state = PlaybackState.PLAYING

        real_leader = MockPlayer(provider, "leader", "Leader")
        real_leader._attr_group_members = ["leader", "member"]
        real_leader._attr_playback_state = PlaybackState.PLAYING
        member = MockPlayer(provider, "member", "Member")

        other = MockPlayer(provider, "other", "Other")
        other._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        other._attr_can_group_with = {"solo", "leader"}

        controller._players = {
            "solo": solo_candidate,
            "leader": real_leader,
            "member": member,
            "other": other,
        }
        mock_mass.players = controller

        for player in (solo_candidate, real_leader, member, other):
            player.update_state(signal_event=False)

        # Solo player is still offered, a real (multi-member) leader stays excluded
        assert "solo" in other.state.can_group_with
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

        # Mark players as initialized so they are returned by all_players()
        leader.set_initialized()
        member.set_initialized()

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

        # Mark players as initialized so they are returned by all_players()
        player_a.set_initialized()
        player_b.set_initialized()
        player_c.set_initialized()

        # Trigger state calculation
        player_a.update_state(signal_event=False)
        player_b.update_state(signal_event=False)
        player_c.update_state(signal_event=False)

        # Provider instance ID should expand to include all players from that provider
        can_group = player_a.state.can_group_with
        assert "player_b" in can_group
        assert "player_c" in can_group

    def test_provider_instance_id_excludes_unknown_players(self, mock_mass: MagicMock) -> None:
        """Test that players without an output type are not offered as grouping targets."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        mock_mass.get_provider = MagicMock(return_value=provider)

        leader = MockPlayer(
            provider,
            "leader",
            "Leader",
            player_type=PlayerType.PROTOCOL,
        )
        leader._attr_can_group_with = {"test"}
        public_player = MockPlayer(provider, "public", "Public Player")
        unknown_player = MockPlayer(
            provider,
            "unknown",
            "Unknown Player",
            player_type=PlayerType.UNKNOWN,
        )
        controller._players = {
            "leader": leader,
            "public": public_player,
            "unknown": unknown_player,
        }
        mock_mass.players = controller

        for player in controller._players.values():
            player.set_initialized()
        for player in controller._players.values():
            player.update_state(signal_event=False)

        assert "public" in leader.state.can_group_with
        assert "unknown" not in leader.state.can_group_with

        leader._attr_can_group_with = {"unknown"}
        leader.update_state(signal_event=False, force_update=True)

        assert leader.state.can_group_with == set()


class TestFinalActiveGroupNewModel:
    """
    The active_group derivation respects is_active_session and the powered signal.

    Verifies the post-refactor contract:

    - A group whose ``powered`` attribute is ``False`` (e.g. user explicitly
      pinned it off via Fake control) never captures its members.
    - A group whose ``powered`` is ``True`` (fake-pinned on) captures members
      even without a live session.
    - A group with ``powered=None`` (no power control assigned) only captures
      members while ``is_active_session`` is ``True`` — i.e. while it has a
      sync_leader, an active stream, or a pending idle-grace task.
    """

    def test_dormant_group_does_not_capture_members(self, mock_mass: MagicMock) -> None:
        """No power signal, no session → member's active_group is None."""
        controller = PlayerController(mock_mass)
        group_provider = MockProvider("test_group", instance_id="test_group", mass=mock_mass)
        member_provider = MockProvider("test", instance_id="test", mass=mock_mass)

        group = MockPlayer(group_provider, "g1", "Group", player_type=PlayerType.GROUP)
        # explicitly "no opinion" on power (matches new default for groups)
        group._attr_powered = None
        # listed as a configured member, but no active session
        group._attr_group_members = ["member"]
        # is_active_session base default is False → group is dormant
        group._cache.clear()

        member = MockPlayer(member_provider, "member", "Member")

        controller._players = {"g1": group, "member": member}
        mock_mass.players = controller

        group.set_initialized()
        member.set_initialized()
        group.update_state(signal_event=False)
        member.update_state(signal_event=False)

        assert member.state.active_group is None

    def test_powered_true_group_captures_members(self, mock_mass: MagicMock) -> None:
        """Group with _attr_powered=True (fake pin) captures members regardless of session."""
        controller = PlayerController(mock_mass)
        group_provider = MockProvider("test_group", instance_id="test_group", mass=mock_mass)
        member_provider = MockProvider("test", instance_id="test", mass=mock_mass)

        group = MockPlayer(group_provider, "g1", "Group", player_type=PlayerType.GROUP)
        group._attr_powered = True
        group._attr_group_members = ["member"]
        group._cache.clear()

        member = MockPlayer(member_provider, "member", "Member")

        controller._players = {"g1": group, "member": member}
        mock_mass.players = controller

        group.set_initialized()
        member.set_initialized()
        group.update_state(signal_event=False)
        member.update_state(signal_event=False)

        assert member.state.active_group == "g1"

    def test_powered_false_group_does_not_capture_members(self, mock_mass: MagicMock) -> None:
        """Group with _attr_powered=False (explicit off) does not capture, even with a session."""
        controller = PlayerController(mock_mass)
        group_provider = MockProvider("test_group", instance_id="test_group", mass=mock_mass)
        member_provider = MockProvider("test", instance_id="test", mass=mock_mass)

        group = MockPlayer(group_provider, "g1", "Group", player_type=PlayerType.GROUP)
        group._attr_powered = False
        group._attr_group_members = ["member"]
        group._cache.clear()

        member = MockPlayer(member_provider, "member", "Member")

        controller._players = {"g1": group, "member": member}
        mock_mass.players = controller

        group.set_initialized()
        member.set_initialized()
        group.update_state(signal_event=False)
        member.update_state(signal_event=False)

        assert member.state.active_group is None

    def test_session_active_group_captures_members(self, mock_mass: MagicMock) -> None:
        """Group with powered=None but is_active_session=True captures members."""
        controller = PlayerController(mock_mass)
        group_provider = MockProvider("test_group", instance_id="test_group", mass=mock_mass)
        member_provider = MockProvider("test", instance_id="test", mass=mock_mass)

        # subclass MockPlayer to override is_active_session for this test
        class _SessionedGroup(MockPlayer):
            @property
            def is_active_session(self) -> bool:
                return True

        group = _SessionedGroup(group_provider, "g1", "Group", player_type=PlayerType.GROUP)
        group._attr_powered = None  # no opinion on power
        group._attr_group_members = ["member"]
        group._cache.clear()

        member = MockPlayer(member_provider, "member", "Member")

        controller._players = {"g1": group, "member": member}
        mock_mass.players = controller

        group.set_initialized()
        member.set_initialized()
        group.update_state(signal_event=False)
        member.update_state(signal_event=False)

        assert member.state.active_group == "g1"


class _LockingGroup(MockPlayer):
    """Group player that locks its sync leader from inside set_members, like a syncgroup."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the group player."""
        super().__init__(*args, **kwargs)
        self.leader_id = ""
        # entered/release let a test hold this group inside set_members, standing in
        # for the power-on of a joining member that the real controller awaits there
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Apply the member change on the sync leader, under the leader's lock."""
        self.entered.set()
        await self.release.wait()
        async with self.mass.players.get_player_lock(self.leader_id, PlayerLockPurpose.PLAYBACK):
            await super().set_members(player_ids_to_add, player_ids_to_remove)
            # publish the change the way a provider's state push would, so a command
            # waiting for the member to report its new group state sees it
            for player in self.mass.players.iter_players():
                player.update_state(force_update=True)


def _spy_on_lock_order(controller: PlayerController) -> list[str]:
    """Replace the controller's get_player_lock with a recording wrapper."""
    lock_keys: list[str] = []
    acquire_lock = controller.get_player_lock

    def _record(
        player_id: str,
        purpose: PlayerLockPurpose = PlayerLockPurpose.PLAYBACK,
        strict: bool = False,
    ) -> AbstractAsyncContextManager[None]:
        # this records the order the locks are requested in, which is the order they
        # are entered in as well here: nothing else holds them in these tests
        lock_keys.append(f"{purpose.value}_{player_id}")
        return acquire_lock(player_id, purpose, strict=strict)

    controller.get_player_lock = _record  # type: ignore[assignment]
    return lock_keys


class _PlayingQueues:
    """Queue controller stand-in exposing what handle_play_action touches."""

    mass: MusicAssistant

    def __init__(self, mass: MagicMock, queue_id: str) -> None:
        """Initialize the stand-in with one queue."""
        self.mass = mass
        queue = PlayerQueue(
            queue_id=queue_id, active=True, display_name=queue_id, available=True, items=0
        )
        self._queue_data = {queue_id: PlayerQueueData(queue=queue)}

    def get(self, queue_id: str) -> PlayerQueue | None:
        """Return the queue, if it is the one held."""
        queue_data = self._queue_data.get(queue_id)
        return queue_data.queue if queue_data else None

    @handle_play_action
    async def resume(self, queue_id: str) -> None:
        """Resume the queue: a play action, so it runs under the group and player lock."""

    def signal_update(self, queue_id: str, items_changed: bool = False) -> None:
        """Ignore the queue updates."""

    def on_player_update(self, player: Player, changed_values: dict[str, tuple[Any, Any]]) -> None:
        """Ignore the player updates."""


@handle_play_action
async def _play_on_queue(self: PlayerQueuesController, queue_id: str) -> None:
    """Start playback on the queue's player, the way play_index does."""
    await self.mass.players.play_media(queue_id, MagicMock(uri="x", source_id=queue_id))


class _PowerablePlayer(MockPlayer):
    """Player with native power control that records the power commands it received."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the player."""
        super().__init__(*args, **kwargs)
        self._attr_supported_features = {PlayerFeature.POWER, PlayerFeature.SET_MEMBERS}
        self.power_commands: list[bool] = []
        self._cache.clear()

    async def power(self, powered: bool) -> None:
        """Apply the power command."""
        self.power_commands.append(powered)
        self._attr_powered = powered
        self._cache.clear()


class TestGroupAndMemberLockOrder:
    """
    A command on a group member takes the group's lock before the member's own.

    The group's own set_members locks its sync leader, so a command that locks a
    member first and only then reaches the group ends up in the opposite order and
    the two commands lock each other out. A power off, an announcement and a play
    action on the member's own queue all reach the group.
    """

    def _setup(
        self, mock_mass: MagicMock
    ) -> tuple[PlayerController, _LockingGroup, MockPlayer, MockPlayer]:
        controller = PlayerController(mock_mass)
        group_provider = MockProvider("test_group", instance_id="test_group", mass=mock_mass)
        member_provider = MockProvider("test", instance_id="test", mass=mock_mass)

        group = _LockingGroup(group_provider, "g1", "Group", player_type=PlayerType.GROUP)
        group._attr_powered = True
        group._attr_group_members = ["member"]
        group._attr_supported_features = {PlayerFeature.SET_MEMBERS}
        group._attr_can_group_with = {"member", "joiner"}
        group.leader_id = "member"

        member = MockPlayer(member_provider, "member", "Member")
        joiner = MockPlayer(member_provider, "joiner", "Joiner")

        controller._players = {"g1": group, "member": member, "joiner": joiner}
        mock_mass.players = controller
        for player in controller._players.values():
            player._cache.clear()
            player.set_initialized()
            player.update_state(signal_event=False)

        assert member.state.active_group == "g1"
        return controller, group, member, joiner

    @staticmethod
    def _add_follower(controller: PlayerController, leader: MockPlayer) -> MockPlayer:
        """Sync "extra" to the given group member, which makes that member a captured leader."""
        extra = MockPlayer(leader.provider, "extra", "Extra")  # type: ignore[arg-type]
        leader._attr_supported_features = {PlayerFeature.SET_MEMBERS}
        leader._attr_group_members = [leader.player_id, "extra"]
        controller._players["extra"] = extra
        extra.set_initialized()
        for player in controller._players.values():
            player._cache.clear()
            player.update_state(signal_event=False)
        assert extra.state.synced_to == leader.player_id
        assert leader.state.active_group == "g1"
        return extra

    def _setup_announcement(
        self, mock_mass: MagicMock
    ) -> tuple[PlayerController, _LockingGroup, MockPlayer]:
        """Set up the group member so an announcement runs the default implementation on it."""
        controller, group, member, _ = self._setup(mock_mass)
        use_real_create_task(mock_mass)
        render = MagicMock()
        render.wait_ready = AsyncMock(return_value=True)
        render.wait_finished = AsyncMock(return_value=1.0)
        renderer = mock_mass.streams.announcement_renderer
        renderer.register = MagicMock(return_value=render)
        renderer.get = MagicMock(return_value=render)
        renderer.unregister = AsyncMock()
        mock_mass.streams.get_announcement_url = MagicMock(return_value="http://ma/announce.mp3")
        # the clip itself is not under test: the device plays it and reports back
        # right away, at whatever volume it has
        controller._handle_play_media = AsyncMock()  # type: ignore[method-assign]
        controller._wait_for_playback_state = AsyncMock()  # type: ignore[method-assign]
        controller._unmute_and_set_announcement_volume = AsyncMock()  # type: ignore[method-assign]
        return controller, group, member

    async def _assert_no_lockout_with_a_join(
        self,
        controller: PlayerController,
        group: _LockingGroup,
        command: Coroutine[Any, Any, None],
    ) -> None:
        """Run the command against a join that holds the group's lock; both must complete."""
        join = asyncio.create_task(controller.cmd_set_members("g1", player_ids_to_add=["joiner"]))
        task: asyncio.Task[None] | None = None
        try:
            # the join holds the group's lock and is parked inside set_members
            await group.entered.wait()
            task = asyncio.create_task(command)
            # let the command get as far as it can before the join continues
            for _ in range(10):
                await asyncio.sleep(0)
            # the command is parked on the group's lock, which the join still holds
            assert not task.done()
            assert controller._player_command_locks["playback_g1"].locked()
            group.release.set()
            # the join now wants the member's lock: a deadlock never resolves,
            # so a timeout here is the assertion
            async with asyncio.timeout(2):
                await asyncio.gather(join, task)
        finally:
            for pending in (join, task):
                if pending is not None and not pending.done():
                    pending.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await pending

    async def test_a_power_off_and_a_join_do_not_lock_each_other_out(
        self, mock_mass: MagicMock
    ) -> None:
        """Powering off a group member while another player joins must not deadlock."""
        controller, group, _, _ = self._setup(mock_mass)

        await self._assert_no_lockout_with_a_join(
            controller, group, controller.cmd_power("member", False)
        )

    async def test_a_power_off_takes_the_group_lock_first(self, mock_mass: MagicMock) -> None:
        """The group's lock is acquired before the member's own, never the other way round."""
        controller, group, _, _ = self._setup(mock_mass)
        group.release.set()
        lock_keys = _spy_on_lock_order(controller)

        await controller.cmd_power("member", False)

        assert lock_keys[:2] == ["playback_g1", "playback_member"]

    async def test_a_power_off_follows_the_set_members_redirect(self, mock_mass: MagicMock) -> None:
        """A player synced to a captured leader locks that leader's group, not the leader."""
        controller, group, member, _ = self._setup(mock_mass)
        group.release.set()
        # "extra" is synced to the group's own member, so removing it is redirected
        # to the group player - which is the lock that has to be taken first
        self._add_follower(controller, member)
        lock_keys = _spy_on_lock_order(controller)

        await controller.cmd_power("extra", False)

        assert lock_keys[:2] == ["playback_g1", "playback_extra"]

    async def test_redirect_lock_and_set_members_agree_on_the_owner(
        self, mock_mass: MagicMock
    ) -> None:
        """Commands, locks and member changes for a captured leader's follower reach its group."""
        controller, group, member, _ = self._setup(mock_mass)
        self._add_follower(controller, member)
        lock_keys = _spy_on_lock_order(controller)
        controller._handle_set_members = AsyncMock()  # type: ignore[method-assign]

        assert controller._get_player_with_redirect("extra") is group
        async with controller.get_group_and_player_lock("extra"):
            pass
        await controller.cmd_set_members("member", player_ids_to_remove=["extra"])

        assert lock_keys[:2] == ["playback_g1", "playback_extra"]
        controller._handle_set_members.assert_awaited_once_with(group, None, ["extra"])

    async def test_a_leader_power_off_leaves_its_followers_powered(
        self, mock_mass: MagicMock
    ) -> None:
        """A leader's power off releases its followers but leaves their power alone."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test", instance_id="test", mass=mock_mass)

        leader = _PowerablePlayer(provider, "leader", "Leader")
        leader._attr_can_group_with = {"follower"}
        leader._attr_group_members = ["leader", "follower"]
        follower = _PowerablePlayer(provider, "follower", "Follower")

        controller._players = {"leader": leader, "follower": follower}
        mock_mass.players = controller
        mock_mass.player_queues.get = MagicMock(return_value=None)
        for player in controller._players.values():
            player._cache.clear()
            player.set_initialized()
            player.update_state(signal_event=False)

        assert follower.state.synced_to == "leader"

        # the release runs from under the locks the power off holds, so a timeout
        # here would mean it waited on one of them
        async with asyncio.timeout(2):
            await controller.cmd_power("leader", False)

        assert leader.power_commands == [False]
        assert follower.power_commands == []

    async def test_an_announcement_and_a_group_command_do_not_lock_each_other_out(
        self, mock_mass: MagicMock
    ) -> None:
        """Announcing on a synced member while a group command runs must not deadlock."""
        controller, group, member = self._setup_announcement(mock_mass)
        # the announcement unsyncs "extra" through the group, and the concurrent command
        # needs "extra" from under the group's lock - a power off of that very player
        # does, the join here stands in for it
        self._add_follower(controller, member)
        group.leader_id = "extra"

        await self._assert_no_lockout_with_a_join(
            controller, group, controller.play_announcement("extra", "http://test/announce.mp3")
        )

    async def test_an_announcement_follows_the_set_members_redirect(
        self, mock_mass: MagicMock
    ) -> None:
        """An announcement on a player synced to a captured leader locks that leader's group."""
        controller, group, member = self._setup_announcement(mock_mass)
        group.release.set()
        self._add_follower(controller, member)
        lock_keys = _spy_on_lock_order(controller)

        await controller.play_announcement("extra", "http://test/announce.mp3")

        assert lock_keys[:2] == ["playback_g1", "playback_extra"]

    async def test_an_announcement_takes_the_group_lock_first(self, mock_mass: MagicMock) -> None:
        """An announcement on a group member locks the group before the member."""
        controller, group, _ = self._setup_announcement(mock_mass)
        group.release.set()
        lock_keys = _spy_on_lock_order(controller)

        await controller.play_announcement("member", "http://test/announce.mp3")

        assert lock_keys[:2] == ["playback_g1", "playback_member"]

    async def test_an_announcement_changes_the_membership_under_the_group_lock(
        self, mock_mass: MagicMock
    ) -> None:
        """The member leaves and rejoins its group through the controller, under its lock."""
        controller, group, member = self._setup_announcement(mock_mass)
        group.release.set()
        member_changes: list[tuple[str, list[str] | None, list[str] | None, bool]] = []
        set_members = controller.cmd_set_members

        async def _record(
            target_player: str,
            player_ids_to_add: list[str] | None = None,
            player_ids_to_remove: list[str] | None = None,
        ) -> None:
            member_changes.append(
                (
                    target_player,
                    player_ids_to_add,
                    player_ids_to_remove,
                    controller._player_command_locks["playback_g1"].locked(),
                )
            )
            await set_members(target_player, player_ids_to_add, player_ids_to_remove)

        controller.cmd_set_members = _record  # type: ignore[method-assign]

        await controller.play_announcement("member", "http://test/announce.mp3")

        assert member_changes == [
            ("g1", None, ["member"], True),
            ("g1", ["member"], None, True),
        ]
        assert member.state.active_group == "g1"

    async def test_a_play_action_and_a_join_do_not_lock_each_other_out(
        self, mock_mass: MagicMock
    ) -> None:
        """Playing on a captured member's own queue while another player joins must not deadlock."""
        controller, group, _, _ = self._setup(mock_mass)
        controller._handle_play_media = AsyncMock()  # type: ignore[method-assign]
        queues = cast("PlayerQueuesController", _PlayingQueues(mock_mass, "member"))

        await self._assert_no_lockout_with_a_join(
            controller, group, _play_on_queue(queues, "member")
        )

    async def test_a_play_action_takes_the_group_lock_first(self, mock_mass: MagicMock) -> None:
        """A play action on a captured member's own queue locks the group before the member."""
        controller, group, _, _ = self._setup(mock_mass)
        group.release.set()
        controller._handle_play_media = AsyncMock()  # type: ignore[method-assign]
        queues = cast("PlayerQueuesController", _PlayingQueues(mock_mass, "member"))
        lock_keys = _spy_on_lock_order(controller)

        await _play_on_queue(queues, "member")

        assert lock_keys[:2] == ["playback_g1", "playback_member"]

    async def test_a_resume_on_a_synced_player_and_a_join_do_not_lock_each_other_out(
        self, mock_mass: MagicMock
    ) -> None:
        """Resuming a player synced to a captured leader while another player joins must not deadlock."""
        controller, group, member, _ = self._setup(mock_mass)
        # "extra" hears the group's queue through "member", the group's captured leader,
        # so its resume lands on that queue and the play action takes the group's lock
        self._add_follower(controller, member)
        mock_mass.player_queues = _PlayingQueues(mock_mass, "g1")

        await self._assert_no_lockout_with_a_join(controller, group, controller.cmd_resume("extra"))

    async def test_a_resume_on_a_synced_player_locks_the_leader_group_not_the_leader(
        self, mock_mass: MagicMock
    ) -> None:
        """A resume on a player synced to a captured leader is run under that leader's group lock."""
        controller, _, member, _ = self._setup(mock_mass)
        self._add_follower(controller, member)
        mock_mass.player_queues = _PlayingQueues(mock_mass, "g1")
        lock_keys = _spy_on_lock_order(controller)

        await controller.cmd_resume("extra")

        assert lock_keys[0] == "playback_g1"
        assert "playback_member" not in lock_keys


class TestPlayerBaseIsActiveSession:
    """The Player base class defaults is_active_session to False; only groups override it."""

    def test_base_player_is_not_an_active_session(self, mock_mass: MagicMock) -> None:
        """A regular MockPlayer should never claim to hold a captured session."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        player = MockPlayer(provider, "p1", "P1")
        assert player.is_active_session is False


def _make_ad_hoc_group(
    controller: PlayerController, mock_mass: MagicMock, airplay_available: bool
) -> None:
    """
    Register an ad-hoc group playing over AirPlay, with only member "b" on that domain.

    Member "a" is a plain native player, member "b" is a native player with a linked
    AirPlay protocol player whose availability is driven by ``airplay_available``.
    """
    sonos = MockProvider("sonos", instance_id="sonos", mass=mock_mass)
    airplay = MockProvider("airplay", instance_id="airplay", mass=mock_mass)

    leader = MockPlayer(sonos, "leader", "Leader")
    leader_protocol = MockPlayer(
        airplay, "leader_airplay", "Leader AirPlay", player_type=PlayerType.PROTOCOL
    )
    leader.set_linked_output_protocols([_airplay_link(leader_protocol.player_id)])
    leader.set_active_output_protocol(leader_protocol.player_id)

    member_a = MockPlayer(sonos, "a", "Member A")
    member_b = MockPlayer(sonos, "b", "Member B")
    member_b_protocol = MockPlayer(
        airplay, "b_airplay", "B AirPlay", player_type=PlayerType.PROTOCOL
    )
    member_b_protocol._attr_available = airplay_available
    member_b.set_linked_output_protocols([_airplay_link(member_b_protocol.player_id)])

    for player in (leader, member_a, member_b):
        player._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        player._cache.clear()

    controller._players = {
        p.player_id: p for p in (leader, leader_protocol, member_a, member_b, member_b_protocol)
    }
    mock_mass.players = controller


def _airplay_link(protocol_id: str) -> LinkedOutputProtocol:
    """Build an AirPlay link to the given protocol player."""
    return LinkedOutputProtocol(
        output_protocol_id=protocol_id,
        protocol_domain="airplay",
        priority=10,
    )


def _queue_stub(queue_id: str, state: PlaybackState = PlaybackState.PLAYING) -> MagicMock:
    """
    Build a queue stub carrying the id and state the set_members path reads.

    :param queue_id: The id the queue reports, i.e. the player it belongs to.
    :param state: Playback state the queue reports.
    """
    queue = MagicMock()
    queue.queue_id = queue_id
    queue.state = state
    return queue


def _ad_hoc_leader(
    mock_mass: MagicMock, member_type: PlayerType = PlayerType.VISUALIZER
) -> tuple[PlayerController, MockPlayer, AsyncMock, AsyncMock]:
    """
    Build a sync leader with a single member, ready to be removed from itself.

    A non-audio member leaves no playback heir, so the group dissolves; pass
    ``PlayerType.PLAYER`` to get a heir and reach the leadership transfer instead.

    :param mock_mass: The mocked MusicAssistant instance to attach the controller to.
    :param member_type: Type to register the group member as.
    :return: The controller, the leader, its stubbed device stop and queue stop.
    """
    controller = PlayerController(mock_mass)
    provider = MockProvider("test", instance_id="test", mass=mock_mass)

    leader = MockPlayer(provider, "leader", "Leader")
    leader._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
    leader._attr_can_group_with = {"member"}
    leader._attr_group_members = ["leader", "member"]
    member = MockPlayer(provider, "member", "Member", player_type=member_type)

    controller._players = {"leader": leader, "member": member}
    mock_mass.players = controller
    # refresh each player's state snapshot so it reflects the mocked player type
    for player in (leader, member):
        player.update_state(signal_event=False)

    controller._handle_set_members_with_protocols = AsyncMock()  # type: ignore[method-assign]
    device_stop = AsyncMock()
    controller._handle_cmd_stop = device_stop  # type: ignore[method-assign]
    queue_stop = AsyncMock()
    mock_mass.player_queues._handle_stop = queue_stop
    return controller, leader, device_stop, queue_stop


class TestAdHocLeadershipTransfer:
    """Unjoining an ad-hoc sync leader transfers leadership instead of dissolving."""

    def test_select_ad_hoc_leader_prefers_active_protocol(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """The new leader should be a member that supports the group's active protocol."""
        # member_a can't do airplay, member_b has an airplay protocol player that is up
        _make_ad_hoc_group(controller, mock_mass, airplay_available=True)

        leader = controller.get_player("leader")
        assert leader is not None
        assert controller._select_ad_hoc_leader(leader, ["a", "b"]) == "b"

    def test_select_ad_hoc_leader_skips_offline_protocol(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """A member whose protocol player went offline must not inherit the session."""
        # member_b still claims an airplay link, but its protocol player is gone
        _make_ad_hoc_group(controller, mock_mass, airplay_available=False)

        leader = controller.get_player("leader")
        assert leader is not None

        assert controller._select_ad_hoc_leader(leader, ["a", "b"]) == "a"

    def test_select_ad_hoc_leader_accepts_native_member_on_active_domain(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """A member that plays the active protocol natively is still a valid leader."""
        # a Chromecast speaker has no linked protocol player: it *is* the chromecast output
        chromecast = MockProvider("chromecast", instance_id="chromecast", mass=mock_mass)
        sonos = MockProvider("sonos", instance_id="sonos", mass=mock_mass)

        leader = MockPlayer(sonos, "leader", "Leader")
        leader_protocol = MockPlayer(
            chromecast, "leader_cast", "Leader Cast", player_type=PlayerType.PROTOCOL
        )
        leader.set_linked_output_protocols(
            [
                LinkedOutputProtocol(
                    output_protocol_id=leader_protocol.player_id,
                    protocol_domain="chromecast",
                    priority=30,
                )
            ]
        )
        leader.set_active_output_protocol(leader_protocol.player_id)

        member_a = MockPlayer(sonos, "a", "Member A")
        member_c = MockPlayer(chromecast, "c", "Member C")
        for player in (leader, member_a, member_c):
            player._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
            player._cache.clear()

        controller._players = {
            p.player_id: p for p in (leader, leader_protocol, member_a, member_c)
        }
        mock_mass.players = controller

        assert controller._select_ad_hoc_leader(leader, ["a", "c"]) == "c"

    def test_select_ad_hoc_leader_falls_back_to_first(self, controller: PlayerController) -> None:
        """Without an active protocol to match, fall back to the first remaining member."""
        leader = MagicMock()
        leader.active_output_protocol = None
        member_a = MagicMock()
        member_a.state.type = PlayerType.PLAYER
        member_b = MagicMock()
        member_b.state.type = PlayerType.PLAYER
        controller._players = {"a": member_a, "b": member_b}

        assert controller._select_ad_hoc_leader(leader, ["a", "b"]) == "a"

    def test_select_ad_hoc_leader_never_picks_non_audio_member(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """A visualizer listed before an audio member must never inherit the queue."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        visualizer = MockPlayer(provider, "viz", "Visualizer", player_type=PlayerType.VISUALIZER)
        member_b = MockPlayer(provider, "b", "Member B")
        controller._players = {"viz": visualizer, "b": member_b}
        # refresh each player's state snapshot so it reflects the mocked player type
        for player in (visualizer, member_b):
            player.update_state(signal_event=False)

        leader = MagicMock()
        leader.active_output_protocol = None

        assert controller._select_ad_hoc_leader(leader, ["viz", "b"]) == "b"

    async def test_handle_set_members_transfers_leader_when_playing(
        self, mock_mass: MagicMock
    ) -> None:
        """Removing the leader from itself while playing routes to a leadership transfer."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test", instance_id="test", mass=mock_mass)

        leader = MockPlayer(provider, "leader", "Leader")
        leader._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        leader._attr_can_group_with = {"member_a", "member_b"}
        leader._attr_group_members = ["leader", "member_a", "member_b"]
        member_a = MockPlayer(provider, "member_a", "Member A")
        member_b = MockPlayer(provider, "member_b", "Member B")

        controller._players = {"leader": leader, "member_a": member_a, "member_b": member_b}
        mock_mass.players = controller
        leader.update_state(signal_event=False)

        controller.get_active_queue = MagicMock(return_value=_queue_stub("leader"))  # type: ignore[method-assign]
        controller._transfer_ad_hoc_leadership = AsyncMock()  # type: ignore[method-assign]

        await controller._handle_set_members(leader, player_ids_to_remove=["leader"])

        controller._transfer_ad_hoc_leadership.assert_awaited_once()
        called_leader, called_remaining = controller._transfer_ad_hoc_leadership.call_args.args
        assert called_leader is leader
        assert set(called_remaining) == {"member_a", "member_b"}

    async def test_handle_set_members_keeps_non_audio_member_as_follower(
        self, mock_mass: MagicMock
    ) -> None:
        """A non-audio member does not block the transfer and stays in the regroup set."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test", instance_id="test", mass=mock_mass)

        leader = MockPlayer(provider, "leader", "Leader")
        leader._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        leader._attr_can_group_with = {"member_a", "visualizer"}
        leader._attr_group_members = ["leader", "member_a", "visualizer"]
        member_a = MockPlayer(provider, "member_a", "Member A")
        visualizer = MockPlayer(
            provider, "visualizer", "Visualizer", player_type=PlayerType.VISUALIZER
        )

        controller._players = {
            "leader": leader,
            "member_a": member_a,
            "visualizer": visualizer,
        }
        mock_mass.players = controller
        # refresh each player's state snapshot so it reflects the mocked player type
        for player in (leader, member_a, visualizer):
            player.update_state(signal_event=False)

        controller.get_active_queue = MagicMock(return_value=_queue_stub("leader"))  # type: ignore[method-assign]
        controller._transfer_ad_hoc_leadership = AsyncMock()  # type: ignore[method-assign]

        await controller._handle_set_members(leader, player_ids_to_remove=["leader"])

        controller._transfer_ad_hoc_leadership.assert_awaited_once()
        _, called_remaining = controller._transfer_ad_hoc_leadership.call_args.args
        # the visualizer stays a group member (it follows the new leader), the
        # heir itself is picked from the audio-capable members only
        assert set(called_remaining) == {"member_a", "visualizer"}

    async def test_handle_set_members_dissolves_when_only_non_audio_members_remain(
        self, mock_mass: MagicMock
    ) -> None:
        """With only non-audio members left there is no heir: dissolve and stop."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test", instance_id="test", mass=mock_mass)

        leader = MockPlayer(provider, "leader", "Leader")
        leader._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        leader._attr_can_group_with = {"visualizer"}
        leader._attr_group_members = ["leader", "visualizer"]
        visualizer = MockPlayer(
            provider, "visualizer", "Visualizer", player_type=PlayerType.VISUALIZER
        )

        controller._players = {"leader": leader, "visualizer": visualizer}
        mock_mass.players = controller
        # refresh each player's state snapshot so it reflects the mocked player type
        for player in (leader, visualizer):
            player.update_state(signal_event=False)

        controller.get_active_queue = MagicMock(return_value=_queue_stub("leader"))  # type: ignore[method-assign]
        controller._transfer_ad_hoc_leadership = AsyncMock()  # type: ignore[method-assign]
        controller._handle_set_members_with_protocols = AsyncMock()  # type: ignore[method-assign]
        controller._handle_cmd_stop = AsyncMock()  # type: ignore[method-assign]
        queue_stop = AsyncMock()
        mock_mass.player_queues._handle_stop = queue_stop

        await controller._handle_set_members(leader, player_ids_to_remove=["leader"])

        controller._transfer_ad_hoc_leadership.assert_not_awaited()
        # the dissolve removes the visualizer from the group and ends the leader's queue
        controller._handle_set_members_with_protocols.assert_awaited_once_with(
            leader, [], ["visualizer"], new_content=False
        )
        queue_stop.assert_awaited_once_with("leader")
        controller._handle_cmd_stop.assert_not_awaited()

    async def test_handle_set_members_dissolves_leader_when_idle(
        self, mock_mass: MagicMock
    ) -> None:
        """Removing the leader from itself while idle dissolves the group and stops."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test", instance_id="test", mass=mock_mass)

        leader = MockPlayer(provider, "leader", "Leader")
        leader._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        leader._attr_can_group_with = {"member_a"}
        leader._attr_group_members = ["leader", "member_a"]
        member_a = MockPlayer(provider, "member_a", "Member A")

        controller._players = {"leader": leader, "member_a": member_a}
        mock_mass.players = controller
        leader.update_state(signal_event=False)

        controller.get_active_queue = MagicMock(  # type: ignore[method-assign]
            return_value=_queue_stub("leader", state=PlaybackState.IDLE)
        )
        controller._transfer_ad_hoc_leadership = AsyncMock()  # type: ignore[method-assign]
        controller._handle_set_members_with_protocols = AsyncMock()  # type: ignore[method-assign]
        controller._handle_cmd_stop = AsyncMock()  # type: ignore[method-assign]
        queue_stop = AsyncMock()
        mock_mass.player_queues._handle_stop = queue_stop

        await controller._handle_set_members(leader, player_ids_to_remove=["leader"])

        controller._transfer_ad_hoc_leadership.assert_not_awaited()
        # an idle queue is where leftovers hide: its session and item buffers outlive
        # the playback that already ended, so the dissolve tears them down too
        queue_stop.assert_awaited_once_with("leader")
        controller._handle_cmd_stop.assert_not_awaited()

    async def test_transfer_stops_nothing_on_the_way_out(self, mock_mass: MagicMock) -> None:
        """
        Handing the group to a new leader must not stop anything here.

        transfer_queue moves the playback position to the new leader and stops the old
        one itself, so a stop issued here would land on playback that already moved.
        """
        controller, leader, device_stop, queue_stop = _ad_hoc_leader(
            mock_mass, member_type=PlayerType.PLAYER
        )
        controller.get_active_queue = MagicMock(return_value=_queue_stub("leader"))  # type: ignore[method-assign]
        controller._transfer_ad_hoc_leadership = AsyncMock()  # type: ignore[method-assign]

        await controller._handle_set_members(leader, player_ids_to_remove=["leader"])

        controller._transfer_ad_hoc_leadership.assert_awaited_once_with(leader, ["member"])
        device_stop.assert_not_awaited()
        queue_stop.assert_not_awaited()

    async def test_a_groups_leader_hands_over_nothing(self, mock_mass: MagicMock) -> None:
        """
        The member a group player elected as its leader has no queue of its own to hand over.

        Its followers ride the group's queue and may still show on it for a while after
        that group dissolved around it. Removing it from itself dissolves what is left,
        rather than moving the group's queue onto one of the followers.
        """
        controller, leader, device_stop, queue_stop = _ad_hoc_leader(
            mock_mass, member_type=PlayerType.PLAYER
        )
        controller.get_active_queue = MagicMock(return_value=_queue_stub("group"))  # type: ignore[method-assign]
        controller._transfer_ad_hoc_leadership = AsyncMock()  # type: ignore[method-assign]

        await controller._handle_set_members(leader, player_ids_to_remove=["leader"])

        controller._transfer_ad_hoc_leadership.assert_not_awaited()
        protocol_set_members = cast("AsyncMock", controller._handle_set_members_with_protocols)
        # dissolving what is left publishes no new content, so no takeover is opened
        protocol_set_members.assert_awaited_once_with(leader, [], ["member"], new_content=False)
        # the group's queue is not the leader's to end, so only the device is stopped
        device_stop.assert_awaited_once_with("leader")
        queue_stop.assert_not_awaited()


class TestDissolvedLeaderEndsTheQueue:
    """
    Dissolving a sync leader has to end the queue it was playing, not just the device.

    Stopping only the device leaves the queue session open, so its preloading keeps
    pulling audio and a provider serving a live session (Spotify) stays tethered to
    Music Assistant for another track or two.
    """

    async def test_dissolve_ends_the_leaders_own_queue(self, mock_mass: MagicMock) -> None:
        """A leader dissolved mid-playback has its own queue ended, not just its device."""
        controller, leader, device_stop, queue_stop = _ad_hoc_leader(mock_mass)
        controller.get_active_queue = MagicMock(return_value=_queue_stub("leader"))  # type: ignore[method-assign]

        await controller._handle_set_members(leader, player_ids_to_remove=["leader"])

        queue_stop.assert_awaited_once_with("leader")
        # the queue stop issues the device stop itself
        device_stop.assert_not_awaited()

    async def test_dissolve_leaves_another_players_queue_alone(self, mock_mass: MagicMock) -> None:
        """
        A leader resolving to someone else's queue only gets its device stopped.

        get_active_queue follows a sync link or protocol parent, and that queue is
        playing for other players: ending it here would stop them too.
        """
        controller, leader, device_stop, queue_stop = _ad_hoc_leader(mock_mass)
        controller.get_active_queue = MagicMock(return_value=_queue_stub("other_player"))  # type: ignore[method-assign]

        await controller._handle_set_members(leader, player_ids_to_remove=["leader"])

        device_stop.assert_awaited_once_with("leader")
        queue_stop.assert_not_awaited()

    async def test_dissolve_without_a_queue_stops_the_device(self, mock_mass: MagicMock) -> None:
        """A leader playing a live external source has no queue to end."""
        controller, leader, device_stop, queue_stop = _ad_hoc_leader(mock_mass)
        controller.get_active_queue = MagicMock(return_value=None)  # type: ignore[method-assign]

        await controller._handle_set_members(leader, player_ids_to_remove=["leader"])

        device_stop.assert_awaited_once_with("leader")
        queue_stop.assert_not_awaited()


class TestSessionBoundLeaderJoinsAnotherGroup:
    """A leader whose members ride its own stream gives that group up when it joins another."""

    def _build(
        self, mock_mass: MagicMock, leader_class: type[MockPlayer]
    ) -> tuple[PlayerController, MockPlayer, MockPlayer, MockPlayer]:
        """Build an idle leader with one native member, plus the group it is about to join."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("airplay", instance_id="airplay", mass=mock_mass)

        leader = leader_class(provider, "leader", "Living Room")
        leader._attr_supported_features |= {PlayerFeature.SET_MEMBERS, PlayerFeature.PLAY_MEDIA}
        leader._attr_can_group_with = {"member", "target"}
        leader._attr_group_members = ["leader", "member"]

        member = MockPlayer(provider, "member", "Kitchen")
        member._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)

        target = MockPlayer(provider, "target", "Study")
        target._attr_supported_features |= {PlayerFeature.SET_MEMBERS, PlayerFeature.PLAY_MEDIA}
        target._attr_can_group_with = {"leader", "member"}

        controller._players = {p.player_id: p for p in (leader, member, target)}
        mock_mass.players = controller
        for player in controller._players.values():
            player.set_initialized()
            player.update_state(signal_event=False)
        assert member.synced_to == "leader"
        return controller, leader, member, target

    async def test_own_group_is_dissolved_before_it_joins(self, mock_mass: MagicMock) -> None:
        """
        The members are released before the leader becomes a member itself.

        A native group outlives its session, so a leader that stopped still lists its
        members. Joining another group leaves it unable to serve them, and they would be
        silent while the UI still shows them grouped.
        """
        controller, leader, member, target = self._build(mock_mass, SessionBoundMockPlayer)

        await controller._handle_set_members(target, player_ids_to_add=["leader"])

        assert "member" not in leader.group_members
        assert member.synced_to is None
        assert target.group_members == ["target", "leader"]

    async def test_a_leader_without_members_is_left_alone(self, mock_mass: MagicMock) -> None:
        """A player that leads nothing has no group to give up."""
        controller, leader, _member, target = self._build(mock_mass, SessionBoundMockPlayer)
        leader._attr_group_members = []
        leader.update_state(signal_event=False)
        leader.set_members = AsyncMock()  # type: ignore[method-assign]

        await controller._handle_set_members(target, player_ids_to_add=["leader"])

        leader.set_members.assert_not_awaited()
        assert target.group_members == ["target", "leader"]

    async def test_a_playing_group_is_never_torn_down(self, mock_mass: MagicMock) -> None:
        """
        A leader still serving its own group stays out instead of silencing its members.

        Grouping never offers a rendering leader as a target, but the candidate list is a
        snapshot: it is only recomputed when a group membership or availability changes, so
        one taken before playback started still lists the leader.
        """
        controller, leader, member, target = self._build(mock_mass, SessionBoundMockPlayer)
        leader._attr_playback_state = PlaybackState.PLAYING
        leader.update_state(signal_event=False)
        leader.set_members = AsyncMock()  # type: ignore[method-assign]

        await controller._handle_set_members(target, player_ids_to_add=["leader"])

        leader.set_members.assert_not_awaited()
        assert target.group_members == []
        assert member.synced_to == "leader"

    async def test_a_group_that_cannot_be_dissolved_keeps_the_player_out(
        self, mock_mass: MagicMock
    ) -> None:
        """
        A leader that could not give its group up stays out instead of joining on top of it.

        Joining anyway leaves it leading a group it can no longer serve, and a player that
        is synced refuses every later member change, so the state cannot be cleaned up.
        """
        controller, leader, member, target = self._build(mock_mass, SessionBoundMockPlayer)
        leader.set_members = AsyncMock(side_effect=RuntimeError("speaker unreachable"))  # type: ignore[method-assign]

        await controller._handle_set_members(target, player_ids_to_add=["leader"])

        assert target.group_members == []
        assert member.synced_to == "leader"

    async def test_a_group_that_survives_the_join_is_left_alone(self, mock_mass: MagicMock) -> None:
        """A provider whose members do not ride the leader's own stream keeps its group."""
        controller, leader, member, target = self._build(mock_mass, MockPlayer)

        await controller._handle_set_members(target, player_ids_to_add=["leader"])

        assert leader.group_members == ["leader", "member"]
        assert member.synced_to == "leader"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
