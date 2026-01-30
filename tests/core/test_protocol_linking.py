"""Tests for protocol player linking and universal player creation."""

import logging
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import IdentifierType, PlayerFeature, PlayerType
from music_assistant_models.player import OutputProtocol

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
        self.name = f"Mock {domain.title()}"
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

    def test_device_key_from_ip_falls_back_to_player_id(self, mock_mass):
        """Test that device key falls back to player_id for IP-only players (IP not used)."""
        controller = PlayerController(mock_mass)

        provider = MockProvider("airplay")
        player = MockPlayer(
            provider,
            "ap_123456",
            "Test Player",
            identifiers={IdentifierType.IP_ADDRESS: "192.168.1.100"},
        )

        device_key = controller._get_device_key_from_players([player])

        # IP address is not used for device key - falls back to player_id
        # This allows protocol players without MAC/UUID to still get a UniversalPlayer
        assert device_key == "ap_123456"

    def test_device_key_from_no_identifiers_falls_back_to_player_id(self, mock_mass):
        """Test that device key falls back to player_id when no identifiers at all."""
        controller = PlayerController(mock_mass)

        provider = MockProvider("sendspin")
        player = MockPlayer(
            provider,
            "sendspin-device-abc",
            "Test Player",
            # No identifiers at all (like Sendspin protocol players)
        )

        device_key = controller._get_device_key_from_players([player])

        # Falls back to player_id when no MAC/UUID identifiers
        assert device_key == "sendspindeviceabc"


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


class TestCachedProtocolParentRestore:
    """Tests for restoring cached protocol parent links."""

    def test_protocol_parent_id_restored_from_config(self, mock_mass):
        """Test that cached protocol_parent_id is loaded and used for immediate linking."""
        controller = PlayerController(mock_mass)

        # Mock config to return cached parent_id when queried
        def mock_config_get(key, default=None):
            if "protocol_parent_id" in str(key):
                return "native_player_id"
            return default

        mock_mass.config.get.side_effect = mock_config_get

        # Create native player
        native_provider = MockProvider("sonos", mass=mock_mass)
        native_player = MockPlayer(
            native_provider,
            "native_player_id",
            "Sonos Speaker",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )

        # Create protocol player
        dlna_provider = MockProvider("dlna", mass=mock_mass)
        protocol_player = MockPlayer(
            dlna_provider,
            "uuid:RINCON_AABBCCDDEEFF_MR",
            "Sonos DLNA",
            player_type=PlayerType.PROTOCOL,
        )

        # Register native player
        controller._players = {"native_player_id": native_player}
        controller._player_throttlers = {"native_player_id": Throttler(1, 0.05)}

        # Try to link protocol to native - should load cached parent_id
        controller._try_link_protocol_to_native(protocol_player)

        # Verify protocol_parent_id was set
        assert protocol_player._attr_protocol_parent_id == "native_player_id"

        # Verify protocol was linked to native player
        assert any(
            link.output_protocol_id == protocol_player.player_id
            for link in native_player._attr_linked_protocols
        )

    def test_protocol_parent_id_prevents_universal_player_creation(self, mock_mass):
        """Test that cached protocol_parent_id prevents creating universal player."""
        controller = PlayerController(mock_mass)

        # Mock config to return cached parent_id (parent not yet registered)
        def mock_config_get(key, default=None):
            if "protocol_parent_id" in str(key):
                return "native_player_id"
            return default

        mock_mass.config.get.side_effect = mock_config_get

        # Create protocol player
        dlna_provider = MockProvider("dlna", mass=mock_mass)
        protocol_player = MockPlayer(
            dlna_provider,
            "uuid:RINCON_AABBCCDDEEFF_MR",
            "Sonos DLNA",
            player_type=PlayerType.PROTOCOL,
        )

        # No native player registered yet
        controller._players = {}

        # Try to link protocol - should set parent_id and skip evaluation
        controller._try_link_protocol_to_native(protocol_player)

        # Verify protocol_parent_id was set
        assert protocol_player._attr_protocol_parent_id == "native_player_id"

        # Since parent_id is set, delayed evaluation won't create a universal player


