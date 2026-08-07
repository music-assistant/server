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
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from music_assistant_models.auth import User, UserRole
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
from music_assistant_models.player import OutputProtocol, PlayerMedia, PlayerSource
from music_assistant_models.player_control import PlayerControl

from music_assistant.constants import (
    ATTR_FAKE_MUTE,
    ATTR_MUTE_LOCK,
    ATTR_PREVIOUS_VOLUME,
    CONF_AUTO_PLAY,
    CONF_MAX_VOLUME,
    CONF_MIN_VOLUME,
    CONF_MUTE_CONTROL,
    CONF_POWER_CONTROL,
    CONF_VOLUME_CONTROL,
)
from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.webserver.helpers.auth_middleware import current_user
from tests.common import MockPlayer, MockProvider


def _player_config_stub(
    values: dict[str, object] | None = None,
    *,
    min_volume: int = 0,
    max_volume: int = 100,
) -> Callable[..., object]:
    """
    Build a ``get_raw_player_config_value`` side effect.

    :param values: Extra config keys to answer, e.g. ``{CONF_MUTE_CONTROL: PLAYER_CONTROL_FAKE}``.
    :param min_volume: Value returned for the ``min_volume`` key.
    :param max_volume: Value returned for the ``max_volume`` key.
    """
    config: dict[str, object] = {
        CONF_MIN_VOLUME: min_volume,
        CONF_MAX_VOLUME: max_volume,
        **(values or {}),
    }

    def _conf(_player_id: str, key: str, default: object = None) -> object:
        if key in config:
            return config[key]
        return default

    return _conf


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance."""
    mass = MagicMock()
    mass.closing = False
    mass.loop = None
    mass.config = MagicMock()
    mass.config.get = MagicMock(return_value=[])
    mass.config.get_raw_player_config_value = MagicMock(side_effect=_player_config_stub())
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


def _group_with_member(
    mock_mass: MagicMock, *, initialize_group: bool = True
) -> tuple[PlayerController, MockPlayer, MockPlayer]:
    """
    Build a controller holding one group player with a single member.

    :param mock_mass: The mocked MusicAssistant instance to attach the controller to.
    :param initialize_group: Whether the group player is marked as fully registered.

    :return: The controller, the group player and its member.
    """
    controller = PlayerController(mock_mass)
    provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

    group_player = MockPlayer(provider, "group", "Group", player_type=PlayerType.GROUP)
    member = MockPlayer(provider, "member", "Member")

    controller._players = {"group": group_player, "member": member}
    mock_mass.players = controller

    group_player._attr_group_members = ["member"]
    member.initialized.set()
    if initialize_group:
        group_player.initialized.set()
    for player in (group_player, member):
        player.update_state(signal_event=False)
    return controller, group_player, member


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

    def test_sync_leader_updates_also_reach_its_group_player(self, mock_mass: MagicMock) -> None:
        """A sync leader must notify its group player as well as its own sync children."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        group_player = MockPlayer(provider, "group", "Group", player_type=PlayerType.GROUP)
        leader = MockPlayer(provider, "leader", "Leader")
        follower = MockPlayer(provider, "follower", "Follower")

        controller._players = {"group": group_player, "leader": leader, "follower": follower}
        mock_mass.players = controller

        group_player._attr_group_members = ["leader", "follower"]
        leader._attr_group_members = ["leader", "follower"]
        for player in (group_player, leader, follower):
            # _get_player_groups only considers players the controller finished registering
            player.initialized.set()
            player.update_state(signal_event=False)

        with (
            patch.object(group_player, "on_group_member_updated") as on_group_member_updated,
            patch.object(follower, "on_sync_parent_updated") as on_sync_parent_updated,
        ):
            changed_values = {"playback_state": (PlaybackState.IDLE, PlaybackState.PLAYING)}
            controller._forward_state_update(leader, changed_values)

        on_group_member_updated.assert_called_once_with(leader, changed_values)
        on_sync_parent_updated.assert_called_once_with(leader, changed_values)

    def test_group_player_is_notified_under_restricted_user_context(
        self, mock_mass: MagicMock
    ) -> None:
        """The fan-out must not be narrowed by the user that triggered the update."""
        controller, group_player, member = _group_with_member(mock_mass)

        # a non-admin user that may only see the member; the contextvar is copied into
        # the task an API command runs in, so it is live during the state fan-out
        restricted_user = User(
            user_id="user_1",
            username="restricted",
            role=UserRole.USER,
            player_filter=["member"],
        )
        token = current_user.set(restricted_user)
        try:
            assert group_player not in controller.all_players()
            with patch.object(group_player, "on_group_member_updated") as on_group_member_updated:
                changed_values = {"playback_state": (PlaybackState.IDLE, PlaybackState.PLAYING)}
                controller._forward_state_update(member, changed_values)
        finally:
            current_user.reset(token)

        on_group_member_updated.assert_called_once_with(member, changed_values)

    def test_unavailable_group_player_is_still_notified(self, mock_mass: MagicMock) -> None:
        """A group player mirrors its members, so it must update while unavailable too."""
        controller, group_player, member = _group_with_member(mock_mass)
        group_player._attr_available = False
        group_player.update_state(signal_event=False)
        assert group_player.state.available is False

        with patch.object(group_player, "on_group_member_updated") as on_group_member_updated:
            changed_values = {"playback_state": (PlaybackState.IDLE, PlaybackState.PLAYING)}
            controller._forward_state_update(member, changed_values)

        on_group_member_updated.assert_called_once_with(member, changed_values)

    def test_disabled_group_player_is_not_notified(self, mock_mass: MagicMock) -> None:
        """A disabled player takes no part, unlike one that is merely unavailable."""
        controller, group_player, member = _group_with_member(mock_mass)
        group_player._config.enabled = False
        group_player.update_state(signal_event=False, force_update=True)
        assert group_player.state.enabled is False

        with patch.object(group_player, "on_group_member_updated") as on_group_member_updated:
            controller._forward_state_update(
                member, {"playback_state": (PlaybackState.IDLE, PlaybackState.PLAYING)}
            )

        on_group_member_updated.assert_not_called()

    def test_uninitialized_group_player_is_not_notified(self, mock_mass: MagicMock) -> None:
        """A player the controller is still registering is not fully set up yet."""
        controller, group_player, member = _group_with_member(mock_mass, initialize_group=False)

        with patch.object(group_player, "on_group_member_updated") as on_group_member_updated:
            controller._forward_state_update(
                member, {"playback_state": (PlaybackState.IDLE, PlaybackState.PLAYING)}
            )

        on_group_member_updated.assert_not_called()


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
        return default

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
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_player_config_stub({CONF_POWER_CONTROL: PLAYER_CONTROL_NATIVE})
        )

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


