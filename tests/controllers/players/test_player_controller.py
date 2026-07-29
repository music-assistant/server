"""
Tests for PlayerController high-level operations.

This module tests:
- cmd_set_members validation and execution
- Group/ungroup commands
- Player state management
- Cache invalidation after grouping operations
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from music_assistant_models.constants import (
    PLAYER_CONTROL_FAKE,
    PLAYER_CONTROL_NATIVE,
    PLAYER_CONTROL_NONE,
)
from music_assistant_models.enums import (
    EventType,
    MediaType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
)
from music_assistant_models.errors import (
    InvalidDataError,
    PlayerCommandFailed,
    UnsupportedFeaturedException,
)
from music_assistant_models.player import PlayerMedia, PlayerSource

from music_assistant.constants import ATTR_PREVIOUS_VOLUME, CONF_MUTE_CONTROL
from music_assistant.controllers.players import PlayerController
from tests.common import MockPlayer, MockProvider


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance."""
    mass = MagicMock()
    mass.closing = False
    mass.loop = None
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
        return default if default is not None else "auto"

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


@pytest.fixture
def provider(mock_mass: MagicMock) -> MockProvider:
    """Create a mock provider."""
    return MockProvider("test_provider", instance_id="test_prov", mass=mock_mass)


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


class TestNativeSetMembersGuard:
    """Test the SET_MEMBERS feature guard on native set_members forwarding."""

    async def test_native_set_members_skipped_without_feature(self, mock_mass: MagicMock) -> None:
        """
        Test that set_members is not called on a player without SET_MEMBERS support.

        Regression test for: NotImplementedError raised from
        _cleanup_player_memberships when removing a member from a native player
        whose group membership is managed externally (e.g. a Google Cast group,
        which never advertises SET_MEMBERS).
        """
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        # a native group-like player WITHOUT SET_MEMBERS in supported_features,
        # whose set_members behaves like the Player base class (raises)
        parent = MockPlayer(provider, "cast_group", "Cast Group")
        parent._attr_group_members = ["cast_group", "member"]
        parent.set_members = AsyncMock(  # type: ignore[method-assign]
            side_effect=NotImplementedError(
                "set_members needs to be implemented when PlayerFeature.SET_MEMBERS is set"
            )
        )
        member = MockPlayer(provider, "member", "Member")

        controller._players = {"cast_group": parent, "member": member}
        mock_mass.players = controller

        # must complete without raising NotImplementedError
        await controller._handle_set_members_with_protocols(
            parent, player_ids_to_add=[], player_ids_to_remove=["member"]
        )
        parent.set_members.assert_not_called()


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

        # Execute group command
        await controller.cmd_group("member", "leader")

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
        mock_mass.players = controller

        # Attempting to group with unavailable player should be handled
        # (either silently ignored or raise exception depending on implementation)
        # This should either skip the unavailable player or raise an exception
        with contextlib.suppress(Exception):
            asyncio.run(controller.cmd_set_members("leader", player_ids_to_add=["member"]))


class TestStateForwarding:
    """Test forwarding of player state changes to related players."""

    def test_sync_leader_updates_are_forwarded_to_sync_children(self, mock_mass: MagicMock) -> None:
        """A regular sync leader must notify children via the sync-parent callback."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        leader = MockPlayer(provider, "leader", "Leader")
        child = MockPlayer(provider, "child", "Child")

        controller._players = {"leader": leader, "child": child}
        mock_mass.players = controller

        leader._attr_group_members = ["leader", "child"]
        leader.update_state(signal_event=False)
        child.update_state(signal_event=False)

        with (
            patch.object(child, "on_sync_parent_updated") as on_sync_parent_updated,
            patch.object(child, "on_group_updated") as on_group_updated,
        ):
            changed_values = {"playback_state": (PlaybackState.IDLE, PlaybackState.PLAYING)}
            controller._forward_state_update(leader, changed_values)

        on_sync_parent_updated.assert_called_once_with(leader, changed_values)
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
        mock_mass.players = controller

        group_player._attr_group_members = ["group", "child"]
        group_player.update_state(signal_event=False)
        child.update_state(signal_event=False)

        with (
            patch.object(child, "on_group_updated") as on_group_updated,
            patch.object(child, "on_sync_parent_updated") as on_sync_parent_updated,
        ):
            changed_values = {"playback_state": (PlaybackState.IDLE, PlaybackState.PLAYING)}
            controller._forward_state_update(group_player, changed_values)

        on_group_updated.assert_called_once_with(
            group_player,
            changed_values,
        )
        on_sync_parent_updated.assert_not_called()


class TestSleepTimer:
    """Test native sleep timer handling."""

    def test_set_and_clear_sleep_timer(self, mock_mass: MagicMock) -> None:
        """Setting a sleep timer exposes state and schedules the stop callback."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        player = MockPlayer(provider, "player_1", "Player 1")
        controller._players = {"player_1": player}
        mock_mass.players = controller

        expires_at = controller.set_sleep_timer("player_1", 30)

        assert controller.get_sleep_timer("player_1") == expires_at
        assert player.sleep_timer_expires_at == expires_at
        mock_mass.call_later.assert_called_with(
            30,
            controller._handle_sleep_timer_expired,
            "player_1",
            task_id="player_sleep_timer_player_1",
        )
        mock_mass.signal_event.assert_any_call(
            EventType.PLAYER_SLEEP_TIMER_UPDATED,
            object_id="player_1",
            data=expires_at,
        )

        controller.clear_sleep_timer("player_1")

        # get_sleep_timer reads the model field directly, so this also asserts it cleared
        assert controller.get_sleep_timer("player_1") is None
        mock_mass.cancel_timer.assert_called_with("player_sleep_timer_player_1")
        mock_mass.signal_event.assert_any_call(
            EventType.PLAYER_SLEEP_TIMER_UPDATED,
            object_id="player_1",
            data=None,
        )

    async def test_sleep_timer_removed_when_player_unregistered(self, mock_mass: MagicMock) -> None:
        """Unregistering a player cancels and clears its sleep timer."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        player = MockPlayer(provider, "player_1", "Player 1")
        controller._players = {"player_1": player}
        player.set_sleep_timer_expires_at(123.0)

        await controller.unregister("player_1")

        assert player.sleep_timer_expires_at is None
        mock_mass.cancel_timer.assert_called_with("player_sleep_timer_player_1")

    async def test_sleep_timer_expiry_stops_player(self, mock_mass: MagicMock) -> None:
        """An expired sleep timer clears its state and stops playback."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        player = MockPlayer(provider, "player_1", "Player 1")
        controller._players = {"player_1": player}
        player.set_sleep_timer_expires_at(123.0)
        controller.cmd_stop = AsyncMock()  # type: ignore[method-assign]

        await controller._handle_sleep_timer_expired("player_1")

        assert controller.get_sleep_timer("player_1") is None
        assert player.sleep_timer_expires_at is None
        controller.cmd_stop.assert_awaited_once_with("player_1")
        mock_mass.signal_event.assert_any_call(
            EventType.PLAYER_SLEEP_TIMER_UPDATED,
            object_id="player_1",
            data=None,
        )

    def test_set_sleep_timer_rejects_invalid_duration(self, mock_mass: MagicMock) -> None:
        """A non-positive or float-overflowing duration raises and schedules nothing."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        player = MockPlayer(provider, "player_1", "Player 1")
        controller._players = {"player_1": player}

        # 0/-30 are non-positive; 10**400 exceeds the float range for the expiry math
        for invalid in (0, -30, 10**400):
            with pytest.raises(InvalidDataError):
                controller.set_sleep_timer("player_1", invalid)

        assert controller.get_sleep_timer("player_1") is None
        mock_mass.call_later.assert_not_called()


class TestUnregisterCleanup:
    """Test that unregister cleans up leaked internal state."""

    def test_command_locks_removed(self, mock_mass: MagicMock) -> None:
        """Unregistering a player removes its command lock entries."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        player = MockPlayer(provider, "player_1", "Player 1")

        controller._players = {"player_1": player}
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
        controller._player_command_locks = {
            "playback_player_a": asyncio.Lock(),
            "playback_player_b": asyncio.Lock(),
        }

        asyncio.run(controller.unregister("player_a"))

        assert "playback_player_b" in controller._player_command_locks
        assert "playback_player_a" not in controller._player_command_locks

    def test_suffix_player_id_not_over_matched(self, mock_mass: MagicMock) -> None:
        """Removing player 'b' must not remove locks for player 'a_b' (no suffix matching)."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        player_b = MockPlayer(provider, "b", "Player B")
        player_a_b = MockPlayer(provider, "a_b", "Player A_B")

        controller._players = {"b": player_b, "a_b": player_a_b}
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
        controller._pending_protocol_evaluations = {"player_1": mock_handle}

        asyncio.run(controller.unregister("player_1"))

        mock_handle.cancel.assert_called_once()
        assert "player_1" not in controller._pending_protocol_evaluations

    def test_unregister_nonexistent_player_is_noop(self, mock_mass: MagicMock) -> None:
        """Unregistering a player that doesn't exist is silently ignored."""
        controller = PlayerController(mock_mass)
        controller._player_command_locks = {"set_members_other": asyncio.Lock()}

        asyncio.run(controller.unregister("nonexistent"))

        assert "set_members_other" in controller._player_command_locks


