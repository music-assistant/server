"""
Tests for Player.playback_domains.

Covers the live availability view: native playback, linked protocol players that
are (un)available, and wrapper players that only contribute their linked protocols.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import PlayerFeature, PlayerType

from music_assistant.models.player import LinkedOutputProtocol
from tests.common import MockPlayer, MockProvider


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance."""
    mass = MagicMock()
    mass.closing = False
    mass.config.get = MagicMock(return_value=[])
    mass.config.get_raw_player_config_value = MagicMock(return_value=None)
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    mass.config.set = MagicMock()
    mass.players.get_player = MagicMock(return_value=None)
    return mass


def _make_native_player(mock_mass: MagicMock, domain: str = "sonos") -> MockPlayer:
    """Create a native player (own playback path) of the given provider domain."""
    provider = MockProvider(domain, instance_id=domain, mass=mock_mass)
    player = MockPlayer(provider, "parent", "Parent")
    player._attr_supported_features = {PlayerFeature.PLAY_MEDIA}
    player._cache.clear()
    return player


def _link_protocol(
    mock_mass: MagicMock,
    parent: MockPlayer,
    protocol_id: str,
    domain: str,
    available: bool,
    needs_setup: bool = False,
) -> MockPlayer:
    """Link a protocol player of the given domain to the parent and register it."""
    provider = MockProvider(domain, instance_id=domain, mass=mock_mass)
    protocol_player = MockPlayer(provider, protocol_id, domain, player_type=PlayerType.PROTOCOL)
    protocol_player._attr_available = available
    protocol_player._attr_needs_setup = needs_setup
    protocol_player._cache.clear()
    parent.set_linked_output_protocols(
        [
            *parent.linked_output_protocols,
            LinkedOutputProtocol(
                output_protocol_id=protocol_id,
                protocol_domain=domain,
                priority=10,
            ),
        ]
    )
    registered = {protocol_player.player_id: protocol_player}
    mock_mass.players.get_player = MagicMock(side_effect=lambda pid: registered.get(pid))
    parent._cache.clear()
    return protocol_player


class TestPlaybackDomains:
    """Tests for Player.playback_domains."""

    def test_native_player_reports_own_domain(self, mock_mass: MagicMock) -> None:
        """Test that a native player reports its own provider domain."""
        player = _make_native_player(mock_mass)
        assert player.playback_domains == {"sonos"}

    def test_unavailable_native_player_reports_nothing(self, mock_mass: MagicMock) -> None:
        """Test that an offline native player no longer offers its own domain."""
        player = _make_native_player(mock_mass)
        player._attr_available = False
        player._cache.clear()
        assert player.playback_domains == set()

    def test_native_player_needing_setup_reports_nothing(self, mock_mass: MagicMock) -> None:
        """Test that a player that still needs setup cannot accept a stream."""
        player = _make_native_player(mock_mass)
        player._attr_needs_setup = True
        player._cache.clear()
        assert player.playback_domains == set()

    def test_linked_protocol_adds_its_domain(self, mock_mass: MagicMock) -> None:
        """Test that an available linked protocol adds its domain."""
        player = _make_native_player(mock_mass)
        _link_protocol(mock_mass, player, "ap_1", "airplay", available=True)
        assert player.playback_domains == {"sonos", "airplay"}

    def test_offline_linked_protocol_is_left_out(self, mock_mass: MagicMock) -> None:
        """Test that a linked protocol whose player went offline is not reported."""
        player = _make_native_player(mock_mass)
        _link_protocol(mock_mass, player, "ap_1", "airplay", available=False)
        assert player.playback_domains == {"sonos"}

    def test_linked_protocol_needing_setup_is_left_out(self, mock_mass: MagicMock) -> None:
        """Test that an unpaired protocol receiver cannot accept a stream."""
        player = _make_native_player(mock_mass)
        _link_protocol(mock_mass, player, "ap_1", "airplay", available=True, needs_setup=True)
        assert player.playback_domains == {"sonos"}

    def test_unregistered_linked_protocol_is_left_out(self, mock_mass: MagicMock) -> None:
        """Test that a linked protocol without a live player is not reported."""
        player = _make_native_player(mock_mass)
        _link_protocol(mock_mass, player, "ap_1", "airplay", available=True)
        mock_mass.players.get_player = MagicMock(return_value=None)
        player._cache.clear()
        assert player.playback_domains == {"sonos"}

    def test_wrapper_player_only_reports_linked_protocols(self, mock_mass: MagicMock) -> None:
        """Test that a UniversalPlayer never contributes its own domain."""
        provider = MockProvider("universal_player", instance_id="universal_player", mass=mock_mass)
        player = MockPlayer(provider, "up_1", "Universal")
        player._attr_supported_features = {PlayerFeature.PLAY_MEDIA}
        player._cache.clear()
        _link_protocol(mock_mass, player, "ap_1", "airplay", available=True)
        assert player.playback_domains == {"airplay"}

    def test_protocol_endpoint_reports_own_domain(self, mock_mass: MagicMock) -> None:
        """Test that a player that is itself a protocol endpoint reports its domain."""
        provider = MockProvider("airplay", instance_id="airplay", mass=mock_mass)
        player = MockPlayer(provider, "ap_1", "AirPlay", player_type=PlayerType.PROTOCOL)
        player._attr_supported_features = {PlayerFeature.SET_MEMBERS}
        player._cache.clear()
        assert player.playback_domains == {"airplay"}

        player._attr_available = False
        player._cache.clear()
        assert player.playback_domains == set()

    def test_recomputed_after_state_update(self, mock_mass: MagicMock) -> None:
        """Test that the cached value follows the protocol player going offline."""
        player = _make_native_player(mock_mass)
        protocol_player = _link_protocol(mock_mass, player, "ap_1", "airplay", available=True)
        assert player.playback_domains == {"sonos", "airplay"}

        protocol_player._attr_available = False
        player.update_state(signal_event=False)
        assert player.playback_domains == {"sonos"}
