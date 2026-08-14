"""
Tests for Player control auto-select and explicit config logic.

Covers the power_control, volume_control, and mute_control properties
on the Player model, including:
- Explicit config values (native, fake, none)
- Explicit config pointing to a protocol player or player control
- Auto-select fallback: native -> protocol player -> none
- Protocol player preference for active output protocol
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from music_assistant_models.constants import (
    PLAYER_CONTROL_FAKE,
    PLAYER_CONTROL_NATIVE,
    PLAYER_CONTROL_NONE,
)
from music_assistant_models.enums import PlayerFeature

from music_assistant.constants import (
    CONF_MUTE_CONTROL,
    CONF_POWER_CONTROL,
    CONF_PREFERRED_OUTPUT_PROTOCOL,
    CONF_VOLUME_CONTROL,
)
from music_assistant.models.player import LinkedOutputProtocol
from tests.common import MockPlayer, MockProvider

_PROTO_DOMAIN = "test_protocol"


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance."""
    mass = MagicMock()
    mass.closing = False
    mass.loop = None
    mass.config.get = MagicMock(return_value=[])
    mass.config.get_raw_player_config_value = MagicMock(return_value=None)
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    mass.config.set = MagicMock()
    mass.signal_event = MagicMock()
    mass.get_providers = MagicMock(return_value=[])
    mass.players.get_player = MagicMock(return_value=None)
    mass.players.get_player_control = MagicMock(return_value=None)
    return mass


def _make_config_side_effect(
    overrides: dict[str, str | None],
) -> Any:
    """Create a side_effect for get_raw_player_config_value."""

    def _side_effect(_pid: str, key: str, *_args: Any) -> str | None:
        return overrides.get(key)

    return _side_effect


def _create_player(
    mock_mass: MagicMock,
    features: set[PlayerFeature] | None = None,
) -> MockPlayer:
    """Create a MockPlayer with the given features."""
    provider = MockProvider("test", mass=mock_mass)
    player = MockPlayer(provider, "main_player", "Main")
    player._attr_supported_features = features or set()
    player._cache.clear()
    return player


def _create_protocol_player(
    mock_mass: MagicMock,
    player_id: str,
    features: set[PlayerFeature],
    available: bool = True,
) -> MockPlayer:
    """Create a mock protocol player."""
    provider = MockProvider("protocol", mass=mock_mass)
    player = MockPlayer(provider, player_id, f"Protocol {player_id}")
    player._attr_supported_features = features
    player._attr_available = available
    player._cache.clear()
    return player


def _make_protocol_link(
    protocol_id: str,
    priority: int = 0,
) -> LinkedOutputProtocol:
    """Create a protocol link for testing."""
    return LinkedOutputProtocol(
        output_protocol_id=protocol_id,
        protocol_domain=_PROTO_DOMAIN,
        priority=priority,
    )


def _link_protocols(
    player: MockPlayer,
    protocols: list[LinkedOutputProtocol],
    active_protocol_id: str | None = None,
) -> None:
    """Link output protocols to a player and optionally set active protocol."""
    player.set_linked_output_protocols(protocols)
    if active_protocol_id is not None:
        player.set_active_output_protocol(active_protocol_id)
    player._cache.clear()