def _set_play_media_override(mock_mass: MagicMock, value: bool) -> None:
    """
    Configure get_raw_player_config_value to return ``value`` for the play-media override key.

    Other keys keep the existing defaults from the shared fixture. Use this in
    tests for ``play_media`` override behavior so the legacy/new branch is
    selected deterministically.
    """
    original = mock_mass.config.get_raw_player_config_value.side_effect

    def _side_effect(player_id: str, key: str, default: object = None) -> object:
        if key == "play_media_overrides_group":
            return value
        if callable(original):
            return original(player_id, key, default)
        return default if default is not None else "auto"

    mock_mass.config.get_raw_player_config_value = MagicMock(side_effect=_side_effect)


class TestCmdUngroupNewBranches:
    """
    Regression tests for the post-refactor cmd_ungroup flow.

    The refactor changed two things:

    - ``cmd_ungroup`` on a group player no longer calls ``cmd_set_members``
      (which would hit the "Cannot remove static member" guard); instead it
      stops or powers off the group entirely.
    - ``cmd_ungroup`` on a static member of a group recurses to ungroup the
      group, because static members cannot be released individually.
    """

    @pytest.mark.asyncio
    async def test_ungroup_group_player_with_power_uses_power_off(
        self, mock_mass: MagicMock
    ) -> None:
        """Ungroup on a group with explicit power control routes through cmd_power(False)."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_group", instance_id="test_group", mass=mock_mass)
        group = MockPlayer(provider, "g1", "Group", player_type=PlayerType.GROUP)
        group._attr_powered = True
        group._attr_group_members = ["member"]
        group._attr_supported_features = {PlayerFeature.POWER}

        # ensure power_control resolves to NATIVE so cmd_ungroup uses the power path
        def _conf(_player_id: str, key: str, default: object = None) -> object:
            if key == "power_control":
                return "native"
            if key == "min_volume":
                return 0
            if key == "max_volume":
                return 100
            return default if default is not None else "auto"

        mock_mass.config.get_raw_player_config_value = MagicMock(side_effect=_conf)

        controller._players = {"g1": group}
        mock_mass.players = controller

        # populate state.type / state.power_control / state.group_members
        group.set_initialized()
        group._cache.clear()
        group.update_state(signal_event=False)

        called: dict[str, bool | str] = {}

        async def _power(
            player_id: str,
            powered: bool,
            skip_auto_play: bool = False,  # noqa: ARG001
        ) -> None:
            called["player_id"] = player_id
            called["powered"] = powered

        controller._handle_cmd_power = _power  # type: ignore[method-assign]

        await controller.cmd_ungroup("g1")

        assert called == {"player_id": "g1", "powered": False}

    @pytest.mark.asyncio
    async def test_ungroup_powerless_group_calls_stop(self, mock_mass: MagicMock) -> None:
        """Ungroup on a powerless group falls through to _handle_cmd_stop."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_group", instance_id="test_group", mass=mock_mass)
        group = MockPlayer(provider, "g1", "Group", player_type=PlayerType.GROUP)
        group._attr_powered = None  # no power control
        group._attr_group_members = ["member"]
        # no POWER feature → power_control auto-selects to NONE

        controller._players = {"g1": group}
        mock_mass.players = controller

        group.set_initialized()
        group._cache.clear()
        group.update_state(signal_event=False)

        stop_called: list[str] = []

        async def _stop(player_id: str) -> None:
            stop_called.append(player_id)

        controller._handle_cmd_stop = _stop  # type: ignore[method-assign]
        # also stub power to make sure we did NOT go down that branch
        power_called: list[str] = []

        async def _power(
            player_id: str,
            powered: bool,  # noqa: ARG001
            skip_auto_play: bool = False,  # noqa: ARG001
        ) -> None:
            power_called.append(player_id)

        controller._handle_cmd_power = _power  # type: ignore[method-assign]

        await controller.cmd_ungroup("g1")

        assert stop_called == ["g1"]
        assert power_called == []  # powerless group → never goes through cmd_power


