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
from music_assistant_models.auth import User, UserRole
from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType, ProtocolRole
from music_assistant_models.errors import InsufficientPermissions
from music_assistant_models.player import PlayerMedia
from music_assistant_models.player_queue import PlayerQueue

from music_assistant.controllers.player_queues.helpers import handle_play_action
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.players.constants import PlayerLockPurpose
from music_assistant.controllers.players.grouping import grouping_snapshot
from music_assistant.controllers.players.grouping_plan import elect_ad_hoc_leader
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_user,
    set_current_user,
)
from tests.common import MockProtocol, MockProvider, as_user, attach_mock, use_real_create_task

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

    def _get_player_value(
        _player_id: str, key: str, default: str | int | None = None
    ) -> str | int | None:
        """Return appropriate defaults for player config values."""
        if key == "min_volume":
            return 0
        if key == "max_volume":
            return 100
        return default

    mass.config.get_player_value = MagicMock(side_effect=_get_player_value)
    # Return "GLOBAL" for log level config (standard default)
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    mass.config.set = MagicMock()
    mass.signal_event = MagicMock()
    mass.get_providers = MagicMock(return_value=[])
    return mass


@pytest.fixture
def controller(mock_mass: MagicMock) -> PlayerController:
    """Create a PlayerController instance."""
    controller = PlayerController(mock_mass)
    mock_mass.players = controller
    return controller


class SessionBoundMockProtocol(MockProtocol):
    """Mock protocol whose native members ride its own stream session (e.g. AirPlay)."""

    @property
    def native_grouping_requires_own_stream(self) -> bool:
        """Return True: native members are attached to this protocol's own stream session."""
        return True


def _player(protocol: MockProtocol) -> Player:
    """Return the player a protocol is attached to."""
    assert protocol.player is not None
    return protocol.player


def _refresh(*players: Player) -> None:
    """Recalculate the state of the given players, in order."""
    for player in players:
        player.refresh_state(signal_event=False)


class TestCanGroupWithBasics:
    """Test basic can_group_with filtering logic."""

    def test_ungrouped_players_can_group(self, controller: PlayerController) -> None:
        """Test that two ungrouped players can group with each other."""
        provider = MockProvider(
            "test_provider", instance_id="test", mass=cast("MagicMock", controller.mass)
        )
        player_a, protocol_a = attach_mock(provider, "player_a", "Player A")
        protocol_a._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        # Use explicit protocol IDs instead of provider instance ID for simpler test
        protocol_a._attr_can_group_with = {"player_b"}
        player_b, protocol_b = attach_mock(provider, "player_b", "Player B")
        protocol_b._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        protocol_b._attr_can_group_with = {"player_a"}

        # Trigger state calculation
        _refresh(player_a, player_b)

        # Both players should be able to group with each other
        assert player_b.player_id in player_a.can_group_with
        assert player_a.player_id in player_b.can_group_with

    def test_unavailable_players_excluded(self, controller: PlayerController) -> None:
        """Test that unavailable players are excluded from can_group_with."""
        provider = MockProvider(
            "test_provider", instance_id="test", mass=cast("MagicMock", controller.mass)
        )
        player_a, protocol_a = attach_mock(provider, "player_a", "Player A")
        protocol_a._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        protocol_a._attr_can_group_with = {"player_b"}
        player_b, protocol_b = attach_mock(provider, "player_b", "Player B")
        protocol_b._attr_available = False  # Mark as unavailable

        # Trigger state calculation
        _refresh(player_b, player_a)

        # Unavailable player should be excluded
        assert not player_b.available
        assert player_b.player_id not in player_a.can_group_with

    def test_playing_players_with_different_source_excluded(
        self, controller: PlayerController
    ) -> None:
        """
        Test that players playing different sources are NOT excluded (behavior changed).

        Note: Previously, players with different active sources were excluded from grouping,
        but this was removed as it was difficult to track reliably.
        """
        provider = MockProvider(
            "test_provider", instance_id="test", mass=cast("MagicMock", controller.mass)
        )
        player_a, protocol_a = attach_mock(provider, "player_a", "Player A")
        protocol_a._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        protocol_a._attr_can_group_with = {"player_b"}
        protocol_a._attr_playback_state = PlaybackState.PLAYING
        protocol_a._attr_active_source = "player_a"

        player_b, protocol_b = attach_mock(provider, "player_b", "Player B")
        protocol_b._attr_playback_state = PlaybackState.PLAYING
        protocol_b._attr_active_source = "player_b"  # Different source

        # Trigger state calculation
        _refresh(player_a, player_b)

        # Player with different active source is now ALLOWED (behavior changed)
        assert player_b.player_id in player_a.can_group_with