class TestPowerControlExplicitConfig:
    """Test power_control with explicit config values."""

    def test_explicit_native(self, mock_mass: MagicMock) -> None:
        """Power control returns native when explicitly configured and feature is supported."""
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_make_config_side_effect({CONF_POWER_CONTROL: PLAYER_CONTROL_NATIVE})
        )
        # NATIVE control requires the player to actually advertise POWER —
        # otherwise the getter degrades to NONE (e.g. when a group player
        # drops POWER from its feature set on upgrade but its stale config
        # still says NATIVE).
        player = _create_player(mock_mass, features={PlayerFeature.POWER})
        assert player.power_control == PLAYER_CONTROL_NATIVE

    def test_explicit_native_degrades_when_feature_missing(self, mock_mass: MagicMock) -> None:
        """Stale NATIVE config falls back to NONE when the player no longer advertises POWER."""
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_make_config_side_effect({CONF_POWER_CONTROL: PLAYER_CONTROL_NATIVE})
        )
        # no POWER in features → the explicit NATIVE in config is invalid
        player = _create_player(mock_mass)
        assert player.power_control == PLAYER_CONTROL_NONE

    def test_explicit_fake(self, mock_mass: MagicMock) -> None:
        """Power control returns fake when explicitly configured."""
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_make_config_side_effect({CONF_POWER_CONTROL: PLAYER_CONTROL_FAKE})
        )
        player = _create_player(mock_mass)
        assert player.power_control == PLAYER_CONTROL_FAKE

    def test_explicit_none(self, mock_mass: MagicMock) -> None:
        """Power control returns none when explicitly configured."""
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_make_config_side_effect({CONF_POWER_CONTROL: PLAYER_CONTROL_NONE})
        )
        player = _create_player(mock_mass, features={PlayerFeature.POWER})
        assert player.power_control == PLAYER_CONTROL_NONE

    def test_explicit_protocol_player_id_ignored(self, mock_mass: MagicMock) -> None:
        """Protocol player IDs are not valid for power control; falls through to auto-select."""
        protocol = _create_protocol_player(
            mock_mass, "protocol_1", {PlayerFeature.POWER}, available=True
        )
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_make_config_side_effect({CONF_POWER_CONTROL: "protocol_1"})
        )
        mock_mass.players.get_player = MagicMock(return_value=protocol)
        mock_mass.players.get_player_control = MagicMock(return_value=None)
        player = _create_player(mock_mass)
        # protocol players handle their own power at the protocol level
        assert player.power_control == PLAYER_CONTROL_NONE

    def test_explicit_protocol_player_unavailable_falls_through(self, mock_mass: MagicMock) -> None:
        """When explicitly set to unavailable protocol player, falls to auto-select."""
        protocol = _create_protocol_player(
            mock_mass, "protocol_1", {PlayerFeature.POWER}, available=False
        )
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_make_config_side_effect({CONF_POWER_CONTROL: "protocol_1"})
        )
        mock_mass.players.get_player = MagicMock(return_value=protocol)
        mock_mass.players.get_player_control = MagicMock(return_value=None)
        player = _create_player(mock_mass)
        assert player.power_control == PLAYER_CONTROL_NONE

    def test_explicit_player_control_id(self, mock_mass: MagicMock) -> None:
        """Power control returns player control ID when set and control exists."""
        control = MagicMock()
        control.id = "ext_control_1"
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_make_config_side_effect({CONF_POWER_CONTROL: "ext_control_1"})
        )
        mock_mass.players.get_player = MagicMock(return_value=None)
        mock_mass.players.get_player_control = MagicMock(return_value=control)
        player = _create_player(mock_mass)
        assert player.power_control == "ext_control_1"


class TestPowerControlAutoSelect:
    """Test power_control auto-select logic (config is None or 'auto')."""

    def test_auto_prefers_native(self, mock_mass: MagicMock) -> None:
        """Auto-select returns native when player supports POWER feature."""
        player = _create_player(mock_mass, features={PlayerFeature.POWER})
        assert player.power_control == PLAYER_CONTROL_NATIVE

    def test_auto_does_not_use_protocol_player(self, mock_mass: MagicMock) -> None:
        """Auto-select does not use protocol players for power; they handle it themselves."""
        protocol = _create_protocol_player(mock_mass, "proto_power", {PlayerFeature.POWER})
        mock_mass.players.get_player = MagicMock(return_value=protocol)
        player = _create_player(mock_mass)
        _link_protocols(
            player,
            [_make_protocol_link("proto_power")],
            active_protocol_id="proto_power",
        )
        assert player.power_control == PLAYER_CONTROL_NONE

    def test_auto_returns_none_without_active_protocol(self, mock_mass: MagicMock) -> None:
        """Auto-select returns NONE when protocol exists but is not active."""
        protocol = _create_protocol_player(mock_mass, "proto_power", {PlayerFeature.POWER})
        mock_mass.players.get_player = MagicMock(return_value=protocol)
        player = _create_player(mock_mass)
        _link_protocols(player, [_make_protocol_link("proto_power")])
        assert player.power_control == PLAYER_CONTROL_NONE

    def test_auto_returns_none_when_nothing_available(self, mock_mass: MagicMock) -> None:
        """Auto-select returns NONE when no native and no protocol players."""
        player = _create_player(mock_mass)
        assert player.power_control == PLAYER_CONTROL_NONE

    def test_auto_with_config_set_to_auto(self, mock_mass: MagicMock) -> None:
        """Explicit 'auto' config value triggers the same auto-select."""
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_make_config_side_effect({CONF_POWER_CONTROL: "auto"})
        )
        player = _create_player(mock_mass, features={PlayerFeature.POWER})
        assert player.power_control == PLAYER_CONTROL_NATIVE