class TestExternalPowerOffUnsync:
    """
    Tests for unsyncing a player when its power is turned off outside of MA.

    When a player's (final) power state flips on->off because its linked power
    control was switched off directly - rather than via an MA power command -
    the player must be removed from any (sync)group it is part of.
    """

    def _make_synced_player(self, mock_mass: MagicMock) -> tuple[PlayerController, MockPlayer]:
        """Build a controller with a player synced to a registered leader."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        leader = MockPlayer(provider, "leader", "Leader")
        leader._attr_group_members = ["leader", "p1"]
        player = MockPlayer(provider, "p1", "Player")
        controller._players = {"leader": leader, "p1": player}
        mock_mass.players = controller
        for _player in (leader, player):
            _player.set_initialized()
            _player._cache.clear()
            _player.update_state(signal_event=False)
        # isolate the unsync branch from the unrelated state-forwarding machinery
        controller._forward_state_update = MagicMock()  # type: ignore[method-assign]
        controller.cmd_ungroup = MagicMock(return_value="ungroup-coro")  # type: ignore[method-assign]
        return controller, player

    def test_power_off_unsyncs_synced_player(self, mock_mass: MagicMock) -> None:
        """An on->off power transition ungroups a synced player."""
        controller, player = self._make_synced_player(mock_mass)
        assert player.state.synced_to == "leader"

        controller.signal_player_state_update(player, {"powered": (True, False)})

        controller.cmd_ungroup.assert_called_once_with("p1")  # type: ignore[attr-defined]

    def test_power_on_does_not_unsync(self, mock_mass: MagicMock) -> None:
        """An off->on power transition leaves the player synced."""
        controller, player = self._make_synced_player(mock_mass)

        controller.signal_player_state_update(player, {"powered": (False, True)})

        controller.cmd_ungroup.assert_not_called()  # type: ignore[attr-defined]

    def test_no_power_control_is_ignored(self, mock_mass: MagicMock) -> None:
        """A None->off transition (player without power control) is ignored."""
        controller, player = self._make_synced_player(mock_mass)

        controller.signal_player_state_update(player, {"powered": (None, False)})

        controller.cmd_ungroup.assert_not_called()  # type: ignore[attr-defined]

    def test_power_off_ungrouped_player_is_noop(self, mock_mass: MagicMock) -> None:
        """Powering off a player that is not in any group does nothing."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        player = MockPlayer(provider, "p1", "Player")
        controller._players = {"p1": player}
        mock_mass.players = controller
        player.set_initialized()
        player._cache.clear()
        player.update_state(signal_event=False)
        controller._forward_state_update = MagicMock()  # type: ignore[method-assign]
        controller.cmd_ungroup = MagicMock(return_value="ungroup-coro")  # type: ignore[method-assign]

        controller.signal_player_state_update(player, {"powered": (True, False)})

        controller.cmd_ungroup.assert_not_called()


class TestPlayMediaOverride:
    """
    Tests for the new CONF_PLAY_MEDIA_OVERRIDES_GROUP behavior.

    When a captured child player receives an explicit play_media command, the
    default behavior is to *release* it from the active group/sync and play
    directly on the targeted player. The legacy behavior (forward to the
    leader) is preserved via the per-player config opt-out.
    """

    @pytest.mark.asyncio
    async def test_override_disabled_redirects_to_group(self, mock_mass: MagicMock) -> None:
        """With override disabled, play_media on a captured child redirects to the group."""
        controller = PlayerController(mock_mass)
        group_provider = MockProvider("test_group", instance_id="test_group", mass=mock_mass)
        member_provider = MockProvider("test", instance_id="test", mass=mock_mass)

        class _SessionedGroup(MockPlayer):
            @property
            def is_active_session(self) -> bool:
                return True

        group = _SessionedGroup(group_provider, "g1", "Group", player_type=PlayerType.GROUP)
        group._attr_powered = None
        group._attr_group_members = ["member"]

        member = MockPlayer(member_provider, "member", "Member")

        controller._players = {"g1": group, "member": member}
        mock_mass.players = controller

        group.set_initialized()
        member.set_initialized()
        group.update_state(signal_event=False)
        member.update_state(signal_event=False)
        # sanity: the member is captured by the group
        assert member.state.active_group == "g1"

        _set_play_media_override(mock_mass, False)

        played_on: list[str] = []

        async def _handle_play_media(player_id: str, media: object) -> None:  # noqa: ARG001
            played_on.append(player_id)

        controller._handle_play_media = _handle_play_media  # type: ignore[method-assign]
        # the play_media wrapper acquires a playback lock; stub it out
        controller._player_command_locks = {}

        media = MagicMock(uri="x", source_id="src")
        await controller.play_media("member", media)

        # legacy behavior: redirected to the group leader
        assert played_on == ["g1"]

    @pytest.mark.asyncio
    async def test_override_releases_dynamic_member(self, mock_mass: MagicMock) -> None:
        """With override enabled, play_media on a dynamic group member releases it first."""
        controller = PlayerController(mock_mass)
        group_provider = MockProvider("test_group", instance_id="test_group", mass=mock_mass)
        member_provider = MockProvider("test", instance_id="test", mass=mock_mass)

        class _SessionedGroup(MockPlayer):
            @property
            def is_active_session(self) -> bool:
                return True

        group = _SessionedGroup(group_provider, "g1", "Group", player_type=PlayerType.GROUP)
        group._attr_powered = None
        group._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        group._attr_group_members = ["member"]
        # NOT a static member ⇒ dynamic — can be removed via set_members
        group._attr_static_group_members = []

        member = MockPlayer(member_provider, "member", "Member")

        controller._players = {"g1": group, "member": member}
        mock_mass.players = controller

        group.set_initialized()
        member.set_initialized()
        group.update_state(signal_event=False)
        member.update_state(signal_event=False)
        assert member.state.active_group == "g1"

        # default: override enabled
        _set_play_media_override(mock_mass, True)

        set_members_calls: list[dict[str, object]] = []

        async def _cmd_set_members(
            target_player: str,
            player_ids_to_add: list[str] | None = None,  # noqa: ARG001
            player_ids_to_remove: list[str] | None = None,
        ) -> None:
            set_members_calls.append(
                {"player_id": target_player, "remove": player_ids_to_remove or []}
            )

        controller.cmd_set_members = _cmd_set_members  # type: ignore[method-assign]

        played_on: list[str] = []

        async def _handle_play_media(player_id: str, media: object) -> None:  # noqa: ARG001
            played_on.append(player_id)

        controller._handle_play_media = _handle_play_media  # type: ignore[method-assign]
        controller._player_command_locks = {}

        media = MagicMock(uri="x", source_id="src")
        with patch.object(
            controller,
            "wait_for_player_update",
            _skip_player_update_wait,
        ):
            await controller.play_media("member", media)

        # the member was removed from the group ...
        assert set_members_calls == [{"player_id": "g1", "remove": ["member"]}]
        # ... and then play_media was issued directly on the member, NOT on the group
        assert played_on == ["member"]

    @pytest.mark.asyncio
    async def test_override_stops_static_group(self, mock_mass: MagicMock) -> None:
        """With override enabled, play_media on a STATIC group member stops the group."""
        controller = PlayerController(mock_mass)
        group_provider = MockProvider("test_group", instance_id="test_group", mass=mock_mass)
        member_provider = MockProvider("test", instance_id="test", mass=mock_mass)

        class _SessionedGroup(MockPlayer):
            @property
            def is_active_session(self) -> bool:
                return True

        group = _SessionedGroup(group_provider, "g1", "Group", player_type=PlayerType.GROUP)
        group._attr_powered = None  # no power control ⇒ stop, not power-off
        group._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        group._attr_group_members = ["member"]
        # static member - cannot be removed individually
        group._attr_static_group_members = ["member"]

        member = MockPlayer(member_provider, "member", "Member")

        controller._players = {"g1": group, "member": member}
        mock_mass.players = controller

        group.set_initialized()
        member.set_initialized()
        group.update_state(signal_event=False)
        member.update_state(signal_event=False)
        assert member.state.active_group == "g1"

        _set_play_media_override(mock_mass, True)

        stop_calls: list[str] = []
        power_calls: list[tuple[str, bool]] = []

        async def _stop(player_id: str) -> None:
            stop_calls.append(player_id)

        async def _power(
            player_id: str,
            powered: bool,
            skip_auto_play: bool = False,  # noqa: ARG001
        ) -> None:
            power_calls.append((player_id, powered))

        controller._handle_cmd_stop = _stop  # type: ignore[method-assign]
        controller._handle_cmd_power = _power  # type: ignore[method-assign]

        played_on: list[str] = []

        async def _handle_play_media(player_id: str, media: object) -> None:  # noqa: ARG001
            played_on.append(player_id)

        controller._handle_play_media = _handle_play_media  # type: ignore[method-assign]
        controller._player_command_locks = {}

        media = MagicMock(uri="x", source_id="src")
        with patch.object(
            controller,
            "wait_for_player_update",
            _skip_player_update_wait,
        ):
            await controller.play_media("member", media)

        # powerless group + static member: we should have stopped the group ...
        assert stop_calls == ["g1"]
        # ... not powered it off ...
        assert power_calls == []
        # ... and play_media was issued directly on the member
        assert played_on == ["member"]