class TestSelectBestOutputProtocol:
    """Tests for output protocol selection logic."""

    def test_select_native_when_preferred_is_native(self, mock_mass):
        """Test that native protocol is selected when user prefers native."""
        # Mock config to return "native" as preferred
        mock_mass.config.get_raw_player_config_value = MagicMock(return_value="native")

        controller = PlayerController(mock_mass)
        provider = MockProvider("sonos", mass=mock_mass)

        # Create native player with PLAY_MEDIA support
        native_player = MockPlayer(
            provider,
            "sonos_123",
            "Kantoor",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        native_player._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)

        # Register players
        controller._players = {"sonos_123": native_player}
        controller._player_throttlers = {"sonos_123": Throttler(1, 0.05)}

        # Select protocol
        selected_player, protocol_id = controller._select_best_output_protocol(native_player)

        # Should select native player
        assert selected_player == native_player
        assert protocol_id == "native"

    def test_select_dlna_when_preferred_is_dlna(self, mock_mass):
        """Test that DLNA protocol is selected when user prefers DLNA."""
        # Mock config to return the full player ID as preferred
        mock_mass.config.get_raw_player_config_value = MagicMock(return_value="dlna_AABBCCDDEEFF")

        controller = PlayerController(mock_mass)

        # Create native player with linked protocols
        sonos_provider = MockProvider("sonos", mass=mock_mass)
        native_player = MockPlayer(
            sonos_provider,
            "sonos_123",
            "Kantoor",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        native_player._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)

        # Create DLNA protocol player
        dlna_provider = MockProvider("dlna", mass=mock_mass)
        dlna_player = MockPlayer(
            dlna_provider,
            "dlna_AABBCCDDEEFF",
            "Kantoor DLNA",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )

        # Register players
        controller._players = {
            "sonos_123": native_player,
            "dlna_AABBCCDDEEFF": dlna_player,
        }
        controller._player_throttlers = {
            "sonos_123": Throttler(1, 0.05),
            "dlna_AABBCCDDEEFF": Throttler(1, 0.05),
        }

        # Link DLNA protocol to native player
        native_player._attr_linked_protocols.append(
            OutputProtocol(
                output_protocol_id="dlna_AABBCCDDEEFF",
                name="DLNA",
                protocol_domain="dlna",
                priority=30,
            )
        )

        # Select protocol
        selected_player, protocol_id = controller._select_best_output_protocol(native_player)

        # Should select DLNA player, not native
        assert selected_player == dlna_player
        assert protocol_id == "dlna_AABBCCDDEEFF"

    def test_select_airplay_when_preferred_is_airplay(self, mock_mass):
        """Test that AirPlay protocol is selected when user prefers AirPlay."""
        # Mock config to return the full player ID as preferred
        mock_mass.config.get_raw_player_config_value = MagicMock(
            return_value="airplay_AABBCCDDEEFF"
        )

        controller = PlayerController(mock_mass)

        # Create native player
        sonos_provider = MockProvider("sonos", mass=mock_mass)
        native_player = MockPlayer(
            sonos_provider,
            "sonos_123",
            "Kantoor",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        native_player._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)

        # Create AirPlay and DLNA protocol players
        airplay_provider = MockProvider("airplay", mass=mock_mass)
        airplay_player = MockPlayer(
            airplay_provider,
            "airplay_AABBCCDDEEFF",
            "Kantoor AirPlay",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )

        dlna_provider = MockProvider("dlna", mass=mock_mass)
        dlna_player = MockPlayer(
            dlna_provider,
            "dlna_AABBCCDDEEFF",
            "Kantoor DLNA",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )

        # Register players
        controller._players = {
            "sonos_123": native_player,
            "airplay_AABBCCDDEEFF": airplay_player,
            "dlna_AABBCCDDEEFF": dlna_player,
        }
        controller._player_throttlers = {
            "sonos_123": Throttler(1, 0.05),
            "airplay_AABBCCDDEEFF": Throttler(1, 0.05),
            "dlna_AABBCCDDEEFF": Throttler(1, 0.05),
        }

        # Link protocols to native player
        native_player._attr_linked_protocols.extend(
            [
                OutputProtocol(
                    output_protocol_id="airplay_AABBCCDDEEFF",
                    name="AirPlay",
                    protocol_domain="airplay",
                    priority=10,
                ),
                OutputProtocol(
                    output_protocol_id="dlna_AABBCCDDEEFF",
                    name="DLNA",
                    protocol_domain="dlna",
                    priority=30,
                ),
            ]
        )

        # Select protocol
        selected_player, protocol_id = controller._select_best_output_protocol(native_player)

        # Should select AirPlay player (even though DLNA has lower priority value),
        # because user preference overrides priority
        assert selected_player == airplay_player
        assert protocol_id == "airplay_AABBCCDDEEFF"

    def test_fallback_to_native_when_auto(self, mock_mass):
        """Test that native playback is used when preference is auto."""
        # Mock config to return "auto" as preferred
        mock_mass.config.get_raw_player_config_value = MagicMock(return_value="auto")

        controller = PlayerController(mock_mass)
        provider = MockProvider("sonos", mass=mock_mass)

        native_player = MockPlayer(
            provider,
            "sonos_123",
            "Kantoor",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        native_player._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)

        controller._players = {"sonos_123": native_player}
        controller._player_throttlers = {"sonos_123": Throttler(1, 0.05)}

        # Select protocol with auto preference
        selected_player, protocol_id = controller._select_best_output_protocol(native_player)

        # Should select native player
        assert selected_player == native_player
        assert protocol_id == "native"