class TestProtocolOutputPlayPause:
    """Play/pause on a player rendering through a linked output protocol."""

    @staticmethod
    def _make_player_on_protocol(
        mock_mass: MagicMock,
        controller: PlayerController,
        *,
        playback_state: PlaybackState,
    ) -> MockPlayer:
        """Build a player playing the MA queue through a protocol that cannot pause."""
        native_provider = MockProvider("chromecast", mass=mock_mass)
        player = MockPlayer(native_provider, "player_1", "Test Player")
        player._attr_supported_features.add(PlayerFeature.PAUSE)
        player._attr_playback_state = playback_state

        protocol_provider = MockProvider("sendspin", mass=mock_mass)
        protocol_player = MockPlayer(
            protocol_provider, "proto_1", "Test Protocol", player_type=PlayerType.PROTOCOL
        )
        protocol_player._attr_playback_state = playback_state

        controller._players = {"player_1": player, "proto_1": protocol_player}
        mock_mass.players = controller
        mock_mass.player_queues = MagicMock()
        # a non-empty queue, so the MA queue source advertises play/pause support
        queue = MagicMock()
        queue.items = [MagicMock()]
        mock_mass.player_queues.get = MagicMock(return_value=queue)
        player.set_linked_output_protocols(
            [
                OutputProtocol(
                    output_protocol_id="proto_1",
                    name="Sendspin",
                    protocol_domain="sendspin",
                    priority=40,
                )
            ]
        )
        player.set_active_output_protocol("proto_1")
        player.set_active_mass_source("player_1")
        protocol_player.update_state(signal_event=False)
        player.refresh_state(signal_event=False)
        return player

    async def test_pause_on_protocol_without_pause_falls_back_to_stop(
        self, mock_mass: MagicMock, controller: PlayerController
    ) -> None:
        """The native transport has no session to pause while a protocol renders the audio."""
        player = self._make_player_on_protocol(
            mock_mass, controller, playback_state=PlaybackState.PLAYING
        )
        player.pause = AsyncMock()  # type: ignore[method-assign]
        controller._handle_cmd_stop = AsyncMock()  # type: ignore[method-assign]

        await controller._handle_cmd_pause("player_1")

        player.pause.assert_not_called()
        # STOP goes to the visible player, not the protocol player
        controller._handle_cmd_stop.assert_awaited_once_with("player_1")

    async def test_play_on_protocol_without_pause_does_not_unpause_natively(
        self, mock_mass: MagicMock, controller: PlayerController
    ) -> None:
        """Unpausing must not hit the native transport either; the source is restarted."""
        player = self._make_player_on_protocol(
            mock_mass, controller, playback_state=PlaybackState.PAUSED
        )
        player.play = AsyncMock()  # type: ignore[method-assign]
        controller._handle_select_source = AsyncMock()  # type: ignore[method-assign]

        await controller._handle_cmd_play("player_1")

        player.play.assert_not_called()
        # the MA queue source is restarted, not some other source
        controller._handle_select_source.assert_awaited_once_with("player_1", "player_1")


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
            mute_control=PLAYER_CONTROL_NONE,
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
            return default

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
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_player_config_stub(max_volume=50)
        )

        volume_set = AsyncMock()
        control = PlayerControl(
            id="ext_control",
            provider="test",
            name="External Amp",
            supports_volume=True,
            volume_set=volume_set,
        )
        user = self._volume_player("user_player", "ext_control")
        players = {"user_player": user}

        with (
            patch.object(controller, "get_player", side_effect=players.get),
            patch.object(controller, "_get_active_audio_source", return_value=None),
        ):
            controller._controls = {"ext_control": control}
            await controller._handle_cmd_volume_set("user_player", 100)

        volume_set.assert_awaited_once_with(50)

    @pytest.mark.asyncio
    async def test_external_control_without_volume_support_raises(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """A volume command redirected to a control lacking volume support is rejected."""
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_player_config_stub(max_volume=50)
        )

        volume_set = AsyncMock()
        control = PlayerControl(
            id="ext_control",
            provider="test",
            name="External Amp",
            supports_volume=False,
            volume_set=volume_set,
        )
        user = self._volume_player("user_player", "ext_control")
        players = {"user_player": user}

        with (
            patch.object(controller, "get_player", side_effect=players.get),
            patch.object(controller, "_get_active_audio_source", return_value=None),
        ):
            controller._controls = {"ext_control": control}
            with pytest.raises(UnsupportedFeaturedException):
                await controller._handle_cmd_volume_set("user_player", 100)

        volume_set.assert_not_awaited()


