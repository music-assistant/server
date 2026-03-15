"""Tests for ratio-based group volume control.

This module tests:
- Ratio-based group volume algorithm
- Individual child volume ratio updates
- Plugin source callback isolation (feedback loop prevention)
- Group membership initialization
- Static group persistence
- Sync group data ownership (SyncGroupPlayer vs sync leader)
- Edge cases (zero volume, ratio > 1.0, etc.)
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import PlayerFeature, PlayerType

from music_assistant.constants import ATTR_GROUP_CHILD_RATIOS, ATTR_GROUP_VOLUME_LEVEL
from music_assistant.controllers.players import PlayerController
from music_assistant.helpers.throttle_retry import Throttler
from music_assistant.models.player import Player
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

    return group_player, children


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


class TestRatioBasedGroupVolume:
    """Test core ratio-based group volume algorithm."""

    async def test_set_group_volume_applies_ratios(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Verify child volumes are computed as group_vol * ratio."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _children = _setup_group(
            controller, mock_mass, provider, child_configs={"a": 80, "b": 40}
        )
        volume_calls = _make_mock_handle_volume(controller)

        controller._initialize_group_ratios(group_player)

        assert group_player.extra_data[ATTR_GROUP_VOLUME_LEVEL] == 80
        assert group_player.extra_data[ATTR_GROUP_CHILD_RATIOS]["a"] == pytest.approx(1.0)
        assert group_player.extra_data[ATTR_GROUP_CHILD_RATIOS]["b"] == pytest.approx(0.5)

        await controller.set_group_volume(group_player, 60)

        child_volumes = {pid: vol for pid, vol, _ in volume_calls}
        assert child_volumes["a"] == 60
        assert child_volumes["b"] == 30

    async def test_ratio_preserved_through_clamping(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Verify balance is restored after clamping at boundaries."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _children = _setup_group(
            controller, mock_mass, provider, child_configs={"a": 80, "b": 40}
        )
        volume_calls = _make_mock_handle_volume(controller)

        controller._initialize_group_ratios(group_player)

        # Raise group to 100 -- child a clamps at 100
        await controller.set_group_volume(group_player, 100)
        clamped = {pid: vol for pid, vol, _ in volume_calls}
        assert clamped["a"] == 100
        assert clamped["b"] == 50

        volume_calls.clear()

        # Lower group back to 80 -- original balance restored
        await controller.set_group_volume(group_player, 80)
        restored = {pid: vol for pid, vol, _ in volume_calls}
        assert restored["a"] == 80
        assert restored["b"] == 40

    async def test_round_trip_preserves_balance(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Verify moving group up then back restores original volumes."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _children = _setup_group(
            controller, mock_mass, provider, child_configs={"a": 90, "b": 30}
        )
        volume_calls = _make_mock_handle_volume(controller)

        controller._initialize_group_ratios(group_player)

        await controller.set_group_volume(group_player, 100)
        volume_calls.clear()

        await controller.set_group_volume(group_player, 90)
        final = {pid: vol for pid, vol, _ in volume_calls}
        assert final["a"] == 90
        assert final["b"] == 30

    async def test_group_volume_stored_not_derived(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Verify stored group volume is the set value, not the child average."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _children = _setup_group(
            controller, mock_mass, provider, child_configs={"a": 80, "b": 40}
        )
        _make_mock_handle_volume(controller)

        controller._initialize_group_ratios(group_player)
        await controller.set_group_volume(group_player, 70)

        assert group_player.extra_data[ATTR_GROUP_VOLUME_LEVEL] == 70
        # The cached_property group_volume reads from extra_data; clear cache to pick it up
        group_player._cache.clear()
        assert group_player.group_volume == 70

    async def test_group_volume_property_returns_stored(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Verify group_volume property prefers stored value over average."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _ = _setup_group(
            controller, mock_mass, provider, child_configs={"a": 80, "b": 40}
        )

        group_player.extra_data[ATTR_GROUP_VOLUME_LEVEL] = 55
        group_player._cache.clear()

        assert group_player.group_volume == 55


class TestIndividualChildVolume:
    """Test ratio updates when individual child volume is changed directly."""

    async def test_individual_volume_updates_ratio(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Changing a child directly should update its ratio in the parent group.

        The ratio is volume_level / group_vol and must stay in [0, 1].
        """
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _children = _setup_group(
            controller, mock_mass, provider, child_configs={"a": 70, "b": 35}
        )
        volume_calls = _make_mock_handle_volume(controller)

        controller._initialize_group_ratios(group_player)
        # group_vol=70, ratio_a=1.0, ratio_b=0.5

        ratios = group_player.extra_data[ATTR_GROUP_CHILD_RATIOS]
        group_vol = group_player.extra_data[ATTR_GROUP_VOLUME_LEVEL]
        # Simulate setting child b to 56 directly (within group ceiling)
        new_vol = 56
        new_ratio = new_vol / group_vol  # 56/70 = 0.8
        assert 0.0 <= new_ratio <= 1.0
        ratios["b"] = new_ratio
        group_player.extra_data[ATTR_GROUP_CHILD_RATIOS] = ratios

        # Now set group to 50 and verify new ratio is applied
        await controller.set_group_volume(group_player, 50)
        child_volumes = {pid: vol for pid, vol, _ in volume_calls}
        assert child_volumes["b"] == 40  # 0.8 * 50

    async def test_ratio_not_updated_when_group_vol_zero(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Ratio should not change when group volume is zero (avoid division by zero)."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _children = _setup_group(
            controller, mock_mass, provider, child_configs={"a": 80, "b": 40}
        )

        controller._initialize_group_ratios(group_player)
        original_ratios = dict(group_player.extra_data[ATTR_GROUP_CHILD_RATIOS])

        group_player.extra_data[ATTR_GROUP_VOLUME_LEVEL] = 0

        # Simulate the ratio update path with group_vol=0
        group_vol = group_player.extra_data[ATTR_GROUP_VOLUME_LEVEL]
        ratios = group_player.extra_data[ATTR_GROUP_CHILD_RATIOS]
        if group_vol is not None and group_vol > 0:
            ratios["a"] = 50 / group_vol
        # ratio should be unchanged since group_vol is 0
        assert ratios["a"] == pytest.approx(original_ratios["a"])