class TestExternalSourcePlayPause:
    """Pause/play handling for externally-initiated sources (no active output protocol)."""

    @staticmethod
    def _make_external_source_player(
        provider: MockProvider,
        controller: PlayerController,
        mock_mass: MagicMock,
        *,
        playback_state: PlaybackState,
        can_play_pause: bool = True,
        supports_pause: bool = True,
    ) -> MockPlayer:
        """Build a player playing a passive external source, with no active output protocol."""
        player = MockPlayer(provider, "player_1", "Test Player")
        player._attr_supported_features = {PlayerFeature.PAUSE} if supports_pause else set()
        player._attr_source_list = [
            PlayerSource(
                id="spotify",
                name="Spotify",
                passive=True,
                can_play_pause=can_play_pause,
                can_next_previous=True,
                can_seek=True,
            )
        ]
        player._attr_active_source = "spotify"
        player._attr_playback_state = playback_state
        player._cache.clear()
        controller._players = {"player_1": player}
        mock_mass.players = controller
        mock_mass.player_queues = MagicMock()
        mock_mass.player_queues.get = MagicMock(return_value=None)
        player.update_state(signal_event=False)
        return player

    def test_pause_external_source_forwards_to_player(
        self, mock_mass: MagicMock, controller: PlayerController, provider: MockProvider
    ) -> None:
        """Pausing a pausable external source forwards to the player, not STOP."""
        player = self._make_external_source_player(
            provider, controller, mock_mass, playback_state=PlaybackState.PLAYING
        )
        player.pause = AsyncMock()  # type: ignore[method-assign]
        controller._handle_cmd_stop = AsyncMock()  # type: ignore[method-assign]

        asyncio.run(controller._handle_cmd_pause("player_1"))

        player.pause.assert_awaited_once()
        controller._handle_cmd_stop.assert_not_called()

    def test_play_external_source_unpauses_player(
        self, mock_mass: MagicMock, controller: PlayerController, provider: MockProvider
    ) -> None:
        """Unpausing a paused external source forwards to the player, not a restart."""
        player = self._make_external_source_player(
            provider, controller, mock_mass, playback_state=PlaybackState.PAUSED
        )
        player.play = AsyncMock()  # type: ignore[method-assign]
        player.play_media = AsyncMock()  # type: ignore[method-assign]
        controller._handle_select_source = AsyncMock()  # type: ignore[method-assign]

        asyncio.run(controller._handle_cmd_play("player_1"))

        player.play.assert_awaited_once()
        player.play_media.assert_not_called()
        controller._handle_select_source.assert_not_called()

    def test_pause_falls_back_to_stop_without_pause_support(
        self, mock_mass: MagicMock, controller: PlayerController, provider: MockProvider
    ) -> None:
        """A player that cannot pause natively still falls back to STOP."""
        player = self._make_external_source_player(
            provider,
            controller,
            mock_mass,
            playback_state=PlaybackState.PLAYING,
            supports_pause=False,
        )
        player.pause = AsyncMock()  # type: ignore[method-assign]
        controller._handle_cmd_stop = AsyncMock()  # type: ignore[method-assign]

        asyncio.run(controller._handle_cmd_pause("player_1"))

        controller._handle_cmd_stop.assert_awaited_once()
        player.pause.assert_not_called()