class TestExternalPowerControl:
    """Power commands redirected to an external PlayerControl must forward and gate correctly."""

    def _make_player(
        self, mock_mass: MagicMock, control: PlayerControl
    ) -> tuple[PlayerController, MockPlayer]:
        """Build a controller with a single player whose power control is the given control."""
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_player_config_stub({CONF_POWER_CONTROL: control.id})
        )
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        player = MockPlayer(provider, "player_1", "Player 1")
        controller._controls = {control.id: control}
        controller._players = {"player_1": player}
        mock_mass.players = controller
        # auto-play would otherwise resume the (mocked) player queue on power on
        config_get_value = player.config.get_value
        player.config.get_value = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda key, *args, **kwargs: (
                False if key == CONF_AUTO_PLAY else config_get_value(key, *args, **kwargs)
            )
        )
        player.set_initialized()
        player.update_state(signal_event=False)
        return controller, player

    async def test_power_on_forwards_to_control(self, mock_mass: MagicMock) -> None:
        """Powering on a player redirects to its external control's power_on callback."""
        power_on = AsyncMock()
        power_off = AsyncMock()
        control = PlayerControl(
            id="ext_power",
            provider="test",
            name="External Power",
            supports_power=True,
            power_on=power_on,
            power_off=power_off,
        )

        def _report_powered_on() -> None:
            control.power_state = True

        # the control only reports on once switched on, which releases wait_for_power_on
        power_on.side_effect = _report_powered_on
        controller, player = self._make_player(mock_mass, control)
        assert player.state.powered is False

        await controller._handle_cmd_power("player_1", True)

        power_on.assert_awaited_once()
        power_off.assert_not_awaited()

    async def test_power_on_waits_on_the_control(self, mock_mass: MagicMock) -> None:
        """Powering on waits for the control to report on, not for the player itself."""
        control = PlayerControl(
            id="ext_power",
            provider="test",
            name="External Power",
            supports_power=True,
            power_on=AsyncMock(),
            power_off=AsyncMock(),
        )
        controller, player = self._make_player(mock_mass, control)
        assert player.state.powered is False

        with patch(
            "music_assistant.controllers.players.controller.wait_for_power_on", AsyncMock()
        ) as wait_for_power_on:
            await controller._handle_cmd_power("player_1", True)

        wait_for_power_on.assert_awaited_once()
        assert wait_for_power_on.await_args is not None
        assert wait_for_power_on.await_args.args[2] is control

    async def test_power_off_forwards_to_control(self, mock_mass: MagicMock) -> None:
        """Powering off a player redirects to its external control's power_off callback."""
        power_on = AsyncMock()
        power_off = AsyncMock()
        control = PlayerControl(
            id="ext_power",
            provider="test",
            name="External Power",
            supports_power=True,
            power_state=True,
            power_on=power_on,
            power_off=power_off,
        )
        controller, player = self._make_player(mock_mass, control)
        assert player.state.powered is True

        await controller._handle_cmd_power("player_1", False)

        power_off.assert_awaited_once()
        power_on.assert_not_awaited()

    async def test_control_without_power_support_raises(self, mock_mass: MagicMock) -> None:
        """A power command redirected to a control lacking power support is rejected."""
        power_on = AsyncMock()
        power_off = AsyncMock()
        control = PlayerControl(
            id="ext_power",
            provider="test",
            name="External Power",
            supports_power=False,
            power_on=power_on,
            power_off=power_off,
        )
        controller, player = self._make_player(mock_mass, control)
        assert player.state.powered is False

        with pytest.raises(UnsupportedFeaturedException):
            await controller._handle_cmd_power("player_1", True)

        power_on.assert_not_awaited()
        power_off.assert_not_awaited()


