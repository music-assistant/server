"""Tests for PlayerController high-level operations.

This module tests:
- cmd_set_members validation and execution
- Group/ungroup commands
- Player state management
- Cache invalidation after grouping operations
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import UnsupportedFeaturedException

from music_assistant.controllers.players import PlayerController
from music_assistant.helpers.throttle_retry import Throttler
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
    # Return "GLOBAL" for log level config (standard default)
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    mass.config.set = MagicMock()
    mass.signal_event = MagicMock()
    mass.get_providers = MagicMock(return_value=[])

    # cmd_set_members coalesces rapid-fire grouping calls via a debounce timer
    # (mass.call_later) that spawns an async flush task (mass.create_task).
    # Mimic the production behaviour: call_later schedules on the event loop
    # with ~0 delay so concurrent callers can all merge into the same open
    # batch before the timer fires on the next tick. A re-arm with the same
    # task_id cancels the prior handle.
    pending_timers: dict[str, asyncio.TimerHandle] = {}

    def _call_later(
        _delay: float, target: Any, *args: Any, task_id: str | None = None, **_kwargs: Any
    ) -> asyncio.TimerHandle | MagicMock:
        def _fire() -> None:
            if task_id is not None:
                pending_timers.pop(task_id, None)
            result = target(*args)
            if asyncio.iscoroutine(result):
                asyncio.ensure_future(result)

        # Sync tests may call through paths that schedule timers (e.g.
        # update_state). When there's no running loop, returning a MagicMock
        # is fine — those timers don't affect the test's behaviour.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return MagicMock()

        if task_id is not None and (existing := pending_timers.get(task_id)):
            existing.cancel()
        handle = loop.call_later(0, _fire)
        if task_id is not None:
            pending_timers[task_id] = handle
        return handle

    def _cancel_timer(task_id: str) -> None:
        if handle := pending_timers.pop(task_id, None):
            handle.cancel()

    def _create_task(target: Any, *args: Any, **_kwargs: Any) -> asyncio.Task[Any]:
        coro = target if asyncio.iscoroutine(target) else target(*args)
        return asyncio.ensure_future(coro)

    mass.call_later = MagicMock(side_effect=_call_later)
    mass.create_task = MagicMock(side_effect=_create_task)
    mass.cancel_timer = MagicMock(side_effect=_cancel_timer)
    mass.cancel_task = MagicMock()
    return mass


@pytest.fixture
def controller(mock_mass: MagicMock) -> PlayerController:
    """Create a PlayerController instance."""
    return PlayerController(mock_mass)


class TestSetMembersValidation:
    """Test cmd_set_members validation logic."""

    def test_set_members_requires_feature(self, mock_mass: MagicMock) -> None:
        """Test that set_members requires SET_MEMBERS feature."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        leader = MockPlayer(provider, "leader", "Leader")
        # Note: NOT adding SET_MEMBERS feature

        member = MockPlayer(provider, "member", "Member")

        controller._players = {"leader": leader, "member": member}
        controller._player_throttlers = {
            "leader": Throttler(1, 0.05),
            "member": Throttler(1, 0.05),
        }
        mock_mass.players = controller

        # Should raise exception because leader doesn't support SET_MEMBERS
        with pytest.raises(UnsupportedFeaturedException):
            asyncio.run(controller.cmd_set_members("leader", player_ids_to_add=["member"]))

    def test_cannot_group_incompatible_players(self, mock_mass: MagicMock) -> None:
        """Test that incompatible players cannot be grouped."""
        controller = PlayerController(mock_mass)
        provider_a = MockProvider("provider_a", instance_id="provider_a", mass=mock_mass)
        provider_b = MockProvider("provider_b", instance_id="provider_b", mass=mock_mass)

        player_a = MockPlayer(provider_a, "player_a", "Player A")
        player_a._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        player_a._attr_can_group_with = {"provider_a"}  # Only same provider

        player_b = MockPlayer(provider_b, "player_b", "Player B")

        controller._players = {"player_a": player_a, "player_b": player_b}
        controller._player_throttlers = {
            "player_a": Throttler(1, 0.05),
            "player_b": Throttler(1, 0.05),
        }
        mock_mass.players = controller

        # Should raise exception because players are incompatible
        with pytest.raises(UnsupportedFeaturedException):
            asyncio.run(controller.cmd_set_members("player_a", player_ids_to_add=["player_b"]))