class TestSyncedPlayers:
    """Test behavior with synced/grouped players."""

    def test_sync_leader_excludes_itself_from_members_can_group_with(
        self, controller: PlayerController
    ) -> None:
        """Test that sync leader doesn't appear in its members' can_group_with."""
        provider = MockProvider(
            "test_provider", instance_id="test", mass=cast("MagicMock", controller.mass)
        )
        leader, leader_protocol = attach_mock(provider, "leader", "Leader")
        leader_protocol._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        leader_protocol._attr_can_group_with = {"member"}
        leader_protocol._attr_group_members = ["leader", "member"]

        member, _ = attach_mock(provider, "member", "Member")

        # Trigger synced_to calculation
        _refresh(leader, member)

        # Member is synced, so can_group_with should be empty
        assert member.synced_to == leader.player_id
        assert member.can_group_with == set()

    def test_group_members_included_in_leader_can_group_with(
        self, controller: PlayerController
    ) -> None:
        """
        Test that group members appear in sync leader's can_group_with.

        This allows ungrouping members from the leader.
        """
        provider = MockProvider(
            "test_provider", instance_id="test", mass=cast("MagicMock", controller.mass)
        )
        leader, leader_protocol = attach_mock(provider, "leader", "Leader")
        leader_protocol._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        leader_protocol._attr_can_group_with = {"member_a", "member_b"}
        leader_protocol._attr_group_members = ["leader", "member_a", "member_b"]

        member_a, _ = attach_mock(provider, "member_a", "Member A")
        member_b, _ = attach_mock(provider, "member_b", "Member B")

        # Trigger synced_to calculation
        _refresh(leader, member_a, member_b)

        # Leader should be able to see its own members (for ungrouping)
        assert member_a.player_id in leader.can_group_with
        assert member_b.player_id in leader.can_group_with


class TestSyncLeaderBehavior:
    """Test sync leader specific behavior."""

    def test_sync_leader_excluded_from_can_group_with(self, controller: PlayerController) -> None:
        """Test that players with group members (sync leaders) are excluded."""
        provider = MockProvider(
            "test_provider", instance_id="test", mass=cast("MagicMock", controller.mass)
        )
        leader, leader_protocol = attach_mock(provider, "leader", "Leader")
        leader_protocol._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        leader_protocol._attr_can_group_with = {"member", "other"}
        leader_protocol._attr_group_members = ["leader", "member"]
        # Make it playing so it gets excluded
        leader_protocol._attr_playback_state = PlaybackState.PLAYING

        member, _ = attach_mock(provider, "member", "Member")
        other, other_protocol = attach_mock(provider, "other", "Other")
        other_protocol._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        other_protocol._attr_can_group_with = {"leader", "member"}

        # Trigger synced_to calculation
        _refresh(leader, member, other)

        # Leader should NOT appear in other's can_group_with (has group members)
        assert leader.player_id not in other.can_group_with

    def test_solo_raw_group_members_does_not_exclude_a_playing_candidate(
        self, controller: PlayerController
    ) -> None:
        """Test that a playing player whose raw group_members is only itself can still be grouped."""
        provider = MockProvider(
            "test_provider", instance_id="test", mass=cast("MagicMock", controller.mass)
        )
        # A detached client (e.g. a Sendspin client in a solo group) reports itself
        # as its only raw group member - that must not make it a group leader
        solo_candidate, solo_protocol = attach_mock(provider, "solo", "Solo")
        solo_protocol._attr_group_members = ["solo"]
        solo_protocol._attr_playback_state = PlaybackState.PLAYING

        real_leader, leader_protocol = attach_mock(provider, "leader", "Leader")
        leader_protocol._attr_group_members = ["leader", "member"]
        leader_protocol._attr_playback_state = PlaybackState.PLAYING

        member, _ = attach_mock(provider, "member", "Member")
        other, other_protocol = attach_mock(provider, "other", "Other")
        other_protocol._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        other_protocol._attr_can_group_with = {"solo", "leader"}

        _refresh(solo_candidate, real_leader, member, other)

        # Solo player is still offered, a real (multi-member) leader stays excluded
        assert solo_candidate.player_id in other.can_group_with
        assert real_leader.player_id not in other.can_group_with


class TestCircularDependency:
    """Test that circular dependencies are avoided."""

    def test_no_circular_dependency_in_synced_to(self, controller: PlayerController) -> None:
        """
        Test that synced_to calculation doesn't cause circular dependency.

        Regression test for: synced_to calling group_members causing infinite recursion.
        """
        provider = MockProvider(
            "test_provider", instance_id="test", mass=cast("MagicMock", controller.mass)
        )
        leader, leader_protocol = attach_mock(provider, "leader", "Leader")
        leader_protocol._attr_group_members = ["leader", "member"]
        member, _ = attach_mock(provider, "member", "Member")

        # Trigger synced_to calculation via update_state
        _refresh(leader, member)

        # This should not cause infinite recursion
        assert member.synced_to == leader.player_id
        assert leader.synced_to is None