class TestEnforceVolumeLimits:
    """External volume changes outside the min/max range must be corrected."""

    @staticmethod
    def _set_limits(mock_mass: MagicMock, min_volume: int, max_volume: int) -> None:
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_player_config_stub(min_volume=min_volume, max_volume=max_volume)
        )

    @staticmethod
    def _player(logical_volume: int | None) -> SimpleNamespace:
        return SimpleNamespace(
            player_id="user_player",
            state=SimpleNamespace(volume_level=logical_volume),
        )

    def test_out_of_range_volume_is_corrected(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """A device volume above max_volume (logical > 100) is clamped back to logical 100."""
        self._set_limits(mock_mass, 0, 80)
        # device volume 100 with max 80 resolves to logical 125
        player = self._player(125)
        with patch.object(controller, "_handle_cmd_volume_set", MagicMock()) as cmd:
            controller._enforce_volume_limits(cast("MockPlayer", player))
        cmd.assert_called_once_with("user_player", 100)
        mock_mass.create_task.assert_called_once()

    def test_below_min_volume_is_corrected(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """A device volume below min_volume (logical < 0) is clamped back to logical 0."""
        self._set_limits(mock_mass, 20, 100)
        # device volume 10 with min 20 resolves to a negative logical volume
        player = self._player(-13)
        with patch.object(controller, "_handle_cmd_volume_set", MagicMock()) as cmd:
            controller._enforce_volume_limits(cast("MockPlayer", player))
        cmd.assert_called_once_with("user_player", 0)

    def test_in_range_volume_is_untouched(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """A logical volume within 0-100 needs no correction."""
        self._set_limits(mock_mass, 0, 80)
        player = self._player(100)
        with patch.object(controller, "_handle_cmd_volume_set", MagicMock()) as cmd:
            controller._enforce_volume_limits(cast("MockPlayer", player))
        cmd.assert_not_called()

    def test_no_limits_configured_is_noop(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Default 0-100 limits skip enforcement entirely."""
        self._set_limits(mock_mass, 0, 100)
        player = self._player(100)
        with patch.object(controller, "_handle_cmd_volume_set", MagicMock()) as cmd:
            controller._enforce_volume_limits(cast("MockPlayer", player))
        cmd.assert_not_called()

    def test_unknown_volume_is_noop(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """A player without a resolved volume level is left alone."""
        self._set_limits(mock_mass, 0, 80)
        player = self._player(None)
        with patch.object(controller, "_handle_cmd_volume_set", MagicMock()) as cmd:
            controller._enforce_volume_limits(cast("MockPlayer", player))
        cmd.assert_not_called()


class TestFakeMuteControl:
    """Fake mute must report the muted state and restore the volume on unmute."""

    def _make_player(self, mock_mass: MagicMock) -> tuple[PlayerController, MockPlayer, AsyncMock]:
        """Build a controller with a single player using fake mute control."""
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_player_config_stub({CONF_MUTE_CONTROL: PLAYER_CONTROL_FAKE})
        )
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


class TestFakeMuteInGroup:
    """A fake muted player in a group follows the mute lock, just like a native mute."""

    def _make_synced_pair(
        self, mock_mass: MagicMock, *, member_mute_control: str = PLAYER_CONTROL_FAKE
    ) -> tuple[PlayerController, dict[str, MockPlayer]]:
        """
        Build a leader synced to one member.

        :param member_mute_control: Mute control of the member, the leader always uses fake mute.
        """

        def _conf(player_id: str, key: str, default: object = None) -> object:
            if key == CONF_MUTE_CONTROL and player_id == "member":
                return member_mute_control
            return _player_config_stub({CONF_MUTE_CONTROL: PLAYER_CONTROL_FAKE})(
                player_id, key, default
            )

        mock_mass.config.get_raw_player_config_value = MagicMock(side_effect=_conf)
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        players: dict[str, MockPlayer] = {}
        for player_id in ("leader", "member"):
            player = MockPlayer(provider, player_id, player_id.title())
            player._attr_supported_features = {
                PlayerFeature.VOLUME_SET,
                PlayerFeature.VOLUME_MUTE,
            }
            player._attr_volume_level = 50
            # let the mocked native volume control behave like a real device
            player.volume_set = AsyncMock(  # type: ignore[method-assign]
                side_effect=lambda volume, _player=player: setattr(
                    _player, "_attr_volume_level", volume
                )
            )
            players[player_id] = player
        players["leader"]._attr_group_members = ["member"]
        controller._players = dict(players)
        mock_mass.players = controller
        mock_mass.player_queues.get = MagicMock(return_value=None)
        for player in players.values():
            player.set_initialized()
            player._cache.clear()
            player.update_state(signal_event=False)
        # a second pass, so the group volume of the leader accounts for its member
        for player in players.values():
            player.update_state(signal_event=False)
        return controller, players

    async def test_group_volume_keeps_a_muted_pair_muted(self, mock_mass: MagicMock) -> None:
        """A group volume change may not bring a muted fake mute pair back to life."""
        controller, players = self._make_synced_pair(mock_mass)
        await controller.cmd_group_volume_mute("leader", True)

        await controller.cmd_group_volume("leader", 30)

        for player in players.values():
            player.update_state()
            assert player.state.volume_muted is True
            assert player.state.volume_level == 0

    async def test_group_volume_down_keeps_a_muted_member_muted(self, mock_mass: MagicMock) -> None:
        """Turning a group down leaves a single muted member silent, at its own volume."""
        controller, players = self._make_synced_pair(mock_mass)
        await controller.cmd_volume_mute("member", True)
        # let the group volume of the leader account for the muted member
        players["leader"].update_state(signal_event=False)

        await controller.cmd_group_volume("leader", 25)

        for player in players.values():
            player.update_state()
        member_state = players["member"].state
        assert member_state.volume_muted is True
        assert member_state.volume_level == 0
        # the player that is not muted follows the group volume as usual
        assert players["leader"].state.volume_level == 25
        # unmuting brings the member back at the volume it had before it was muted
        await controller.cmd_volume_mute("member", False)
        players["member"].update_state()
        assert players["member"].state.volume_level == 50

    async def test_unmute_restores_the_volume_from_before_the_mute(
        self, mock_mass: MagicMock
    ) -> None:
        """A group volume change while muted may not alter the volume to restore."""
        controller, players = self._make_synced_pair(mock_mass)
        await controller.cmd_group_volume_mute("leader", True)
        await controller.cmd_group_volume("leader", 30)

        await controller.cmd_group_volume_mute("leader", False)

        for player in players.values():
            player.update_state()
            assert player.state.volume_muted is False
            assert player.state.volume_level == 50

    async def test_group_volume_keeps_a_mixed_pair_muted(self, mock_mass: MagicMock) -> None:
        """Members with a different mute control stay muted alike on a group volume change."""
        controller, players = self._make_synced_pair(
            mock_mass, member_mute_control=PLAYER_CONTROL_NATIVE
        )
        mute = AsyncMock(
            side_effect=lambda muted: setattr(players["member"], "_attr_volume_muted", muted)
        )
        players["member"].volume_mute = mute  # type: ignore[method-assign]
        await controller.cmd_group_volume_mute("leader", True)

        await controller.cmd_group_volume("leader", 30)

        mute.assert_awaited_once_with(True)
        for player in players.values():
            player.update_state()
            assert player.state.volume_muted is True


class TestMuteControlGuard:
    """Muting is gated on the mute control, independently of the volume control."""

    def _make_player(
        self,
        mock_mass: MagicMock,
        mute_control: str,
        volume_control: str,
        controls: dict[str, PlayerControl] | None = None,
    ) -> tuple[PlayerController, MockPlayer]:
        """Build a controller with a single player using the given control config."""
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_player_config_stub(
                {CONF_MUTE_CONTROL: mute_control, CONF_VOLUME_CONTROL: volume_control}
            )
        )
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        player = MockPlayer(provider, "player_1", "Player 1")
        controller._players = {"player_1": player}
        controller._controls = controls or {}
        mock_mass.players = controller
        mock_mass.player_queues.get = MagicMock(return_value=None)
        player.set_initialized()
        player.update_state(signal_event=False)
        return controller, player

    async def test_external_mute_control_without_volume_control(self, mock_mass: MagicMock) -> None:
        """A player without volume control still mutes through an external PlayerControl."""
        mute_set = AsyncMock()
        control = PlayerControl(
            id="ext_mute",
            provider="test",
            name="External Mute",
            supports_mute=True,
            mute_set=mute_set,
        )
        controller, player = self._make_player(
            mock_mass,
            mute_control="ext_mute",
            volume_control=PLAYER_CONTROL_NONE,
            controls={"ext_mute": control},
        )
        assert player.mute_control == "ext_mute"

        await controller.cmd_volume_mute("player_1", True)
        mute_set.assert_awaited_once_with(True)

    async def test_native_mute_control_without_volume_control(self, mock_mass: MagicMock) -> None:
        """A player without volume control still mutes natively."""
        controller, player = self._make_player(
            mock_mass,
            mute_control=PLAYER_CONTROL_NATIVE,
            volume_control=PLAYER_CONTROL_NONE,
        )
        volume_mute = AsyncMock()
        player.volume_mute = volume_mute  # type: ignore[method-assign]

        await controller.cmd_volume_mute("player_1", True)
        volume_mute.assert_awaited_once_with(True)

    async def test_mute_control_none_raises(self, mock_mass: MagicMock) -> None:
        """A player with volume control but no mute control rejects the command."""
        controller, player = self._make_player(
            mock_mass,
            mute_control=PLAYER_CONTROL_NONE,
            volume_control=PLAYER_CONTROL_NATIVE,
        )
        volume_mute = AsyncMock()
        player.volume_mute = volume_mute  # type: ignore[method-assign]

        with pytest.raises(UnsupportedFeaturedException):
            await controller.cmd_volume_mute("player_1", True)
        volume_mute.assert_not_awaited()

    async def test_fake_mute_without_volume_control_raises(self, mock_mass: MagicMock) -> None:
        """Fake mute needs a volume control to drive, so it rejects the command outright."""
        controller, player = self._make_player(
            mock_mass,
            mute_control=PLAYER_CONTROL_FAKE,
            volume_control=PLAYER_CONTROL_NONE,
        )
        player._attr_volume_level = 40

        with pytest.raises(UnsupportedFeaturedException):
            await controller.cmd_volume_mute("player_1", True)
        assert ATTR_PREVIOUS_VOLUME not in player.extra_data
        assert ATTR_FAKE_MUTE not in player.extra_data

    async def test_vanished_mute_control_raises(self, mock_mass: MagicMock) -> None:
        """A mute control that disappeared after being resolved is reported, not ignored."""
        control = PlayerControl(
            id="ext_mute",
            provider="test",
            name="External Mute",
            supports_mute=True,
            mute_set=AsyncMock(),
        )
        controller, player = self._make_player(
            mock_mass,
            mute_control="ext_mute",
            volume_control=PLAYER_CONTROL_NONE,
            controls={"ext_mute": control},
        )
        # the resolved control is cached on the player, so removing it here leaves
        # the player pointing at a control that no longer exists
        assert player.mute_control == "ext_mute"
        controller._controls = {}

        with pytest.raises(UnsupportedFeaturedException):
            await controller.cmd_volume_mute("player_1", True)

    async def test_unmute_clears_mute_lock_without_mute_control(self, mock_mass: MagicMock) -> None:
        """Unmuting clears a mute lock left behind by a since-removed mute control."""
        controller, player = self._make_player(
            mock_mass,
            mute_control=PLAYER_CONTROL_NONE,
            volume_control=PLAYER_CONTROL_NATIVE,
        )
        player.extra_data[ATTR_MUTE_LOCK] = True

        with pytest.raises(UnsupportedFeaturedException):
            await controller.cmd_volume_mute("player_1", False)
        assert ATTR_MUTE_LOCK not in player.extra_data

    async def test_failed_mute_sets_no_mute_lock(self, mock_mass: MagicMock) -> None:
        """A grouped player whose mute command failed is not left holding a mute lock."""
        control = PlayerControl(
            id="ext_mute",
            provider="test",
            name="External Mute",
            supports_mute=False,
        )
        controller, player = self._make_player(
            mock_mass,
            mute_control="ext_mute",
            volume_control=PLAYER_CONTROL_NONE,
            controls={"ext_mute": control},
        )
        player.state.synced_to = "leader"

        with pytest.raises(UnsupportedFeaturedException):
            await controller.cmd_volume_mute("player_1", True)
        assert ATTR_MUTE_LOCK not in player.extra_data

    async def test_failed_mute_keeps_existing_mute_lock(self, mock_mass: MagicMock) -> None:
        """A failed mute leaves the lock of an earlier successful mute in place."""
        control = PlayerControl(
            id="ext_mute",
            provider="test",
            name="External Mute",
            supports_mute=False,
        )
        controller, player = self._make_player(
            mock_mass,
            mute_control="ext_mute",
            volume_control=PLAYER_CONTROL_NONE,
            controls={"ext_mute": control},
        )
        player.state.synced_to = "leader"
        player.extra_data[ATTR_MUTE_LOCK] = True

        with pytest.raises(UnsupportedFeaturedException):
            await controller.cmd_volume_mute("player_1", True)
        assert player.extra_data[ATTR_MUTE_LOCK] is True


class TestGroupMuteMemberFilter:
    """Group mute skips members that have no mute control of their own."""

    async def test_member_without_mute_control_is_skipped(self, mock_mass: MagicMock) -> None:
        """A member without a mute control must not fail the whole group command."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        leader = MockPlayer(provider, "leader", "Leader")
        leader._attr_supported_features = {PlayerFeature.VOLUME_SET, PlayerFeature.VOLUME_MUTE}
        leader._attr_group_members = ["leader", "member"]
        member = MockPlayer(provider, "member", "Member")
        member._attr_supported_features = {PlayerFeature.VOLUME_SET}
        controller._players = {"leader": leader, "member": member}
        mock_mass.players = controller
        mock_mass.player_queues.get = MagicMock(return_value=None)
        for player in (leader, member):
            player.set_initialized()
            player.update_state(signal_event=False)
        leader_mute = AsyncMock()
        leader.volume_mute = leader_mute  # type: ignore[method-assign]

        await controller.cmd_group_volume_mute("leader", True)
        leader_mute.assert_awaited_once_with(True)


class TestGroupPlayerMuteRedirect:
    """A mute command on a group player is handled at group level."""

    def _setup(self, mock_mass: MagicMock) -> tuple[PlayerController, MockPlayer, MockPlayer]:
        """Build a controller with a group player holding a single mute capable member."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        group = MockPlayer(provider, "group", "Group", player_type=PlayerType.GROUP)
        group._attr_supported_features = {PlayerFeature.VOLUME_SET, PlayerFeature.VOLUME_MUTE}
        group._attr_group_members = ["member"]
        member = MockPlayer(provider, "member", "Member")
        member._attr_supported_features = {PlayerFeature.VOLUME_SET, PlayerFeature.VOLUME_MUTE}
        controller._players = {"group": group, "member": member}
        mock_mass.players = controller
        mock_mass.player_queues.get = MagicMock(return_value=None)
        for player in (group, member):
            player.set_initialized()
            player.update_state(signal_event=False)
        return controller, group, member

    async def test_mute_on_group_player_is_forwarded_to_members(self, mock_mass: MagicMock) -> None:
        """A group player has no mute of its own, so the members must be muted instead."""
        controller, _group, member = self._setup(mock_mass)
        member_mute = AsyncMock()
        member.volume_mute = member_mute  # type: ignore[method-assign]

        await controller.cmd_volume_mute("group", True)

        member_mute.assert_awaited_once_with(True)

    async def test_mute_on_group_player_without_own_mute_control(
        self, mock_mass: MagicMock
    ) -> None:
        """A group that has no mute control of its own must still mute its members."""
        controller, group, member = self._setup(mock_mass)
        group._attr_supported_features = {PlayerFeature.VOLUME_SET}
        group._cache.clear()
        group.update_state(signal_event=False)
        assert group.mute_control == PLAYER_CONTROL_NONE
        member_mute = AsyncMock()
        member.volume_mute = member_mute  # type: ignore[method-assign]

        await controller.cmd_volume_mute("group", True)

        member_mute.assert_awaited_once_with(True)

    async def test_mute_on_group_player_without_mute_capable_members(
        self, mock_mass: MagicMock
    ) -> None:
        """A group whose members cannot mute must not raise, just like group mute itself."""
        controller, _group, member = self._setup(mock_mass)
        member._attr_supported_features = {PlayerFeature.VOLUME_SET}
        member._cache.clear()
        member.update_state(signal_event=False)

        await controller.cmd_volume_mute("group", True)


class TestGroupMuteOnNonGroupPlayer:
    """A group mute command works on any player, just like the group volume command."""

    def _setup(
        self, mock_mass: MagicMock, *members: str
    ) -> tuple[PlayerController, dict[str, MockPlayer]]:
        """Build a controller with a mute capable leader synced to the given members."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        players: dict[str, MockPlayer] = {}
        for player_id in ("leader", *members):
            player = MockPlayer(provider, player_id, player_id.title())
            player._attr_supported_features = {
                PlayerFeature.VOLUME_SET,
                PlayerFeature.VOLUME_MUTE,
            }
            player._attr_volume_level = 50
            players[player_id] = player
        if members:
            # the leader is not listed as its own member here, so the tests also cover
            # that a sync leader is injected into its own final group_members
            players["leader"]._attr_group_members = list(members)
        controller._players = dict(players)
        mock_mass.players = controller
        mock_mass.player_queues.get = MagicMock(return_value=None)
        for player in players.values():
            player.set_initialized()
            player._cache.clear()
            player.update_state(signal_event=False)
        return controller, players

    def _stub_mutes(self, players: dict[str, MockPlayer]) -> dict[str, AsyncMock]:
        """Replace the native mute command of every given player with a mock."""
        mutes: dict[str, AsyncMock] = {}
        for player_id, player in players.items():
            mutes[player_id] = AsyncMock()
            player.volume_mute = mutes[player_id]  # type: ignore[method-assign]
        return mutes

    async def test_group_mute_on_synced_member_redirects_to_leader(
        self, mock_mass: MagicMock
    ) -> None:
        """A member of a sync group mutes the whole group through its sync leader."""
        controller, players = self._setup(mock_mass, "member")
        assert players["member"].state.synced_to == "leader"
        mutes = self._stub_mutes(players)

        await controller.cmd_group_volume_mute("member", True)

        mutes["leader"].assert_awaited_once_with(True)
        mutes["member"].assert_awaited_once_with(True)
        assert ATTR_MUTE_LOCK in players["member"].extra_data

    async def test_group_mute_on_sync_leader_mutes_the_leader_once(
        self, mock_mass: MagicMock
    ) -> None:
        """A sync leader is part of its own member list, so it must be muted only once."""
        controller, players = self._setup(mock_mass, "member")
        mutes = self._stub_mutes(players)

        await controller.cmd_group_volume_mute("leader", True)

        mutes["leader"].assert_awaited_once_with(True)
        mutes["member"].assert_awaited_once_with(True)

    async def test_group_mute_on_plain_player_mutes_that_player(self, mock_mass: MagicMock) -> None:
        """A player that is not grouped at all is muted as a normal player."""
        controller, players = self._setup(mock_mass)
        mutes = self._stub_mutes(players)

        await controller.cmd_group_volume_mute("leader", True)

        mutes["leader"].assert_awaited_once_with(True)

    async def test_group_mute_on_plain_player_without_mute_control(
        self, mock_mass: MagicMock
    ) -> None:
        """A plain player that cannot mute reports that, just like a normal mute command."""
        controller, players = self._setup(mock_mass)
        players["leader"]._attr_supported_features = {PlayerFeature.VOLUME_SET}
        players["leader"]._cache.clear()
        players["leader"].update_state(signal_event=False)

        with pytest.raises(UnsupportedFeaturedException):
            await controller.cmd_group_volume_mute("leader", True)

    async def test_group_unmute_on_synced_member_redirects_to_leader(
        self, mock_mass: MagicMock
    ) -> None:
        """Unmuting through a member clears the mute (and mute lock) of every group member."""
        controller, players = self._setup(mock_mass, "member")
        mutes = self._stub_mutes(players)
        players["member"].extra_data[ATTR_MUTE_LOCK] = True

        await controller.cmd_group_volume_mute("member", False)

        mutes["leader"].assert_awaited_once_with(False)
        mutes["member"].assert_awaited_once_with(False)
        assert ATTR_MUTE_LOCK not in players["member"].extra_data

    async def test_group_mute_locks_the_sync_leader_too(self, mock_mass: MagicMock) -> None:
        """A sync leader is as much part of the group as its members, so it is locked too."""
        controller, players = self._setup(mock_mass, "member")
        self._stub_mutes(players)

        await controller.cmd_group_volume_mute("leader", True)

        assert ATTR_MUTE_LOCK in players["leader"].extra_data
        assert ATTR_MUTE_LOCK in players["member"].extra_data

    async def test_group_volume_keeps_a_muted_sync_pair_muted(self, mock_mass: MagicMock) -> None:
        """A group volume change may not half-unmute a muted pair of directly synced players."""
        controller, players = self._setup(mock_mass, "member")
        self._stub_mutes(players)
        await controller.cmd_group_volume_mute("leader", True)
        # the mock players do not act on the mute command, so reflect it in their state
        for player in players.values():
            player._attr_volume_muted = True
            player.update_state(signal_event=False)
            player.volume_set = AsyncMock()  # type: ignore[method-assign]
        # re-stub so only the mute commands of the group volume change are counted
        mutes = self._stub_mutes(players)

        await controller.cmd_group_volume("leader", 30)

        for mute in mutes.values():
            mute.assert_not_awaited()


class TestMuteLockAfterUngroup:
    """A mute lock is only honored while the player it belongs to is still grouped."""

    def _make_synced_pair(
        self, mock_mass: MagicMock, member_mute_control: str
    ) -> tuple[PlayerController, dict[str, MockPlayer]]:
        """
        Build a leader with one synced member.

        :param member_mute_control: Mute control to configure on both players.
        """
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_player_config_stub({CONF_MUTE_CONTROL: member_mute_control})
        )
        controller = PlayerController(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        players: dict[str, MockPlayer] = {}
        for player_id in ("leader", "member"):
            player = MockPlayer(provider, player_id, player_id.title())
            player._attr_supported_features = {
                PlayerFeature.VOLUME_SET,
                PlayerFeature.VOLUME_MUTE,
            }
            player._attr_volume_level = 50
            player.volume_set = AsyncMock(  # type: ignore[method-assign]
                side_effect=lambda volume, _player=player: setattr(
                    _player, "_attr_volume_level", volume
                )
            )
            players[player_id] = player
        players["leader"]._attr_group_members = ["member"]
        controller._players = dict(players)
        mock_mass.players = controller
        mock_mass.player_queues.get = MagicMock(return_value=None)
        for player in players.values():
            player.set_initialized()
            player._cache.clear()
            player.update_state(signal_event=False)
        return controller, players

    def _dissolve_group(self, players: dict[str, MockPlayer]) -> None:
        """Drop the sync group, the way a provider side topology change does."""
        players["leader"]._attr_group_members = []
        for player in players.values():
            player.refresh_state(signal_event=False)

    async def test_fake_muted_player_follows_volume_again(self, mock_mass: MagicMock) -> None:
        """A fake muted player is no longer forced silent once its group is gone."""
        controller, players = self._make_synced_pair(mock_mass, PLAYER_CONTROL_FAKE)
        await controller.cmd_volume_mute("member", True)
        self._dissolve_group(players)

        await controller.cmd_volume_set("member", 70)

        players["member"].update_state()
        assert players["member"].state.volume_level == 70
        assert players["member"].state.volume_muted is False

    async def test_natively_muted_player_is_unmuted_again(self, mock_mass: MagicMock) -> None:
        """A natively muted player is auto-unmuted by a volume change once its group is gone."""
        controller, players = self._make_synced_pair(mock_mass, PLAYER_CONTROL_NATIVE)
        mute = AsyncMock(
            side_effect=lambda muted: setattr(players["member"], "_attr_volume_muted", muted)
        )
        players["member"].volume_mute = mute  # type: ignore[method-assign]
        await controller.cmd_volume_mute("member", True)
        self._dissolve_group(players)

        await controller.cmd_volume_set("member", 70)

        assert mute.await_args_list == [call(True), call(False)]
        players["member"].update_state()
        assert players["member"].state.volume_level == 70
        assert players["member"].state.volume_muted is False

    async def test_still_grouped_player_keeps_its_lock(self, mock_mass: MagicMock) -> None:
        """A muted player that is still grouped stays silent on a volume change."""
        controller, players = self._make_synced_pair(mock_mass, PLAYER_CONTROL_FAKE)
        await controller.cmd_volume_mute("member", True)

        await controller.cmd_volume_set("member", 70)

        players["member"].update_state()
        assert players["member"].state.volume_level == 0
        assert players["member"].state.volume_muted is True

    async def test_protocol_player_follows_the_lock_of_its_parent(
        self, mock_mass: MagicMock
    ) -> None:
        """A protocol player inherits the lock of the parent it renders for, group and all."""
        controller, players = self._make_synced_pair(mock_mass, PLAYER_CONTROL_NATIVE)
        member = players["member"]
        protocol_player = MockPlayer(
            MockProvider("sendspin", instance_id="sendspin", mass=mock_mass),
            "proto_member",
            "Member Protocol",
            player_type=PlayerType.PROTOCOL,
        )
        protocol_player._attr_supported_features = {
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
        }
        protocol_player._attr_volume_muted = True
        protocol_player.set_protocol_parent_id("member")
        # an unmute on a protocol player is redirected to the parent it renders for
        mute = AsyncMock()
        member.volume_mute = mute  # type: ignore[method-assign]
        protocol_player.volume_mute = AsyncMock()  # type: ignore[method-assign]
        protocol_player.volume_set = AsyncMock()  # type: ignore[method-assign]
        controller._players["proto_member"] = protocol_player
        member.set_linked_output_protocols(
            [
                OutputProtocol(
                    output_protocol_id="proto_member",
                    name="Sendspin",
                    protocol_domain="sendspin",
                    priority=40,
                )
            ]
        )
        protocol_player.set_initialized()
        protocol_player.update_state(signal_event=False)
        member.refresh_state(signal_event=False)
        member.extra_data[ATTR_MUTE_LOCK] = True

        # a group volume change reaches the protocol player through the internal handler
        await controller._handle_cmd_volume_set("proto_member", 70)
        mute.assert_not_awaited()

        self._dissolve_group(players)
        await controller._handle_cmd_volume_set("proto_member", 70)

        mute.assert_awaited_once_with(False)


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

    @staticmethod
    def _mute_natively(player: MockPlayer) -> AsyncMock:
        """Give the player a native mute control, mute it and return its mute handler."""
        player._attr_supported_features.add(PlayerFeature.VOLUME_MUTE)
        player._attr_volume_muted = True
        mute_mock = AsyncMock(
            side_effect=lambda muted: setattr(player, "_attr_volume_muted", muted)
        )
        player.volume_mute = mute_mock  # type: ignore[method-assign]
        player._cache.clear()
        player.update_state(force_update=True, signal_event=False)
        assert player.state.volume_muted is True
        return mute_mock

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

    async def test_muted_player_is_unmuted_and_muted_back(self, mock_mass: MagicMock) -> None:
        """A muted player hears the announcement and is muted again afterwards."""
        controller, player, _ = self._make_player(
            mock_mass, PlayerMedia(uri="http://test/track.mp3", media_type=MediaType.TRACK)
        )
        mute_mock = self._mute_natively(player)

        await controller._play_announcement(player, self._announcement())

        assert mute_mock.await_args_list == [call(False), call(True)]

    async def test_player_without_volume_control_is_still_unmuted(
        self, mock_mass: MagicMock
    ) -> None:
        """A player that can only be muted is unmuted for the announcement all the same."""
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_player_config_stub({CONF_VOLUME_CONTROL: PLAYER_CONTROL_NONE})
        )
        controller, player, _ = self._make_player(
            mock_mass, PlayerMedia(uri="http://test/track.mp3", media_type=MediaType.TRACK)
        )
        mute_mock = self._mute_natively(player)
        assert player.state.volume_control == PLAYER_CONTROL_NONE

        await controller._play_announcement(player, self._announcement())

        assert mute_mock.await_args_list == [call(False), call(True)]

    async def test_mute_is_restored_before_the_player_is_regrouped(
        self, mock_mass: MagicMock
    ) -> None:
        """A player is handed back to its group already muted, holding on to its mute lock."""
        controller, player, _ = self._make_player(
            mock_mass, PlayerMedia(uri="http://test/track.mp3", media_type=MediaType.TRACK)
        )
        group = self._add_group(controller, player, supports_set_members=True)
        real_set_members = group.set_members

        async def _set_members(**kwargs: list[str]) -> None:
            # let the membership really change, so the player is ungrouped while the
            # announcement plays - just like it is in production. neither player picks
            # the new membership up on its own here, so publish it on both.
            await real_set_members(**kwargs)
            group.update_state(force_update=True, signal_event=False)
            player._cache.clear()
            player.update_state(force_update=True, signal_event=False)

        set_members = AsyncMock(side_effect=_set_members)
        group.set_members = set_members  # type: ignore[method-assign]
        player.extra_data[ATTR_MUTE_LOCK] = True
        recorder = MagicMock()
        recorder.attach_mock(self._mute_natively(player), "mute")
        recorder.attach_mock(set_members, "set_members")

        await controller._play_announcement(player, self._announcement())

        assert recorder.mock_calls == [
            call.set_members(player_ids_to_remove=["player_1"]),
            call.mute(False),
            call.mute(True),
            call.set_members(player_ids_to_add=["player_1"]),
        ]
        # the lock survives the announcement, so the regroup does not unmute the player
        assert player.extra_data[ATTR_MUTE_LOCK] is True

    async def test_unmuted_player_is_left_alone(self, mock_mass: MagicMock) -> None:
        """A player that was not muted is never sent a mute command."""
        controller, player, _ = self._make_player(
            mock_mass, PlayerMedia(uri="http://test/track.mp3", media_type=MediaType.TRACK)
        )
        mute_mock = self._mute_natively(player)
        player._attr_volume_muted = False
        player._cache.clear()
        player.update_state(force_update=True, signal_event=False)
        mute_mock.reset_mock()

        await controller._play_announcement(player, self._announcement())

        mute_mock.assert_not_awaited()

    async def test_mute_is_restored_when_playback_fails(self, mock_mass: MagicMock) -> None:
        """A failing announcement never leaves the player unmuted."""
        controller, player, _ = self._make_player(
            mock_mass, PlayerMedia(uri="http://test/track.mp3", media_type=MediaType.TRACK)
        )
        mute_mock = self._mute_natively(player)
        controller._handle_play_media = AsyncMock(  # type: ignore[method-assign]
            side_effect=PlayerCommandFailed("player went away")
        )

        with pytest.raises(PlayerCommandFailed):
            await controller._play_announcement(player, self._announcement())

        assert mute_mock.await_args_list == [call(False), call(True)]

    async def test_muted_sync_group_members_all_hear_the_announcement(
        self, mock_mass: MagicMock
    ) -> None:
        """Every member of a muted sync group is unmuted, keeping its mute lock."""
        controller, leader, _ = self._make_player(
            mock_mass, PlayerMedia(uri="http://test/track.mp3", media_type=MediaType.TRACK)
        )
        member = MockPlayer(cast("MockProvider", leader.provider), "player_2", "Player 2")
        controller._players["player_2"] = member
        member.set_initialized()
        leader._attr_group_members = ["player_1", "player_2"]
        mute_mocks = {player.player_id: self._mute_natively(player) for player in (leader, member)}
        # both members were muted while grouped, so both hold a mute lock
        for player in (leader, member):
            player.extra_data[ATTR_MUTE_LOCK] = True

        await controller._play_announcement(leader, self._announcement())

        for player_id, mute_mock in mute_mocks.items():
            assert mute_mock.await_args_list == [call(False), call(True)], player_id
            assert controller._players[player_id].extra_data[ATTR_MUTE_LOCK] is True

    async def test_fake_muted_player_announces_at_its_real_volume(
        self, mock_mass: MagicMock
    ) -> None:
        """A fake muted player announces at its real volume, not at the zero it is parked on."""
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_player_config_stub({CONF_MUTE_CONTROL: PLAYER_CONTROL_FAKE})
        )
        controller, player, _ = self._make_player(
            mock_mass, PlayerMedia(uri="http://test/track.mp3", media_type=MediaType.TRACK)
        )

        def _apply_volume(volume: int) -> None:
            player._attr_volume_level = volume
            player.update_state(signal_event=False)

        player._attr_volume_level = 40
        volume_set = AsyncMock(side_effect=_apply_volume)
        player.volume_set = volume_set  # type: ignore[method-assign]
        player._cache.clear()
        player.update_state(force_update=True, signal_event=False)
        await controller.cmd_volume_mute("player_1", True)
        assert player.state.volume_muted is True
        controller.get_announcement_volume = MagicMock(return_value=80)  # type: ignore[method-assign]

        await controller._play_announcement(player, self._announcement())

        # unmute to 40, announce at 80, restore 40 and park back on 0 for the fake mute
        assert volume_set.await_args_list == [
            call(0),
            call(40),
            call(80),
            call(40),
            call(0),
        ]
        assert player.state.volume_muted is True
        assert player.extra_data[ATTR_PREVIOUS_VOLUME] == 40


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


class TestRemovePlayerControl:
    """Test removing a registered player control."""

    def test_removal_refreshes_the_players_that_used_it(self, mock_mass: MagicMock) -> None:
        """Test that only the players configured to use the removed control are refreshed."""
        mock_mass.loop = MagicMock()
        controller = PlayerController(mock_mass)
        using_control = MagicMock()
        using_control.state.power_control = "switch.amp"
        using_control.state.volume_control = PLAYER_CONTROL_NATIVE
        using_control.state.mute_control = PLAYER_CONTROL_NATIVE
        unrelated = MagicMock()
        unrelated.state.power_control = PLAYER_CONTROL_NATIVE
        unrelated.state.volume_control = PLAYER_CONTROL_NATIVE
        unrelated.state.mute_control = PLAYER_CONTROL_NATIVE
        controller._players = {"using_control": using_control, "unrelated": unrelated}
        controller._controls = {
            "switch.amp": PlayerControl(id="switch.amp", provider="test_prov", name="Amp")
        }

        controller.remove_player_control("switch.amp")

        assert controller.player_controls() == []
        mock_mass.loop.call_soon.assert_called_once_with(using_control.refresh_state)

    async def test_a_returning_control_is_picked_back_up(self, mock_mass: MagicMock) -> None:
        """Test that a control removed and registered again re-attaches to its player."""
        # run the scheduled refresh straight away so each step is observable
        mock_mass.loop = MagicMock()
        mock_mass.loop.call_soon.side_effect = lambda callback, *args: callback(*args)
        mock_mass.config.get_raw_player_config_value.side_effect = _player_config_stub(
            {CONF_POWER_CONTROL: "switch.amp"}
        )
        controller = PlayerController(mock_mass)
        mock_mass.players = controller
        provider = MockProvider("test_provider", instance_id="test_prov", mass=mock_mass)
        mock_mass.get_provider.return_value = provider
        player = MockPlayer(provider, "player", "Player")
        controller._players = {"player": player}
        control = PlayerControl(id="switch.amp", provider="test_prov", name="Amp")

        await controller.register_or_update_player_control(control)
        assert player.state.power_control == "switch.amp"

        # the Home Assistant plugin drops and re-registers its controls around a reload
        controller.remove_player_control(control.id)
        assert player.state.power_control == PLAYER_CONTROL_NONE

        await controller.register_or_update_player_control(control)
        assert player.state.power_control == "switch.amp"

    def test_removing_an_unknown_control_does_nothing(self, mock_mass: MagicMock) -> None:
        """Test that removing a control that was never registered is a no-op."""
        mock_mass.loop = MagicMock()
        controller = PlayerController(mock_mass)
        player = MagicMock()
        player.state.power_control = PLAYER_CONTROL_NATIVE
        player.state.volume_control = PLAYER_CONTROL_NATIVE
        player.state.mute_control = PLAYER_CONTROL_NATIVE
        controller._players = {"player": player}

        controller.remove_player_control("switch.gone")

        mock_mass.loop.call_soon.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