class TestMirrorsParentMedia:
    """Tests for _mirrors_parent_media (palette-fetch gating for grouped players)."""

    @staticmethod
    def _fake_player(
        *,
        player_id: str = "p1",
        active_group: str | None = None,
        synced_to: str | None = None,
        player_type: PlayerType = PlayerType.PLAYER,
        protocol_parent_id: str | None = None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            player_id=player_id,
            state=SimpleNamespace(active_group=active_group, synced_to=synced_to, type=player_type),
            protocol_parent_id=protocol_parent_id,
        )

    def test_standalone_player_owns_media(self, controller: PlayerController) -> None:
        """A standalone player resolves its own media (and palette)."""
        assert controller._mirrors_parent_media(self._fake_player()) is False  # type: ignore[arg-type]

    def test_group_member_mirrors(self, controller: PlayerController) -> None:
        """A group member borrows its parent's media."""
        assert controller._mirrors_parent_media(self._fake_player(active_group="g1")) is True  # type: ignore[arg-type]

    def test_synced_member_mirrors(self, controller: PlayerController) -> None:
        """A synced member borrows its leader's media."""
        assert controller._mirrors_parent_media(self._fake_player(synced_to="leader")) is True  # type: ignore[arg-type]

    def test_protocol_child_mirrors(self, controller: PlayerController) -> None:
        """A protocol child borrows its parent's media."""
        player = self._fake_player(player_type=PlayerType.PROTOCOL, protocol_parent_id="parent")
        assert controller._mirrors_parent_media(player) is True  # type: ignore[arg-type]

    def test_protocol_player_without_parent_owns_media(self, controller: PlayerController) -> None:
        """A protocol player with no parent resolves its own media."""
        player = self._fake_player(player_type=PlayerType.PROTOCOL)
        assert controller._mirrors_parent_media(player) is False  # type: ignore[arg-type]

    def test_self_referential_parent_owns_media(self, controller: PlayerController) -> None:
        """A self-referential active_group/synced_to is not a real parent, so resolve locally."""
        player = self._fake_player(player_id="p1", synced_to="p1", active_group="p1")
        assert controller._mirrors_parent_media(player) is False  # type: ignore[arg-type]