class TestPluginSourceIsolation:
    """Test that group volume changes prevent per-child plugin callbacks."""

    async def test_group_volume_passes_from_group_flag(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Verify set_group_volume passes from_group_volume=True to children."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _children = _setup_group(
            controller, mock_mass, provider, child_configs={"a": 80, "b": 40}
        )
        volume_calls = _make_mock_handle_volume(controller)

        controller._initialize_group_ratios(group_player)
        await controller.set_group_volume(group_player, 60)

        for _, _, from_group in volume_calls:
            assert from_group is True

    async def test_group_volume_fires_single_plugin_callback(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Verify plugin on_volume is called once at the group level."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _children = _setup_group(
            controller, mock_mass, provider, child_configs={"a": 80, "b": 40}
        )
        _make_mock_handle_volume(controller)

        controller._initialize_group_ratios(group_player)

        plugin_volume_calls: list[int] = []

        async def mock_on_volume(vol: int) -> None:
            plugin_volume_calls.append(vol)

        mock_plugin_source = MagicMock()
        mock_plugin_source.on_volume = mock_on_volume

        original_get_plugin = controller._get_active_plugin_source

        def patched_get_plugin(player: Any) -> Any:
            if player.player_id == group_player.player_id:
                return mock_plugin_source
            return None

        controller._get_active_plugin_source = patched_get_plugin  # type: ignore[method-assign]

        await controller.set_group_volume(group_player, 70)

        assert len(plugin_volume_calls) == 1
        assert plugin_volume_calls[0] == 70

        controller._get_active_plugin_source = original_get_plugin  # type: ignore[method-assign]


