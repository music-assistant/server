"""Tests for player control config resolution (volume, mute, power).

Verifies that:
- Explicitly configured values are always returned (even if not yet resolvable)
- Only unset configs (raw value is None) trigger auto-detection fallthrough
- Debug logging fires when a configured value doesn't currently resolve
- Post-startup reconciliation clears stale configs
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock, patch

import pytest
from music_assistant_models.constants import (
    PLAYER_CONTROL_FAKE,
    PLAYER_CONTROL_NATIVE,
    PLAYER_CONTROL_NONE,
)
from music_assistant_models.enums import PlayerFeature
from music_assistant_models.player import OutputProtocol

from music_assistant.constants import CONF_MUTE_CONTROL, CONF_POWER_CONTROL, CONF_VOLUME_CONTROL
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
    mass.config.get_raw_player_config_value = MagicMock(return_value=None)
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    mass.config.set = MagicMock()
    mass.signal_event = MagicMock()
    mass.get_providers = MagicMock(return_value=[])
    mass.players = MagicMock()
    mass.players.get_player = MagicMock(return_value=None)
    mass.players.get_player_control = MagicMock(return_value=None)
    return mass


@pytest.fixture
def player(mock_mass: MagicMock) -> MockPlayer:
    """Create a mock player with volume support."""
    provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
    p = MockPlayer(provider, "test_player", "Test Player")
    p._attr_supported_features = {PlayerFeature.VOLUME_SET, PlayerFeature.VOLUME_MUTE}
    p._cache.clear()
    return p


class TestResolveControlConfig:
    """Test _resolve_control_config resolution logic."""

    def test_returns_native_constant(self, player: MockPlayer, mock_mass: MagicMock) -> None:
        """Known constants are returned directly."""
        mock_mass.config.get_raw_player_config_value.return_value = PLAYER_CONTROL_NATIVE
        assert player._resolve_control_config(CONF_VOLUME_CONTROL) == PLAYER_CONTROL_NATIVE

    def test_returns_fake_constant(self, player: MockPlayer, mock_mass: MagicMock) -> None:
        """FAKE constant is returned directly."""
        mock_mass.config.get_raw_player_config_value.return_value = PLAYER_CONTROL_FAKE
        assert player._resolve_control_config(CONF_VOLUME_CONTROL) == PLAYER_CONTROL_FAKE

    def test_returns_none_constant(self, player: MockPlayer, mock_mass: MagicMock) -> None:
        """NONE constant (the string, not Python None) is returned directly."""
        mock_mass.config.get_raw_player_config_value.return_value = PLAYER_CONTROL_NONE
        assert player._resolve_control_config(CONF_VOLUME_CONTROL) == PLAYER_CONTROL_NONE

    def test_returns_valid_player_id(self, player: MockPlayer, mock_mass: MagicMock) -> None:
        """Config referencing an existing player is returned."""
        mock_mass.config.get_raw_player_config_value.return_value = "other_player_id"
        mock_mass.players.get_player.return_value = MagicMock()
        assert player._resolve_control_config(CONF_VOLUME_CONTROL) == "other_player_id"

    def test_returns_valid_player_control_id(
        self, player: MockPlayer, mock_mass: MagicMock
    ) -> None:
        """Config referencing an existing player control is returned."""
        mock_mass.config.get_raw_player_config_value.return_value = "some_control_id"
        mock_mass.players.get_player.return_value = None
        mock_mass.players.get_player_control.return_value = MagicMock()
        assert player._resolve_control_config(CONF_VOLUME_CONTROL) == "some_control_id"

    def test_unresolvable_config_still_returned(
        self, player: MockPlayer, mock_mass: MagicMock
    ) -> None:
        """Explicitly set config is returned even if it doesn't currently resolve."""
        mock_mass.config.get_raw_player_config_value.return_value = "not_yet_registered_player"
        mock_mass.players.get_player.return_value = None
        mock_mass.players.get_player_control.return_value = None
        assert player._resolve_control_config(CONF_VOLUME_CONTROL) == "not_yet_registered_player"

    def test_no_config_returns_none(self, player: MockPlayer, mock_mass: MagicMock) -> None:
        """No config value (raw None) returns None for auto-detection."""
        mock_mass.config.get_raw_player_config_value.return_value = None
        assert player._resolve_control_config(CONF_VOLUME_CONTROL) is None

    def test_empty_string_treated_as_unset(self, player: MockPlayer, mock_mass: MagicMock) -> None:
        """Empty string config is treated as unset, returning None for auto-detection."""
        mock_mass.config.get_raw_player_config_value.return_value = ""
        assert player._resolve_control_config(CONF_VOLUME_CONTROL) is None

    def test_unresolvable_config_logs_debug(
        self, player: MockPlayer, mock_mass: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Unresolvable config emits a DEBUG-level log."""
        mock_mass.config.get_raw_player_config_value.return_value = "spb_notregisteredyet"
        mock_mass.players.get_player.return_value = None
        mock_mass.players.get_player_control.return_value = None
        with caplog.at_level(logging.DEBUG):
            player._resolve_control_config(CONF_VOLUME_CONTROL)
        debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("spb_notregisteredyet" in r.message for r in debug_records)
        assert any("test_player" in r.message for r in debug_records)

    def test_resolvable_config_no_debug_log(
        self, player: MockPlayer, mock_mass: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Config that resolves to an existing player does not log."""
        mock_mass.config.get_raw_player_config_value.return_value = "existing_player"
        mock_mass.players.get_player.return_value = MagicMock()
        with caplog.at_level(logging.DEBUG):
            player._resolve_control_config(CONF_VOLUME_CONTROL)
        assert not any("does not currently resolve" in r.message for r in caplog.records)

    def test_known_constant_no_debug_log(
        self, player: MockPlayer, mock_mass: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Known constants do not trigger the debug log."""
        mock_mass.config.get_raw_player_config_value.return_value = PLAYER_CONTROL_NATIVE
        with caplog.at_level(logging.DEBUG):
            player._resolve_control_config(CONF_VOLUME_CONTROL)
        assert not any("does not currently resolve" in r.message for r in caplog.records)


class TestVolumeControlAutoDetection:
    """Test volume_control auto-detection when no config is set."""

    def test_no_config_native_support(self, player: MockPlayer, mock_mass: MagicMock) -> None:
        """No config with native volume support auto-detects to NATIVE."""
        mock_mass.config.get_raw_player_config_value.return_value = None
        assert player.volume_control == PLAYER_CONTROL_NATIVE

    def test_no_config_protocol_player_fallthrough(
        self, player: MockPlayer, mock_mass: MagicMock
    ) -> None:
        """No config without native support falls through to protocol player."""
        player._attr_supported_features = set()
        protocol_player = MagicMock()
        protocol_player.player_id = "ap_protocol_player"
        protocol_player.available = True
        protocol_player.supported_features = {PlayerFeature.VOLUME_SET}
        player.set_linked_output_protocols(
            [
                OutputProtocol(
                    output_protocol_id="ap_protocol_player",
                    name="AirPlay",
                    protocol_domain="airplay",
                )
            ]
        )
        player._cache.clear()
        mock_mass.config.get_raw_player_config_value.return_value = None
        mock_mass.players.get_player.side_effect = (
            lambda pid: protocol_player if pid == "ap_protocol_player" else None
        )
        assert player.volume_control == "ap_protocol_player"

    def test_no_config_no_native_no_protocol_falls_to_none(
        self, player: MockPlayer, mock_mass: MagicMock
    ) -> None:
        """No config with no native support and no protocol player falls to NONE."""
        player._attr_supported_features = set()
        player._cache.clear()
        mock_mass.config.get_raw_player_config_value.return_value = None
        mock_mass.players.get_player.return_value = None
        assert player.volume_control == PLAYER_CONTROL_NONE

    def test_explicit_config_always_used(self, player: MockPlayer, mock_mass: MagicMock) -> None:
        """Explicitly set config is used even if the player doesn't exist yet."""
        mock_mass.config.get_raw_player_config_value.return_value = "spb_notregisteredyet"
        mock_mass.players.get_player.return_value = None
        mock_mass.players.get_player_control.return_value = None
        assert player.volume_control == "spb_notregisteredyet"


class TestMuteControlAutoDetection:
    """Test mute_control auto-detection when no config is set."""

    def test_no_config_native_support(self, player: MockPlayer, mock_mass: MagicMock) -> None:
        """No config with native mute support auto-detects to NATIVE."""
        mock_mass.config.get_raw_player_config_value.return_value = None
        assert player.mute_control == PLAYER_CONTROL_NATIVE

    def test_no_config_protocol_player_fallthrough(
        self, player: MockPlayer, mock_mass: MagicMock
    ) -> None:
        """No config without native support falls through to protocol player."""
        player._attr_supported_features = set()
        protocol_player = MagicMock()
        protocol_player.player_id = "ap_protocol_player"
        protocol_player.available = True
        protocol_player.supported_features = {PlayerFeature.VOLUME_MUTE}
        player.set_linked_output_protocols(
            [
                OutputProtocol(
                    output_protocol_id="ap_protocol_player",
                    name="AirPlay",
                    protocol_domain="airplay",
                )
            ]
        )
        player._cache.clear()
        mock_mass.config.get_raw_player_config_value.return_value = None
        mock_mass.players.get_player.side_effect = (
            lambda pid: protocol_player if pid == "ap_protocol_player" else None
        )
        assert player.mute_control == "ap_protocol_player"

    def test_no_config_no_native_no_protocol_falls_to_none(
        self, player: MockPlayer, mock_mass: MagicMock
    ) -> None:
        """No config with no native support and no protocol player falls to NONE."""
        player._attr_supported_features = set()
        player._cache.clear()
        mock_mass.config.get_raw_player_config_value.return_value = None
        mock_mass.players.get_player.return_value = None
        assert player.mute_control == PLAYER_CONTROL_NONE


class TestPowerControlAutoDetection:
    """Test power_control auto-detection when no config is set."""

    def test_no_config_no_native_falls_to_none(
        self, player: MockPlayer, mock_mass: MagicMock
    ) -> None:
        """No config without native power support falls to NONE."""
        mock_mass.config.get_raw_player_config_value.return_value = None
        assert player.power_control == PLAYER_CONTROL_NONE

    def test_no_config_native_support(self, player: MockPlayer, mock_mass: MagicMock) -> None:
        """No config with native power support auto-detects to NATIVE."""
        player._attr_supported_features.add(PlayerFeature.POWER)
        player._cache.clear()
        mock_mass.config.get_raw_player_config_value.return_value = None
        assert player.power_control == PLAYER_CONTROL_NATIVE

    def test_no_config_ignores_protocol_player_with_power(
        self, player: MockPlayer, mock_mass: MagicMock
    ) -> None:
        """Power control deliberately skips protocol player fallthrough."""
        player._attr_supported_features = set()
        protocol_player = MagicMock()
        protocol_player.player_id = "ap_protocol_player"
        protocol_player.available = True
        protocol_player.supported_features = {PlayerFeature.POWER}
        player.set_linked_output_protocols(
            [
                OutputProtocol(
                    output_protocol_id="ap_protocol_player",
                    name="AirPlay",
                    protocol_domain="airplay",
                )
            ]
        )
        player._cache.clear()
        mock_mass.config.get_raw_player_config_value.return_value = None
        mock_mass.players.get_player.side_effect = (
            lambda pid: protocol_player if pid == "ap_protocol_player" else None
        )
        assert player.power_control == PLAYER_CONTROL_NONE


class TestCacheInvalidationLifecycle:
    """Test that cached properties re-evaluate correctly after cache invalidation."""

    def test_unresolvable_config_resolves_after_player_registers(
        self, player: MockPlayer, mock_mass: MagicMock
    ) -> None:
        """Config pointing to a not-yet-registered player works once it registers."""
        mock_mass.config.get_raw_player_config_value.return_value = "spb_bridge_player"
        mock_mass.players.get_player.return_value = None

        # first evaluation: player not registered yet, but config is returned anyway
        assert player.volume_control == "spb_bridge_player"

        # player registers, cache cleared (simulates update_state)
        mock_mass.players.get_player.return_value = MagicMock()
        player._cache.clear()

        # second evaluation: same value, now resolvable
        assert player.volume_control == "spb_bridge_player"

    def test_no_config_auto_detects_then_persists(
        self, player: MockPlayer, mock_mass: MagicMock
    ) -> None:
        """No config auto-detects to NATIVE and caches the result."""
        mock_mass.config.get_raw_player_config_value.return_value = None
        assert player.volume_control == PLAYER_CONTROL_NATIVE

        # even if config changes underneath, cached value persists
        mock_mass.config.get_raw_player_config_value.return_value = "new_player_id"
        assert player.volume_control == PLAYER_CONTROL_NATIVE

        # only after clearing does it pick up the new config
        player._cache.clear()
        assert player.volume_control == "new_player_id"


class TestReconcileStaleControlConfigs:
    """Test the controller's post-startup reconciliation of stale control configs."""

    @pytest.fixture
    def controller_with_player(
        self, mock_mass: MagicMock, player: MockPlayer
    ) -> tuple[PlayerController, MockPlayer]:
        """Create a PlayerController with a registered player."""
        ctrl = PlayerController(mock_mass)
        ctrl._players = {player.player_id: player}
        ctrl._player_throttlers = {player.player_id: Throttler(1, 0.05)}
        mock_mass.players = ctrl
        return ctrl, player

    def test_clears_stale_volume_config(
        self, controller_with_player: tuple[PlayerController, MockPlayer], mock_mass: MagicMock
    ) -> None:
        """Stale volume config referencing a nonexistent player is cleared."""
        ctrl, player = controller_with_player
        mock_mass.config.get_raw_player_config_value.return_value = "deleted_chromecast_id"
        asyncio.run(ctrl._reconcile_stale_control_configs())
        mock_mass.config.set_raw_player_config_value.assert_any_call(
            player.player_id, CONF_VOLUME_CONTROL, None
        )

    def test_clears_all_three_stale_configs(
        self, controller_with_player: tuple[PlayerController, MockPlayer], mock_mass: MagicMock
    ) -> None:
        """All three control configs are cleared when all are stale."""
        ctrl, _player = controller_with_player
        mock_mass.config.get_raw_player_config_value.return_value = "nonexistent_player"
        asyncio.run(ctrl._reconcile_stale_control_configs())
        calls = mock_mass.config.set_raw_player_config_value.call_args_list
        cleared_keys = {call.args[1] for call in calls}
        assert cleared_keys == {CONF_VOLUME_CONTROL, CONF_MUTE_CONTROL, CONF_POWER_CONTROL}

    def test_preserves_native_constant(
        self, controller_with_player: tuple[PlayerController, MockPlayer], mock_mass: MagicMock
    ) -> None:
        """NATIVE constant is not cleared."""
        ctrl, _ = controller_with_player
        mock_mass.config.get_raw_player_config_value.return_value = PLAYER_CONTROL_NATIVE
        asyncio.run(ctrl._reconcile_stale_control_configs())
        mock_mass.config.set_raw_player_config_value.assert_not_called()

    def test_preserves_fake_constant(
        self, controller_with_player: tuple[PlayerController, MockPlayer], mock_mass: MagicMock
    ) -> None:
        """FAKE constant is not cleared."""
        ctrl, _ = controller_with_player
        mock_mass.config.get_raw_player_config_value.return_value = PLAYER_CONTROL_FAKE
        asyncio.run(ctrl._reconcile_stale_control_configs())
        mock_mass.config.set_raw_player_config_value.assert_not_called()

    def test_preserves_none_constant(
        self, controller_with_player: tuple[PlayerController, MockPlayer], mock_mass: MagicMock
    ) -> None:
        """NONE constant (the string) is not cleared."""
        ctrl, _ = controller_with_player
        mock_mass.config.get_raw_player_config_value.return_value = PLAYER_CONTROL_NONE
        asyncio.run(ctrl._reconcile_stale_control_configs())
        mock_mass.config.set_raw_player_config_value.assert_not_called()

    def test_preserves_resolvable_player_id(
        self, controller_with_player: tuple[PlayerController, MockPlayer], mock_mass: MagicMock
    ) -> None:
        """Config pointing to a registered player is not cleared."""
        ctrl, _ = controller_with_player
        other = MagicMock()
        other.player_id = "other_player"
        ctrl._players["other_player"] = other
        mock_mass.config.get_raw_player_config_value.return_value = "other_player"
        asyncio.run(ctrl._reconcile_stale_control_configs())
        mock_mass.config.set_raw_player_config_value.assert_not_called()

    def test_preserves_resolvable_player_control_id(
        self, controller_with_player: tuple[PlayerController, MockPlayer], mock_mass: MagicMock
    ) -> None:
        """Config pointing to a registered player control is not cleared."""
        ctrl, _ = controller_with_player
        ctrl._controls["some_control_id"] = MagicMock()
        mock_mass.config.get_raw_player_config_value.return_value = "some_control_id"
        asyncio.run(ctrl._reconcile_stale_control_configs())
        mock_mass.config.set_raw_player_config_value.assert_not_called()

    def test_preserves_unset_config(
        self, controller_with_player: tuple[PlayerController, MockPlayer], mock_mass: MagicMock
    ) -> None:
        """None (unset) config is left alone — auto-detection handles it."""
        ctrl, _ = controller_with_player
        mock_mass.config.get_raw_player_config_value.return_value = None
        asyncio.run(ctrl._reconcile_stale_control_configs())
        mock_mass.config.set_raw_player_config_value.assert_not_called()

    def test_calls_update_state_on_affected_player(
        self, controller_with_player: tuple[PlayerController, MockPlayer], mock_mass: MagicMock
    ) -> None:
        """update_state is called on players that had stale configs cleared."""
        ctrl, player = controller_with_player
        mock_mass.config.get_raw_player_config_value.return_value = "stale_id"
        with patch.object(player, "update_state") as mock_update:
            asyncio.run(ctrl._reconcile_stale_control_configs())
            mock_update.assert_called_once()

    def test_no_update_state_when_nothing_cleared(
        self, controller_with_player: tuple[PlayerController, MockPlayer], mock_mass: MagicMock
    ) -> None:
        """update_state is not called when no configs were cleared."""
        ctrl, player = controller_with_player
        mock_mass.config.get_raw_player_config_value.return_value = PLAYER_CONTROL_NATIVE
        with patch.object(player, "update_state") as mock_update:
            asyncio.run(ctrl._reconcile_stale_control_configs())
            mock_update.assert_not_called()

    def test_logs_summary_when_configs_cleared(
        self,
        controller_with_player: tuple[PlayerController, MockPlayer],
        mock_mass: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A summary log is emitted when stale configs are cleared."""
        ctrl, _ = controller_with_player
        mock_mass.config.get_raw_player_config_value.return_value = "stale_id"
        with caplog.at_level(logging.INFO):
            asyncio.run(ctrl._reconcile_stale_control_configs())
        assert any("Reconciliation complete" in r.message for r in caplog.records)

    def test_no_summary_log_when_nothing_cleared(
        self,
        controller_with_player: tuple[PlayerController, MockPlayer],
        mock_mass: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """No summary log when all configs are valid."""
        ctrl, _ = controller_with_player
        mock_mass.config.get_raw_player_config_value.return_value = PLAYER_CONTROL_NATIVE
        with caplog.at_level(logging.INFO):
            asyncio.run(ctrl._reconcile_stale_control_configs())
        assert not any("Reconciliation complete" in r.message for r in caplog.records)

    def test_only_affected_player_gets_update_state(self, mock_mass: MagicMock) -> None:
        """With two players, update_state is called only on the one with stale config."""
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        stale_player = MockPlayer(provider, "stale_player", "Stale Player")
        valid_player = MockPlayer(provider, "valid_player", "Valid Player")

        ctrl = PlayerController(mock_mass)
        ctrl._players = {
            "stale_player": stale_player,
            "valid_player": valid_player,
        }
        ctrl._player_throttlers = {
            "stale_player": Throttler(1, 0.05),
            "valid_player": Throttler(1, 0.05),
        }
        mock_mass.players = ctrl

        def config_side_effect(player_id: str, _key: str) -> str | None:
            if player_id == "stale_player":
                return "deleted_chromecast_id"
            return PLAYER_CONTROL_NATIVE

        mock_mass.config.get_raw_player_config_value.side_effect = config_side_effect

        with (
            patch.object(stale_player, "update_state") as mock_stale_update,
            patch.object(valid_player, "update_state") as mock_valid_update,
        ):
            asyncio.run(ctrl._reconcile_stale_control_configs())
            mock_stale_update.assert_called_once()
            mock_valid_update.assert_not_called()
