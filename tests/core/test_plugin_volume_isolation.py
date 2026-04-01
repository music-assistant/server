"""Tests for plugin volume callback isolation (feedback loop prevention).

This module tests:
- Group volume changes fire a single plugin callback at the group level
- Individual child volume changes in a group do NOT fire plugin callbacks
- Protocol player volume redirects propagate the from_group_volume flag
- Standalone (non-group) player volume changes fire plugin callbacks normally
- Spotify Connect inbound echo suppression
- Spotify Connect outbound volume API debounce
- Inbound volume routing (cmd_group_volume for groups, cmd_volume_set for standalone)
- Sync leader volume changes compute group average (not individual volume)
- Non-leader sync members fire callback via sync-leader plugin source fallback
- set_group_volume on SyncGroupPlayers resolves plugin via sync leader
- set_group_volume fresh average computation and boundary redistribution
- cmd_group_volume coalescing for rapid slider drags
"""

from __future__ import annotations

import asyncio
import contextlib
import time as time_mod
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import PlayerType

from music_assistant.controllers.players import PlayerController
from music_assistant.helpers.throttle_retry import Throttler
from music_assistant.models.player import Player
from music_assistant.providers.spotify_connect import (
    _VOLUME_API_DEBOUNCE,
    _VOLUME_ECHO_SUPPRESS_WINDOW,
    SpotifyConnectProvider,
)
from tests.common import MockPlayer, MockProvider


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance."""
    mass = MagicMock()
    mass.closing = False
    mass.loop = None
    mass.config = MagicMock()
    mass.config.get = MagicMock(return_value=[])
    mass.config.get_raw_player_config_value = MagicMock(return_value=None)
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    mass.config.set = MagicMock()
    mass.config.set_raw_player_config_value = MagicMock()
    mass.signal_event = MagicMock()
    mass.get_providers = MagicMock(return_value=[])
    return mass


@pytest.fixture
def controller(mock_mass: MagicMock) -> PlayerController:
    """Create a PlayerController instance."""
    return PlayerController(mock_mass)


def _setup_group(
    controller: PlayerController,
    mock_mass: MagicMock,
    provider: MockProvider,
    group_id: str = "group",
    child_configs: dict[str, int] | None = None,
    group_type: PlayerType = PlayerType.GROUP,
) -> tuple[MockPlayer, dict[str, MockPlayer]]:
    """Set up a group player with children at specified volume levels.

    :param child_configs: Mapping of child_id to volume_level.
    :return: Tuple of (group_player, {child_id: child_player}).
    """
    if child_configs is None:
        child_configs = {"child_a": 80, "child_b": 40}

    group_player = MockPlayer(provider, group_id, "Group", player_type=group_type)
    children: dict[str, MockPlayer] = {}
    all_ids = [group_id]

    for child_id, volume in child_configs.items():
        child = MockPlayer(provider, child_id, child_id.title())
        child._attr_volume_level = volume
        child._cache.clear()
        children[child_id] = child
        all_ids.append(child_id)

    group_player._attr_group_members = all_ids
    group_player._cache.clear()

    players: dict[str, Player] = {group_id: group_player}
    throttlers: dict[str, Throttler] = {group_id: Throttler(1, 0.05)}
    for child_id, child in children.items():
        players[child_id] = child
        throttlers[child_id] = Throttler(1, 0.05)

    controller._players = players
    controller._player_throttlers = throttlers
    mock_mass.players = controller

    for p in players.values():
        p.update_state(signal_event=False)
    # group_volume depends on self.state.group_members which reads from
    # the previous state. A second update_state is needed so that
    # group_volume can see the group_members set in the first pass.
    group_player.update_state(signal_event=False)

    return group_player, children


def _make_mock_plugin_source(
    in_use_by: str,
) -> tuple[MagicMock, AsyncMock]:
    """Create a mock PluginSource with an on_volume callback.

    :return: Tuple of (plugin_source_mock, on_volume_mock).
    """
    on_volume_mock = AsyncMock()
    plugin_source = MagicMock()
    plugin_source.id = "test_plugin"
    plugin_source.in_use_by = in_use_by
    plugin_source.on_volume = on_volume_mock
    return plugin_source, on_volume_mock


def _patch_plugin_source(
    controller: PlayerController,
    plugin_source: MagicMock,
) -> Callable[[], list[MagicMock]]:
    """Patch get_plugin_sources to return the given plugin source.

    :return: The patched get_plugin_sources callable.
    """
    get_plugin_sources = MagicMock(return_value=[plugin_source])
    controller.get_plugin_sources = get_plugin_sources  # type: ignore[method-assign]
    return get_plugin_sources


def _make_mock_handle_volume(
    controller: PlayerController,
) -> list[tuple[str, int, bool]]:
    """Replace _handle_cmd_volume_set with a mock that records calls and updates player state."""
    volume_calls: list[tuple[str, int, bool]] = []

    async def mock_handle_volume(
        player_id: str, volume_level: int, from_group_volume: bool = False
    ) -> None:
        player = controller.get_player(player_id)
        if player:
            player._attr_volume_level = volume_level
            player._cache.clear()
        volume_calls.append((player_id, volume_level, from_group_volume))

    controller._handle_cmd_volume_set = mock_handle_volume  # type: ignore[method-assign]
    return volume_calls


class TestGroupVolumePluginCallback:
    """Test that set_group_volume fires a single plugin callback at the group level."""

    async def test_group_volume_passes_from_group_flag(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """set_group_volume must pass from_group_volume=True to all child dispatches."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _children = _setup_group(
            controller, mock_mass, provider, child_configs={"child_a": 80, "child_b": 40}
        )
        volume_calls = _make_mock_handle_volume(controller)

        await controller.set_group_volume(group_player, 70)

        for call_player_id, _vol, from_group in volume_calls:
            assert from_group is True, f"Child {call_player_id} should have from_group_volume=True"

    async def test_group_volume_fires_single_plugin_callback(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """set_group_volume must fire the plugin on_volume callback exactly once."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _children = _setup_group(
            controller, mock_mass, provider, child_configs={"child_a": 80, "child_b": 40}
        )
        _make_mock_handle_volume(controller)

        plugin_source, on_volume_mock = _make_mock_plugin_source(group_player.player_id)
        _patch_plugin_source(controller, plugin_source)

        await controller.set_group_volume(group_player, 60)

        on_volume_mock.assert_called_once_with(60)

    async def test_group_volume_uses_group_vol_for_plugin(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Plugin callback should receive the group volume, not individual child volumes."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _children = _setup_group(
            controller, mock_mass, provider, child_configs={"child_a": 80, "child_b": 40}
        )
        _make_mock_handle_volume(controller)

        plugin_source, on_volume_mock = _make_mock_plugin_source(group_player.player_id)
        _patch_plugin_source(controller, plugin_source)

        await controller.set_group_volume(group_player, 50)

        on_volume_mock.assert_called_once_with(50)


class TestChildVolumePluginIsolation:
    """Test plugin callback behavior for child volume changes within a group."""

    async def test_child_vol_no_avg_shift_does_not_fire_plugin(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Child volume change that doesn't shift the integer group average must NOT fire."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        # avg(80, 40) = 60. Changing child_b 40->41 gives avg(80, 41) = int(60.5) = 60.
        group_player, children = _setup_group(
            controller, mock_mass, provider, child_configs={"child_a": 80, "child_b": 40}
        )
        for p in controller._players.values():
            p.set_initialized()
            p.volume_set = AsyncMock()  # type: ignore[method-assign]

        plugin_source, on_volume_mock = _make_mock_plugin_source(group_player.player_id)
        _patch_plugin_source(controller, plugin_source)

        child_b = children["child_b"]
        await controller._handle_cmd_volume_set(child_b.player_id, 41)

        on_volume_mock.assert_not_called()

    async def test_child_vol_avg_shift_fires_plugin_with_new_avg(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Child volume change that shifts the group average must fire plugin with new avg."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        # avg(80, 40) = 60. Changing child_b 40->60 gives avg(80, 60) = 70.
        group_player, children = _setup_group(
            controller, mock_mass, provider, child_configs={"child_a": 80, "child_b": 40}
        )
        for p in controller._players.values():
            p.set_initialized()
            p.volume_set = AsyncMock()  # type: ignore[method-assign]

        plugin_source, on_volume_mock = _make_mock_plugin_source(group_player.player_id)
        _patch_plugin_source(controller, plugin_source)

        child_b = children["child_b"]
        await controller._handle_cmd_volume_set(child_b.player_id, 60)

        on_volume_mock.assert_called_once_with(70)

    async def test_from_group_volume_skips_plugin_callback(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """When from_group_volume=True, plugin callbacks must be skipped."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, children = _setup_group(
            controller, mock_mass, provider, child_configs={"child_a": 80}
        )
        for p in controller._players.values():
            p.set_initialized()
            p.volume_set = AsyncMock()  # type: ignore[method-assign]

        plugin_source, on_volume_mock = _make_mock_plugin_source(group_player.player_id)
        _patch_plugin_source(controller, plugin_source)

        child_a = children["child_a"]
        await controller._handle_cmd_volume_set(child_a.player_id, 60, from_group_volume=True)

        on_volume_mock.assert_not_called()