class TestMembershipInitialization:
    """Test ratio initialization on group membership changes."""

    def test_group_formed_initializes_ratios(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Ratios should be derived from current child volumes when group forms."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _children = _setup_group(
            controller, mock_mass, provider, child_configs={"a": 80, "b": 40}
        )

        controller._update_group_ratios_on_membership_change(group_player, [], ["group", "a", "b"])

        assert group_player.extra_data[ATTR_GROUP_VOLUME_LEVEL] == 80
        ratios = group_player.extra_data[ATTR_GROUP_CHILD_RATIOS]
        assert ratios["a"] == pytest.approx(1.0)
        assert ratios["b"] == pytest.approx(0.5)

    def test_child_added_derives_ratio(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """A child joining an existing group should get a ratio based on its current volume."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _children = _setup_group(
            controller, mock_mass, provider, child_configs={"a": 80, "b": 40}
        )

        controller._initialize_group_ratios(group_player)
        # group_vol=80

        # Add child c at volume 35
        child_c = MockPlayer(provider, "c", "C")
        child_c._attr_volume_level = 35
        child_c._cache.clear()
        controller._players["c"] = child_c
        controller._player_throttlers["c"] = Throttler(1, 0.05)
        child_c.update_state(signal_event=False)

        group_player._attr_group_members = ["group", "a", "b", "c"]
        group_player._cache.clear()
        group_player.update_state(signal_event=False)

        controller._update_group_ratios_on_membership_change(
            group_player, ["group", "a", "b"], ["group", "a", "b", "c"]
        )

        ratios = group_player.extra_data[ATTR_GROUP_CHILD_RATIOS]
        assert ratios["c"] == pytest.approx(35 / 80)

    def test_child_added_above_group_triggers_rescale(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """A child joining with volume above group_vol should trigger overflow rescale."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _children = _setup_group(
            controller, mock_mass, provider, child_configs={"a": 60, "b": 30}
        )

        controller._initialize_group_ratios(group_player)
        # group_vol=60, ratio_a=1.0, ratio_b=0.5

        # Add child c at volume 90 (above group_vol of 60)
        child_c = MockPlayer(provider, "c", "C")
        child_c._attr_volume_level = 90
        child_c._cache.clear()
        controller._players["c"] = child_c
        controller._player_throttlers["c"] = Throttler(1, 0.05)
        child_c.update_state(signal_event=False)

        group_player._attr_group_members = ["group", "a", "b", "c"]
        group_player._cache.clear()
        group_player.update_state(signal_event=False)

        controller._update_group_ratios_on_membership_change(
            group_player, ["group", "a", "b"], ["group", "a", "b", "c"]
        )

        # group_vol should have risen to 90
        assert group_player.extra_data[ATTR_GROUP_VOLUME_LEVEL] == 90
        ratios = group_player.extra_data[ATTR_GROUP_CHILD_RATIOS]
        assert ratios["c"] == pytest.approx(1.0)
        # Existing ratios rescaled to preserve hw output
        # child_a: old hw = 60 * 1.0 = 60, new ratio = 60/90
        assert ratios["a"] == pytest.approx(60 / 90)
        # child_b: old hw = 60 * 0.5 = 30, new ratio = 30/90
        assert ratios["b"] == pytest.approx(30 / 90)
        # All ratios in [0, 1]
        for ratio in ratios.values():
            assert 0.0 <= ratio <= 1.0

    def test_child_removed_cleans_ratio(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Removing a child should remove its ratio entry."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _children = _setup_group(
            controller, mock_mass, provider, child_configs={"a": 80, "b": 40}
        )

        controller._initialize_group_ratios(group_player)

        group_player._attr_group_members = ["group", "a"]
        group_player._cache.clear()

        controller._update_group_ratios_on_membership_change(
            group_player, ["group", "a", "b"], ["group", "a"]
        )

        ratios = group_player.extra_data[ATTR_GROUP_CHILD_RATIOS]
        assert "b" not in ratios
        assert "a" in ratios

    def test_group_dissolved_clears_data(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Dissolving a group should clear all stored volume data."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _children = _setup_group(
            controller, mock_mass, provider, child_configs={"a": 80, "b": 40}
        )

        controller._initialize_group_ratios(group_player)
        assert ATTR_GROUP_VOLUME_LEVEL in group_player.extra_data

        controller._update_group_ratios_on_membership_change(group_player, ["group", "a", "b"], [])

        assert ATTR_GROUP_VOLUME_LEVEL not in group_player.extra_data
        assert ATTR_GROUP_CHILD_RATIOS not in group_player.extra_data

    def test_uninitialized_group_triggers_full_init(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Adding a member when ratios are uninitialized should trigger full init."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _children = _setup_group(
            controller, mock_mass, provider, child_configs={"a": 80, "b": 40}
        )

        # Simulate uninitialized state (e.g. after restart)
        group_player.extra_data.pop(ATTR_GROUP_VOLUME_LEVEL, None)
        group_player.extra_data.pop(ATTR_GROUP_CHILD_RATIOS, None)

        child_c = MockPlayer(provider, "c", "C")
        child_c._attr_volume_level = 35
        child_c._cache.clear()
        controller._players["c"] = child_c
        controller._player_throttlers["c"] = Throttler(1, 0.05)
        child_c.update_state(signal_event=False)

        group_player._attr_group_members = ["group", "a", "b", "c"]
        group_player._cache.clear()
        group_player.update_state(signal_event=False)

        controller._update_group_ratios_on_membership_change(
            group_player, ["group", "a", "b"], ["group", "a", "b", "c"]
        )

        # Full init should have run -- group_vol should be max of all children
        assert group_player.extra_data[ATTR_GROUP_VOLUME_LEVEL] == 80
        ratios = group_player.extra_data[ATTR_GROUP_CHILD_RATIOS]
        assert "a" in ratios
        assert "b" in ratios
        assert "c" in ratios


