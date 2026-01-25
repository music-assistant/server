"""Tests for protocol player linking and universal player creation."""

import logging
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import IdentifierType, PlayerFeature, PlayerType

from music_assistant.controllers.players import PlayerController
from music_assistant.helpers.throttle_retry import Throttler
from music_assistant.models.player import DeviceInfo, Player


def create_mock_config(name: str) -> MagicMock:
    """Create a mock player config with the given name."""
    config = MagicMock()
    config.name = None  # No custom name, use default
    config.default_name = name
    return config


class MockProvider:
    """Mock player provider for testing."""

    def __init__(
        self, domain: str, instance_id: str = "test_instance", mass: MagicMock | None = None
    ) -> None:
        """Initialize the mock provider."""
        self.domain = domain
        self.instance_id = instance_id
        self.manifest = MagicMock()
        self.manifest.name = f"Mock {domain} Provider"
        self.mass = mass or MagicMock()
        self.logger = logging.getLogger(f"test.{domain}")


class MockPlayer(Player):
    """Mock player for testing."""

    def __init__(
        self,
        provider: MockProvider,
        player_id: str,
        name: str,
        player_type: PlayerType = PlayerType.PLAYER,
        identifiers: dict[IdentifierType, str] | None = None,
    ) -> None:
        """Initialize the mock player."""
        # Set up the mock config before calling super().__init__
        # because the parent __init__ accesses config
        provider.mass.config.get_base_player_config.return_value = create_mock_config(name)

        super().__init__(provider, player_id)
        self._attr_name = name
        # Set type as instance attribute (overrides class attribute)
        self._attr_type = player_type
        self._attr_available = True
        self._attr_powered = True
        self._attr_supported_features = {PlayerFeature.VOLUME_SET}

        # Set up device info with identifiers
        self._attr_device_info = DeviceInfo(
            model="Test Model",
            manufacturer="Test Manufacturer",
        )
        if identifiers:
            for conn_type, value in identifiers.items():
                self._attr_device_info.add_identifier(conn_type, value)

    async def stop(self) -> None:
        """Stop playback - required abstract method."""


@pytest.fixture
def mock_mass():
    """Create a mock MusicAssistant instance."""
    mass = MagicMock()
    mass.closing = False
    mass.config = MagicMock()
    mass.config.get = MagicMock(return_value=[])
    mass.config.get_raw_player_config_value = MagicMock(return_value="auto")
    # Return "GLOBAL" for log level config (standard default)
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    mass.config.set = MagicMock()
    mass.signal_event = MagicMock()
    mass.get_providers = MagicMock(return_value=[])
    return mass