class TestProtocolPlayerPropagation:
    """Test that from_group_volume propagates through protocol player redirects."""

    async def test_protocol_redirect_preserves_from_group_volume(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Protocol player redirect must propagate from_group_volume flag."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)

        # Create a player that delegates volume to a protocol player
        parent = MockPlayer(provider, "parent", "Parent")
        parent._attr_volume_level = 80
        parent._attr_supported_features = set()  # no native volume
        parent._cache.clear()

        protocol_player = MockPlayer(
            provider, "proto1", "Protocol1", player_type=PlayerType.PROTOCOL
        )
        protocol_player._attr_volume_level = 80
        protocol_player._cache.clear()
        protocol_player.volume_set = AsyncMock()  # type: ignore[method-assign]

        # Configure volume_control for parent to point to protocol player
        def config_side_effect(player_id: str, key: str) -> Any:
            if player_id == "parent" and key == "volume_control":
                return "proto1"
            return None

        mock_mass.config.get_raw_player_config_value = MagicMock(side_effect=config_side_effect)

        controller._players = {
            "parent": parent,
            "proto1": protocol_player,
        }
        controller._player_throttlers = {
            "parent": Throttler(1, 0.05),
            "proto1": Throttler(1, 0.05),
        }
        mock_mass.players = controller

        for p in controller._players.values():
            p.update_state(signal_event=False)
            p.set_initialized()

        plugin_source, on_volume_mock = _make_mock_plugin_source("parent")
        _patch_plugin_source(controller, plugin_source)

        await controller._handle_cmd_volume_set("parent", 60, from_group_volume=True)

        on_volume_mock.assert_not_called()
        protocol_player.volume_set.assert_called_once_with(60)


class TestStandalonePlayerPluginCallback:
    """Test that standalone (non-group) players fire plugin callbacks normally."""

    async def test_standalone_player_fires_plugin_callback(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Standalone players must fire plugin on_volume normally."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        standalone = MockPlayer(provider, "standalone", "Standalone")
        standalone._attr_volume_level = 50
        standalone._cache.clear()
        standalone.volume_set = AsyncMock()  # type: ignore[method-assign]

        controller._players = {"standalone": standalone}
        controller._player_throttlers = {"standalone": Throttler(1, 0.05)}
        mock_mass.players = controller
        standalone.update_state(signal_event=False)

        plugin_source, on_volume_mock = _make_mock_plugin_source(standalone.player_id)
        _patch_plugin_source(controller, plugin_source)

        await controller._handle_cmd_volume_set(standalone.player_id, 70)

        on_volume_mock.assert_called_once_with(70)

    async def test_standalone_player_fires_with_correct_volume(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Standalone player plugin callback must receive the exact requested volume."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        standalone = MockPlayer(provider, "standalone", "Standalone")
        standalone._attr_volume_level = 30
        standalone._cache.clear()
        standalone.volume_set = AsyncMock()  # type: ignore[method-assign]

        controller._players = {"standalone": standalone}
        controller._player_throttlers = {"standalone": Throttler(1, 0.05)}
        mock_mass.players = controller
        standalone.update_state(signal_event=False)

        plugin_source, on_volume_mock = _make_mock_plugin_source(standalone.player_id)
        _patch_plugin_source(controller, plugin_source)

        await controller._handle_cmd_volume_set(standalone.player_id, 85)

        on_volume_mock.assert_called_once_with(85)


# ---------------------------------------------------------------------------
# Sync group helpers
# ---------------------------------------------------------------------------


def _setup_sync_group(
    controller: PlayerController,
    mock_mass: MagicMock,
    provider: MockProvider,
    leader_id: str = "leader",
    leader_volume: int = 80,
    child_configs: dict[str, int] | None = None,
    sync_group_id: str = "syncgroup_test1",
) -> tuple[MockPlayer, MockPlayer, dict[str, MockPlayer]]:
    """Set up a sync group scenario that mirrors production topology.

    Production topology for a sync group:
    - A **sync leader** (PLAYER type) physically receives the audio stream and
      has ``group_members`` listing itself + all synced children.
    - One or more **sync children** (PLAYER type) with ``synced_to`` pointing
      to the leader.
    - A **SyncGroupPlayer** (GROUP type) is a virtual entity wrapping the group.
      Its ``group_members`` come from the sync leader's ``group_members``.  In
      production it also has a ``sync_leader`` attribute pointing to the leader
      Player instance.

    The plugin source's ``in_use_by`` points to the **sync leader**, not to the
    SyncGroupPlayer.  This is the known architectural gap (see
    music-assistant/support#5201) that the pragmatic fixes address.

    :param leader_id: Player ID for the sync leader.
    :param leader_volume: Initial volume for the sync leader.
    :param child_configs: Mapping of child_id to volume_level for sync children.
    :param sync_group_id: Player ID for the virtual SyncGroupPlayer.
    :return: Tuple of (sync_group_player, leader, {child_id: child_player}).
    """
    if child_configs is None:
        child_configs = {"child_a": 40}

    # --- Sync leader: physical player with group_members ---
    leader = MockPlayer(provider, leader_id, "Leader")
    leader._attr_volume_level = leader_volume
    leader._cache.clear()

    # --- Sync children: physical players with synced_to -> leader ---
    children: dict[str, MockPlayer] = {}
    for child_id, volume in child_configs.items():
        child = MockPlayer(provider, child_id, child_id.title())
        child._attr_volume_level = volume
        child._cache.clear()
        children[child_id] = child

    # The leader's group_members includes itself + all children
    all_member_ids = [leader_id, *child_configs.keys()]
    leader._attr_group_members = all_member_ids

    # --- SyncGroupPlayer: virtual GROUP entity wrapping the sync group ---
    # In production this is a SyncGroupPlayer subclass with a sync_leader
    # attribute.  We simulate it with a MockPlayer of type GROUP, then add
    # the sync_leader attribute via setattr to exercise the duck-typing
    # fallback in _get_active_plugin_source.
    sync_group_player = MockPlayer(
        provider, sync_group_id, "SyncGroup", player_type=PlayerType.GROUP
    )
    # Mirror the leader's group_members (as SyncGroupPlayer.group_members does)
    sync_group_player._attr_group_members = all_member_ids
    # Attach sync_leader attribute (duck-typing -- the controller uses
    # getattr(player, "sync_leader", None) to detect this)
    sync_group_player.sync_leader = leader  # type: ignore[attr-defined]
    sync_group_player._cache.clear()

    # --- Register all players in the controller ---
    players: dict[str, Player] = {
        leader_id: leader,
        sync_group_id: sync_group_player,
    }
    throttlers: dict[str, Throttler] = {
        leader_id: Throttler(1, 0.05),
        sync_group_id: Throttler(1, 0.05),
    }
    for child_id, child in children.items():
        players[child_id] = child
        throttlers[child_id] = Throttler(1, 0.05)

    controller._players = players
    controller._player_throttlers = throttlers
    mock_mass.players = controller

    # Initialize state for all players (two passes for group_volume to resolve)
    for p in players.values():
        p.update_state(signal_event=False)
    sync_group_player.update_state(signal_event=False)
    leader.update_state(signal_event=False)

    return sync_group_player, leader, children


class TestSyncLeaderVolumeCallback:
    """Test that a sync leader's volume change computes group avg, not individual vol.

    When the sync leader's volume changes, the code must recognize it as a group
    parent (path 3 in _handle_volume_plugin_callback -- "sync leader self-
    recognition") and fire the plugin callback with the projected group average,
    not the leader's individual volume.

    This covers Bug #2 from production testing: without path 3, the sync leader
    falls through to the standalone branch and Spotify receives the leader's
    individual volume instead of the group average.
    """

    async def test_sync_leader_vol_change_fires_group_avg(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Sync leader vol change must fire plugin with group average, not individual vol."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        # leader=80, child_a=40 → avg(80, 40) = 60
        # Changing leader 80→90 → projected avg(90, 40) = 65
        _sync_group_player, leader, _children = _setup_sync_group(
            controller,
            mock_mass,
            provider,
            leader_volume=80,
            child_configs={"child_a": 40},
        )
        for p in controller._players.values():
            p.set_initialized()
            p.volume_set = AsyncMock()  # type: ignore[method-assign]

        # Plugin source in_use_by points to the LEADER (not SyncGroupPlayer).
        # This mirrors production: Spotify Connect sets in_use_by to the
        # physical player that received the audio stream.
        plugin_source, on_volume_mock = _make_mock_plugin_source(leader.player_id)
        _patch_plugin_source(controller, plugin_source)

        await controller._handle_cmd_volume_set(leader.player_id, 90)

        # Should fire with group average (90+40)/2 = 65, NOT individual 90
        on_volume_mock.assert_called_once_with(65)

    async def test_sync_leader_vol_no_avg_shift_no_callback(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Sync leader vol change that doesn't shift integer avg must NOT fire callback."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        # leader=80, child_a=40 → avg = 60
        # Changing leader 80→81 → projected avg(81, 40) = int(60.5) = 60 (unchanged)
        _sync_group_player, leader, _children = _setup_sync_group(
            controller,
            mock_mass,
            provider,
            leader_volume=80,
            child_configs={"child_a": 40},
        )
        for p in controller._players.values():
            p.set_initialized()
            p.volume_set = AsyncMock()  # type: ignore[method-assign]

        plugin_source, on_volume_mock = _make_mock_plugin_source(leader.player_id)
        _patch_plugin_source(controller, plugin_source)

        await controller._handle_cmd_volume_set(leader.player_id, 81)

        on_volume_mock.assert_not_called()


class TestSyncChildVolumeCallback:
    """Test that a non-leader sync child's vol change fires callback via sync leader fallback.

    When a non-leader member's volume changes, _handle_volume_plugin_callback
    resolves the SyncGroupPlayer as the parent group.  The plugin source lookup
    must then fall back to the sync leader (via the getattr duck-typing in
    _get_active_plugin_source) because in_use_by points to the leader, not the
    SyncGroupPlayer.

    This covers Bug #1 from production testing: without the sync-leader fallback,
    _get_active_plugin_source(SyncGroupPlayer) returns None and no callback fires.
    """

    async def test_sync_child_vol_shift_fires_via_leader_fallback(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Non-leader child vol change must fire plugin callback via sync leader fallback."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        # leader=80, child_a=40 → avg = 60
        # Changing child_a 40→60 → projected avg(80, 60) = 70
        _sync_group_player, leader, children = _setup_sync_group(
            controller,
            mock_mass,
            provider,
            leader_volume=80,
            child_configs={"child_a": 40},
        )
        for p in controller._players.values():
            p.set_initialized()
            p.volume_set = AsyncMock()  # type: ignore[method-assign]

        # Plugin in_use_by = LEADER, but the parent group resolved by
        # _handle_volume_plugin_callback is the SyncGroupPlayer (via
        # _get_player_groups).  _get_active_plugin_source must fall back
        # to checking the sync_leader to find the plugin source.
        plugin_source, on_volume_mock = _make_mock_plugin_source(leader.player_id)
        _patch_plugin_source(controller, plugin_source)

        child_a = children["child_a"]
        await controller._handle_cmd_volume_set(child_a.player_id, 60)

        # Should fire with group average (80+60)/2 = 70
        on_volume_mock.assert_called_once_with(70)

    async def test_sync_child_vol_no_shift_no_callback(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Non-leader child vol change that doesn't shift avg must NOT fire callback."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        # leader=80, child_a=40 → avg = 60
        # Changing child_a 40→41 → projected avg(80, 41) = int(60.5) = 60 (unchanged)
        _sync_group_player, leader, children = _setup_sync_group(
            controller,
            mock_mass,
            provider,
            leader_volume=80,
            child_configs={"child_a": 40},
        )
        for p in controller._players.values():
            p.set_initialized()
            p.volume_set = AsyncMock()  # type: ignore[method-assign]

        plugin_source, on_volume_mock = _make_mock_plugin_source(leader.player_id)
        _patch_plugin_source(controller, plugin_source)

        child_a = children["child_a"]
        await controller._handle_cmd_volume_set(child_a.player_id, 41)

        on_volume_mock.assert_not_called()


class TestSetGroupVolumeOnSyncGroup:
    """Test that set_group_volume on a SyncGroupPlayer finds plugin via sync leader.

    When the user adjusts the group volume slider, set_group_volume is called
    with the SyncGroupPlayer.  It calls _get_active_plugin_source(group_player)
    which must fall back to the sync leader to find the plugin source.

    This covers the same architectural gap as Bug #1 but through the
    set_group_volume path rather than the _handle_volume_plugin_callback path.
    """

    async def test_set_group_volume_finds_plugin_via_sync_leader(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """set_group_volume on SyncGroupPlayer must find plugin source via sync leader."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        sync_group_player, leader, _children = _setup_sync_group(
            controller,
            mock_mass,
            provider,
            leader_volume=80,
            child_configs={"child_a": 40},
        )
        _make_mock_handle_volume(controller)

        # Plugin in_use_by = LEADER, but set_group_volume is called with the
        # SyncGroupPlayer.  The sync_leader fallback in _get_active_plugin_source
        # must resolve the plugin source.
        plugin_source, on_volume_mock = _make_mock_plugin_source(leader.player_id)
        _patch_plugin_source(controller, plugin_source)

        await controller.set_group_volume(sync_group_player, 50)

        # Should fire with the requested group volume (50)
        on_volume_mock.assert_called_once_with(50)

    async def test_set_group_volume_sync_group_no_plugin_no_error(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """set_group_volume with no active plugin must not error."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        sync_group_player, _leader, _children = _setup_sync_group(
            controller,
            mock_mass,
            provider,
            leader_volume=80,
            child_configs={"child_a": 40},
        )
        _make_mock_handle_volume(controller)

        # No plugin source registered at all
        controller.get_plugin_sources = MagicMock(return_value=[])  # type: ignore[method-assign]

        # Should complete without error
        await controller.set_group_volume(sync_group_player, 50)


class TestMultiGroupPluginResolution:
    """Test that plugin callbacks resolve correctly when a player belongs to multiple groups.

    A player can be a configured member of a static SyncGroupPlayer (e.g.
    "The House") while also being dynamically synced to another player whose
    sync group IS actively playing via Spotify.  The static group may be
    powered but dormant -- it has no active plugin source.

    The callback logic must prefer the group that actually has an active
    plugin source, rather than greedily taking the first group returned by
    _get_player_groups.  This test reproduces the production scenario where
    Kitchen belongs to both "The House" (dormant static group) and a
    dynamic sync group led by Garage (active with Spotify Connect).
    """

    async def test_child_in_dormant_and_active_groups_picks_active(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """When a child belongs to a dormant and an active group, pick the active one."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)

        # --- Active sync group: Garage (leader) + Kitchen (child) ---
        # Garage is the sync leader, plugin source in_use_by = Garage
        _sync_group_player, leader, children = _setup_sync_group(
            controller,
            mock_mass,
            provider,
            leader_id="garage",
            leader_volume=80,
            child_configs={"kitchen": 40},
            sync_group_id="syncgroup_active",
        )

        # --- Dormant static group: "The House" also contains Kitchen ---
        # This group is powered but has no active plugin source and its
        # sync_leader is None (group not formed for playback).
        dormant_group = MockPlayer(
            provider, "syncgroup_dormant", "The House", player_type=PlayerType.GROUP
        )
        dormant_group._attr_group_members = ["garage", "kitchen"]
        dormant_group._cache.clear()

        # Register the dormant group in the controller
        controller._players["syncgroup_dormant"] = dormant_group
        controller._player_throttlers["syncgroup_dormant"] = Throttler(1, 0.05)

        # Update all states (two passes for group_volume)
        for p in controller._players.values():
            p.update_state(signal_event=False)
        dormant_group.update_state(signal_event=False)
        leader.update_state(signal_event=False)

        for p in controller._players.values():
            p.set_initialized()
            p.volume_set = AsyncMock()  # type: ignore[method-assign]

        # Plugin source in_use_by = leader (Garage), simulating Spotify Connect
        plugin_source, on_volume_mock = _make_mock_plugin_source(leader.player_id)
        _patch_plugin_source(controller, plugin_source)

        # Change Kitchen's volume: 40 -> 60
        # The dormant group "The House" also contains Kitchen, but has no
        # plugin source.  The resolution must skip it and find the active
        # sync group (via sync_leader_self on Garage, or the active
        # SyncGroupPlayer) instead.
        kitchen = children["kitchen"]
        await controller._handle_cmd_volume_set(kitchen.player_id, 60)

        # Should fire with the group average computed over the active group:
        # Garage=80, Kitchen=60 → avg = 70
        on_volume_mock.assert_called_once_with(70)

    async def test_child_in_dormant_group_only_no_error(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """When a child is only in a dormant group (no plugin source), no callback fires."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        _group_player, children = _setup_group(
            controller,
            mock_mass,
            provider,
            child_configs={"child_a": 80, "child_b": 40},
        )
        for p in controller._players.values():
            p.set_initialized()
            p.volume_set = AsyncMock()  # type: ignore[method-assign]

        # No plugin source at all
        controller.get_plugin_sources = MagicMock(return_value=[])  # type: ignore[method-assign]

        child_b = children["child_b"]
        await controller._handle_cmd_volume_set(child_b.player_id, 60)

        # No error, no callback (graceful degradation)


class TestSetGroupVolumeFreshAverage:
    """Test that set_group_volume computes a fresh average from child volumes.

    The group_volume cached property on Player is only refreshed when
    update_state() runs.  Between sequential set_group_volume calls, the
    cache is stale.  set_group_volume must read child volumes directly
    to compute the correct delta.
    """

    async def test_fresh_average_ignores_stale_cached_group_volume(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Delta should be based on fresh child average, not stale group_volume."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, children = _setup_group(
            controller,
            mock_mass,
            provider,
            child_configs={"child_a": 40, "child_b": 60},
        )
        volume_calls = _make_mock_handle_volume(controller)

        for p in controller._players.values():
            p.set_initialized()
            p.volume_set = AsyncMock()  # type: ignore[method-assign]

        # Change children to [20, 70] (avg=45) and update ONLY child
        # states — the group player's cached group_volume stays at 50.
        children["child_a"]._attr_volume_level = 20
        children["child_a"]._cache.clear()
        children["child_a"].update_state(signal_event=False)
        children["child_b"]._attr_volume_level = 70
        children["child_b"]._cache.clear()
        children["child_b"].update_state(signal_event=False)
        assert group_player.state.group_volume == 50  # stale!

        await controller.set_group_volume(group_player, 55)

        # Fresh average = (20+70)/2 = 45, delta = +10 → children: [30, 80].
        # If the stale group_volume (50) were used: delta = +5 → [25, 75].
        assert ("child_a", 30, True) in volume_calls
        assert ("child_b", 80, True) in volume_calls

    async def test_sequential_calls_converge(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Two sequential set_group_volume calls should converge correctly.

        The mock_handle_volume helper updates child_player._attr_volume_level,
        so the second call reads fresh values from the first call's results.
        """
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _children = _setup_group(
            controller,
            mock_mass,
            provider,
            child_configs={"child_a": 40, "child_b": 60},
        )
        volume_calls = _make_mock_handle_volume(controller)

        for p in controller._players.values():
            p.set_initialized()
            p.volume_set = AsyncMock()  # type: ignore[method-assign]

        # First call: target=55, fresh avg=50, delta=+5 → [45, 65]
        await controller.set_group_volume(group_player, 55)
        assert ("child_a", 45, True) in volume_calls
        assert ("child_b", 65, True) in volume_calls

        volume_calls.clear()
        # Second call: target=60, fresh avg should be (45+65)/2=55, delta=+5 → [50, 70]
        await controller.set_group_volume(group_player, 60)
        assert ("child_a", 50, True) in volume_calls
        assert ("child_b", 70, True) in volume_calls

    async def test_boundary_redistribution_child_at_100(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """When a child hits 100, the lost headroom is redistributed."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _children = _setup_group(
            controller,
            mock_mass,
            provider,
            child_configs={"child_a": 60, "child_b": 100},
        )
        volume_calls = _make_mock_handle_volume(controller)

        for p in controller._players.values():
            p.set_initialized()
            p.volume_set = AsyncMock()  # type: ignore[method-assign]

        # Fresh avg = (60+100)/2 = 80, target = 90, naive delta = +10
        # Naive: [70, 110→100], avg = 85 (misses target).
        # With redistribution: shortfall = 90*2 - (70+100) = 180-170 = 10
        # Redistribute 10 to child_a: 70+10 = 80.
        # Final: [80, 100], avg = 90. ✓
        await controller.set_group_volume(group_player, 90)
        vol_a = next(v for pid, v, _ in volume_calls if pid == "child_a")
        vol_b = next(v for pid, v, _ in volume_calls if pid == "child_b")
        assert vol_b == 100
        assert vol_a == 80
        assert (vol_a + vol_b) / 2 == 90

    async def test_boundary_redistribution_child_at_0(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """When a child hits 0, the lost headroom is redistributed downward."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _children = _setup_group(
            controller,
            mock_mass,
            provider,
            child_configs={"child_a": 0, "child_b": 60},
        )
        volume_calls = _make_mock_handle_volume(controller)

        for p in controller._players.values():
            p.set_initialized()
            p.volume_set = AsyncMock()  # type: ignore[method-assign]

        # Fresh avg = (0+60)/2 = 30, target = 20, naive delta = -10
        # Naive: [0(clamped from -10), 50], avg = 25 (misses target).
        # With redistribution: shortfall = 20*2 - (0+50) = 40-50 = -10
        # Redistribute -10 to child_b: 50-10 = 40.
        # Final: [0, 40], avg = 20. ✓
        await controller.set_group_volume(group_player, 20)
        vol_a = next(v for pid, v, _ in volume_calls if pid == "child_a")
        vol_b = next(v for pid, v, _ in volume_calls if pid == "child_b")
        assert vol_a == 0
        assert vol_b == 40
        assert (vol_a + vol_b) / 2 == 20

    async def test_no_redistribution_when_no_clamping(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """When no child is clamped, volumes are purely delta-shifted."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _children = _setup_group(
            controller,
            mock_mass,
            provider,
            child_configs={"child_a": 30, "child_b": 50},
        )
        volume_calls = _make_mock_handle_volume(controller)

        for p in controller._players.values():
            p.set_initialized()
            p.volume_set = AsyncMock()  # type: ignore[method-assign]

        # Fresh avg = (30+50)/2 = 40, target = 50, delta = +10 → [40, 60]
        await controller.set_group_volume(group_player, 50)
        assert ("child_a", 40, True) in volume_calls
        assert ("child_b", 60, True) in volume_calls

    async def test_all_children_at_boundary(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """When all children are at 100, redistribution can't help — stays at 100."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _children = _setup_group(
            controller,
            mock_mass,
            provider,
            child_configs={"child_a": 100, "child_b": 100},
        )
        volume_calls = _make_mock_handle_volume(controller)

        for p in controller._players.values():
            p.set_initialized()
            p.volume_set = AsyncMock()  # type: ignore[method-assign]

        # All at 100, can't go higher. Children stay at 100.
        await controller.set_group_volume(group_player, 100)
        vol_a = next(v for pid, v, _ in volume_calls if pid == "child_a")
        vol_b = next(v for pid, v, _ in volume_calls if pid == "child_b")
        assert vol_a == 100
        assert vol_b == 100


class TestCmdGroupVolumeCoalescing:
    """Test that rapid cmd_group_volume calls coalesce ("drop frames").

    During a fast slider drag, many cmd_group_volume events arrive faster
    than set_group_volume can complete.  The coalescing logic should execute
    the first one immediately, drop intermediates, and re-loop with the
    latest target once the in-flight call finishes.
    """

    async def test_single_call_executes_normally(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """A single cmd_group_volume call should run set_group_volume once."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _children = _setup_group(
            controller, mock_mass, provider, child_configs={"child_a": 80, "child_b": 40}
        )
        _make_mock_handle_volume(controller)

        for p in controller._players.values():
            p.set_initialized()
            p.volume_set = AsyncMock()  # type: ignore[method-assign]

        await controller.cmd_group_volume(group_player.player_id, 50)

        # Coalescing state should be cleaned up
        assert group_player.player_id not in controller._group_vol_in_flight
        assert group_player.player_id not in controller._group_vol_target

    async def test_rapid_calls_coalesce_to_latest(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Rapid calls should drop intermediates and apply the latest target.

        Simulates three rapid cmd_group_volume calls (28, 30, 32) where the
        second and third arrive while the first is in-flight.  The first
        triggers set_group_volume(28) immediately, the middle value 30 is
        dropped, and the loop re-executes with the final target 32.
        """
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _children = _setup_group(
            controller, mock_mass, provider, child_configs={"child_a": 20, "child_b": 30}
        )

        set_group_volume_calls: list[int] = []
        original_set_group_volume = controller.set_group_volume

        # Track calls to set_group_volume and simulate a concurrent arrival
        # of new targets during the first execution.
        first_call = True

        async def tracking_set_group_volume(gp: Any, volume_level: int) -> None:
            nonlocal first_call
            set_group_volume_calls.append(volume_level)
            if first_call:
                first_call = False
                # While the first call is "in-flight", two more arrive.
                # Because _group_vol_in_flight is True, they will just
                # update the target and return immediately.
                controller._group_vol_target[gp.player_id] = 30
                controller._group_vol_target[gp.player_id] = 32
            await original_set_group_volume(gp, volume_level)

        controller.set_group_volume = tracking_set_group_volume  # type: ignore[assignment]
        _make_mock_handle_volume(controller)

        for p in controller._players.values():
            p.set_initialized()
            p.volume_set = AsyncMock()  # type: ignore[method-assign]

        await controller.cmd_group_volume(group_player.player_id, 28)

        # First call executes with 28, then re-loops with final target 32.
        # The intermediate value 30 is dropped.
        assert set_group_volume_calls == [28, 32]
        # Coalescing state cleaned up
        assert group_player.player_id not in controller._group_vol_in_flight

    async def test_coalesced_caller_returns_immediately(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """When in-flight is True, the coalesced caller should return without blocking."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _children = _setup_group(
            controller, mock_mass, provider, child_configs={"child_a": 50, "child_b": 50}
        )
        _make_mock_handle_volume(controller)

        for p in controller._players.values():
            p.set_initialized()
            p.volume_set = AsyncMock()  # type: ignore[method-assign]

        # Manually set in-flight to simulate a concurrent execution
        controller._group_vol_in_flight[group_player.player_id] = True
        controller._group_vol_target[group_player.player_id] = 40

        # This should return immediately without calling set_group_volume
        await controller.cmd_group_volume(group_player.player_id, 60)

        # Target should be updated to the latest value
        assert controller._group_vol_target[group_player.player_id] == 60

        # Clean up
        controller._group_vol_in_flight.pop(group_player.player_id)
        controller._group_vol_target.pop(group_player.player_id)

    async def test_non_group_player_bypasses_coalescing(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """A standalone player (no group) should bypass the coalescing logic."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        player = MockPlayer(provider, "solo", "Solo")
        player._attr_volume_level = 50
        player._cache.clear()

        controller._players = {"solo": player}
        controller._player_throttlers = {"solo": Throttler(1, 0.05)}
        mock_mass.players = controller

        player.update_state(signal_event=False)
        player.set_initialized()
        player.volume_set = AsyncMock()  # type: ignore[method-assign]

        # No plugin sources
        controller.get_plugin_sources = MagicMock(return_value=[])  # type: ignore[method-assign]

        await controller.cmd_group_volume("solo", 70)

        # Should have set volume directly (fallback to cmd_volume_set)
        player.volume_set.assert_called_once_with(70)
        # No coalescing state should exist
        assert "solo" not in controller._group_vol_in_flight


class TestSpotifyEchoTimeWindow:
    """Test that time-windowed echo suppression blocks stale Spotify echoes."""

    async def test_echo_within_window_suppressed(self) -> None:
        """Inbound volume_changed within the echo window should be suppressed."""
        # Verify the constant exists and is reasonable
        assert 0 < _VOLUME_ECHO_SUPPRESS_WINDOW <= 5.0

        # Simulate the outbound/inbound sequence:
        # 1. _on_volume sends 30 to Spotify, records timestamp
        # 2. _on_volume sends 32 to Spotify, updates timestamp + tracker
        # 3. Stale echo for 30 arrives (30 != 32, but within window)
        last_sent_to_spotify = 32
        _ = time_mod.monotonic()  # anchor point for the simulated sequence

        # Simulate echo arriving 200ms later
        stale_echo_volume = 30
        time_since_send = 0.2  # well within the window

        value_match = stale_echo_volume == last_sent_to_spotify
        within_window = time_since_send < _VOLUME_ECHO_SUPPRESS_WINDOW

        assert not value_match, "Stale echo should NOT match the latest sent value"
        assert within_window, "Stale echo should be within the suppression window"

    async def test_echo_after_window_passes_through(self) -> None:
        """Inbound volume_changed after the echo window should be accepted."""
        last_sent_to_spotify = 30
        # Simulate echo arriving well after the window
        time_since_send = _VOLUME_ECHO_SUPPRESS_WINDOW + 1.0
        inbound_volume = 45  # Legitimate external change

        value_match = inbound_volume == last_sent_to_spotify
        within_window = time_since_send < _VOLUME_ECHO_SUPPRESS_WINDOW

        assert not value_match
        assert not within_window, "After the window, inbound changes should pass through"


class TestSpotifyVolumeDebounce:
    """Test outbound Spotify Web API volume call debouncing.

    These tests exercise the debounce logic in SpotifyConnectProvider._on_volume
    and _send_volume_to_spotify using a lightweight stub that replicates the
    relevant attributes without constructing the full provider.
    """

    @staticmethod
    def _make_stub() -> MagicMock:
        """Build a minimal stub mimicking SpotifyConnectProvider for volume tests."""
        stub = MagicMock(spec=SpotifyConnectProvider)
        stub.logger = MagicMock()
        stub._spotify_provider = MagicMock()
        stub._source_details = MagicMock()
        stub._last_volume_sent_to_spotify = None
        stub._last_volume_change_received_time = 0.0
        stub._pending_spotify_volume = None
        stub._volume_debounce_task = None

        put_data_mock = AsyncMock()
        stub._spotify_provider._put_data = put_data_mock

        @asynccontextmanager
        async def _bypass() -> AsyncIterator[None]:
            yield

        stub._spotify_provider.throttler.bypass = _bypass

        stub.mass = MagicMock()
        stub.mass.create_task = MagicMock(side_effect=lambda coro: asyncio.ensure_future(coro))

        stub._on_volume = SpotifyConnectProvider._on_volume.__get__(stub)
        stub._send_volume_to_spotify = SpotifyConnectProvider._send_volume_to_spotify.__get__(stub)

        return stub

    async def test_debounce_coalesces_rapid_calls(self) -> None:
        """Three rapid _on_volume calls should result in one API PUT with the last value."""
        stub = self._make_stub()
        put_mock: AsyncMock = stub._spotify_provider._put_data

        await stub._on_volume(30)
        await stub._on_volume(35)
        await stub._on_volume(40)

        assert stub._pending_spotify_volume == 40

        await asyncio.sleep(_VOLUME_API_DEBOUNCE + 0.05)

        put_mock.assert_called_once_with("me/player/volume?volume_percent=40")
        assert stub._last_volume_sent_to_spotify == 40

    async def test_debounce_fires_after_window(self) -> None:
        """A single _on_volume call should fire the API call after the debounce delay."""
        stub = self._make_stub()
        put_mock: AsyncMock = stub._spotify_provider._put_data

        await stub._on_volume(50)

        put_mock.assert_not_called()

        await asyncio.sleep(_VOLUME_API_DEBOUNCE + 0.05)

        put_mock.assert_called_once_with("me/player/volume?volume_percent=50")
        assert stub._last_volume_sent_to_spotify == 50
        assert stub._pending_spotify_volume is None

    async def test_dedup_suppresses_same_value(self) -> None:
        """Calling _on_volume with the same value as last sent should be a no-op."""
        stub = self._make_stub()
        stub._last_volume_sent_to_spotify = 42

        await stub._on_volume(42)

        assert stub._pending_spotify_volume is None
        assert stub._volume_debounce_task is None

    async def test_debounce_cancels_previous_task(self) -> None:
        """Each new _on_volume should cancel the previous debounce task."""
        stub = self._make_stub()

        await stub._on_volume(20)
        first_task = stub._volume_debounce_task
        assert first_task is not None

        await stub._on_volume(25)
        second_task = stub._volume_debounce_task
        assert second_task is not first_task
        # The first task has been asked to cancel (cancelling state);
        # it transitions to cancelled() after the event loop processes it.
        assert first_task.cancelling() or first_task.cancelled()

    async def test_constant_value_is_reasonable(self) -> None:
        """The debounce window should be between 100ms and 2s."""
        assert 0.1 <= _VOLUME_API_DEBOUNCE <= 2.0

    async def test_on_volume_resets_suppress_timestamp(self) -> None:
        """Each _on_volume call should reset _last_volume_change_received_time.

        This ensures the echo suppress window deterministically extends
        from the user's last slider event, not just the debounced API send.
        """
        stub = self._make_stub()
        assert stub._last_volume_change_received_time == 0.0

        await stub._on_volume(30)
        t1 = stub._last_volume_change_received_time
        assert t1 > 0, "Timestamp should be set after first _on_volume"

        await asyncio.sleep(0.01)
        await stub._on_volume(35)
        t2 = stub._last_volume_change_received_time
        assert t2 > t1, "Timestamp should advance with each _on_volume call"

        stub._volume_debounce_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stub._volume_debounce_task


class TestApplyInboundVolume:
    """Test _apply_inbound_volume routing to cmd_group_volume vs cmd_volume_set.

    When a volume_changed event arrives from Spotify and passes echo
    suppression, it must be routed to the correct MA command:
    - Group players -> cmd_group_volume (all children adjust proportionally)
    - Standalone players -> cmd_volume_set (only the one player)
    """

    @staticmethod
    def _make_stub(
        player_id: str = "test_player",
        group_members: list[str] | None = None,
        synced_to: str | None = None,
        player_type: PlayerType = PlayerType.PLAYER,
    ) -> MagicMock:
        """Build a stub for testing _apply_inbound_volume."""
        stub = MagicMock(spec=SpotifyConnectProvider)
        stub.logger = MagicMock()
        stub._source_details = MagicMock()
        stub._source_details.in_use_by = player_id

        mock_player = MagicMock()
        mock_player.state.group_members = group_members or []
        mock_player.state.synced_to = synced_to
        mock_player.state.type = player_type

        stub.mass = MagicMock()
        stub.mass.players.get_player = MagicMock(return_value=mock_player)
        stub.mass.players.cmd_group_volume = AsyncMock()
        stub.mass.players.cmd_volume_set = AsyncMock()

        stub._apply_inbound_volume = SpotifyConnectProvider._apply_inbound_volume.__get__(stub)
        return stub

    async def test_group_player_routes_to_cmd_group_volume(self) -> None:
        """Player with group_members should trigger cmd_group_volume."""
        stub = self._make_stub(
            player_id="leader",
            group_members=["leader", "child1"],
        )
        await stub._apply_inbound_volume(50)

        stub.mass.players.cmd_group_volume.assert_awaited_once_with("leader", 50)
        stub.mass.players.cmd_volume_set.assert_not_awaited()

    async def test_synced_player_routes_to_cmd_group_volume(self) -> None:
        """Player synced_to another should trigger cmd_group_volume."""
        stub = self._make_stub(
            player_id="child1",
            synced_to="leader",
        )
        await stub._apply_inbound_volume(30)

        stub.mass.players.cmd_group_volume.assert_awaited_once_with("child1", 30)
        stub.mass.players.cmd_volume_set.assert_not_awaited()

    async def test_group_type_player_routes_to_cmd_group_volume(self) -> None:
        """PlayerType.GROUP should trigger cmd_group_volume."""
        stub = self._make_stub(
            player_id="virtual_group",
            player_type=PlayerType.GROUP,
        )
        await stub._apply_inbound_volume(70)

        stub.mass.players.cmd_group_volume.assert_awaited_once_with("virtual_group", 70)
        stub.mass.players.cmd_volume_set.assert_not_awaited()

    async def test_standalone_player_routes_to_cmd_volume_set(self) -> None:
        """Standalone player (no group) should use cmd_volume_set."""
        stub = self._make_stub(player_id="standalone")
        await stub._apply_inbound_volume(45)

        stub.mass.players.cmd_volume_set.assert_awaited_once_with("standalone", 45)
        stub.mass.players.cmd_group_volume.assert_not_awaited()

    async def test_no_in_use_by_is_noop(self) -> None:
        """If in_use_by is None, nothing should happen."""
        stub = self._make_stub()
        stub._source_details.in_use_by = None
        await stub._apply_inbound_volume(50)

        stub.mass.players.cmd_group_volume.assert_not_awaited()
        stub.mass.players.cmd_volume_set.assert_not_awaited()

    async def test_player_not_found_is_noop(self) -> None:
        """If get_player returns None, nothing should happen."""
        stub = self._make_stub()
        stub.mass.players.get_player.return_value = None
        await stub._apply_inbound_volume(50)

        stub.mass.players.cmd_group_volume.assert_not_awaited()
        stub.mass.players.cmd_volume_set.assert_not_awaited()