class TestVolumeControlExplicitConfig:
    """Test volume_control with explicit config values."""

    def test_explicit_native(self, mock_mass: MagicMock) -> None:
        """Volume control returns native when explicitly configured."""
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_make_config_side_effect({CONF_VOLUME_CONTROL: PLAYER_CONTROL_NATIVE})
        )
        player = _create_player(mock_mass)
        assert player.volume_control == PLAYER_CONTROL_NATIVE

    def test_explicit_none(self, mock_mass: MagicMock) -> None:
        """Volume control returns none even with native support."""
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_make_config_side_effect({CONF_VOLUME_CONTROL: PLAYER_CONTROL_NONE})
        )
        player = _create_player(mock_mass, features={PlayerFeature.VOLUME_SET})
        assert player.volume_control == PLAYER_CONTROL_NONE

    def test_explicit_protocol_player_id(self, mock_mass: MagicMock) -> None:
        """Volume control returns protocol player when set and available."""
        protocol = _create_protocol_player(
            mock_mass, "vol_player", {PlayerFeature.VOLUME_SET}, available=True
        )
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_make_config_side_effect({CONF_VOLUME_CONTROL: "vol_player"})
        )
        mock_mass.players.get_player = MagicMock(return_value=protocol)
        player = _create_player(mock_mass)
        assert player.volume_control == "vol_player"


class TestVolumeControlAutoSelect:
    """Test volume_control auto-select logic."""

    def test_auto_prefers_native(self, mock_mass: MagicMock) -> None:
        """Auto-select returns native when player supports VOLUME_SET."""
        player = _create_player(mock_mass, features={PlayerFeature.VOLUME_SET})
        assert player.volume_control == PLAYER_CONTROL_NATIVE

    def test_auto_falls_back_to_protocol_player(self, mock_mass: MagicMock) -> None:
        """Auto-select returns protocol player for volume."""
        protocol = _create_protocol_player(mock_mass, "proto_vol", {PlayerFeature.VOLUME_SET})
        mock_mass.players.get_player = MagicMock(return_value=protocol)
        player = _create_player(mock_mass)
        _link_protocols(player, [_make_protocol_link("proto_vol")])
        assert player.volume_control == "proto_vol"

    def test_auto_returns_none_when_nothing_available(self, mock_mass: MagicMock) -> None:
        """Auto-select returns NONE when nothing supports volume."""
        player = _create_player(mock_mass)
        assert player.volume_control == PLAYER_CONTROL_NONE