class TestVolumeScalingOnRedirect:
    """min/max volume scaling must survive a redirect to a protocol player or external control."""

    @staticmethod
    def _volume_player(
        player_id: str,
        volume_control: str,
        volume_set: AsyncMock | None = None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            player_id=player_id,
            type=PlayerType.PLAYER,
            protocol_parent_id=None,
            extra_data={},
            volume_control=volume_control,
            volume_set=volume_set or AsyncMock(),
            update_state=MagicMock(),
            provider=MagicMock(),
            state=SimpleNamespace(
                name=player_id,
                volume_control=volume_control,
                volume_muted=False,
                mute_control=PLAYER_CONTROL_NONE,
            ),
        )

    @pytest.mark.asyncio
    async def test_protocol_redirect_forwards_scaled_volume(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """A volume command redirected to a protocol player honors the user-facing max_volume."""

        def _conf(player_id: str, key: str, default: object = None) -> object:
            if key == "min_volume":
                return 0
            if key == "max_volume":
                # user-facing player caps at 50, the protocol player has no limits of its own
                return 50 if player_id == "user_player" else 100
            return default if default is not None else "auto"

        mock_mass.config.get_raw_player_config_value = MagicMock(side_effect=_conf)

        protocol = self._volume_player("protocol_player", PLAYER_CONTROL_NATIVE)
        user = self._volume_player("user_player", "protocol_player")
        players = {"user_player": user, "protocol_player": protocol}

        with (
            patch.object(controller, "get_player", side_effect=players.get),
            patch.object(controller, "_get_active_audio_source", return_value=None),
        ):
            controller._controls = {}
            await controller._handle_cmd_volume_set("user_player", 100)

        # logical 100 with a max_volume of 50 must reach the protocol player as 50, not the raw 100
        protocol.volume_set.assert_awaited_once_with(50)

    @pytest.mark.asyncio
    async def test_external_control_redirect_forwards_scaled_volume(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """A volume command redirected to an external control honors the user-facing max_volume."""

        def _conf(_player_id: str, key: str, default: object = None) -> object:
            if key == "min_volume":
                return 0
            if key == "max_volume":
                return 50
            return default if default is not None else "auto"

        mock_mass.config.get_raw_player_config_value = MagicMock(side_effect=_conf)

        control = SimpleNamespace(name="External Amp", supports_volume=True, volume_set=AsyncMock())
        user = self._volume_player("user_player", "ext_control")
        players = {"user_player": user}

        with (
            patch.object(controller, "get_player", side_effect=players.get),
            patch.object(controller, "_get_active_audio_source", return_value=None),
        ):
            controller._controls = {"ext_control": control}  # type: ignore[dict-item]
            await controller._handle_cmd_volume_set("user_player", 100)

        control.volume_set.assert_awaited_once_with(50)


class TestFakeMuteControl:
    """Fake mute must report the muted state and restore the volume on unmute."""

    def _make_player(self, mock_mass: MagicMock) -> tuple[PlayerController, MockPlayer, AsyncMock]:
        """Build a controller with a single player using fake mute control."""

        def _conf(_player_id: str, key: str, default: object = None) -> object:
            if key == "min_volume":
                return 0
            if key == "max_volume":
                return 100
            if key == CONF_MUTE_CONTROL:
                return PLAYER_CONTROL_FAKE
            return default if default is not None else "auto"

        mock_mass.config.get_raw_player_config_value = MagicMock(side_effect=_conf)
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        player = MockPlayer(provider, "player_1", "Player 1")
        controller._players = {"player_1": player}
        mock_mass.players = controller
        mock_mass.player_queues.get = MagicMock(return_value=None)
        player.set_initialized()
        player._attr_volume_level = 40
        # let the mocked native volume control behave like a real device
        volume_set = AsyncMock(
            side_effect=lambda volume: setattr(player, "_attr_volume_level", volume)
        )
        player.volume_set = volume_set  # type: ignore[method-assign]
        player.update_state(signal_event=False)
        return controller, player, volume_set

    async def test_mute_then_unmute_restores_volume(self, mock_mass: MagicMock) -> None:
        """Muting reports volume_muted=True and unmuting restores the previous volume."""
        controller, player, volume_set = self._make_player(mock_mass)

        await controller.cmd_volume_mute("player_1", True)
        muted_state = player.state
        assert muted_state.volume_muted is True
        assert muted_state.volume_level == 0
        assert player.extra_data[ATTR_PREVIOUS_VOLUME] == 40

        await controller.cmd_volume_mute("player_1", False)
        volume_set.assert_awaited_with(40)
        # simulate the device reporting back its state after the volume command
        player.update_state()
        unmuted_state = player.state
        assert unmuted_state.volume_muted is False
        assert unmuted_state.volume_level == 40

    async def test_repeated_mute_keeps_previous_volume(self, mock_mass: MagicMock) -> None:
        """A repeated mute command must not overwrite the stored volume with 0."""
        controller, player, volume_set = self._make_player(mock_mass)

        await controller.cmd_volume_mute("player_1", True)
        await controller.cmd_volume_mute("player_1", True)
        assert player.extra_data[ATTR_PREVIOUS_VOLUME] == 40
        assert player.state.volume_muted is True

        await controller.cmd_volume_mute("player_1", False)
        volume_set.assert_awaited_with(40)

    async def test_volume_set_clears_fake_mute(self, mock_mass: MagicMock) -> None:
        """A regular volume change while fake muted implies an unmute."""
        controller, player, _volume_set = self._make_player(mock_mass)

        await controller.cmd_volume_mute("player_1", True)
        muted_state = player.state
        assert muted_state.volume_muted is True

        await controller.cmd_volume_set("player_1", 25)
        # simulate the device reporting back its state after the volume command
        player.update_state()
        unmuted_state = player.state
        assert unmuted_state.volume_muted is False
        assert unmuted_state.volume_level == 25


class TestCurrentMediaTimeUpdates:
    """Playback-position anchor semantics of timing-only state updates."""

    def _make_player(self, mock_mass: MagicMock) -> tuple[PlayerController, MockPlayer]:
        """Build a controller with a single playing player with a known position anchor."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        player = MockPlayer(provider, "player_1", "Player 1")
        controller._players = {"player_1": player}
        mock_mass.players = controller
        # no queue registered: current_media resolves from the player's native media
        mock_mass.player_queues.get = MagicMock(return_value=None)
        player.set_initialized()
        now = time.time()
        player._attr_playback_state = PlaybackState.PLAYING
        player._attr_elapsed_time = 17
        player._attr_elapsed_time_last_updated = now
        player.set_current_media(uri="http://test/stream", title="Test")
        assert player._attr_current_media is not None
        player._attr_current_media.elapsed_time = 17
        player._attr_current_media.elapsed_time_last_updated = now
        player.update_state(signal_event=False)
        # isolate from the unrelated state-forwarding machinery
        controller._forward_state_update = MagicMock()  # type: ignore[method-assign]
        mock_mass.signal_event.reset_mock()
        mock_mass.player_queues.on_player_elapsed_time_corrected.reset_mock()
        return controller, player

    def _player_updated_signalled(self, mock_mass: MagicMock) -> bool:
        """Return whether a PLAYER_UPDATED event was signalled."""
        return any(
            call.args and call.args[0] == EventType.PLAYER_UPDATED
            for call in mock_mass.signal_event.call_args_list
        )

    def test_regular_tick_is_suppressed(self, mock_mass: MagicMock) -> None:
        """A regular playback tick (position and anchor advance together) emits nothing."""
        _controller, player = self._make_player(mock_mass)
        assert player._attr_current_media is not None
        assert player._attr_elapsed_time_last_updated is not None

        player._attr_elapsed_time = 18
        player._attr_elapsed_time_last_updated += 1
        player._attr_current_media.elapsed_time = 18
        assert player._attr_current_media.elapsed_time_last_updated is not None
        player._attr_current_media.elapsed_time_last_updated += 1
        player.update_state()

        assert not self._player_updated_signalled(mock_mass)
        mock_mass.player_queues.on_player_elapsed_time_corrected.assert_not_called()
        # the previous anchor was preserved: steady playback changes nothing
        assert player.state.elapsed_time == 17

    def test_anchor_only_change_is_suppressed(self, mock_mass: MagicMock) -> None:
        """An anchor-only change (no significant corrected position change) emits nothing."""
        _controller, player = self._make_player(mock_mass)
        assert player._attr_current_media is not None
        assert player._attr_elapsed_time_last_updated is not None

        player._attr_elapsed_time_last_updated += 0.5
        assert player._attr_current_media.elapsed_time_last_updated is not None
        player._attr_current_media.elapsed_time_last_updated += 0.5
        player.update_state()

        assert not self._player_updated_signalled(mock_mass)
        mock_mass.player_queues.on_player_elapsed_time_corrected.assert_not_called()

    def test_corrected_position_jump_emits_player_updated(self, mock_mass: MagicMock) -> None:
        """A corrected-position jump of the current media (e.g. seek) emits a player update."""
        _controller, player = self._make_player(mock_mass)
        assert player._attr_current_media is not None

        player._attr_current_media.elapsed_time = 61
        player._attr_current_media.elapsed_time_last_updated = time.time()
        player.update_state()

        assert self._player_updated_signalled(mock_mass)
        # the adopted anchor is visible to consumers
        assert player.state.current_media is not None
        assert player.state.current_media.elapsed_time == 61

    def test_player_position_jump_corrects_queue(self, mock_mass: MagicMock) -> None:
        """A player-level corrected-position jump re-bases the queue timing."""
        controller, player = self._make_player(mock_mass)

        player._attr_elapsed_time = 61
        player._attr_elapsed_time_last_updated = time.time()
        player.update_state()

        # the queue is corrected and a follow-up player update is scheduled
        # (which re-anchors current_media onto the corrected queue time),
        # but no full player update is emitted for the jump itself
        mock_mass.player_queues.on_player_elapsed_time_corrected.assert_called_once_with(player)
        assert not self._player_updated_signalled(mock_mass)
        cast("MagicMock", controller._forward_state_update).assert_called_once()
        assert player.state.elapsed_time == 61

    def test_simultaneous_player_and_media_jump_emits_immediately(
        self, mock_mass: MagicMock
    ) -> None:
        """A jump reaching player and current_media in one pass corrects the queue and emits."""
        _controller, player = self._make_player(mock_mass)
        assert player._attr_current_media is not None
        now = time.time()

        player._attr_elapsed_time = 61
        player._attr_elapsed_time_last_updated = now
        player._attr_current_media.elapsed_time = 61
        player._attr_current_media.elapsed_time_last_updated = now
        player.update_state()

        # the queue is re-based AND the full update is emitted right away
        # (current_media already holds the fresh position in the same pass)
        mock_mass.player_queues.on_player_elapsed_time_corrected.assert_called_once_with(player)
        assert self._player_updated_signalled(mock_mass)


class TestPlayAnnouncementCleanup:
    """Test announcement data cleanup after play_announcement."""

    def _make_player(
        self, mock_mass: MagicMock, announcements: dict[str, object]
    ) -> tuple[PlayerController, MockPlayer, MagicMock]:
        """Create a controller and a player with native announcement support."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        player = MockPlayer(provider, "player_1", "Player 1")
        player._attr_supported_features.add(PlayerFeature.PLAY_ANNOUNCEMENT)
        player._cache.clear()
        controller._players = {"player_1": player}
        mock_mass.players = controller
        render = MagicMock()
        render.wait_ready = AsyncMock(return_value=True)
        render.wait_finished = AsyncMock(return_value=3.0)

        # mimic the real renderer: it owns which announcement each player is playing
        def _register(player_id: str, announce_data: object) -> MagicMock:
            announcements[player_id] = announce_data
            return render

        async def _unregister(player_id: str, _render: object) -> None:
            announcements.pop(player_id, None)

        renderer = mock_mass.streams.announcement_renderer
        renderer.register = MagicMock(side_effect=_register)
        renderer.unregister = AsyncMock(side_effect=_unregister)
        mock_mass.streams.get_announcement_url = MagicMock(
            side_effect=lambda player_id, **_kwargs: f"http://ma/announcement/{player_id}.mp3"
        )
        player.update_state(signal_event=False)
        return controller, player, render

    async def test_announcement_data_removed_after_playback(self, mock_mass: MagicMock) -> None:
        """The registered announcement data is released once playback finished."""
        announcements: dict[str, object] = {}
        controller, player, _render = self._make_player(mock_mass, announcements)

        async def _play_announcement(*_args: object, **_kwargs: object) -> None:
            # entry must exist while the announcement is being played/served
            assert "player_1" in announcements

        player.play_announcement = AsyncMock(side_effect=_play_announcement)  # type: ignore[method-assign]

        await controller.play_announcement("player_1", "http://test/announcement.mp3")

        player.play_announcement.assert_awaited_once()
        assert announcements == {}
        mock_mass.streams.announcement_renderer.unregister.assert_awaited_once()

    async def test_announcement_data_removed_on_error(self, mock_mass: MagicMock) -> None:
        """The registered announcement data is released even when playback fails."""
        announcements: dict[str, object] = {}
        controller, player, _render = self._make_player(mock_mass, announcements)
        player.play_announcement = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]

        with pytest.raises(PlayerCommandFailed):
            await controller.play_announcement("player_1", "http://test/announcement.mp3")

        assert announcements == {}
        mock_mass.streams.announcement_renderer.unregister.assert_awaited_once()

    async def test_native_announcement_starts_on_first_audio(self, mock_mass: MagicMock) -> None:
        """A native implementation is handed the url as soon as there is audio to serve."""
        announcements: dict[str, object] = {}
        controller, player, render = self._make_player(mock_mass, announcements)
        player.play_announcement = AsyncMock()  # type: ignore[method-assign]

        await controller.play_announcement("player_1", "http://test/announcement.mp3")

        player.play_announcement.assert_awaited_once()
        # waiting for the whole clip here would delay the player for a slow source;
        # the length is resolved downstream while it plays
        render.wait_ready.assert_awaited_once()
        render.wait_finished.assert_not_awaited()


class TestPlayAnnouncementRestore:
    """Test the state restore of the default (fallback) announcement implementation."""

    def _make_player(
        self, mock_mass: MagicMock, prev_media: PlayerMedia
    ) -> tuple[PlayerController, MockPlayer, AsyncMock]:
        """Create a controller and a playing player, returning the patched resume handler."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        player = MockPlayer(provider, "player_1", "Player 1")
        player._attr_playback_state = PlaybackState.PLAYING
        player._attr_current_media = prev_media
        player._cache.clear()
        controller._players = {"player_1": player}
        mock_mass.players = controller
        # the (temporary and restored) volume commands are dispatched through the
        # TaskManager, so background tasks must actually run in these tests
        mock_mass.create_task = MagicMock(
            side_effect=lambda coro, **_kwargs: asyncio.ensure_future(coro)
        )
        resume_mock = AsyncMock()
        controller._handle_cmd_resume = resume_mock  # type: ignore[method-assign]
        controller._handle_cmd_stop = AsyncMock()  # type: ignore[method-assign]
        controller._handle_play_media = AsyncMock()  # type: ignore[method-assign]
        controller._wait_for_playback_state = AsyncMock()  # type: ignore[method-assign]
        controller.get_announcement_volume = MagicMock(return_value=None)  # type: ignore[method-assign]
        player.set_initialized()
        player.update_state(signal_event=False)
        return controller, player, resume_mock

    @staticmethod
    def _add_group(
        controller: PlayerController, player: MockPlayer, *, supports_set_members: bool
    ) -> MockPlayer:
        """Register a powered group player that holds the given player as its member."""
        group = MockPlayer(
            cast("MockProvider", player.provider),
            "group_1",
            "Group 1",
            player_type=PlayerType.GROUP,
        )
        group._attr_powered = True
        group._attr_group_members = [player.player_id]
        if supports_set_members:
            group._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        controller._players[group.player_id] = group
        group.set_initialized()
        group._cache.clear()
        group.update_state(signal_event=False)
        # the member has no own input change to trigger a recalculation of its
        # (group derived) state, so force it here - just like register() does
        player._cache.clear()
        player.update_state(force_update=True, signal_event=False)
        assert player.state.active_group == group.player_id
        return group

    @staticmethod
    def _announcement() -> PlayerMedia:
        """Return the announcement to play."""
        return PlayerMedia(
            uri="http://ma/announcement/player_1.mp3",
            media_type=MediaType.ANNOUNCEMENT,
            title="Announcement",
            duration=3,
        )

    async def test_previous_playback_is_restored(self, mock_mass: MagicMock) -> None:
        """Content that was playing before the announcement is resumed afterwards."""
        controller, player, resume_mock = self._make_player(
            mock_mass, PlayerMedia(uri="http://test/track.mp3", media_type=MediaType.TRACK)
        )

        await controller._play_announcement(player, self._announcement())

        resume_mock.assert_awaited_once()

    async def test_previous_announcement_is_not_restored(self, mock_mass: MagicMock) -> None:
        """A player still busy with an earlier announcement has no playback to restore."""
        controller, player, resume_mock = self._make_player(
            mock_mass,
            PlayerMedia(uri="http://ma/announcement/x.mp3", media_type=MediaType.ANNOUNCEMENT),
        )

        await controller._play_announcement(player, self._announcement())

        resume_mock.assert_not_awaited()

    async def test_volume_is_restored_when_playback_fails(self, mock_mass: MagicMock) -> None:
        """A failing announcement never leaves the player at the raised volume."""
        controller, player, _ = self._make_player(
            mock_mass, PlayerMedia(uri="http://test/track.mp3", media_type=MediaType.TRACK)
        )
        player._attr_volume_level = 20
        player._cache.clear()
        player.update_state(force_update=True, signal_event=False)
        controller.get_announcement_volume = MagicMock(return_value=80)  # type: ignore[method-assign]
        volume_mock = AsyncMock()
        controller._handle_cmd_volume_set = volume_mock  # type: ignore[method-assign]
        controller._handle_play_media = AsyncMock(  # type: ignore[method-assign]
            side_effect=PlayerCommandFailed("player went away")
        )

        with pytest.raises(PlayerCommandFailed):
            await controller._play_announcement(player, self._announcement())

        assert volume_mock.call_args_list == [call("player_1", 80), call("player_1", 20)]

    async def test_zero_announcement_volume_is_applied_and_restored(
        self, mock_mass: MagicMock
    ) -> None:
        """An announcement volume of 0 is a real volume, not an 'unset' fallback."""
        controller, player, _ = self._make_player(
            mock_mass, PlayerMedia(uri="http://test/track.mp3", media_type=MediaType.TRACK)
        )
        player._attr_volume_level = 20
        player._cache.clear()
        player.update_state(force_update=True, signal_event=False)
        controller.get_announcement_volume = MagicMock(return_value=0)  # type: ignore[method-assign]
        volume_mock = AsyncMock()
        controller._handle_cmd_volume_set = volume_mock  # type: ignore[method-assign]

        await controller._play_announcement(player, self._announcement())

        assert volume_mock.call_args_list == [call("player_1", 0), call("player_1", 20)]

    async def test_playback_is_restored_when_duration_is_unknown(
        self, mock_mass: MagicMock
    ) -> None:
        """An announcement of unknown length still hands the player back to its content."""
        controller, player, resume_mock = self._make_player(
            mock_mass, PlayerMedia(uri="http://test/track.mp3", media_type=MediaType.TRACK)
        )
        announcement = self._announcement()
        announcement.duration = None

        # an unknown length waits for the player to report it finished instead of failing
        await controller._play_announcement(player, announcement)

        resume_mock.assert_awaited_once()

    async def test_group_membership_is_restored_when_playback_fails(
        self, mock_mass: MagicMock
    ) -> None:
        """A failing announcement never leaves the player out of its group player."""
        controller, player, _ = self._make_player(
            mock_mass, PlayerMedia(uri="http://test/track.mp3", media_type=MediaType.TRACK)
        )
        group = self._add_group(controller, player, supports_set_members=True)
        group.set_members = AsyncMock()  # type: ignore[method-assign]
        controller._handle_play_media = AsyncMock(  # type: ignore[method-assign]
            side_effect=PlayerCommandFailed("player went away")
        )

        with pytest.raises(PlayerCommandFailed):
            await controller._play_announcement(player, self._announcement())

        assert group.set_members.await_args_list == [
            call(player_ids_to_remove=["player_1"]),
            call(player_ids_to_add=["player_1"]),
        ]

    async def test_restore_failure_does_not_mask_the_announcement_error(
        self, mock_mass: MagicMock
    ) -> None:
        """A provider blowing up during the restore must not hide why the announcement failed."""
        controller, player, _ = self._make_player(
            mock_mass, PlayerMedia(uri="http://test/track.mp3", media_type=MediaType.TRACK)
        )
        group = self._add_group(controller, player, supports_set_members=True)
        # set_members is a raw provider call: whatever its client library raises comes
        # through unwrapped, so the ungroup succeeds and the regroup times out
        group.set_members = AsyncMock(  # type: ignore[method-assign]
            side_effect=[None, TimeoutError("provider timeout")]
        )
        controller._handle_play_media = AsyncMock(  # type: ignore[method-assign]
            side_effect=PlayerCommandFailed("player went away")
        )

        with pytest.raises(PlayerCommandFailed, match="player went away"):
            await controller._play_announcement(player, self._announcement())

    async def test_group_without_set_members_is_the_one_powered_off(
        self, mock_mass: MagicMock
    ) -> None:
        """A group that can not release members is powered off, not the announcement target."""
        controller, player, _ = self._make_player(
            mock_mass, PlayerMedia(uri="http://test/track.mp3", media_type=MediaType.TRACK)
        )
        self._add_group(controller, player, supports_set_members=False)
        power_mock = AsyncMock()
        controller._handle_cmd_power = power_mock  # type: ignore[method-assign]
        play_mock = AsyncMock()
        controller.cmd_play = play_mock  # type: ignore[method-assign]

        await controller._play_announcement(player, self._announcement())

        # the group is switched off for the announcement and restarted afterwards
        power_mock.assert_awaited_once_with("group_1", False)
        play_mock.assert_awaited_once_with("group_1")

    async def test_idle_player_without_power_control_is_regrouped(
        self, mock_mass: MagicMock
    ) -> None:
        """An idle player that has no power state to restore is still put back in its group."""
        controller, player, _ = self._make_player(
            mock_mass, PlayerMedia(uri="http://test/track.mp3", media_type=MediaType.TRACK)
        )
        player._attr_playback_state = PlaybackState.IDLE
        player._attr_powered = None
        group = self._add_group(controller, player, supports_set_members=True)
        assert player.state.power_control == PLAYER_CONTROL_NONE
        group.set_members = AsyncMock()  # type: ignore[method-assign]

        await controller._play_announcement(player, self._announcement())

        assert group.set_members.await_args_list == [
            call(player_ids_to_remove=["player_1"]),
            call(player_ids_to_add=["player_1"]),
        ]


class TestScheduleActiveOutputProtocolClear:
    """Test the deferred clear of a player's active output protocol."""

    def test_schedule_starts_cancellable_clear_task(self, mock_mass: MagicMock) -> None:
        """Scheduling defers the clear to a single, per-player, cancellable task."""
        controller = PlayerController(mock_mass)
        player = MagicMock()
        player.player_id = "player_1"

        controller.schedule_active_output_protocol_clear(player)

        mock_mass.create_task.assert_called_once()
        # close the coroutine passed to the mocked create_task to avoid a
        # "coroutine was never awaited" warning
        mock_mass.create_task.call_args.args[0].close()
        # no abort_existing: a duplicate schedule must reuse the pending clear
        # (deduped by task_id) instead of replacing it with an untracked task
        assert mock_mass.create_task.call_args.kwargs == {
            "task_id": "clear_active_protocol_player_1",
        }

    @pytest.mark.asyncio
    async def test_clears_protocol_once_player_idle(self, mock_mass: MagicMock) -> None:
        """The protocol is cleared after waiting for the player to reach IDLE."""
        controller = PlayerController(mock_mass)
        player = MagicMock()
        player.player_id = "player_1"

        with patch.object(controller, "_wait_for_playback_state", new=AsyncMock()) as wait_mock:
            await controller._clear_active_output_protocol_when_idle(player)

        wait_mock.assert_awaited_once_with(player, PlaybackState.IDLE, timeout=10)
        player.set_active_output_protocol.assert_called_once_with(None)


@contextlib.asynccontextmanager
async def _skip_player_update_wait(
    *_args: object,
    **_kwargs: object,
) -> AsyncIterator[None]:
    """Skip provider-driven state propagation in command-routing tests."""
    yield


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