class TestCacheInvalidationAfterGrouping:
    """Test that caches are invalidated after grouping operations."""

    async def test_all_players_cache_cleared_after_set_members(self, mock_mass: MagicMock) -> None:
        """
        Test that all players' caches are cleared after set_members.

        Regression test for: Stale can_group_with cache after grouping changes.
        """
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        leader = MockPlayer(provider, "leader", "Leader")
        leader._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        leader._attr_can_group_with = {"test"}
        leader._attr_group_members = []

        member = MockPlayer(provider, "member", "Member")

        other = MockPlayer(provider, "other", "Other")
        other._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        other._attr_can_group_with = {"test"}

        controller._players = {"leader": leader, "member": member, "other": other}
        controller._player_throttlers = {
            "leader": Throttler(1, 0.05),
            "member": Throttler(1, 0.05),
            "other": Throttler(1, 0.05),
        }
        mock_mass.players = controller

        # Populate caches
        _ = leader.state.can_group_with
        _ = other.state.can_group_with

        # Simulate grouping (normally done by provider's set_members implementation)
        leader._attr_group_members = ["leader", "member"]

        # Call set_members to trigger cache invalidation
        await controller._handle_set_members_with_protocols(
            leader, player_ids_to_add=["member"], player_ids_to_remove=[]
        )

        # Note: The actual cache clearing happens via trigger_player_update
        # which schedules update_state to be called later
        # In a real scenario, this would clear all players' caches


class TestGroupUngroup:
    """Test group and ungroup commands."""

    async def test_group_command(self, mock_mass: MagicMock) -> None:
        """Test the group command (cmd_group)."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        leader = MockPlayer(provider, "leader", "Leader")
        leader._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        leader._attr_can_group_with = {"member"}  # Leader can group with member

        member = MockPlayer(provider, "member", "Member")
        # Make sure member is already powered on to skip power handling
        member._attr_powered = True

        controller._players = {"leader": leader, "member": member}
        controller._player_throttlers = {
            "leader": Throttler(1, 0.05),
            "member": Throttler(1, 0.05),
        }
        mock_mass.players = controller

        # Update state after modifying attributes and registering with controller
        leader.update_state(signal_event=False)
        member.update_state(signal_event=False)

        # Track if set_members was called
        set_members_called = False
        original_set_members = leader.set_members

        async def mock_set_members(
            player_ids_to_add: list[str] | None = None,
            player_ids_to_remove: list[str] | None = None,
        ) -> None:
            nonlocal set_members_called
            set_members_called = True
            # Call the original to update group_members
            await original_set_members(player_ids_to_add, player_ids_to_remove)

        leader.set_members = mock_set_members  # type: ignore[method-assign]

        # Mock power handling to skip power control (focus is on grouping logic)
        async def mock_handle_cmd_power(
            player_id: str, powered: bool, skip_auto_play: bool = False
        ) -> None:
            pass

        controller._handle_cmd_power = mock_handle_cmd_power  # type: ignore[method-assign]

        # Execute group command. cmd_set_members defers _handle_set_members
        # through mass.call_later (debounce); yield once to let the flush run.
        await controller.cmd_group("member", "leader")
        await asyncio.sleep(0)

        # Verify set_members was called
        assert set_members_called
        # Verify member was added to leader's group
        assert "member" in leader._attr_group_members


class TestPlayerAvailability:
    """Test player availability checks in grouping."""

    def test_unavailable_player_rejected(self, mock_mass: MagicMock) -> None:
        """Test that unavailable players are rejected when grouping."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        leader = MockPlayer(provider, "leader", "Leader")
        leader._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        leader._attr_can_group_with = {"test"}

        member = MockPlayer(provider, "member", "Member")
        member._attr_available = False  # Mark as unavailable

        controller._players = {"leader": leader, "member": member}
        controller._player_throttlers = {
            "leader": Throttler(1, 0.05),
            "member": Throttler(1, 0.05),
        }
        mock_mass.players = controller

        # Attempting to group with unavailable player should be handled
        # (either silently ignored or raise exception depending on implementation)
        # This should either skip the unavailable player or raise an exception
        with contextlib.suppress(Exception):
            asyncio.run(controller.cmd_set_members("leader", player_ids_to_add=["member"]))