class TestCacheInvalidation:
    """Test that caches are invalidated correctly."""

    def test_can_group_with_cache_cleared_on_update_state(
        self, controller: PlayerController
    ) -> None:
        """Test that can_group_with cache is cleared when update_state is called."""
        provider = MockProvider(
            "test_provider", instance_id="test", mass=cast("MagicMock", controller.mass)
        )
        player_a, protocol_a = attach_mock(provider, "player_a", "Player A")
        protocol_a._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        protocol_a._attr_can_group_with = {"player_b"}
        player_b, _ = attach_mock(provider, "player_b", "Player B")

        # Update state after setting attributes and registering with controller
        _refresh(player_a, player_b)

        # Get can_group_with to populate cache
        initial = player_a.can_group_with
        assert player_b.player_id in initial

        # Modify underlying data
        protocol_a._attr_can_group_with = set()

        # Cache should still have old value
        assert player_a.can_group_with == initial

        # Clear cache via update_state
        player_a.update_state(signal_event=False)

        # Cache should be cleared, new value should be returned
        assert player_a.can_group_with == set()


class TestProviderInstanceIdExpansion:
    """Test expansion of provider instance IDs in can_group_with."""

    def test_provider_instance_id_expands_to_all_players(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Test that provider instance IDs expand to all available players from that provider."""
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        player_a, protocol_a = attach_mock(provider, "player_a", "Player A")
        protocol_a._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        protocol_a._attr_can_group_with = {"test"}  # Provider instance ID
        player_b, _ = attach_mock(provider, "player_b", "Player B")
        player_c, _ = attach_mock(provider, "player_c", "Player C")

        # Set up get_provider to return the provider for instance ID
        mock_mass.get_provider = MagicMock(return_value=provider)

        # Trigger state calculation
        _refresh(player_a, player_b, player_c)

        # Provider instance ID should expand to include all players from that provider
        can_group = player_a.can_group_with
        assert player_b.player_id in can_group
        assert player_c.player_id in can_group

    def test_provider_instance_id_excludes_unknown_players(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Test that players without an output type are not offered as grouping targets."""
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        mock_mass.get_provider = MagicMock(return_value=provider)
        leader, leader_protocol = attach_mock(provider, "leader", "Leader")
        leader_protocol._attr_can_group_with = {"test"}
        public_player, _ = attach_mock(provider, "public", "Public Player")
        unknown_player, _ = attach_mock(
            provider,
            "unknown",
            "Unknown Player",
            player_type=PlayerType.UNKNOWN,
        )

        _refresh(leader, public_player, unknown_player)

        assert public_player.player_id in leader.can_group_with
        assert unknown_player.player_id not in leader.can_group_with

        leader_protocol._attr_can_group_with = {"unknown"}
        leader.update_state(signal_event=False, force_update=True)

        assert leader.can_group_with == set()


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

    @staticmethod
    def _group_of(
        controller: PlayerController,
        powered: bool | None,
        protocol_class: type[MockProtocol] = MockProtocol,
    ) -> tuple[Player, Player]:
        """
        Attach a group that lists a member as configured member; return the group and member.

        :param controller: The players controller.
        :param powered: What the group reports as its power state.
        :param protocol_class: The class of the group's protocol.
        """
        group_provider = MockProvider(
            "test_group", instance_id="test_group", mass=cast("MagicMock", controller.mass)
        )
        member_provider = MockProvider(
            "test", instance_id="test", mass=cast("MagicMock", controller.mass)
        )
        member, _ = attach_mock(member_provider, "member", "Member")
        group, group_protocol = attach_mock(
            group_provider,
            "g1",
            "Group",
            role=ProtocolRole.VIRTUAL,
            player_type=PlayerType.GROUP,
            protocol_class=protocol_class,
        )
        group_protocol._attr_powered = powered
        # a group lists its members by their player ids
        group_protocol._attr_group_members = [member.player_id]
        _refresh(group, member)
        return group, member

    def test_dormant_group_does_not_capture_members(self, controller: PlayerController) -> None:
        """No power signal, no session → member's active_group is None."""
        # explicitly "no opinion" on power (matches new default for groups); listed as a
        # configured member, but no active session: is_active_session base default is False
        _group, member = self._group_of(controller, powered=None)

        assert member.active_group is None

    def test_powered_true_group_captures_members(self, controller: PlayerController) -> None:
        """Group with _attr_powered=True (fake pin) captures members regardless of session."""
        group, member = self._group_of(controller, powered=True)

        assert member.active_group == group.player_id

    def test_powered_false_group_does_not_capture_members(
        self, controller: PlayerController
    ) -> None:
        """Group with _attr_powered=False (explicit off) does not capture, even with a session."""
        _group, member = self._group_of(controller, powered=False)

        assert member.active_group is None

    def test_session_active_group_captures_members(self, controller: PlayerController) -> None:
        """Group with powered=None but is_active_session=True captures members."""

        # subclass MockProtocol to override is_active_session for this test
        class _SessionedGroup(MockProtocol):
            @property
            def is_active_session(self) -> bool:
                return True

        group, member = self._group_of(controller, powered=None, protocol_class=_SessionedGroup)

        assert member.active_group == group.player_id


class _LockingGroup(MockProtocol):
    """Group protocol that locks its sync leader from inside set_members, like a syncgroup."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the group protocol."""
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
            # a group lists its members by their player ids
            members = [
                member_id
                for member_id in self._attr_group_members
                if member_id not in (player_ids_to_remove or [])
            ]
            members += [
                member_id for member_id in player_ids_to_add or [] if member_id not in members
            ]
            self._attr_group_members = members
            # publish the change the way a provider's state push would, so a command
            # waiting for the member to report its new group state sees it
            for player in self.mass.players.iter_players():
                player.refresh_state()


def _spy_on_lock_order(controller: PlayerController) -> list[str]:
    """Replace the controller's get_player_lock with a recording wrapper."""
    lock_keys: list[str] = []
    acquire_lock = controller.get_player_lock

    def _record(
        player_id: str, purpose: PlayerLockPurpose = PlayerLockPurpose.PLAYBACK
    ) -> AbstractAsyncContextManager[None]:
        # this records the order the locks are requested in, which is the order they
        # are entered in as well here: nothing else holds them in these tests
        lock_keys.append(f"{purpose.value}_{player_id}")
        return acquire_lock(player_id, purpose)

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


class _PowerableProtocol(MockProtocol):
    """Protocol with native power control that records the power commands it received."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the protocol."""
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
    ) -> tuple[PlayerController, _LockingGroup, Player, Player]:
        """
        Attach group "g1" capturing player "member", and a player "joiner"; the protocols differ.

        :param mock_mass: The mocked server.
        """
        controller = PlayerController(mock_mass)
        mock_mass.players = controller
        group_provider = MockProvider("test_group", instance_id="test_group", mass=mock_mass)
        member_provider = MockProvider("test", instance_id="test", mass=mock_mass)

        _, protocol = attach_mock(
            group_provider,
            "g1_group",
            "Group",
            role=ProtocolRole.VIRTUAL,
            player_type=PlayerType.GROUP,
            player_id="g1",
            protocol_class=_LockingGroup,
        )
        group_protocol = cast("_LockingGroup", protocol)
        group_protocol._attr_powered = True
        group_protocol._attr_group_members = ["member"]
        group_protocol._attr_supported_features = {PlayerFeature.SET_MEMBERS}
        group_protocol._attr_can_group_with = {"member", "joiner"}
        group_protocol.leader_id = "member"

        member, _ = attach_mock(member_provider, "member_device", "Member", player_id="member")
        joiner, _ = attach_mock(member_provider, "joiner_device", "Joiner", player_id="joiner")

        _refresh(*controller._players.values())

        assert member.active_group == "g1"
        return controller, group_protocol, member, joiner

    @staticmethod
    def _add_follower(controller: PlayerController, leader: Player) -> Player:
        """Sync "extra" to the given group member, which makes that member a captured leader."""
        leader_protocol = cast("MockProtocol", leader.own_protocol)
        extra, _ = attach_mock(
            cast("MockProvider", leader_protocol.provider),
            "extra_device",
            "Extra",
            player_id="extra",
        )
        leader_protocol._attr_supported_features = {PlayerFeature.SET_MEMBERS}
        leader_protocol._attr_group_members = [leader_protocol.protocol_id, "extra_device"]
        _refresh(*controller._players.values())
        assert extra.synced_to == leader.player_id
        assert leader.active_group == "g1"
        return extra

    def _setup_announcement(
        self, mock_mass: MagicMock
    ) -> tuple[PlayerController, _LockingGroup, Player]:
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
        controller, _, member, _ = self._setup(mock_mass)
        group = controller.get_player("g1")
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
        mock_mass.players = controller
        provider = MockProvider("test", instance_id="test", mass=mock_mass)

        leader, leader_protocol = attach_mock(
            provider, "leader_device", "Leader", protocol_class=_PowerableProtocol
        )
        leader_protocol._attr_can_group_with = {"follower_device"}
        leader_protocol._attr_group_members = ["leader_device", "follower_device"]
        follower, follower_protocol = attach_mock(
            provider, "follower_device", "Follower", protocol_class=_PowerableProtocol
        )

        mock_mass.player_queues.get = MagicMock(return_value=None)
        _refresh(leader, follower)

        assert follower.synced_to == leader.player_id

        # the release runs from under the locks the power off holds, so a timeout
        # here would mean it waited on one of them
        async with asyncio.timeout(2):
            await controller.cmd_power(leader.player_id, False)

        assert cast("_PowerableProtocol", leader_protocol).power_commands == [False]
        assert cast("_PowerableProtocol", follower_protocol).power_commands == []

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
        assert member.active_group == "g1"

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

    @pytest.mark.parametrize("command", ["play", "resume", "resume-with-media"])
    async def test_a_command_that_waits_for_an_announcement_needs_access_to_the_group(
        self, mock_mass: MagicMock, command: str
    ) -> None:
        """
        A play or resume that waits for the member's announcement is refused on the group.

        The announcement takes the member out of its group and joins it back under the
        member's lock, so the command finds the group only once it holds that lock.
        """
        controller, group, member = self._setup_announcement(mock_mass)
        group.release.set()
        mock_mass.player_queues.get = MagicMock(return_value=None)
        # the group holds a paused source of a plugin, which a play would unpause
        source = MagicMock(can_play_pause=True, item_id="plugin-source")
        plugin = MagicMock()
        plugin.on_source_control = AsyncMock()
        controller.get_player_audio_source = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda player_id: (source, plugin) if player_id == "g1" else None
        )
        announcing = asyncio.Event()
        finish = asyncio.Event()

        async def _clip(_player_id: str, _media: PlayerMedia) -> None:
            announcing.set()
            await finish.wait()

        controller._handle_play_media = AsyncMock(side_effect=_clip)  # type: ignore[method-assign]
        device_calls = {
            (protocol.protocol_id, method): AsyncMock()
            for protocol in (group, member.own_protocol)
            if protocol is not None
            for method in ("play_media", "play")
        }
        for (protocol_id, method), mock in device_calls.items():
            setattr(controller._protocols[protocol_id], method, mock)
        media = PlayerMedia(uri="http://media.test/stream.mp3", title="Stream")
        commands = {
            "play": lambda: controller.cmd_play("member"),
            "resume": lambda: controller.cmd_resume("member"),
            "resume-with-media": lambda: controller.cmd_resume("member", "other", media),
        }

        with as_user(["member"]):
            announcement = asyncio.create_task(
                controller.play_announcement("member", "http://test/announce.mp3")
            )
            async with asyncio.timeout(5):
                await announcing.wait()
            # the member plays the clip on its own, so the command passes its first check
            assert member.active_group != "g1"
            task = asyncio.create_task(commands[command]())
            for _ in range(20):
                await asyncio.sleep(0)
            assert not task.done()
            finish.set()
            async with asyncio.timeout(5):
                await announcement
                with pytest.raises(
                    InsufficientPermissions, match=r"limited does not have access to player Group$"
                ):
                    await task

        assert member.active_group == "g1"
        device_calls["g1_group", "play_media"].assert_not_awaited()
        device_calls["g1_group", "play"].assert_not_awaited()
        plugin.on_source_control.assert_not_awaited()


