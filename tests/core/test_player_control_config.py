"""Tests for player control config validation (volume, mute, power).

Verifies that stale config values (referencing players/controls that no longer exist)
fall through to auto-detection instead of being blindly trusted.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
from music_assistant_models.constants import (
    PLAYER_CONTROL_FAKE,
    PLAYER_CONTROL_NATIVE,
    PLAYER_CONTROL_NONE,
)
from music_assistant_models.enums import PlayerFeature
from music_assistant_models.player import OutputProtocol

from music_assistant.constants import CONF_VOLUME_CONTROL
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
    """Test _resolve_control_config validation logic."""

    def test_returns_native_constant(self, player: MockPlayer, mock_mass: MagicMock) -> None:
        """Known constants are returned directly without player lookup."""
        mock_mass.config.get_raw_player_config_value.return_value = PLAYER_CONTROL_NATIVE
        assert player._resolve_control_config(CONF_VOLUME_CONTROL) == PLAYER_CONTROL_NATIVE

    def test_returns_fake_constant(self, player: MockPlayer, mock_mass: MagicMock) -> None:
        """FAKE constant is returned directly."""
        mock_mass.config.get_raw_player_config_value.return_value = PLAYER_CONTROL_FAKE
        assert player._resolve_control_config(CONF_VOLUME_CONTROL) == PLAYER_CONTROL_FAKE

    def test_returns_none_constant(self, player: MockPlayer, mock_mass: MagicMock) -> None:
        """NONE constant is returned directly."""
        mock_mass.config.get_raw_player_config_value.return_value = PLAYER_CONTROL_NONE
        assert player._resolve_control_config(CONF_VOLUME_CONTROL) == PLAYER_CONTROL_NONE

    def test_returns_valid_player_id(self, player: MockPlayer, mock_mass: MagicMock) -> None:
        """Config referencing an existing player is accepted."""
        mock_mass.config.get_raw_player_config_value.return_value = "other_player_id"
        mock_mass.players.get_player.return_value = MagicMock()  # player exists
        assert player._resolve_control_config(CONF_VOLUME_CONTROL) == "other_player_id"

    def test_returns_valid_player_control_id(
        self, player: MockPlayer, mock_mass: MagicMock
    ) -> None:
        """Config referencing an existing player control is accepted."""
        mock_mass.config.get_raw_player_config_value.return_value = "some_control_id"
        mock_mass.players.get_player.return_value = None
        mock_mass.players.get_player_control.return_value = MagicMock()  # control exists
        assert player._resolve_control_config(CONF_VOLUME_CONTROL) == "some_control_id"

    def test_stale_config_returns_none(self, player: MockPlayer, mock_mass: MagicMock) -> None:
        """Config referencing a nonexistent player/control returns None."""
        mock_mass.config.get_raw_player_config_value.return_value = "deleted_player_id"
        mock_mass.players.get_player.return_value = None
        mock_mass.players.get_player_control.return_value = None
        assert player._resolve_control_config(CONF_VOLUME_CONTROL) is None

    def test_no_config_returns_none(self, player: MockPlayer, mock_mass: MagicMock) -> None:
        """No config value at all returns None."""
        mock_mass.config.get_raw_player_config_value.return_value = None
        assert player._resolve_control_config(CONF_VOLUME_CONTROL) is None

    def test_empty_string_config_returns_none(
        self, player: MockPlayer, mock_mass: MagicMock
    ) -> None:
        """Empty string config is treated as unset (falsy walrus operator)."""
        mock_mass.config.get_raw_player_config_value.return_value = ""
        assert player._resolve_control_config(CONF_VOLUME_CONTROL) is None

    def test_integer_config_treated_as_stale(
        self, player: MockPlayer, mock_mass: MagicMock
    ) -> None:
        """Non-string config values are coerced via str() and treated as stale."""
        mock_mass.config.get_raw_player_config_value.return_value = 42
        mock_mass.players.get_player.return_value = None
        mock_mass.players.get_player_control.return_value = None
        assert player._resolve_control_config(CONF_VOLUME_CONTROL) is None

    def test_bool_false_config_returns_none(self, player: MockPlayer, mock_mass: MagicMock) -> None:
        """False is falsy, so the walrus operator skips it entirely."""
        mock_mass.config.get_raw_player_config_value.return_value = False
        assert player._resolve_control_config(CONF_VOLUME_CONTROL) is None

    def test_bool_true_config_treated_as_stale(
        self, player: MockPlayer, mock_mass: MagicMock
    ) -> None:
        """True is truthy but str(True)='True' won't match any player."""
        mock_mass.config.get_raw_player_config_value.return_value = True
        mock_mass.players.get_player.return_value = None
        mock_mass.players.get_player_control.return_value = None
        assert player._resolve_control_config(CONF_VOLUME_CONTROL) is None

    def test_stale_config_logs_warning(
        self, player: MockPlayer, mock_mass: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Stale config emits a WARNING-level log with the stale ID and player ID."""
        mock_mass.config.get_raw_player_config_value.return_value = "deleted_chromecast_id"
        mock_mass.players.get_player.return_value = None
        mock_mass.players.get_player_control.return_value = None
        with caplog.at_level(logging.WARNING):
            player._resolve_control_config(CONF_VOLUME_CONTROL)
        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning_records) == 1
        assert "deleted_chromecast_id" in warning_records[0].message
        assert "test_player" in warning_records[0].message


class TestVolumeControlFallthrough:
    """Test that volume_control falls through to auto-detection on stale config."""

    def test_stale_config_falls_through_to_native(
        self, player: MockPlayer, mock_mass: MagicMock
    ) -> None:
        """Stale volume config falls through to NATIVE when player supports VOLUME_SET."""
        mock_mass.config.get_raw_player_config_value.return_value = "deleted_chromecast_id"
        mock_mass.players.get_player.return_value = None
        mock_mass.players.get_player_control.return_value = None
        assert player.volume_control == PLAYER_CONTROL_NATIVE

    def test_stale_config_no_native_falls_to_protocol_player(
        self, player: MockPlayer, mock_mass: MagicMock
    ) -> None:
        """Stale config without native support falls through to protocol player."""
        player._attr_supported_features = set()  # no native volume
        # set up a linked protocol player with volume support
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

        mock_mass.config.get_raw_player_config_value.return_value = "deleted_player_id"
        # stale ID returns None, but protocol player lookup returns the player
        mock_mass.players.get_player.side_effect = (
            lambda pid: protocol_player if pid == "ap_protocol_player" else None
        )
        mock_mass.players.get_player_control.return_value = None

        assert player.volume_control == "ap_protocol_player"

    def test_stale_config_no_native_no_protocol_falls_to_none(
        self, player: MockPlayer, mock_mass: MagicMock
    ) -> None:
        """Stale config with no native support and no protocol player falls to NONE."""
        player._attr_supported_features = set()
        player._cache.clear()
        mock_mass.config.get_raw_player_config_value.return_value = "deleted_player_id"
        mock_mass.players.get_player.return_value = None
        mock_mass.players.get_player_control.return_value = None
        assert player.volume_control == PLAYER_CONTROL_NONE


class TestMuteControlFallthrough:
    """Test that mute_control falls through to auto-detection on stale config."""

    def test_stale_config_falls_through_to_native(
        self, player: MockPlayer, mock_mass: MagicMock
    ) -> None:
        """Stale mute config falls through to NATIVE when player supports VOLUME_MUTE."""
        mock_mass.config.get_raw_player_config_value.return_value = "deleted_id"
        mock_mass.players.get_player.return_value = None
        mock_mass.players.get_player_control.return_value = None
        assert player.mute_control == PLAYER_CONTROL_NATIVE

    def test_stale_config_no_native_falls_to_protocol_player(
        self, player: MockPlayer, mock_mass: MagicMock
    ) -> None:
        """Stale mute config without native support falls through to protocol player."""
        player._attr_supported_features = set()  # no native mute
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

        mock_mass.config.get_raw_player_config_value.return_value = "deleted_id"
        mock_mass.players.get_player.side_effect = (
            lambda pid: protocol_player if pid == "ap_protocol_player" else None
        )
        mock_mass.players.get_player_control.return_value = None

        assert player.mute_control == "ap_protocol_player"

    def test_stale_config_no_native_no_protocol_falls_to_none(
        self, player: MockPlayer, mock_mass: MagicMock
    ) -> None:
        """Stale mute config with no native support and no protocol player falls to NONE."""
        player._attr_supported_features = set()
        player._cache.clear()
        mock_mass.config.get_raw_player_config_value.return_value = "deleted_id"
        mock_mass.players.get_player.return_value = None
        mock_mass.players.get_player_control.return_value = None
        assert player.mute_control == PLAYER_CONTROL_NONE


class TestPowerControlFallthrough:
    """Test that power_control falls through to auto-detection on stale config."""

    def test_stale_config_falls_through_to_none(
        self, player: MockPlayer, mock_mass: MagicMock
    ) -> None:
        """Stale power config falls through to NONE (no native power support)."""
        mock_mass.config.get_raw_player_config_value.return_value = "deleted_id"
        mock_mass.players.get_player.return_value = None
        mock_mass.players.get_player_control.return_value = None
        # MockPlayer doesn't have POWER feature by default
        assert player.power_control == PLAYER_CONTROL_NONE

    def test_stale_config_falls_through_to_native(
        self, player: MockPlayer, mock_mass: MagicMock
    ) -> None:
        """Stale power config falls through to NATIVE when player supports POWER."""
        player._attr_supported_features.add(PlayerFeature.POWER)
        player._cache.clear()
        mock_mass.config.get_raw_player_config_value.return_value = "deleted_id"
        mock_mass.players.get_player.return_value = None
        mock_mass.players.get_player_control.return_value = None
        assert player.power_control == PLAYER_CONTROL_NATIVE

    def test_stale_config_ignores_protocol_player_with_power(
        self, player: MockPlayer, mock_mass: MagicMock
    ) -> None:
        """Power control deliberately skips protocol player fallthrough."""
        player._attr_supported_features = set()  # no native power
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
        mock_mass.config.get_raw_player_config_value.return_value = "deleted_id"
        mock_mass.players.get_player.side_effect = (
            lambda pid: protocol_player if pid == "ap_protocol_player" else None
        )
        mock_mass.players.get_player_control.return_value = None
        assert player.power_control == PLAYER_CONTROL_NONE


class TestCacheInvalidationLifecycle:
    """Test that cached properties re-evaluate correctly after cache invalidation."""

    def test_stale_config_becomes_valid_after_cache_clear(
        self, player: MockPlayer, mock_mass: MagicMock
    ) -> None:
        """Config pointing to a not-yet-registered player resolves after cache clear."""
        target_player = MagicMock()
        mock_mass.config.get_raw_player_config_value.return_value = "other_player_id"
        mock_mass.players.get_player.return_value = None
        mock_mass.players.get_player_control.return_value = None

        # first evaluation: player not registered yet, falls through to native
        assert player.volume_control == PLAYER_CONTROL_NATIVE

        # player registers, cache cleared (simulates update_state)
        mock_mass.players.get_player.return_value = target_player
        player._cache.clear()

        # second evaluation: config now resolves
        assert player.volume_control == "other_player_id"

    def test_valid_config_becomes_stale_after_cache_clear(
        self, player: MockPlayer, mock_mass: MagicMock
    ) -> None:
        """Config pointing to a removed player falls through after cache clear."""
        target_player = MagicMock()
        mock_mass.config.get_raw_player_config_value.return_value = "other_player_id"
        mock_mass.players.get_player.return_value = target_player
        mock_mass.players.get_player_control.return_value = None

        # first evaluation: player exists, config resolves
        assert player.volume_control == "other_player_id"

        # player removed, cache cleared
        mock_mass.players.get_player.return_value = None
        player._cache.clear()

        # second evaluation: falls through to native
        assert player.volume_control == PLAYER_CONTROL_NATIVE

    def test_cached_value_persists_without_clear(
        self, player: MockPlayer, mock_mass: MagicMock
    ) -> None:
        """Cached property retains its value until cache is explicitly cleared."""
        mock_mass.config.get_raw_player_config_value.return_value = "deleted_id"
        mock_mass.players.get_player.return_value = None
        mock_mass.players.get_player_control.return_value = None

        # first evaluation: stale, falls through to native
        assert player.volume_control == PLAYER_CONTROL_NATIVE

        # change mock to make it resolve, but DON'T clear cache
        mock_mass.players.get_player.return_value = MagicMock()

        # cached value persists (still NATIVE)
        assert player.volume_control == PLAYER_CONTROL_NATIVE

        # only after clearing does it re-evaluate
        player._cache.clear()
        assert player.volume_control == "deleted_id"