class TestMuteControlExplicitConfig:
    """Test mute_control with explicit config values."""

    def test_explicit_native(self, mock_mass: MagicMock) -> None:
        """Mute control returns native when explicitly configured."""
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_make_config_side_effect({CONF_MUTE_CONTROL: PLAYER_CONTROL_NATIVE})
        )
        player = _create_player(mock_mass)
        assert player.mute_control == PLAYER_CONTROL_NATIVE

    def test_explicit_none(self, mock_mass: MagicMock) -> None:
        """Mute control returns none when explicitly configured."""
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_make_config_side_effect({CONF_MUTE_CONTROL: PLAYER_CONTROL_NONE})
        )
        player = _create_player(mock_mass, features={PlayerFeature.VOLUME_MUTE})
        assert player.mute_control == PLAYER_CONTROL_NONE

    def test_explicit_fake(self, mock_mass: MagicMock) -> None:
        """Mute control returns fake when explicitly configured and a volume control exists."""
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_make_config_side_effect({CONF_MUTE_CONTROL: PLAYER_CONTROL_FAKE})
        )
        player = _create_player(mock_mass, features={PlayerFeature.VOLUME_SET})
        assert player.mute_control == PLAYER_CONTROL_FAKE

    def test_explicit_fake_degrades_without_volume_control(self, mock_mass: MagicMock) -> None:
        """Fake mute drives the volume control, so it degrades to NONE without one."""
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_make_config_side_effect(
                {
                    CONF_MUTE_CONTROL: PLAYER_CONTROL_FAKE,
                    CONF_VOLUME_CONTROL: PLAYER_CONTROL_NONE,
                }
            )
        )
        # a native mute capability is irrelevant here: the user explicitly picked fake
        player = _create_player(mock_mass, features={PlayerFeature.VOLUME_MUTE})
        assert player.mute_control == PLAYER_CONTROL_NONE
        assert PlayerFeature.VOLUME_MUTE not in player.state.supported_features


class TestMuteControlAutoSelect:
    """Test mute_control auto-select logic."""

    def test_auto_prefers_native(self, mock_mass: MagicMock) -> None:
        """Auto-select returns native when player supports VOLUME_MUTE."""
        player = _create_player(mock_mass, features={PlayerFeature.VOLUME_MUTE})
        assert player.mute_control == PLAYER_CONTROL_NATIVE

    def test_auto_falls_back_to_protocol_player(self, mock_mass: MagicMock) -> None:
        """Auto-select returns protocol player for mute."""
        protocol = _create_protocol_player(mock_mass, "proto_mute", {PlayerFeature.VOLUME_MUTE})
        mock_mass.players.get_player = MagicMock(return_value=protocol)
        player = _create_player(mock_mass)
        _link_protocols(player, [_make_protocol_link("proto_mute")])
        assert player.mute_control == "proto_mute"

    def test_auto_returns_none_when_nothing_available(self, mock_mass: MagicMock) -> None:
        """Auto-select returns NONE when nothing supports mute."""
        player = _create_player(mock_mass)
        assert player.mute_control == PLAYER_CONTROL_NONE