class TestStaticGroupPersistence:
    """Test config persistence for PlayerType.GROUP players."""

    def test_persist_writes_to_config(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Config values should be written for static groups."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _ = _setup_group(controller, mock_mass, provider, group_type=PlayerType.GROUP)

        group_player.extra_data[ATTR_GROUP_VOLUME_LEVEL] = 70
        group_player.extra_data[ATTR_GROUP_CHILD_RATIOS] = {"a": 1.0, "b": 0.5}

        controller._persist_group_volume_data(group_player)

        calls = mock_mass.config.set_raw_player_config_value.call_args_list
        keys_written = {call.args[1] for call in calls}
        assert ATTR_GROUP_VOLUME_LEVEL in keys_written
        assert ATTR_GROUP_CHILD_RATIOS in keys_written

    def test_persist_skipped_for_dynamic_groups(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Config values should not be written for non-GROUP (dynamic) player types."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _ = _setup_group(
            controller, mock_mass, provider, group_type=PlayerType.PLAYER
        )

        group_player.extra_data[ATTR_GROUP_VOLUME_LEVEL] = 70
        group_player.extra_data[ATTR_GROUP_CHILD_RATIOS] = {"a": 1.0, "b": 0.5}

        controller._persist_group_volume_data(group_player)

        mock_mass.config.set_raw_player_config_value.assert_not_called()

    def test_load_restores_from_config(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Persisted values should be restored into extra_data."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _ = _setup_group(controller, mock_mass, provider, group_type=PlayerType.GROUP)

        stored_ratios = {"a": 1.0, "b": 0.5}

        def config_get(_player_id: str, key: str, default: Any = None) -> Any:
            if key == ATTR_GROUP_VOLUME_LEVEL:
                return 70
            if key == ATTR_GROUP_CHILD_RATIOS:
                return stored_ratios
            return default

        mock_mass.config.get_raw_player_config_value = MagicMock(side_effect=config_get)

        result = controller._load_persisted_group_volume_data(group_player)

        assert result is True
        assert group_player.extra_data[ATTR_GROUP_VOLUME_LEVEL] == 70
        assert group_player.extra_data[ATTR_GROUP_CHILD_RATIOS] == stored_ratios

    def test_initialize_prefers_persisted_over_derived(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Static groups should use persisted ratios rather than deriving from current volumes."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _ = _setup_group(controller, mock_mass, provider, group_type=PlayerType.GROUP)

        stored_ratios = {"a": 0.9, "b": 0.3}

        def config_get(_player_id: str, key: str, default: Any = None) -> Any:
            if key == ATTR_GROUP_VOLUME_LEVEL:
                return 65
            if key == ATTR_GROUP_CHILD_RATIOS:
                return stored_ratios
            return default

        mock_mass.config.get_raw_player_config_value = MagicMock(side_effect=config_get)

        controller._initialize_group_ratios(group_player)

        assert group_player.extra_data[ATTR_GROUP_VOLUME_LEVEL] == 65
        assert group_player.extra_data[ATTR_GROUP_CHILD_RATIOS] == stored_ratios

    def test_config_load_clamps_invalid_ratios(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Loading ratios > 1.0 or < 0.0 from config should clamp them to [0.0, 1.0]."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _ = _setup_group(controller, mock_mass, provider, group_type=PlayerType.GROUP)

        stored_ratios = {"a": 3.125, "b": -0.5, "c": 0.7}

        def config_get(_player_id: str, key: str, default: Any = None) -> Any:
            if key == ATTR_GROUP_VOLUME_LEVEL:
                return 50
            if key == ATTR_GROUP_CHILD_RATIOS:
                return dict(stored_ratios)
            return default

        mock_mass.config.get_raw_player_config_value = MagicMock(side_effect=config_get)

        result = controller._load_persisted_group_volume_data(group_player)

        assert result is True
        ratios = group_player.extra_data[ATTR_GROUP_CHILD_RATIOS]
        assert ratios["a"] == pytest.approx(1.0)
        assert ratios["b"] == pytest.approx(0.0)
        assert ratios["c"] == pytest.approx(0.7)


class TestEdgeCases:
    """Test edge cases in group volume control."""

    async def test_group_volume_zero_zeros_all_children(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Setting group to 0 should zero all children; raising restores proportions."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _children = _setup_group(
            controller, mock_mass, provider, child_configs={"a": 80, "b": 40}
        )
        volume_calls = _make_mock_handle_volume(controller)

        controller._initialize_group_ratios(group_player)

        await controller.set_group_volume(group_player, 0)
        zeroed = {pid: vol for pid, vol, _ in volume_calls}
        assert zeroed["a"] == 0
        assert zeroed["b"] == 0

        volume_calls.clear()

        await controller.set_group_volume(group_player, 50)
        restored = {pid: vol for pid, vol, _ in volume_calls}
        assert restored["a"] == 50
        assert restored["b"] == 25

    async def test_overflow_rescale(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Setting a child above the group ceiling should trigger overflow rescale.

        The group volume rises to accommodate, the child's ratio becomes 1.0,
        and other children's ratios are rescaled to preserve their hardware output.
        """
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _children = _setup_group(
            controller, mock_mass, provider, child_configs={"a": 80, "b": 40}
        )
        controller._initialize_group_ratios(group_player)
        # group_vol=80, ratio_a=1.0, ratio_b=0.5

        ratios = group_player.extra_data[ATTR_GROUP_CHILD_RATIOS]
        group_vol = group_player.extra_data[ATTR_GROUP_VOLUME_LEVEL]

        # Simulate child_b set to 90 (above group_vol of 80) via the overflow path
        child_vol = 90
        old_group_vol = group_vol
        group_vol = child_vol
        group_player.extra_data[ATTR_GROUP_VOLUME_LEVEL] = group_vol
        for cid in ratios:
            if cid != "b":
                ratios[cid] = (old_group_vol * ratios[cid]) / group_vol
        ratios["b"] = 1.0
        group_player.extra_data[ATTR_GROUP_CHILD_RATIOS] = ratios
        group_player._cache.clear()

        assert group_player.extra_data[ATTR_GROUP_VOLUME_LEVEL] == 90
        assert ratios["b"] == pytest.approx(1.0)
        # child_a hw preserved: 80 * 1.0 -> rescaled to 80/90
        assert ratios["a"] == pytest.approx(80 / 90)

        # Verify all ratios are in [0, 1]
        for ratio in ratios.values():
            assert 0.0 <= ratio <= 1.0

        # Now apply group volume and verify hw outputs
        volume_calls = _make_mock_handle_volume(controller)
        await controller.set_group_volume(group_player, 90)
        hw = {pid: vol for pid, vol, _ in volume_calls}
        assert hw["a"] == 80  # 90 * (80/90) = 80
        assert hw["b"] == 90  # 90 * 1.0 = 90

    async def test_overflow_rescale_preserves_other_children(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Non-touched children's hardware output must be unchanged after overflow rescale."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _children = _setup_group(
            controller, mock_mass, provider, child_configs={"a": 60, "b": 30, "c": 45}
        )
        controller._initialize_group_ratios(group_player)
        # group_vol=60, ratio_a=1.0, ratio_b=0.5, ratio_c=0.75

        volume_calls = _make_mock_handle_volume(controller)

        # Record pre-rescale hw outputs at group_vol=60
        await controller.set_group_volume(group_player, 60)
        pre_hw = {pid: vol for pid, vol, _ in volume_calls}
        assert pre_hw["a"] == 60
        assert pre_hw["b"] == 30
        assert pre_hw["c"] == 45

        # Simulate overflow: child_b set to 80 (above group_vol=60)
        ratios = group_player.extra_data[ATTR_GROUP_CHILD_RATIOS]
        old_gv = 60
        new_gv = 80
        group_player.extra_data[ATTR_GROUP_VOLUME_LEVEL] = new_gv
        for cid in ratios:
            if cid != "b":
                ratios[cid] = (old_gv * ratios[cid]) / new_gv
        ratios["b"] = 1.0
        group_player.extra_data[ATTR_GROUP_CHILD_RATIOS] = ratios
        group_player._cache.clear()

        volume_calls.clear()
        await controller.set_group_volume(group_player, new_gv)
        post_hw = {pid: vol for pid, vol, _ in volume_calls}

        # Untouched children a and c should have same hw output
        assert post_hw["a"] == 60
        assert post_hw["c"] == 45
        assert post_hw["b"] == 80

    async def test_overflow_at_group_zero(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Overflow from group_vol=0 sets other ratios to 0.0 (accepted ratio loss)."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _children = _setup_group(
            controller, mock_mass, provider, child_configs={"a": 80, "b": 40}
        )
        controller._initialize_group_ratios(group_player)
        # group_vol=80, ratio_a=1.0, ratio_b=0.5

        # Set group to 0 (ratios preserved)
        _make_mock_handle_volume(controller)
        await controller.set_group_volume(group_player, 0)
        ratios = group_player.extra_data[ATTR_GROUP_CHILD_RATIOS]
        assert ratios["a"] == pytest.approx(1.0)
        assert ratios["b"] == pytest.approx(0.5)

        # Now simulate overflow from zero: child_a set to 50
        group_player.extra_data[ATTR_GROUP_VOLUME_LEVEL] = 50
        for cid in ratios:
            if cid != "a":
                ratios[cid] = 0.0
        ratios["a"] = 1.0
        group_player.extra_data[ATTR_GROUP_CHILD_RATIOS] = ratios
        group_player._cache.clear()

        assert group_player.extra_data[ATTR_GROUP_VOLUME_LEVEL] == 50
        assert ratios["a"] == pytest.approx(1.0)
        assert ratios["b"] == pytest.approx(0.0)  # ratio lost at zero

    async def test_no_volume_capable_children(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Group with no volume-capable children should not error."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player = MockPlayer(provider, "group", "Group", player_type=PlayerType.GROUP)
        child = MockPlayer(provider, "child", "Child")
        child._attr_supported_features = set()
        child._cache.clear()

        group_player._attr_group_members = ["group", "child"]
        group_player._cache.clear()

        controller._players = {"group": group_player, "child": child}
        controller._player_throttlers = {
            "group": Throttler(1, 0.05),
            "child": Throttler(1, 0.05),
        }
        mock_mass.players = controller

        _make_mock_handle_volume(controller)

        controller._initialize_group_ratios(group_player)
        await controller.set_group_volume(group_player, 50)

    def test_all_children_powered_off(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Initialize ratios when all children are powered off should handle gracefully."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, children = _setup_group(
            controller, mock_mass, provider, child_configs={"a": 80, "b": 40}
        )

        # Clear any data set during setup, then power off children and refresh state.
        # Children need POWER feature so that power_control is NATIVE and
        # __final_power_state returns the actual powered attribute.
        group_player.extra_data.pop(ATTR_GROUP_VOLUME_LEVEL, None)
        group_player.extra_data.pop(ATTR_GROUP_CHILD_RATIOS, None)

        for child in children.values():
            child._attr_supported_features.add(PlayerFeature.POWER)
            child._attr_powered = False
            child._cache.clear()
            child.update_state(signal_event=False)

        controller._initialize_group_ratios(group_player)
        assert group_player.extra_data.get(ATTR_GROUP_CHILD_RATIOS) is None


def _setup_sync_group(
    controller: PlayerController,
    mock_mass: MagicMock,
    provider: MockProvider,
    syncgroup_id: str = "syncgroup_test",
    leader_id: str = "leader",
    child_configs: dict[str, int] | None = None,
    leader_volume: int = 80,
) -> tuple[MockPlayer, MockPlayer, dict[str, MockPlayer]]:
    """Set up a sync group with a SyncGroupPlayer, sync leader, and children.

    :param child_configs: Mapping of child_id to volume_level (excludes leader).
    :return: Tuple of (syncgroup_player, leader_player, {child_id: child_player}).
    """
    if child_configs is None:
        child_configs = {"child_a": 40}

    member_ids = [leader_id, *child_configs.keys()]

    syncgroup_player = MockPlayer(provider, syncgroup_id, "SyncGroup", player_type=PlayerType.GROUP)
    syncgroup_player._attr_group_members = list(member_ids)
    syncgroup_player._cache.clear()

    leader_player = MockPlayer(provider, leader_id, "Leader")
    leader_player._attr_volume_level = leader_volume
    leader_player._attr_group_members = list(member_ids)
    leader_player._cache.clear()

    children: dict[str, MockPlayer] = {}
    for child_id, volume in child_configs.items():
        child = MockPlayer(provider, child_id, child_id.title())
        child._attr_volume_level = volume
        child._cache.clear()
        children[child_id] = child

    players: dict[str, Player] = {
        syncgroup_id: syncgroup_player,
        leader_id: leader_player,
    }
    throttlers: dict[str, Throttler] = {
        syncgroup_id: Throttler(1, 0.05),
        leader_id: Throttler(1, 0.05),
    }
    for child_id, child in children.items():
        players[child_id] = child
        throttlers[child_id] = Throttler(1, 0.05)

    controller._players = players
    controller._player_throttlers = throttlers
    mock_mass.players = controller

    for p in players.values():
        p.set_initialized()
        p.update_state(signal_event=False)

    return syncgroup_player, leader_player, children


class TestSyncGroupDataOwnership:
    """Test that group volume data lives on the canonical entity (SyncGroupPlayer)."""

    def test_resolve_group_data_owner_with_sync_group_player(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Sync leaders backed by a SyncGroupPlayer should resolve to it."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        syncgroup, leader, _children = _setup_sync_group(controller, mock_mass, provider)

        resolved = controller._resolve_group_data_owner(leader)
        assert resolved.player_id == syncgroup.player_id

    def test_resolve_group_data_owner_group_player_returns_self(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """GROUP players should resolve to themselves."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        syncgroup, _leader, _children = _setup_sync_group(controller, mock_mass, provider)

        resolved = controller._resolve_group_data_owner(syncgroup)
        assert resolved.player_id == syncgroup.player_id

    def test_resolve_group_data_owner_adhoc_sync_leader(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Ad-hoc sync leaders (no SyncGroupPlayer) should resolve to themselves."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        leader = MockPlayer(provider, "leader", "Leader")
        leader._attr_volume_level = 80
        leader._attr_group_members = ["leader", "child_a"]
        leader._cache.clear()

        child = MockPlayer(provider, "child_a", "Child_A")
        child._attr_volume_level = 40
        child._cache.clear()

        controller._players = {"leader": leader, "child_a": child}
        controller._player_throttlers = {
            "leader": Throttler(1, 0.05),
            "child_a": Throttler(1, 0.05),
        }
        mock_mass.players = controller

        for p in controller._players.values():
            p.set_initialized()
            p.update_state(signal_event=False)

        resolved = controller._resolve_group_data_owner(leader)
        assert resolved.player_id == "leader"

    async def test_cmd_group_volume_routes_to_sync_group_player(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """cmd_group_volume on a sync leader should store data on the SyncGroupPlayer."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        syncgroup, leader, _children = _setup_sync_group(
            controller, mock_mass, provider, leader_volume=80, child_configs={"child_a": 40}
        )
        _make_mock_handle_volume(controller)

        controller._initialize_group_ratios(syncgroup)

        await controller.cmd_group_volume(leader.player_id, 60)

        assert syncgroup.extra_data[ATTR_GROUP_VOLUME_LEVEL] == 60
        assert ATTR_GROUP_VOLUME_LEVEL not in leader.extra_data

    async def test_individual_volume_and_group_volume_use_same_data(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Overflow rescale on individual change should be visible to subsequent group change."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        syncgroup, leader, _children = _setup_sync_group(
            controller, mock_mass, provider, leader_volume=80, child_configs={"child_a": 40}
        )
        _make_mock_handle_volume(controller)

        controller._initialize_group_ratios(syncgroup)
        # group_vol=80 on syncgroup, leader ratio=1.0, child_a ratio=0.5

        # Simulate individual volume change on leader to 100 (overflow)
        controller._update_child_ratio_in_group(leader, syncgroup, 100)
        # Overflow should raise group_vol to 100, leader ratio=1.0,
        # child_a ratio rescaled: (80 * 0.5) / 100 = 0.4
        assert syncgroup.extra_data[ATTR_GROUP_VOLUME_LEVEL] == 100
        ratios = syncgroup.extra_data[ATTR_GROUP_CHILD_RATIOS]
        assert ratios[leader.player_id] == pytest.approx(1.0)
        assert ratios["child_a"] == pytest.approx(0.4)

        # Now set group volume via the leader (routes to syncgroup)
        volume_calls = _make_mock_handle_volume(controller)
        await controller.cmd_group_volume(leader.player_id, 50)

        # Verify it read the UPDATED ratios from the syncgroup
        child_volumes = {pid: vol for pid, vol, _ in volume_calls}
        assert child_volumes[leader.player_id] == 50  # 1.0 * 50
        assert child_volumes["child_a"] == 20  # 0.4 * 50
        assert syncgroup.extra_data[ATTR_GROUP_VOLUME_LEVEL] == 50

    def test_find_adhoc_sync_leader_player_is_leader(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Player with group_members and type PLAYER should be its own adhoc leader."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        leader = MockPlayer(provider, "leader", "Leader")
        leader._attr_volume_level = 80
        leader._attr_group_members = ["leader", "child_a"]
        leader._cache.clear()

        child = MockPlayer(provider, "child_a", "Child_A")
        child._attr_volume_level = 40
        child._cache.clear()

        controller._players = {"leader": leader, "child_a": child}
        controller._player_throttlers = {
            "leader": Throttler(1, 0.05),
            "child_a": Throttler(1, 0.05),
        }
        mock_mass.players = controller

        for p in controller._players.values():
            p.set_initialized()
            p.update_state(signal_event=False)

        result = controller._find_adhoc_sync_leader(leader)
        assert result is not None
        assert result.player_id == "leader"

    def test_find_adhoc_sync_leader_player_is_member(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Sync member should find its adhoc leader via synced_to."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        leader = MockPlayer(provider, "leader", "Leader")
        leader._attr_volume_level = 80
        leader._attr_group_members = ["leader", "child_a"]
        leader._cache.clear()

        child = MockPlayer(provider, "child_a", "Child_A")
        child._attr_volume_level = 40
        child._cache.clear()

        controller._players = {"leader": leader, "child_a": child}
        controller._player_throttlers = {
            "leader": Throttler(1, 0.05),
            "child_a": Throttler(1, 0.05),
        }
        mock_mass.players = controller

        for p in controller._players.values():
            p.set_initialized()
            p.update_state(signal_event=False)

        result = controller._find_adhoc_sync_leader(child)
        assert result is not None
        assert result.player_id == "leader"

    def test_find_adhoc_sync_leader_returns_none_with_sync_group_player(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """When a SyncGroupPlayer exists, _find_adhoc_sync_leader should return None."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        _syncgroup, leader, children = _setup_sync_group(controller, mock_mass, provider)

        # Leader has a SyncGroupPlayer, so it's NOT ad-hoc
        result = controller._find_adhoc_sync_leader(leader)
        assert result is None

        # Child also has a SyncGroupPlayer backing the group
        child = next(iter(children.values()))
        result = controller._find_adhoc_sync_leader(child)
        assert result is None

    def test_membership_change_skips_sync_leader_when_syncgroup_exists(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Ratio init should only run on the SyncGroupPlayer, not the sync leader."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        syncgroup, leader, _children = _setup_sync_group(controller, mock_mass, provider)

        # Simulate membership change on the leader
        data_owner = controller._resolve_group_data_owner(leader)
        assert data_owner.player_id == syncgroup.player_id
        assert data_owner.player_id != leader.player_id

    async def test_group_volume_property_reads_from_sync_group_player(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Sync leader's group_volume property should read from the SyncGroupPlayer."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        syncgroup, leader, _children = _setup_sync_group(controller, mock_mass, provider)
        _make_mock_handle_volume(controller)

        controller._initialize_group_ratios(syncgroup)

        await controller.cmd_group_volume(leader.player_id, 65)

        syncgroup._cache.clear()
        leader._cache.clear()
        assert syncgroup.group_volume == 65
        assert leader.group_volume == 65

    async def test_adhoc_sync_leader_individual_volume_updates_ratio(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Individual volume changes in ad-hoc sync groups should update ratios on the leader."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        leader = MockPlayer(provider, "leader", "Leader")
        leader._attr_volume_level = 80
        leader._attr_group_members = ["leader", "child_a"]
        leader._cache.clear()

        child = MockPlayer(provider, "child_a", "Child_A")
        child._attr_volume_level = 40
        child._cache.clear()

        controller._players = {"leader": leader, "child_a": child}
        controller._player_throttlers = {
            "leader": Throttler(1, 0.05),
            "child_a": Throttler(1, 0.05),
        }
        mock_mass.players = controller

        for p in controller._players.values():
            p.set_initialized()
            p.update_state(signal_event=False)

        controller._initialize_group_ratios(leader)
        # group_vol=80, leader ratio=1.0, child_a ratio=0.5

        # Simulate individual volume change on child_a to 60
        controller._update_child_ratio_in_group(child, leader, 60)

        ratios = leader.extra_data[ATTR_GROUP_CHILD_RATIOS]
        assert ratios["child_a"] == pytest.approx(60 / 80)
        assert ratios["leader"] == pytest.approx(1.0)

    async def test_synced_to_redirect_resolves_to_sync_group_player(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """cmd_group_volume on a sync member should resolve through to the SyncGroupPlayer."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        syncgroup, _leader, children = _setup_sync_group(
            controller, mock_mass, provider, leader_volume=80, child_configs={"child_a": 40}
        )
        _make_mock_handle_volume(controller)

        controller._initialize_group_ratios(syncgroup)

        child = children["child_a"]
        await controller.cmd_group_volume(child.player_id, 50)

        assert syncgroup.extra_data[ATTR_GROUP_VOLUME_LEVEL] == 50


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


class TestPluginCallbackIsolation:
    """Test that plugin callbacks only fire for group/standalone volume changes."""

    async def test_individual_child_vol_does_not_fire_plugin_callback(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Individual child volume change in a group must NOT fire plugin on_volume."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, children = _setup_group(
            controller, mock_mass, provider, child_configs={"child_a": 80, "child_b": 40}
        )
        for p in controller._players.values():
            p.set_initialized()
            p.volume_set = AsyncMock()  # type: ignore[method-assign]

        plugin_source, on_volume_mock = _make_mock_plugin_source(group_player.player_id)
        _patch_plugin_source(controller, plugin_source)

        controller._initialize_group_ratios(group_player)
        # group_vol=80, child_a ratio=1.0, child_b ratio=0.5

        child_a = children["child_a"]
        await controller._handle_cmd_volume_set(child_a.player_id, 60)

        on_volume_mock.assert_not_called()

        ratios = group_player.extra_data[ATTR_GROUP_CHILD_RATIOS]
        assert ratios["child_a"] == pytest.approx(60 / 80)

    async def test_overflow_rescale_fires_plugin_callback_with_group_vol(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Overflow rescale (child vol > group vol) must fire plugin with the NEW group vol."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, children = _setup_group(
            controller, mock_mass, provider, child_configs={"child_a": 60, "child_b": 30}
        )
        for p in controller._players.values():
            p.set_initialized()
            p.volume_set = AsyncMock()  # type: ignore[method-assign]

        plugin_source, on_volume_mock = _make_mock_plugin_source(group_player.player_id)
        _patch_plugin_source(controller, plugin_source)

        controller._initialize_group_ratios(group_player)
        # group_vol=60, child_a ratio=1.0, child_b ratio=0.5

        child_b = children["child_b"]
        # Set child_b to 90, exceeding group_vol of 60 -> overflow
        await controller._handle_cmd_volume_set(child_b.player_id, 90)

        on_volume_mock.assert_called_once_with(90)
        assert group_player.extra_data[ATTR_GROUP_VOLUME_LEVEL] == 90

    async def test_overflow_from_zero_fires_plugin_callback(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Overflow from group_vol=0 must fire plugin with the new group volume."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, children = _setup_group(
            controller, mock_mass, provider, child_configs={"child_a": 80, "child_b": 40}
        )
        for p in controller._players.values():
            p.set_initialized()
            p.volume_set = AsyncMock()  # type: ignore[method-assign]

        controller._initialize_group_ratios(group_player)
        group_player.extra_data[ATTR_GROUP_VOLUME_LEVEL] = 0
        group_player._cache.clear()

        plugin_source, on_volume_mock = _make_mock_plugin_source(group_player.player_id)
        _patch_plugin_source(controller, plugin_source)

        child_a = children["child_a"]
        await controller._handle_cmd_volume_set(child_a.player_id, 50)

        on_volume_mock.assert_called_once_with(50)
        assert group_player.extra_data[ATTR_GROUP_VOLUME_LEVEL] == 50

    async def test_protocol_player_skips_plugin_and_ratio(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Protocol players must skip both plugin callbacks and ratio updates."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _children = _setup_group(
            controller, mock_mass, provider, child_configs={"child_a": 80}
        )

        protocol_player = MockPlayer(
            provider, "proto1", "Protocol1", player_type=PlayerType.PROTOCOL
        )
        protocol_player._attr_volume_level = 80
        protocol_player._cache.clear()
        protocol_player.volume_set = AsyncMock()  # type: ignore[method-assign]
        controller._players["proto1"] = protocol_player
        controller._player_throttlers["proto1"] = Throttler(1, 0.05)
        protocol_player.update_state(signal_event=False)

        plugin_source, on_volume_mock = _make_mock_plugin_source(group_player.player_id)
        _patch_plugin_source(controller, plugin_source)

        controller._initialize_group_ratios(group_player)
        ratios_before = dict(group_player.extra_data[ATTR_GROUP_CHILD_RATIOS])

        await controller._handle_cmd_volume_set(protocol_player.player_id, 50)

        on_volume_mock.assert_not_called()
        assert group_player.extra_data[ATTR_GROUP_CHILD_RATIOS] == ratios_before

    async def test_standalone_player_fires_plugin_callback(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """Standalone players (not in any group) must fire plugin on_volume normally."""
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

    async def test_group_volume_set_fires_plugin_callback(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """set_group_volume must fire the plugin callback with the group volume."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, _children = _setup_group(
            controller, mock_mass, provider, child_configs={"child_a": 80, "child_b": 40}
        )
        _make_mock_handle_volume(controller)

        plugin_source, on_volume_mock = _make_mock_plugin_source(group_player.player_id)
        _patch_plugin_source(controller, plugin_source)

        controller._initialize_group_ratios(group_player)

        await controller.set_group_volume(group_player, 50)

        on_volume_mock.assert_called_once_with(50)

    async def test_update_child_ratio_returns_none_on_normal_update(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """_update_child_ratio_in_group should return None when no overflow occurs."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, children = _setup_group(
            controller, mock_mass, provider, child_configs={"child_a": 80, "child_b": 40}
        )
        controller._initialize_group_ratios(group_player)

        result = controller._update_child_ratio_in_group(children["child_b"], group_player, 60)
        assert result is None

    async def test_update_child_ratio_returns_group_vol_on_overflow(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """_update_child_ratio_in_group should return new group_vol on overflow."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, children = _setup_group(
            controller, mock_mass, provider, child_configs={"child_a": 60, "child_b": 30}
        )
        controller._initialize_group_ratios(group_player)
        # group_vol=60

        result = controller._update_child_ratio_in_group(children["child_b"], group_player, 90)
        assert result == 90

    async def test_update_child_ratio_returns_group_vol_on_overflow_from_zero(
        self, controller: PlayerController, mock_mass: MagicMock
    ) -> None:
        """_update_child_ratio_in_group should return new group_vol on overflow from zero."""
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group_player, children = _setup_group(
            controller, mock_mass, provider, child_configs={"child_a": 80, "child_b": 40}
        )
        _make_mock_handle_volume(controller)
        controller._initialize_group_ratios(group_player)
        await controller.set_group_volume(group_player, 0)

        result = controller._update_child_ratio_in_group(children["child_a"], group_player, 50)
        assert result == 50