class TestPlayerBaseIsActiveSession:
    """The protocol base class defaults is_active_session to False; only groups override it."""

    def test_base_player_is_not_an_active_session(self, mock_mass: MagicMock) -> None:
        """A regular MockProtocol should never claim to hold a captured session."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        _, protocol = attach_mock(provider, "p1", "P1")
        assert protocol.is_active_session is False


def _attach(provider: MockProvider, player_id: str, name: str, **kwargs: Any) -> MockProtocol:
    """
    Attach a protocol with an id of its own to a player with the given id; return the protocol.

    :param provider: The (mock) provider of the protocol.
    :param player_id: Id of the player, also the base of the protocol id.
    :param name: The name the device reports.
    :param kwargs: Further arguments for ``attach_mock``.
    """
    _, protocol = attach_mock(provider, _device(player_id), name, player_id=player_id, **kwargs)
    return protocol


def _device(player_id: str) -> str:
    """Return the id of the protocol ``_attach`` gives the player with the given id."""
    return f"{player_id}_device"


def _make_ad_hoc_group(
    controller: PlayerController, mock_mass: MagicMock, airplay_available: bool
) -> None:
    """
    Attach an ad-hoc group playing over AirPlay, with only member "b" on that domain.

    Member "a" is a plain native player, member "b" is a native player with an attached
    AirPlay protocol whose availability is driven by ``airplay_available``.
    """
    sonos = MockProvider("sonos", instance_id="sonos", mass=mock_mass)
    airplay = MockProvider("airplay", instance_id="airplay", mass=mock_mass)

    natives = [
        _attach(sonos, "leader", "Leader"),
        _attach(sonos, "a", "Member A"),
        _attach(sonos, "b", "Member B"),
    ]
    leader = controller._players["leader"]
    attach_mock(airplay, "leader_airplay", "Leader AirPlay", role=ProtocolRole.AUDIO, player=leader)
    leader.set_active_output_protocol("leader_airplay")
    _, member_b_airplay = attach_mock(
        airplay, "b_airplay", "B AirPlay", role=ProtocolRole.AUDIO, player=controller._players["b"]
    )
    member_b_airplay._attr_available = airplay_available

    for native in natives:
        native._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
    _refresh(*controller._players.values())


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


def _sync_leader(
    provider: MockProvider, *member_ids: str, player_ids: tuple[str, ...] | None = None
) -> MockProtocol:
    """
    Attach player "leader" leading the given members, which report as their own protocols.

    :param provider: The (mock) provider of the leader.
    :param member_ids: The player ids of the members.
    :param player_ids: The players it can group with, the members by default.
    """
    leader_protocol = _attach(provider, "leader", "Leader")
    leader_protocol._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
    leader_protocol._attr_can_group_with = {
        _device(player_id) for player_id in (player_ids or member_ids)
    }
    # a sync leader reports its group members by the ids of their protocols
    leader_protocol._attr_group_members = [_device("leader"), *map(_device, member_ids)]
    return leader_protocol


def _ad_hoc_leader(
    mock_mass: MagicMock, member_type: PlayerType = PlayerType.VISUALIZER
) -> tuple[PlayerController, Player, AsyncMock, AsyncMock]:
    """
    Build a sync leader with a single member, ready to be removed from itself.

    A non-audio member leaves no playback heir, so the group dissolves; pass
    ``PlayerType.PLAYER`` to get a heir and reach the leadership transfer instead.

    :param mock_mass: The mocked MusicAssistant instance to attach the controller to.
    :param member_type: Type to register the group member as.
    :return: The controller, the leader, its stubbed device stop and queue stop.
    """
    controller = PlayerController(mock_mass)
    mock_mass.players = controller
    provider = MockProvider("test", instance_id="test", mass=mock_mass)

    leader_protocol = _sync_leader(provider, "member")
    _attach(provider, "member", "Member", player_type=member_type)

    # refresh each player's state snapshot so it reflects the mocked player type
    _refresh(*controller._players.values())

    leader_protocol.set_members = AsyncMock()  # type: ignore[method-assign]
    device_stop = AsyncMock()
    controller._handle_cmd_stop = device_stop  # type: ignore[method-assign]
    queue_stop = AsyncMock()
    mock_mass.player_queues._handle_stop = queue_stop
    return controller, controller._players["leader"], device_stop, queue_stop


class TestAdHocLeadershipTransfer:
    """Unjoining an ad-hoc sync leader transfers leadership instead of dissolving."""

    def test_select_ad_hoc_leader_prefers_active_protocol(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """The new leader should be a member that supports the group's active protocol."""
        # member_a can't do airplay, member_b has an airplay protocol that is up
        _make_ad_hoc_group(controller, mock_mass, airplay_available=True)
        leader = controller.get_player("leader")
        assert leader is not None

        assert (
            elect_ad_hoc_leader(grouping_snapshot(controller), leader.player_id, ["a", "b"]) == "b"
        )

    def test_select_ad_hoc_leader_skips_offline_protocol(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """A member whose audio protocol went offline must not inherit the session."""
        # member_b still has an airplay protocol attached, but it is unavailable
        _make_ad_hoc_group(controller, mock_mass, airplay_available=False)
        leader = controller.get_player("leader")
        assert leader is not None

        assert (
            elect_ad_hoc_leader(grouping_snapshot(controller), leader.player_id, ["a", "b"]) == "a"
        )

    def test_select_ad_hoc_leader_accepts_native_member_on_active_domain(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """A member that plays the active protocol natively is still a valid leader."""
        # a Chromecast speaker has no audio protocol: its native protocol *is* the output
        chromecast = MockProvider("chromecast", instance_id="chromecast", mass=mock_mass)
        sonos = MockProvider("sonos", instance_id="sonos", mass=mock_mass)

        natives = [
            _attach(sonos, "leader", "Leader"),
            _attach(sonos, "a", "Member A"),
            _attach(chromecast, "c", "Member C"),
        ]
        leader = controller._players["leader"]
        attach_mock(
            chromecast, "leader_cast", "Leader Cast", role=ProtocolRole.AUDIO, player=leader
        )
        leader.set_active_output_protocol("leader_cast")
        for native in natives:
            native._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        _refresh(*controller._players.values())

        assert (
            elect_ad_hoc_leader(grouping_snapshot(controller), leader.player_id, ["a", "c"]) == "c"
        )

    def test_select_ad_hoc_leader_falls_back_to_first(self, controller: PlayerController) -> None:
        """Without an active protocol to match, fall back to the first remaining member."""
        provider = MockProvider("test", instance_id="test", mass=cast("MagicMock", controller.mass))
        for player_id in ("leader", "a", "b"):
            _attach(provider, player_id, player_id.title())
        _refresh(*controller._players.values())
        leader = controller._players["leader"]

        assert (
            elect_ad_hoc_leader(grouping_snapshot(controller), leader.player_id, ["a", "b"]) == "a"
        )

    def test_select_ad_hoc_leader_never_picks_non_audio_member(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """A visualizer listed before an audio member must never inherit the queue."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        _attach(provider, "viz", "Visualizer", player_type=PlayerType.VISUALIZER)
        _attach(provider, "b", "Member B")
        # refresh each player's state snapshot so it reflects the mocked player type
        _attach(provider, "leader", "Leader")
        _refresh(*controller._players.values())
        leader = controller._players["leader"]

        assert (
            elect_ad_hoc_leader(grouping_snapshot(controller), leader.player_id, ["viz", "b"])
            == "b"
        )

    async def test_handle_set_members_transfers_leader_when_playing(
        self, controller: PlayerController
    ) -> None:
        """Removing the leader from itself while playing routes to a leadership transfer."""
        provider = MockProvider("test", instance_id="test", mass=cast("MagicMock", controller.mass))
        _sync_leader(provider, "member_a", "member_b")
        _attach(provider, "member_a", "Member A")
        _attach(provider, "member_b", "Member B")
        _refresh(*controller._players.values())
        leader = controller._players["leader"]

        controller.get_active_queue = MagicMock(return_value=_queue_stub("leader"))  # type: ignore[method-assign]
        controller._transfer_ad_hoc_leadership = AsyncMock()  # type: ignore[method-assign]

        await controller._handle_set_members(leader, player_ids_to_remove=["leader"])

        controller._transfer_ad_hoc_leadership.assert_awaited_once()
        called_leader, called_remaining = controller._transfer_ad_hoc_leadership.call_args.args
        assert called_leader is leader
        assert set(called_remaining) == {"member_a", "member_b"}

    async def test_handle_set_members_keeps_non_audio_member_as_follower(
        self, controller: PlayerController
    ) -> None:
        """A non-audio member does not block the transfer and stays in the regroup set."""
        provider = MockProvider("test", instance_id="test", mass=cast("MagicMock", controller.mass))
        _sync_leader(provider, "member_a", "visualizer")
        _attach(provider, "member_a", "Member A")
        _attach(provider, "visualizer", "Visualizer", player_type=PlayerType.VISUALIZER)
        # refresh each player's state snapshot so it reflects the mocked player type
        _refresh(*controller._players.values())
        leader = controller._players["leader"]

        controller.get_active_queue = MagicMock(return_value=_queue_stub("leader"))  # type: ignore[method-assign]
        controller._transfer_ad_hoc_leadership = AsyncMock()  # type: ignore[method-assign]

        await controller._handle_set_members(leader, player_ids_to_remove=["leader"])

        controller._transfer_ad_hoc_leadership.assert_awaited_once()
        _, called_remaining = controller._transfer_ad_hoc_leadership.call_args.args
        # the visualizer stays a group member (it follows the new leader), the
        # heir itself is picked from the audio-capable members only
        assert set(called_remaining) == {"member_a", "visualizer"}

    async def test_handle_set_members_dissolves_when_only_non_audio_members_remain(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """With only non-audio members left there is no heir: dissolve and stop."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        leader_protocol = _sync_leader(provider, "visualizer")
        _attach(provider, "visualizer", "Visualizer", player_type=PlayerType.VISUALIZER)
        # refresh each player's state snapshot so it reflects the mocked player type
        _refresh(*controller._players.values())
        leader = controller._players["leader"]

        controller.get_active_queue = MagicMock(return_value=_queue_stub("leader"))  # type: ignore[method-assign]
        controller._transfer_ad_hoc_leadership = AsyncMock()  # type: ignore[method-assign]
        leader_protocol.set_members = AsyncMock()  # type: ignore[method-assign]
        controller._handle_cmd_stop = AsyncMock()  # type: ignore[method-assign]
        queue_stop = AsyncMock()
        mock_mass.player_queues._handle_stop = queue_stop

        await controller._handle_set_members(leader, player_ids_to_remove=["leader"])

        controller._transfer_ad_hoc_leadership.assert_not_awaited()
        # the dissolve removes the visualizer from the group and ends the leader's queue
        leader_protocol.set_members.assert_awaited_once_with(
            player_ids_to_add=None, player_ids_to_remove=[_device("visualizer")]
        )
        queue_stop.assert_awaited_once_with("leader")
        controller._handle_cmd_stop.assert_not_awaited()

    async def test_handle_set_members_dissolves_leader_when_idle(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Removing the leader from itself while idle dissolves the group and stops."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        leader_protocol = _sync_leader(provider, "member_a")
        _attach(provider, "member_a", "Member A")
        _refresh(*controller._players.values())
        leader = controller._players["leader"]

        controller.get_active_queue = MagicMock(  # type: ignore[method-assign]
            return_value=_queue_stub("leader", state=PlaybackState.IDLE)
        )
        controller._transfer_ad_hoc_leadership = AsyncMock()  # type: ignore[method-assign]
        leader_protocol.set_members = AsyncMock()  # type: ignore[method-assign]
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
        own = leader.own_protocol
        assert own is not None
        # dissolving what is left removes the follower from the leader's own group
        cast("AsyncMock", own.set_members).assert_awaited_once_with(
            player_ids_to_add=None, player_ids_to_remove=[_device("member")]
        )
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
        self, mock_mass: MagicMock, leader_class: type[MockProtocol]
    ) -> tuple[PlayerController, MockProtocol, MockProtocol, MockProtocol]:
        """
        Build an idle leader with one native member, plus the group it is about to join.

        Returns the controller and the protocols of the leader, the member and the target.
        """
        controller = PlayerController(mock_mass)
        mock_mass.players = controller
        provider = MockProvider("airplay", instance_id="airplay", mass=mock_mass)

        leader_protocol = _attach(provider, "leader", "Living Room", protocol_class=leader_class)
        leader_protocol._attr_supported_features |= {
            PlayerFeature.SET_MEMBERS,
            PlayerFeature.PLAY_MEDIA,
        }
        leader_protocol._attr_can_group_with = {_device("member"), _device("target")}
        leader_protocol._attr_group_members = [_device("leader"), _device("member")]

        member_protocol = _attach(provider, "member", "Kitchen")
        member_protocol._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)

        target_protocol = _attach(provider, "target", "Study")
        target_protocol._attr_supported_features |= {
            PlayerFeature.SET_MEMBERS,
            PlayerFeature.PLAY_MEDIA,
        }
        target_protocol._attr_can_group_with = {_device("leader"), _device("member")}

        _refresh(*controller._players.values())
        assert member_protocol.synced_to == _device("leader")
        return controller, leader_protocol, member_protocol, target_protocol

    async def test_own_group_is_dissolved_before_it_joins(self, mock_mass: MagicMock) -> None:
        """
        The members are released before the leader becomes a member itself.

        A native group outlives its session, so a leader that stopped still lists its
        members. Joining another group leaves it unable to serve them, and they would be
        silent while the UI still shows them grouped.
        """
        controller, leader_protocol, member_protocol, target_protocol = self._build(
            mock_mass, SessionBoundMockProtocol
        )

        await controller._handle_set_members(_player(target_protocol), player_ids_to_add=["leader"])

        assert _device("member") not in leader_protocol.group_members
        assert member_protocol.synced_to is None
        assert target_protocol.group_members == [_device("target"), _device("leader")]

    async def test_a_leader_without_members_is_left_alone(self, mock_mass: MagicMock) -> None:
        """A player that leads nothing has no group to give up."""
        controller, leader_protocol, _member_protocol, target_protocol = self._build(
            mock_mass, SessionBoundMockProtocol
        )
        leader_protocol._attr_group_members = []
        leader_protocol.update_state(signal_event=False)
        leader_protocol.set_members = AsyncMock()  # type: ignore[method-assign]

        await controller._handle_set_members(_player(target_protocol), player_ids_to_add=["leader"])

        leader_protocol.set_members.assert_not_awaited()
        assert target_protocol.group_members == [_device("target"), _device("leader")]

    async def test_a_playing_group_is_never_torn_down(self, mock_mass: MagicMock) -> None:
        """
        A leader still serving its own group stays out instead of silencing its members.

        Grouping never offers a rendering leader as a target, but the candidate list is a
        snapshot: it is only recomputed when a group membership or availability changes, so
        one taken before playback started still lists the leader.
        """
        controller, leader_protocol, member_protocol, target_protocol = self._build(
            mock_mass, SessionBoundMockProtocol
        )
        leader_protocol._attr_playback_state = PlaybackState.PLAYING
        leader_protocol.update_state(signal_event=False)
        leader_protocol.set_members = AsyncMock()  # type: ignore[method-assign]

        await controller._handle_set_members(_player(target_protocol), player_ids_to_add=["leader"])

        leader_protocol.set_members.assert_not_awaited()
        assert target_protocol.group_members == []
        assert member_protocol.synced_to == _device("leader")

    async def test_a_group_that_cannot_be_dissolved_keeps_the_player_out(
        self, mock_mass: MagicMock
    ) -> None:
        """
        A leader that could not give its group up stays out instead of joining on top of it.

        Joining anyway leaves it leading a group it can no longer serve, and a player that
        is synced refuses every later member change, so the state cannot be cleaned up.
        """
        controller, leader_protocol, member_protocol, target_protocol = self._build(
            mock_mass, SessionBoundMockProtocol
        )
        leader_protocol.set_members = AsyncMock(side_effect=RuntimeError("speaker unreachable"))  # type: ignore[method-assign]

        await controller._handle_set_members(_player(target_protocol), player_ids_to_add=["leader"])

        assert target_protocol.group_members == []
        assert member_protocol.synced_to == _device("leader")

    async def test_a_group_that_survives_the_join_is_left_alone(self, mock_mass: MagicMock) -> None:
        """A provider whose members do not ride the leader's own stream keeps its group."""
        controller, leader_protocol, member_protocol, target_protocol = self._build(
            mock_mass, MockProtocol
        )

        await controller._handle_set_members(_player(target_protocol), player_ids_to_add=["leader"])

        assert leader_protocol.group_members == [_device("leader"), _device("member")]
        assert member_protocol.synced_to == _device("leader")


async def test_leadership_transfer_runs_as_the_server(mock_mass: MagicMock) -> None:
    """The queue moves to the server's choice of leader, whatever players the acting user has."""
    controller = PlayerController(mock_mass)
    mock_mass.players = controller
    provider = MockProvider("test", instance_id="test", mass=mock_mass)
    leader = _player(_attach(provider, "leader", "Leader"))
    _attach(provider, "member", "Member")
    controller.get_active_queue = MagicMock(return_value=_queue_stub("leader"))  # type: ignore[method-assign]
    acting_users: list[User | None] = []

    async def _transfer(*_args: Any, **_kwargs: Any) -> None:
        acting_users.append(get_current_user())

    mock_mass.player_queues.transfer_queue = AsyncMock(side_effect=_transfer)
    mock_mass.player_queues.resume = AsyncMock(side_effect=_transfer)
    user = User(user_id="u1", username="u1", role=UserRole.USER, player_filter=["leader"])
    set_current_user(user)
    try:
        await controller._transfer_ad_hoc_leadership(leader, ["member"])
        assert get_current_user() is user
    finally:
        set_current_user(None)

    mock_mass.player_queues.transfer_queue.assert_awaited_once_with(
        "leader", "member", auto_play=False
    )
    assert acting_users == [None, None]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