class TestProtocolPlayerPreferActive:
    """Test that auto-select prefers the active output protocol."""

    def _get_player_lookup(self, players: dict[str, MockPlayer]) -> Any:
        """Create a side_effect that looks up players by ID."""

        def _lookup(pid: str) -> MockPlayer | None:
            return players.get(pid)

        return _lookup

    def test_prefers_active_protocol(self, mock_mass: MagicMock) -> None:
        """Auto-select prefers the active output protocol."""
        proto_a = _create_protocol_player(mock_mass, "proto_a", {PlayerFeature.VOLUME_SET})
        proto_b = _create_protocol_player(mock_mass, "proto_b", {PlayerFeature.VOLUME_SET})
        mock_mass.players.get_player = MagicMock(
            side_effect=self._get_player_lookup({"proto_a": proto_a, "proto_b": proto_b})
        )
        player = _create_player(mock_mass)
        _link_protocols(
            player,
            [
                _make_protocol_link("proto_a", priority=0),
                _make_protocol_link("proto_b", priority=1),
            ],
            active_protocol_id="proto_b",
        )
        assert player.volume_control == "proto_b"

    def test_falls_back_to_any_when_no_active(self, mock_mass: MagicMock) -> None:
        """When no active protocol, falls back to first available."""
        proto_a = _create_protocol_player(mock_mass, "proto_a", {PlayerFeature.VOLUME_SET})
        proto_b = _create_protocol_player(mock_mass, "proto_b", {PlayerFeature.VOLUME_SET})
        mock_mass.players.get_player = MagicMock(
            side_effect=self._get_player_lookup({"proto_a": proto_a, "proto_b": proto_b})
        )
        player = _create_player(mock_mass)
        _link_protocols(
            player,
            [
                _make_protocol_link("proto_a", priority=0),
                _make_protocol_link("proto_b", priority=1),
            ],
        )
        assert player.volume_control == "proto_a"

    def test_power_requires_active_protocol(self, mock_mass: MagicMock) -> None:
        """Power auto-select requires active protocol; returns NONE without one."""
        proto_a = _create_protocol_player(mock_mass, "proto_a", {PlayerFeature.POWER})
        mock_mass.players.get_player = MagicMock(
            side_effect=self._get_player_lookup({"proto_a": proto_a})
        )
        player = _create_player(mock_mass)
        _link_protocols(
            player,
            [_make_protocol_link("proto_a", priority=0)],
        )
        # no active protocol set -> power returns NONE
        assert player.power_control == PLAYER_CONTROL_NONE

    def test_power_ignores_active_protocol(self, mock_mass: MagicMock) -> None:
        """Power auto-select does not use protocol players even when active."""
        proto_a = _create_protocol_player(mock_mass, "proto_a", {PlayerFeature.POWER})
        mock_mass.players.get_player = MagicMock(
            side_effect=self._get_player_lookup({"proto_a": proto_a})
        )
        player = _create_player(mock_mass)
        _link_protocols(
            player,
            [_make_protocol_link("proto_a", priority=0)],
            active_protocol_id="proto_a",
        )
        assert player.power_control == PLAYER_CONTROL_NONE

    def test_volume_skips_unavailable_protocol_player(self, mock_mass: MagicMock) -> None:
        """Volume auto-select skips unavailable protocol players."""
        proto_a = _create_protocol_player(
            mock_mass, "proto_a", {PlayerFeature.VOLUME_SET}, available=False
        )
        proto_b = _create_protocol_player(
            mock_mass, "proto_b", {PlayerFeature.VOLUME_SET}, available=True
        )
        mock_mass.players.get_player = MagicMock(
            side_effect=self._get_player_lookup({"proto_a": proto_a, "proto_b": proto_b})
        )
        player = _create_player(mock_mass)
        _link_protocols(
            player,
            [
                _make_protocol_link("proto_a", priority=0),
                _make_protocol_link("proto_b", priority=1),
            ],
        )
        assert player.volume_control == "proto_b"


class TestPreferredOutputProtocolAutoValue:
    """Test that 'auto' preferred output protocol is treated as no preference."""

    def _get_player_lookup(self, players: dict[str, MockPlayer]) -> Any:
        """Create a side_effect that looks up players by ID."""

        def _lookup(pid: str) -> MockPlayer | None:
            return players.get(pid)

        return _lookup

    def test_preferred_auto_power_ignores_protocol(self, mock_mass: MagicMock) -> None:
        """With preferred='auto', power control does not use protocol players."""
        proto = _create_protocol_player(mock_mass, "proto_a", {PlayerFeature.POWER})
        mock_mass.players.get_player = MagicMock(
            side_effect=self._get_player_lookup({"proto_a": proto})
        )
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_make_config_side_effect({CONF_PREFERRED_OUTPUT_PROTOCOL: "auto"})
        )
        player = _create_player(mock_mass)
        _link_protocols(
            player,
            [_make_protocol_link("proto_a")],
            active_protocol_id="proto_a",
        )
        assert player.power_control == PLAYER_CONTROL_NONE

    def test_preferred_auto_falls_back_for_volume(self, mock_mass: MagicMock) -> None:
        """With preferred='auto', volume control falls back to any linked protocol."""
        proto = _create_protocol_player(mock_mass, "proto_a", {PlayerFeature.VOLUME_SET})
        mock_mass.players.get_player = MagicMock(
            side_effect=self._get_player_lookup({"proto_a": proto})
        )
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_make_config_side_effect({CONF_PREFERRED_OUTPUT_PROTOCOL: "auto"})
        )
        player = _create_player(mock_mass)
        _link_protocols(
            player,
            [_make_protocol_link("proto_a")],
        )
        # volume doesn't require active, so it falls back to linked protocol
        assert player.volume_control == "proto_a"

    def test_preferred_auto_no_active_power_returns_none(self, mock_mass: MagicMock) -> None:
        """With preferred='auto' and no active protocol, power returns NONE."""
        proto = _create_protocol_player(mock_mass, "proto_a", {PlayerFeature.POWER})
        mock_mass.players.get_player = MagicMock(
            side_effect=self._get_player_lookup({"proto_a": proto})
        )
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_make_config_side_effect({CONF_PREFERRED_OUTPUT_PROTOCOL: "auto"})
        )
        player = _create_player(mock_mass)
        _link_protocols(
            player,
            [_make_protocol_link("proto_a")],
        )
        assert player.power_control == PLAYER_CONTROL_NONE


