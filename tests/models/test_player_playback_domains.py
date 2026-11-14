"""
Tests for Player.playback_domains and Player.get_linked_protocol.

Covers the live availability view: native playback, audio protocols that are
(un)available or detached, and players that only have an audio protocol.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import PlayerFeature, ProtocolRole

from music_assistant.models.player import Player
from music_assistant.models.player_protocol import PlayerProtocol
from tests.common import MockProtocol, MockProvider, attach_mock, mock_player_mass


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance."""
    mass = mock_player_mass()
    mass.config.get_player_value = MagicMock(return_value=None)
    return mass


def _make_native_player(mock_mass: MagicMock, domain: str = "sonos") -> Player:
    """Create a player with a native protocol (own playback path) of the given provider domain."""
    provider = MockProvider(domain, instance_id=domain, mass=mock_mass)
    player, protocol = attach_mock(provider, "parent", "Parent")
    protocol._attr_supported_features = {PlayerFeature.PLAY_MEDIA}
    return player


def _own(player: Player) -> PlayerProtocol:
    """Return the native protocol of a player."""
    assert player.own_protocol is not None
    return player.own_protocol


def _link_protocol(
    mock_mass: MagicMock,
    parent: Player,
    protocol_id: str,
    domain: str,
    available: bool,
    needs_setup: bool = False,
) -> MockProtocol:
    """Attach an audio protocol of the given domain to the parent."""
    provider = MockProvider(domain, instance_id=domain, mass=mock_mass)
    _, protocol = attach_mock(provider, protocol_id, domain, role=ProtocolRole.AUDIO, player=parent)
    protocol._attr_available = available
    protocol._attr_needs_setup = needs_setup
    parent._cache.clear()
    return protocol


def _detach(mock_mass: MagicMock, player: Player, protocol_id: str, domain: str) -> None:
    """Detach a protocol from a player, leaving the stored record of it behind."""
    player.remove_protocol(protocol_id)
    mock_mass.config.get_protocol_record = MagicMock(
        return_value={"provider": domain, "role": ProtocolRole.AUDIO.value}
    )
    player._cache.clear()


class TestPlaybackDomains:
    """Tests for Player.playback_domains."""

    def test_native_player_reports_own_domain(self, mock_mass: MagicMock) -> None:
        """Test that a native player reports its own provider domain."""
        player = _make_native_player(mock_mass)
        assert player.playback_domains == {"sonos"}

    def test_unavailable_native_player_reports_nothing(self, mock_mass: MagicMock) -> None:
        """Test that an offline native player no longer offers its own domain."""
        player = _make_native_player(mock_mass)
        _own(player)._attr_available = False
        player._cache.clear()
        assert player.playback_domains == set()

    def test_native_player_needing_setup_reports_nothing(self, mock_mass: MagicMock) -> None:
        """Test that a player that still needs setup cannot accept a stream."""
        player = _make_native_player(mock_mass)
        _own(player)._attr_needs_setup = True
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

    def test_detached_protocol_is_left_out(self, mock_mass: MagicMock) -> None:
        """Test that a protocol of the player that is not attached is not reported."""
        player = _make_native_player(mock_mass)
        _link_protocol(mock_mass, player, "ap_1", "airplay", available=True)
        _detach(mock_mass, player, "ap_1", "airplay")
        assert player.playback_domains == {"sonos"}

    def test_player_without_a_native_protocol_reports_its_audio_protocol(
        self, mock_mass: MagicMock
    ) -> None:
        """Test that a player that only has an audio protocol reports that protocol's domain."""
        provider = MockProvider("airplay", instance_id="airplay", mass=mock_mass)
        player, protocol = attach_mock(provider, "ap_1", "AirPlay", role=ProtocolRole.AUDIO)
        protocol._attr_supported_features = {PlayerFeature.PLAY_MEDIA}
        player._cache.clear()
        assert player.playback_domains == {"airplay"}

        protocol._attr_available = False
        player._cache.clear()
        assert player.playback_domains == set()

    def test_recomputed_after_state_update(self, mock_mass: MagicMock) -> None:
        """Test that the cached value follows the protocol player going offline."""
        player = _make_native_player(mock_mass)
        protocol = _link_protocol(mock_mass, player, "ap_1", "airplay", available=True)
        assert player.playback_domains == {"sonos", "airplay"}

        protocol._attr_available = False
        player.update_state(signal_event=False)
        assert player.playback_domains == {"sonos"}


class TestGetLinkedProtocol:
    """Tests for Player.get_linked_protocol."""

    def test_resolves_name_and_availability_from_live_player(self, mock_mass: MagicMock) -> None:
        """Test that the returned entry describes the protocol player as it is right now."""
        player = _make_native_player(mock_mass)
        _link_protocol(mock_mass, player, "ap_1", "airplay", available=True)

        output_protocol = player.get_linked_protocol("ap_1")
        assert output_protocol is not None
        assert output_protocol.name == "Mock Airplay"
        assert output_protocol.available is True
        assert output_protocol.protocol_domain == "airplay"
        assert output_protocol.priority == 10

    def test_reports_offline_protocol_player(self, mock_mass: MagicMock) -> None:
        """Test that an unchanged link stops claiming availability once its player drops."""
        player = _make_native_player(mock_mass)
        protocol = _link_protocol(mock_mass, player, "ap_1", "airplay", available=True)

        protocol._attr_available = False
        output_protocol = player.get_linked_protocol("ap_1")
        assert output_protocol is not None
        assert output_protocol.available is False

    def test_detached_protocol_stays_listed_by_its_domain(self, mock_mass: MagicMock) -> None:
        """Test that a protocol that is not attached is no output to pick, but stays listed."""
        player = _make_native_player(mock_mass)
        _link_protocol(mock_mass, player, "ap_1", "airplay", available=True)
        _detach(mock_mass, player, "ap_1", "airplay")

        assert player.get_linked_protocol("ap_1") is None
        listed = next(o for o in player._output_protocols if o.output_protocol_id == "ap_1")
        assert listed.name == "Airplay"
        assert listed.available is False

    def test_unknown_protocol_id_returns_none(self, mock_mass: MagicMock) -> None:
        """Test that an id that is not linked resolves to nothing."""
        player = _make_native_player(mock_mass)
        _link_protocol(mock_mass, player, "ap_1", "airplay", available=True)
        assert player.get_linked_protocol("cast_1") is None