class TestIdentifiersMatch:
    """Tests for identifier matching logic."""

    def test_mac_address_match(self, mock_mass):
        """Test that MAC addresses match correctly."""
        controller = PlayerController(mock_mass)

        provider = MockProvider("test")
        player_a = MockPlayer(
            provider,
            "player_a",
            "Player A",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        player_b = MockPlayer(
            provider,
            "player_b",
            "Player B",
            identifiers={IdentifierType.MAC_ADDRESS: "aa:bb:cc:dd:ee:ff"},  # lowercase
        )

        assert controller._identifiers_match(player_a, player_b) is True

    def test_mac_address_no_match(self, mock_mass):
        """Test that different MAC addresses don't match."""
        controller = PlayerController(mock_mass)

        provider = MockProvider("test")
        player_a = MockPlayer(
            provider,
            "player_a",
            "Player A",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        player_b = MockPlayer(
            provider,
            "player_b",
            "Player B",
            identifiers={IdentifierType.MAC_ADDRESS: "11:22:33:44:55:66"},
        )

        assert controller._identifiers_match(player_a, player_b) is False

    def test_ip_address_no_match(self, mock_mass):
        """Test that IP addresses don't match (IP is excluded as it's not stable)."""
        controller = PlayerController(mock_mass)

        provider = MockProvider("test")
        player_a = MockPlayer(
            provider,
            "player_a",
            "Player A",
            identifiers={IdentifierType.IP_ADDRESS: "192.168.1.100"},
        )
        player_b = MockPlayer(
            provider,
            "player_b",
            "Player B",
            identifiers={IdentifierType.IP_ADDRESS: "192.168.1.100"},
        )

        # IP address matching is intentionally disabled to prevent false matches
        assert controller._identifiers_match(player_a, player_b) is False

    def test_sonos_uuid_dlna_suffix_match(self, mock_mass):
        """Test Sonos UUID matching with DLNA _MR suffix."""
        controller = PlayerController(mock_mass)

        provider = MockProvider("test")
        # Sonos native player
        player_a = MockPlayer(
            provider,
            "player_a",
            "Sonos Player",
            identifiers={IdentifierType.UUID: "RINCON_000E58123456"},
        )
        # DLNA player with _MR suffix
        player_b = MockPlayer(
            provider,
            "player_b",
            "DLNA Player",
            identifiers={IdentifierType.UUID: "RINCON_000E58123456_MR"},
        )

        assert controller._identifiers_match(player_a, player_b) is True

    def test_no_identifiers_no_match(self, mock_mass):
        """Test that players without identifiers don't match."""
        controller = PlayerController(mock_mass)

        provider = MockProvider("test")
        player_a = MockPlayer(provider, "player_a", "Player A")
        player_b = MockPlayer(provider, "player_b", "Player B")

        assert controller._identifiers_match(player_a, player_b) is False


class TestProtocolPlayerDetection:
    """Tests for protocol player type detection."""

    def test_is_protocol_player_true(self, mock_mass):
        """Test that PlayerType.PROTOCOL is correctly detected."""
        controller = PlayerController(mock_mass)

        provider = MockProvider("airplay")
        player = MockPlayer(
            provider,
            "ap_123456",
            "Samsung TV (AirPlay)",
            player_type=PlayerType.PROTOCOL,
        )

        assert controller._is_protocol_player(player) is True

    def test_is_protocol_player_false(self, mock_mass):
        """Test that PlayerType.PLAYER is not detected as protocol."""
        controller = PlayerController(mock_mass)

        provider = MockProvider("airplay")
        player = MockPlayer(
            provider,
            "ap_123456",
            "HomePod",
            player_type=PlayerType.PLAYER,  # Apple device with native support
        )

        assert controller._is_protocol_player(player) is False


class TestFindMatchingProtocolPlayers:
    """Tests for finding matching protocol players."""

    def test_find_matching_by_mac(self, mock_mass):
        """Test finding matching protocol players by MAC address."""
        controller = PlayerController(mock_mass)

        # Set up providers
        airplay_provider = MockProvider("airplay")
        chromecast_provider = MockProvider("chromecast")

        # Create matching protocol players (same device, different protocols)
        airplay_player = MockPlayer(
            airplay_provider,
            "ap_aabbccddee",
            "Samsung TV (AirPlay)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        chromecast_player = MockPlayer(
            chromecast_provider,
            "cc_aabbccddee",
            "Samsung TV (Chromecast)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )

        # Register players
        controller._players = {
            "ap_aabbccddee": airplay_player,
            "cc_aabbccddee": chromecast_player,
        }
        controller._player_throttlers = {
            "ap_aabbccddee": Throttler(1, 0.05),
            "cc_aabbccddee": Throttler(1, 0.05),
        }

        # Find matching players for AirPlay player
        matches = controller._find_matching_protocol_players(airplay_player)

        assert len(matches) == 2
        assert airplay_player in matches
        assert chromecast_player in matches


class TestGetDeviceKeyFromPlayers:
    """Tests for device key generation."""

    def test_device_key_from_mac(self, mock_mass):
        """Test device key generation from MAC address."""
        controller = PlayerController(mock_mass)

        provider = MockProvider("airplay")
        player = MockPlayer(
            provider,
            "ap_123456",
            "Test Player",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )

        device_key = controller._get_device_key_from_players([player])

        assert device_key == "aabbccddeeff"

    def test_device_key_from_uuid_fallback(self, mock_mass):
        """Test device key generation falls back to UUID when no MAC available."""
        controller = PlayerController(mock_mass)

        provider = MockProvider("dlna")
        player = MockPlayer(
            provider,
            "dlna_123456",
            "Test Player",
            identifiers={IdentifierType.UUID: "uuid:12345678-1234-1234-1234-123456789abc"},
        )

        device_key = controller._get_device_key_from_players([player])

        assert device_key == "uuid12345678123412341234123456789abc"

    def test_device_key_from_ip_returns_none(self, mock_mass):
        """Test that device key returns None for IP-only players (IP not used)."""
        controller = PlayerController(mock_mass)

        provider = MockProvider("airplay")
        player = MockPlayer(
            provider,
            "ap_123456",
            "Test Player",
            identifiers={IdentifierType.IP_ADDRESS: "192.168.1.100"},
        )

        device_key = controller._get_device_key_from_players([player])

        # IP address is not used for device key generation
        assert device_key is None


class TestGetCleanPlayerName:
    """Tests for player name selection."""

    def test_prefers_chromecast_name(self, mock_mass):
        """Test that Chromecast names are preferred over other protocols."""
        controller = PlayerController(mock_mass)

        airplay_provider = MockProvider("airplay")
        chromecast_provider = MockProvider("chromecast")

        airplay_player = MockPlayer(
            airplay_provider,
            "ap_123456",
            "Samsung TV",
            player_type=PlayerType.PROTOCOL,
        )
        chromecast_player = MockPlayer(
            chromecast_provider,
            "cc_123456",
            "Living Room Speaker",
            player_type=PlayerType.PROTOCOL,
        )

        # Chromecast should be preferred (priority 1)
        clean_name = controller._get_clean_player_name([airplay_player, chromecast_player])
        assert clean_name == "Living Room Speaker"

    def test_filters_mac_address_names(self, mock_mass):
        """Test that MAC address-like names are filtered out."""
        controller = PlayerController(mock_mass)

        squeezelite_provider = MockProvider("squeezelite")
        airplay_provider = MockProvider("airplay")

        # Squeezelite with MAC address as name
        sq_player = MockPlayer(
            squeezelite_provider,
            "sq_123456",
            "AA:BB:CC:DD:EE:FF",
            player_type=PlayerType.PROTOCOL,
        )
        # AirPlay with proper name
        ap_player = MockPlayer(
            airplay_provider,
            "ap_123456",
            "Kitchen Speaker",
            player_type=PlayerType.PROTOCOL,
        )

        # Should prefer Kitchen Speaker over MAC address
        clean_name = controller._get_clean_player_name([sq_player, ap_player])
        assert clean_name == "Kitchen Speaker"

    def test_filters_player_id_names(self, mock_mass):
        """Test that player ID-like names are filtered out."""
        controller = PlayerController(mock_mass)

        sendspin_provider = MockProvider("sendspin")
        dlna_provider = MockProvider("dlna")

        # SendSpin with player ID as name
        ss_player = MockPlayer(
            sendspin_provider,
            "sendspin_123456",
            "sendspin_device_abc",
            player_type=PlayerType.PROTOCOL,
        )
        # DLNA with proper name
        dlna_player = MockPlayer(
            dlna_provider,
            "dlna_123456",
            "Bedroom TV",
            player_type=PlayerType.PROTOCOL,
        )

        # Should prefer Bedroom TV over player ID
        clean_name = controller._get_clean_player_name([ss_player, dlna_player])
        assert clean_name == "Bedroom TV"

    def test_valid_name_unchanged(self, mock_mass):
        """Test that valid names are returned unchanged."""
        controller = PlayerController(mock_mass)

        provider = MockProvider("airplay")
        player = MockPlayer(
            provider,
            "ap_123456",
            "HomePod Mini",
            player_type=PlayerType.PLAYER,
        )

        clean_name = controller._get_clean_player_name([player])
        assert clean_name == "HomePod Mini"