class TestActiveOutputProtocolClearCancellation:
    """Test that setting the active output protocol cancels a pending clear."""

    def test_set_cancels_pending_clear_task(self, mock_mass: MagicMock) -> None:
        """
        Setting a protocol cancels the scheduled clear for the same player.

        This is the mechanism that stops a new session's freshly set protocol
        from being wiped by a clear scheduled after the previous group stop.
        """
        player = _create_player(mock_mass)

        player.set_active_output_protocol("ap_1")

        mock_mass.cancel_task.assert_called_once_with("clear_active_protocol_main_player")


class TestControlForOutput:
    """Test the output-scoped volume/mute control resolution."""

    def _get_player_lookup(self, players: dict[str, MockPlayer]) -> Any:
        """Create a side_effect that looks up players by ID."""

        def _lookup(pid: str) -> MockPlayer | None:
            return players.get(pid)

        return _lookup

    def test_resolves_named_output_without_active_protocol(self, mock_mass: MagicMock) -> None:
        """
        The named output owns the volume even when no protocol is marked active.

        This is the ordering trap the ambient volume_control falls into: with nothing
        active it picks a sibling interface, which made a joining AirPlay speaker
        decide another control owned the volume and play at unity gain.
        """
        proto_a = _create_protocol_player(mock_mass, "proto_a", {PlayerFeature.VOLUME_SET})
        proto_b = _create_protocol_player(mock_mass, "proto_b", {PlayerFeature.VOLUME_SET})
        mock_mass.players.get_player = MagicMock(
            side_effect=self._get_player_lookup({"proto_a": proto_a, "proto_b": proto_b})
        )
        player = _create_player(mock_mass)
        _link_protocols(
            player,
            [
                _make_protocol_link("proto_a", priority=0),
                _make_protocol_link("proto_b", priority=1),
            ],
        )
        # the ambient answer picks the first linked protocol, the output-scoped one
        # answers for the interface that is actually about to render
        assert player.volume_control == "proto_a"
        assert player.volume_control_for_output("proto_b") == "proto_b"

    def test_ignores_active_protocol_pointing_elsewhere(self, mock_mass: MagicMock) -> None:
        """A protocol marked active does not override the named output."""
        proto_a = _create_protocol_player(mock_mass, "proto_a", {PlayerFeature.VOLUME_SET})
        proto_b = _create_protocol_player(mock_mass, "proto_b", {PlayerFeature.VOLUME_SET})
        mock_mass.players.get_player = MagicMock(
            side_effect=self._get_player_lookup({"proto_a": proto_a, "proto_b": proto_b})
        )
        player = _create_player(mock_mass)
        _link_protocols(
            player,
            [
                _make_protocol_link("proto_a", priority=0),
                _make_protocol_link("proto_b", priority=1),
            ],
            active_protocol_id="proto_a",
        )
        assert player.volume_control_for_output("proto_b") == "proto_b"

    def test_returns_none_when_output_lacks_feature(self, mock_mass: MagicMock) -> None:
        """No fallback to a sibling: an output without the feature does not own it."""
        proto_a = _create_protocol_player(mock_mass, "proto_a", {PlayerFeature.VOLUME_SET})
        proto_b = _create_protocol_player(mock_mass, "proto_b", set())
        mock_mass.players.get_player = MagicMock(
            side_effect=self._get_player_lookup({"proto_a": proto_a, "proto_b": proto_b})
        )
        player = _create_player(mock_mass)
        _link_protocols(
            player,
            [
                _make_protocol_link("proto_a", priority=0),
                _make_protocol_link("proto_b", priority=1),
            ],
        )
        assert player.volume_control == "proto_a"
        assert player.volume_control_for_output("proto_b") == PLAYER_CONTROL_NONE

    def test_prefers_native(self, mock_mass: MagicMock) -> None:
        """A player with native volume owns it regardless of the output."""
        proto_a = _create_protocol_player(mock_mass, "proto_a", {PlayerFeature.VOLUME_SET})
        mock_mass.players.get_player = MagicMock(
            side_effect=self._get_player_lookup({"proto_a": proto_a})
        )
        player = _create_player(mock_mass, features={PlayerFeature.VOLUME_SET})
        _link_protocols(player, [_make_protocol_link("proto_a")])
        assert player.volume_control_for_output("proto_a") == PLAYER_CONTROL_NATIVE

    def test_explicit_config_wins(self, mock_mass: MagicMock) -> None:
        """An explicitly configured control is a device-wide statement, so it still wins."""
        proto_a = _create_protocol_player(mock_mass, "proto_a", {PlayerFeature.VOLUME_SET})
        mock_mass.players.get_player = MagicMock(
            side_effect=self._get_player_lookup({"proto_a": proto_a})
        )
        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_make_config_side_effect({CONF_VOLUME_CONTROL: PLAYER_CONTROL_FAKE})
        )
        player = _create_player(mock_mass)
        _link_protocols(player, [_make_protocol_link("proto_a")])
        assert player.volume_control_for_output("proto_a") == PLAYER_CONTROL_FAKE

    def test_unknown_output_returns_none(self, mock_mass: MagicMock) -> None:
        """An output that cannot be resolved owns nothing."""
        player = _create_player(mock_mass)
        assert player.volume_control_for_output("nope") == PLAYER_CONTROL_NONE

    def test_mute_resolves_named_output(self, mock_mass: MagicMock) -> None:
        """Mute resolves against the named output the same way volume does."""
        proto_a = _create_protocol_player(mock_mass, "proto_a", {PlayerFeature.VOLUME_MUTE})
        proto_b = _create_protocol_player(mock_mass, "proto_b", {PlayerFeature.VOLUME_MUTE})
        mock_mass.players.get_player = MagicMock(
            side_effect=self._get_player_lookup({"proto_a": proto_a, "proto_b": proto_b})
        )
        player = _create_player(mock_mass)
        _link_protocols(
            player,
            [
                _make_protocol_link("proto_a", priority=0),
                _make_protocol_link("proto_b", priority=1),
            ],
        )
        assert player.mute_control == "proto_a"
        assert player.mute_control_for_output("proto_b") == "proto_b"

    def test_mute_returns_none_when_output_lacks_feature(self, mock_mass: MagicMock) -> None:
        """An output with volume but no mute does not own the mute."""
        proto_a = _create_protocol_player(mock_mass, "proto_a", {PlayerFeature.VOLUME_SET})
        mock_mass.players.get_player = MagicMock(
            side_effect=self._get_player_lookup({"proto_a": proto_a})
        )
        player = _create_player(mock_mass)
        _link_protocols(player, [_make_protocol_link("proto_a")])
        assert player.mute_control_for_output("proto_a") == PLAYER_CONTROL_NONE