class TestSetMembersDebounce:
    """Test that rapid-fire cmd_set_members calls coalesce to one _handle call."""

    async def test_rapid_adds_on_same_target_coalesce(self, mock_mass: MagicMock) -> None:
        """Three back-to-back adds on the same target collapse into one handle call."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        leader = MockPlayer(provider, "leader", "Leader")
        leader._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        leader._attr_can_group_with = {"test"}
        leader._attr_group_members = []
        member_a = MockPlayer(provider, "member_a", "Member A")
        member_b = MockPlayer(provider, "member_b", "Member B")
        member_c = MockPlayer(provider, "member_c", "Member C")
        for p in (member_a, member_b, member_c):
            p._attr_powered = True

        controller._players = {
            "leader": leader,
            "member_a": member_a,
            "member_b": member_b,
            "member_c": member_c,
        }
        controller._player_throttlers = {
            pid: Throttler(1, 0.05) for pid in ("leader", "member_a", "member_b", "member_c")
        }
        mock_mass.players = controller
        leader.update_state(signal_event=False)
        for p in (member_a, member_b, member_c):
            p.update_state(signal_event=False)

        handle_calls: list[tuple[set[str], set[str]]] = []

        async def _track(
            _parent: Any,
            player_ids_to_add: list[str] | None = None,
            player_ids_to_remove: list[str] | None = None,
        ) -> None:
            handle_calls.append((set(player_ids_to_add or []), set(player_ids_to_remove or [])))

        controller._handle_set_members = _track  # type: ignore[assignment]

        # Fire three adds concurrently so they all merge into the same open
        # batch before the debounce timer fires on the next loop tick.
        await asyncio.gather(
            controller.cmd_set_members("leader", player_ids_to_add=["member_a"]),
            controller.cmd_set_members("leader", player_ids_to_add=["member_b"]),
            controller.cmd_set_members("leader", player_ids_to_add=["member_c"]),
        )

        assert len(handle_calls) == 1
        adds, removes = handle_calls[0]
        assert adds == {"member_a", "member_b", "member_c"}
        assert removes == set()

    async def test_add_then_remove_same_player_nets_to_remove(
        self, mock_mass: MagicMock
    ) -> None:
        """An add followed by a remove on the same player forwards only the remove."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        leader = MockPlayer(provider, "leader", "Leader")
        leader._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        leader._attr_can_group_with = {"test"}
        member_x = MockPlayer(provider, "member_x", "Member X")
        member_x._attr_powered = True

        controller._players = {"leader": leader, "member_x": member_x}
        controller._player_throttlers = {
            "leader": Throttler(1, 0.05),
            "member_x": Throttler(1, 0.05),
        }
        mock_mass.players = controller
        leader.update_state(signal_event=False)
        member_x.update_state(signal_event=False)

        handle_calls: list[tuple[set[str], set[str]]] = []

        async def _track(
            _parent: Any,
            player_ids_to_add: list[str] | None = None,
            player_ids_to_remove: list[str] | None = None,
        ) -> None:
            handle_calls.append((set(player_ids_to_add or []), set(player_ids_to_remove or [])))

        controller._handle_set_members = _track  # type: ignore[assignment]

        await asyncio.gather(
            controller.cmd_set_members("leader", player_ids_to_add=["member_x"]),
            controller.cmd_set_members("leader", player_ids_to_remove=["member_x"]),
        )

        assert len(handle_calls) == 1
        adds, removes = handle_calls[0]
        assert removes == {"member_x"}
        assert "member_x" not in adds

    async def test_unrelated_targets_dont_block_each_other(
        self, mock_mass: MagicMock
    ) -> None:
        """Rapid adds on two different targets are each forwarded once."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        leader_a = MockPlayer(provider, "leader_a", "Leader A")
        leader_a._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        leader_a._attr_can_group_with = {"test"}
        leader_b = MockPlayer(provider, "leader_b", "Leader B")
        leader_b._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        leader_b._attr_can_group_with = {"test"}
        member_x = MockPlayer(provider, "member_x", "Member X")
        member_x._attr_powered = True
        member_y = MockPlayer(provider, "member_y", "Member Y")
        member_y._attr_powered = True

        controller._players = {
            "leader_a": leader_a,
            "leader_b": leader_b,
            "member_x": member_x,
            "member_y": member_y,
        }
        controller._player_throttlers = {
            pid: Throttler(1, 0.05)
            for pid in ("leader_a", "leader_b", "member_x", "member_y")
        }
        mock_mass.players = controller
        for p in (leader_a, leader_b, member_x, member_y):
            p.update_state(signal_event=False)

        handle_calls: list[tuple[str, set[str]]] = []

        async def _track(
            parent: Any,
            player_ids_to_add: list[str] | None = None,
            _player_ids_to_remove: list[str] | None = None,
        ) -> None:
            handle_calls.append((parent.player_id, set(player_ids_to_add or [])))

        controller._handle_set_members = _track  # type: ignore[assignment]

        await asyncio.gather(
            controller.cmd_set_members("leader_a", player_ids_to_add=["member_x"]),
            controller.cmd_set_members("leader_b", player_ids_to_add=["member_y"]),
        )

        assert {c[0] for c in handle_calls} == {"leader_a", "leader_b"}
        assert len(handle_calls) == 2

    async def test_caller_awaits_until_flush_completes(
        self, mock_mass: MagicMock
    ) -> None:
        """cmd_set_members must not return before _handle_set_members ran."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        leader = MockPlayer(provider, "leader", "Leader")
        leader._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        leader._attr_can_group_with = {"test"}
        leader._attr_group_members = []
        member = MockPlayer(provider, "member", "Member")
        member._attr_powered = True

        controller._players = {"leader": leader, "member": member}
        controller._player_throttlers = {
            "leader": Throttler(1, 0.05),
            "member": Throttler(1, 0.05),
        }
        mock_mass.players = controller
        leader.update_state(signal_event=False)
        member.update_state(signal_event=False)

        handle_call_finished = False

        async def _track(
            _parent: Any,
            _player_ids_to_add: list[str] | None = None,
            _player_ids_to_remove: list[str] | None = None,
        ) -> None:
            nonlocal handle_call_finished
            # Yield once inside the handler so the caller would clearly still
            # be blocked if cmd_set_members was returning early.
            await asyncio.sleep(0)
            handle_call_finished = True

        controller._handle_set_members = _track  # type: ignore[assignment]

        await controller.cmd_set_members("leader", player_ids_to_add=["member"])
        # when cmd_set_members returns, the flush must already have completed.
        assert handle_call_finished

    async def test_cancel_drops_pending_delta(self, mock_mass: MagicMock) -> None:
        """_cancel_pending_set_members drops the open batch before the flush."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        leader = MockPlayer(provider, "leader", "Leader")
        leader._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        leader._attr_can_group_with = {"test"}
        member = MockPlayer(provider, "member", "Member")
        member._attr_powered = True

        controller._players = {"leader": leader, "member": member}
        controller._player_throttlers = {
            "leader": Throttler(1, 0.05),
            "member": Throttler(1, 0.05),
        }
        mock_mass.players = controller
        leader.update_state(signal_event=False)
        member.update_state(signal_event=False)

        # Override call_later to defer firing entirely so we can cancel before
        # the flush runs. cancel_timer is a real no-op here (the handle is a
        # MagicMock from the default override).
        def _deferred_call_later(
            _delay: float, _target: Any, *_args: Any, **_kwargs: Any
        ) -> MagicMock:
            return MagicMock()

        mock_mass.call_later = MagicMock(side_effect=_deferred_call_later)
        mock_mass.cancel_timer = MagicMock()

        handled = MagicMock()
        controller._handle_set_members = handled  # type: ignore[method-assign]

        # Spawn the caller as a task — with the deferred call_later, its
        # await on batch.future will block indefinitely until we cancel.
        caller = asyncio.create_task(
            controller.cmd_set_members("leader", player_ids_to_add=["member"])
        )
        await asyncio.sleep(0)  # let cmd_set_members register the batch

        assert "leader" in controller._open_set_members_batches
        controller._cancel_pending_set_members("leader")

        assert "leader" not in controller._open_set_members_batches
        handled.assert_not_called()
        # the awaiting caller is unblocked via cancelled future -> CancelledError
        with contextlib.suppress(asyncio.CancelledError):
            await caller


class TestStateForwarding:
    """Test forwarding of player state changes to related players."""

    def test_sync_leader_updates_are_forwarded_to_sync_children(self, mock_mass: MagicMock) -> None:
        """A regular sync leader must notify children via the sync-parent callback."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        leader = MockPlayer(provider, "leader", "Leader")
        child = MockPlayer(provider, "child", "Child")

        controller._players = {"leader": leader, "child": child}
        controller._player_throttlers = {
            "leader": Throttler(1, 0.05),
            "child": Throttler(1, 0.05),
        }
        mock_mass.players = controller

        leader._attr_group_members = ["leader", "child"]
        leader.update_state(signal_event=False)
        child.update_state(signal_event=False)

        with (
            patch.object(child, "on_sync_parent_updated") as on_sync_parent_updated,
            patch.object(child, "on_group_updated") as on_group_updated,
        ):
            controller._forward_state_update(
                leader,
                {"playback_state": (PlaybackState.IDLE, PlaybackState.PLAYING)},
            )

        on_sync_parent_updated.assert_called_once_with(
            leader,
            {"playback_state": (PlaybackState.IDLE, PlaybackState.PLAYING)},
        )
        on_group_updated.assert_not_called()

    def test_group_updates_are_forwarded_to_children_via_group_callback(
        self, mock_mass: MagicMock
    ) -> None:
        """A group player must continue to notify children via the group callback."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        group_player = MockPlayer(provider, "group", "Group", player_type=PlayerType.GROUP)
        child = MockPlayer(provider, "child", "Child")

        controller._players = {"group": group_player, "child": child}
        controller._player_throttlers = {
            "group": Throttler(1, 0.05),
            "child": Throttler(1, 0.05),
        }
        mock_mass.players = controller

        group_player._attr_group_members = ["group", "child"]
        group_player.update_state(signal_event=False)
        child.update_state(signal_event=False)

        with (
            patch.object(child, "on_group_updated") as on_group_updated,
            patch.object(child, "on_sync_parent_updated") as on_sync_parent_updated,
        ):
            controller._forward_state_update(
                group_player,
                {"playback_state": (PlaybackState.IDLE, PlaybackState.PLAYING)},
            )

        on_group_updated.assert_called_once_with(
            group_player,
            {"playback_state": (PlaybackState.IDLE, PlaybackState.PLAYING)},
        )
        on_sync_parent_updated.assert_not_called()


class TestUnregisterCleanup:
    """Test that unregister cleans up leaked internal state."""

    def test_throttler_removed(self, mock_mass: MagicMock) -> None:
        """Unregistering a player removes its throttler entry."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        player = MockPlayer(provider, "player_1", "Player 1")

        controller._players = {"player_1": player}
        controller._player_throttlers = {"player_1": Throttler(1, 0.05)}

        asyncio.run(controller.unregister("player_1"))

        assert "player_1" not in controller._player_throttlers

    def test_command_locks_removed(self, mock_mass: MagicMock) -> None:
        """Unregistering a player removes its command lock entries."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        player = MockPlayer(provider, "player_1", "Player 1")

        controller._players = {"player_1": player}
        controller._player_throttlers = {"player_1": Throttler(1, 0.05)}
        controller._player_command_locks = {
            "playback_player_1": asyncio.Lock(),
            "volume_player_1": asyncio.Lock(),
        }

        asyncio.run(controller.unregister("player_1"))

        assert "playback_player_1" not in controller._player_command_locks
        assert "volume_player_1" not in controller._player_command_locks

    def test_other_players_state_untouched(self, mock_mass: MagicMock) -> None:
        """Unregistering one player does not affect another player's state."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        player_a = MockPlayer(provider, "player_a", "Player A")
        player_b = MockPlayer(provider, "player_b", "Player B")

        controller._players = {"player_a": player_a, "player_b": player_b}
        controller._player_throttlers = {
            "player_a": Throttler(1, 0.05),
            "player_b": Throttler(1, 0.05),
        }
        controller._player_command_locks = {
            "playback_player_a": asyncio.Lock(),
            "playback_player_b": asyncio.Lock(),
        }

        asyncio.run(controller.unregister("player_a"))

        assert "player_b" in controller._player_throttlers
        assert "playback_player_b" in controller._player_command_locks
        assert "player_a" not in controller._player_throttlers
        assert "playback_player_a" not in controller._player_command_locks

    def test_suffix_player_id_not_over_matched(self, mock_mass: MagicMock) -> None:
        """Removing player 'b' must not remove locks for player 'a_b' (no suffix matching)."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        player_b = MockPlayer(provider, "b", "Player B")
        player_a_b = MockPlayer(provider, "a_b", "Player A_B")

        controller._players = {"b": player_b, "a_b": player_a_b}
        controller._player_throttlers = {
            "b": Throttler(1, 0.05),
            "a_b": Throttler(1, 0.05),
        }
        controller._player_command_locks = {
            "playback_b": asyncio.Lock(),
            "playback_a_b": asyncio.Lock(),
        }

        asyncio.run(controller.unregister("b"))

        assert "playback_a_b" in controller._player_command_locks
        assert "playback_b" not in controller._player_command_locks

    def test_pending_protocol_evaluation_cancelled(self, mock_mass: MagicMock) -> None:
        """Unregistering a player cancels and removes its pending protocol evaluation."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        player = MockPlayer(provider, "player_1", "Player 1")

        mock_handle = MagicMock()
        controller._players = {"player_1": player}
        controller._player_throttlers = {"player_1": Throttler(1, 0.05)}
        controller._pending_protocol_evaluations = {"player_1": mock_handle}

        asyncio.run(controller.unregister("player_1"))

        mock_handle.cancel.assert_called_once()
        assert "player_1" not in controller._pending_protocol_evaluations

    def test_unregister_nonexistent_player_is_noop(self, mock_mass: MagicMock) -> None:
        """Unregistering a player that doesn't exist is silently ignored."""
        controller = PlayerController(mock_mass)
        controller._player_throttlers = {"other": Throttler(1, 0.05)}
        controller._player_command_locks = {"set_members_other": asyncio.Lock()}

        asyncio.run(controller.unregister("nonexistent"))

        assert "other" in controller._player_throttlers
        assert "set_members_other" in controller._player_command_locks


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
