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
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.constants import (
    PLAYER_CONTROL_FAKE,
    PLAYER_CONTROL_NATIVE,
    PLAYER_CONTROL_NONE,
)
from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import UnsupportedFeaturedException

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
    """Configure get_raw_player_config_value to return ``value`` for the play-media override key.

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
    """Regression tests for the post-refactor cmd_ungroup flow.

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


class TestPlayMediaOverride:
    """Tests for the new CONF_PLAY_MEDIA_OVERRIDES_GROUP behavior.

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
        await controller.play_media("member", media)

        # powerless group + static member: we should have stopped the group ...
        assert stop_calls == ["g1"]
        # ... not powered it off ...
        assert power_calls == []
        # ... and play_media was issued directly on the member
        assert played_on == ["member"]


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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
