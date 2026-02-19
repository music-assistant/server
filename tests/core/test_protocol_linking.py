"""Tests for protocol player linking and universal player creation."""

import logging
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import (
    IdentifierType,
    PlayerFeature,
    PlayerType,
)
from music_assistant_models.player import OutputProtocol

from music_assistant.controllers.players import PlayerController
from music_assistant.helpers.throttle_retry import Throttler
from music_assistant.models.player import DeviceInfo, Player
from music_assistant.providers.universal_player.provider import UniversalPlayerProvider


def create_mock_config(name: str) -> MagicMock:
    """Create a mock player config with the given name."""
    config = MagicMock()
    config.name = None  # No custom name, use default
    config.default_name = name
    return config


def create_mock_universal_provider(mock_mass: MagicMock) -> UniversalPlayerProvider:
    """Create a mock UniversalPlayerProvider for testing."""
    # Create a mock manifest
    manifest = MagicMock()
    manifest.domain = "universal_player"
    manifest.name = "Universal Player"

    # Create provider with the mock manifest
    provider = UniversalPlayerProvider.__new__(UniversalPlayerProvider)
    provider.mass = mock_mass
    provider.manifest = manifest
    provider.logger = logging.getLogger("test.universal_player")
    return provider


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

        super().__init__(provider, player_id)  # type: ignore[arg-type]
        self._attr_name = name
        # Set type as instance attribute (overrides class attribute)
        self._attr_type = player_type
        self._attr_available = True
        self._attr_powered = True
        self._attr_supported_features = {PlayerFeature.VOLUME_SET}
        self._attr_can_group_with = set()

        # Set up device info with identifiers
        self._attr_device_info = DeviceInfo(
            model="Test Model",
            manufacturer="Test Manufacturer",
        )
        if identifiers:
            for conn_type, value in identifiers.items():
                self._attr_device_info.add_identifier(conn_type, value)

        # Clear cached properties after modifying attributes
        self._cache.clear()

        # Update state to reflect the modified attributes
        self.update_state(signal_event=False)

    async def stop(self) -> None:
        """Stop playback - required abstract method."""


@pytest.fixture
def mock_mass() -> MagicMock:
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

    def test_mac_address_match(self, mock_mass: MagicMock) -> None:
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

    def test_mac_address_no_match(self, mock_mass: MagicMock) -> None:
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

    def test_ip_address_no_match(self, mock_mass: MagicMock) -> None:
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

    def test_sonos_uuid_dlna_suffix_match(self, mock_mass: MagicMock) -> None:
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

    def test_no_identifiers_no_match(self, mock_mass: MagicMock) -> None:
        """Test that players without identifiers don't match."""
        controller = PlayerController(mock_mass)

        provider = MockProvider("test")
        player_a = MockPlayer(provider, "player_a", "Player A")
        player_b = MockPlayer(provider, "player_b", "Player B")

        assert controller._identifiers_match(player_a, player_b) is False


class TestProtocolPlayerDetection:
    """Tests for protocol player type detection."""

    def test_is_protocol_player_true(self, mock_mass: MagicMock) -> None:
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

    def test_is_protocol_player_false(self, mock_mass: MagicMock) -> None:
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

    def test_find_matching_by_mac(self, mock_mass: MagicMock) -> None:
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

    def test_same_protocol_not_matched(self, mock_mass: MagicMock) -> None:
        """Test that multiple players of same protocol on same host are NOT matched together."""
        controller = PlayerController(mock_mass)

        # Set up provider
        snapcast_provider = MockProvider("snapcast")

        # Create multiple Snapcast players on same host (same MAC/IP)
        # This simulates multiple Snapcast clients running on the same server
        snapcast_player_1 = MockPlayer(
            snapcast_provider,
            "snapcast_client_1",
            "Snapcast Client 1",
            player_type=PlayerType.PROTOCOL,
            identifiers={
                IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF",
                IdentifierType.IP_ADDRESS: "192.168.1.100",
            },
        )
        snapcast_player_2 = MockPlayer(
            snapcast_provider,
            "snapcast_client_2",
            "Snapcast Client 2",
            player_type=PlayerType.PROTOCOL,
            identifiers={
                IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF",
                IdentifierType.IP_ADDRESS: "192.168.1.100",
            },
        )

        # Register players
        controller._players = {
            "snapcast_client_1": snapcast_player_1,
            "snapcast_client_2": snapcast_player_2,
        }
        controller._player_throttlers = {
            "snapcast_client_1": Throttler(1, 0.05),
            "snapcast_client_2": Throttler(1, 0.05),
        }

        # Find matching players for first Snapcast player
        matches = controller._find_matching_protocol_players(snapcast_player_1)

        # Should only match itself, NOT the other Snapcast player (same protocol domain)
        assert len(matches) == 1
        assert snapcast_player_1 in matches
        assert snapcast_player_2 not in matches


class TestGetDeviceKeyFromPlayers:
    """Tests for device key generation."""

    def test_device_key_from_mac(self, mock_mass: MagicMock) -> None:
        """Test device key generation from MAC address."""
        universal_provider = create_mock_universal_provider(mock_mass)

        provider = MockProvider("airplay")
        player = MockPlayer(
            provider,
            "ap_123456",
            "Test Player",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )

        device_key = universal_provider._get_device_key_from_players([player])

        assert device_key == "aabbccddeeff"

    def test_device_key_from_uuid_fallback(self, mock_mass: MagicMock) -> None:
        """Test device key generation falls back to UUID when no MAC available."""
        universal_provider = create_mock_universal_provider(mock_mass)

        provider = MockProvider("dlna")
        player = MockPlayer(
            provider,
            "dlna_123456",
            "Test Player",
            identifiers={IdentifierType.UUID: "uuid:12345678-1234-1234-1234-123456789abc"},
        )

        device_key = universal_provider._get_device_key_from_players([player])

        assert device_key == "uuid12345678123412341234123456789abc"

    def test_device_key_from_ip_falls_back_to_player_id(self, mock_mass: MagicMock) -> None:
        """Test that device key falls back to player_id for IP-only players (IP not used)."""
        universal_provider = create_mock_universal_provider(mock_mass)

        provider = MockProvider("airplay")
        player = MockPlayer(
            provider,
            "ap_123456",
            "Test Player",
            identifiers={IdentifierType.IP_ADDRESS: "192.168.1.100"},
        )

        device_key = universal_provider._get_device_key_from_players([player])

        # IP address is not used for device key - falls back to player_id
        # This allows protocol players without MAC/UUID to still get a UniversalPlayer
        assert device_key == "ap_123456"

    def test_device_key_from_no_identifiers_falls_back_to_player_id(
        self, mock_mass: MagicMock
    ) -> None:
        """Test that device key falls back to player_id when no identifiers at all."""
        universal_provider = create_mock_universal_provider(mock_mass)

        provider = MockProvider("sendspin")
        player = MockPlayer(
            provider,
            "sendspin-device-abc",
            "Test Player",
            # No identifiers at all (like Sendspin protocol players)
        )

        device_key = universal_provider._get_device_key_from_players([player])

        # Falls back to player_id when no MAC/UUID identifiers
        assert device_key == "sendspindeviceabc"


class TestGetCleanPlayerName:
    """Tests for player name selection."""

    def test_prefers_chromecast_name(self, mock_mass: MagicMock) -> None:
        """Test that Chromecast names are preferred over other protocols."""
        universal_provider = create_mock_universal_provider(mock_mass)

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
        clean_name = universal_provider._get_clean_player_name([airplay_player, chromecast_player])
        assert clean_name == "Living Room Speaker"

    def test_filters_mac_address_names(self, mock_mass: MagicMock) -> None:
        """Test that MAC address-like names are filtered out."""
        universal_provider = create_mock_universal_provider(mock_mass)

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
        clean_name = universal_provider._get_clean_player_name([sq_player, ap_player])
        assert clean_name == "Kitchen Speaker"

    def test_filters_player_id_names(self, mock_mass: MagicMock) -> None:
        """Test that player ID-like names are filtered out."""
        universal_provider = create_mock_universal_provider(mock_mass)

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
        clean_name = universal_provider._get_clean_player_name([ss_player, dlna_player])
        assert clean_name == "Bedroom TV"

    def test_valid_name_unchanged(self, mock_mass: MagicMock) -> None:
        """Test that valid names are returned unchanged."""
        universal_provider = create_mock_universal_provider(mock_mass)

        provider = MockProvider("airplay")
        player = MockPlayer(
            provider,
            "ap_123456",
            "HomePod Mini",
            player_type=PlayerType.PLAYER,
        )

        clean_name = universal_provider._get_clean_player_name([player])
        assert clean_name == "HomePod Mini"


class TestCachedProtocolParentRestore:
    """Tests for restoring cached protocol parent links."""

    def test_protocol_parent_id_restored_from_config(self, mock_mass: MagicMock) -> None:
        """Test that cached protocol_parent_id is loaded and used for immediate linking."""
        controller = PlayerController(mock_mass)

        # Mock config to return cached parent_id when queried
        def mock_config_get(key: str, default: str | None = None) -> str | None:
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
        assert protocol_player.protocol_parent_id == "native_player_id"

        # Verify protocol was linked to native player
        assert any(
            link.output_protocol_id == protocol_player.player_id
            for link in native_player.linked_output_protocols
        )

    def test_protocol_parent_id_prevents_universal_player_creation(
        self, mock_mass: MagicMock
    ) -> None:
        """Test that cached protocol_parent_id prevents creating universal player."""
        controller = PlayerController(mock_mass)

        # Mock config to return cached parent_id (parent not yet registered)
        def mock_config_get(key: str, default: str | None = None) -> str | None:
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
        assert protocol_player.protocol_parent_id == "native_player_id"

        # Since parent_id is set, delayed evaluation won't create a universal player


class TestSelectBestOutputProtocol:
    """Tests for output protocol selection logic."""

    def test_select_native_when_preferred_is_native(self, mock_mass: MagicMock) -> None:
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

        # Wire up mock_mass.players to controller
        mock_mass.players = controller

        # Register players
        controller._players = {"sonos_123": native_player}
        controller._player_throttlers = {"sonos_123": Throttler(1, 0.05)}

        # Select protocol
        selected_player, output_protocol = controller._select_best_output_protocol(native_player)

        # Should select native player
        assert selected_player == native_player
        assert output_protocol is None  # None means native playback

    def test_select_dlna_when_preferred_is_dlna(self, mock_mass: MagicMock) -> None:
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
        native_player.set_linked_output_protocols(
            [
                OutputProtocol(
                    output_protocol_id="dlna_AABBCCDDEEFF",
                    name="DLNA",
                    protocol_domain="dlna",
                    priority=30,
                )
            ]
        )

        # Select protocol
        selected_player, output_protocol = controller._select_best_output_protocol(native_player)

        # Should select DLNA player, not native
        assert selected_player == dlna_player
        assert output_protocol is not None
        assert output_protocol.output_protocol_id == "dlna_AABBCCDDEEFF"

    def test_select_airplay_when_preferred_is_airplay(self, mock_mass: MagicMock) -> None:
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
        native_player.set_linked_output_protocols(
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
        selected_player, output_protocol = controller._select_best_output_protocol(native_player)

        # Should select AirPlay player (even though DLNA has lower priority value),
        # because user preference overrides priority
        assert selected_player == airplay_player
        assert output_protocol is not None
        assert output_protocol.output_protocol_id == "airplay_AABBCCDDEEFF"

    def test_fallback_to_native_when_auto(self, mock_mass: MagicMock) -> None:
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
        selected_player, output_protocol = controller._select_best_output_protocol(native_player)

        # Should select native player
        assert selected_player == native_player
        assert output_protocol is None  # None means native playback


class TestPlayerGrouping:
    """Tests for player grouping scenarios."""

    def test_native_to_native_grouping(self, mock_mass: MagicMock) -> None:
        """Test that native players from same provider can group together."""
        controller = PlayerController(mock_mass)

        sonos_provider = MockProvider("sonos", mass=mock_mass)

        # Create two Sonos players
        player_a = MockPlayer(
            sonos_provider,
            "sonos_123",
            "Living Room",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:01"},
        )
        player_a._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        player_a._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        player_a._attr_can_group_with = {"sonos_456"}
        player_a._cache.clear()  # Clear cached properties after modifying attributes

        player_b = MockPlayer(
            sonos_provider,
            "sonos_456",
            "Kitchen",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:02"},
        )
        player_b._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        player_b._cache.clear()

        controller._players = {
            "sonos_123": player_a,
            "sonos_456": player_b,
        }
        controller._player_throttlers = {
            "sonos_123": Throttler(1, 0.05),
            "sonos_456": Throttler(1, 0.05),
        }

        # Translate members for native grouping
        protocol_members, native_members, _, _ = controller._translate_members_for_protocols(
            parent_player=player_a,
            player_ids=["sonos_456"],
            parent_protocol_player=None,
            parent_protocol_domain=None,
        )

        # Should use native grouping (same provider)
        assert len(native_members) == 1
        assert "sonos_456" in native_members
        assert len(protocol_members) == 0

    def test_protocol_to_protocol_grouping(self, mock_mass: MagicMock) -> None:
        """Test that protocol players can group via shared protocol."""
        controller = PlayerController(mock_mass)

        # Create two players with AirPlay protocol support
        sonos_provider = MockProvider("sonos", mass=mock_mass)
        wiim_provider = MockProvider("wiim", mass=mock_mass)
        airplay_provider = MockProvider("airplay", mass=mock_mass)

        # Sonos player
        sonos_player = MockPlayer(
            sonos_provider,
            "sonos_123",
            "Living Room",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:01"},
        )
        sonos_player._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        sonos_player._cache.clear()

        # WiiM player
        wiim_player = MockPlayer(
            wiim_provider,
            "wiim_456",
            "Bedroom",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:02"},
        )
        wiim_player._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        wiim_player._cache.clear()

        # AirPlay protocol players
        sonos_airplay = MockPlayer(
            airplay_provider,
            "airplay_sonos",
            "Living Room (AirPlay)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:01"},
        )
        sonos_airplay._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        sonos_airplay._attr_can_group_with = {"airplay_wiim"}
        sonos_airplay._cache.clear()
        sonos_airplay.update_state(signal_event=False)

        wiim_airplay = MockPlayer(
            airplay_provider,
            "airplay_wiim",
            "Bedroom (AirPlay)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:02"},
        )

        # Link protocol players to native players
        sonos_player.set_linked_output_protocols(
            [
                OutputProtocol(
                    output_protocol_id="airplay_sonos",
                    name="AirPlay",
                    protocol_domain="airplay",
                    priority=10,
                    available=True,
                )
            ]
        )
        wiim_player.set_linked_output_protocols(
            [
                OutputProtocol(
                    output_protocol_id="airplay_wiim",
                    name="AirPlay",
                    protocol_domain="airplay",
                    priority=10,
                    available=True,
                )
            ]
        )

        controller._players = {
            "sonos_123": sonos_player,
            "wiim_456": wiim_player,
            "airplay_sonos": sonos_airplay,
            "airplay_wiim": wiim_airplay,
        }
        controller._player_throttlers = {
            "sonos_123": Throttler(1, 0.05),
            "wiim_456": Throttler(1, 0.05),
            "airplay_sonos": Throttler(1, 0.05),
            "airplay_wiim": Throttler(1, 0.05),
        }

        # Translate members for protocol grouping (via AirPlay)
        protocol_members, native_members, protocol_player, _ = (
            controller._translate_members_for_protocols(
                parent_player=sonos_player,
                player_ids=["wiim_456"],
                parent_protocol_player=sonos_airplay,
                parent_protocol_domain="airplay",
            )
        )

        # Should use protocol grouping (AirPlay)
        assert len(protocol_members) == 1
        assert "airplay_wiim" in protocol_members
        assert len(native_members) == 0
        assert protocol_player == sonos_airplay

    def test_hybrid_grouping(self, mock_mass: MagicMock) -> None:
        """Test hybrid grouping: native + protocol players in same group."""
        controller = PlayerController(mock_mass)

        # Create Sonos players (native grouping capability)
        sonos_provider = MockProvider("sonos", instance_id="sonos_instance", mass=mock_mass)
        sonos_a = MockPlayer(
            sonos_provider,
            "sonos_123",
            "Living Room",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:01"},
        )
        sonos_a._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        sonos_a._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        sonos_a._attr_can_group_with = {"sonos_456"}
        sonos_a._cache.clear()

        sonos_b = MockPlayer(
            sonos_provider,
            "sonos_456",
            "Kitchen",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:02"},
        )
        sonos_b._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        sonos_b._cache.clear()

        # Create WiiM player with AirPlay protocol
        wiim_provider = MockProvider("wiim", instance_id="wiim_instance", mass=mock_mass)
        airplay_provider = MockProvider("airplay", instance_id="airplay_instance", mass=mock_mass)

        wiim_player = MockPlayer(
            wiim_provider,
            "wiim_789",
            "Bedroom",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:03"},
        )
        wiim_player._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        wiim_player._cache.clear()

        # AirPlay protocol players
        sonos_airplay = MockPlayer(
            airplay_provider,
            "airplay_sonos",
            "Living Room (AirPlay)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:01"},
        )
        sonos_airplay._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        sonos_airplay._attr_can_group_with = {"airplay_wiim"}
        sonos_airplay._cache.clear()
        sonos_airplay.update_state(signal_event=False)

        wiim_airplay = MockPlayer(
            airplay_provider,
            "airplay_wiim",
            "Bedroom (AirPlay)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:03"},
        )

        # Link AirPlay to Sonos A
        sonos_a.set_linked_output_protocols(
            [
                OutputProtocol(
                    output_protocol_id="airplay_sonos",
                    name="AirPlay",
                    protocol_domain="airplay",
                    priority=10,
                    available=True,
                )
            ]
        )
        wiim_player.set_linked_output_protocols(
            [
                OutputProtocol(
                    output_protocol_id="airplay_wiim",
                    name="AirPlay",
                    protocol_domain="airplay",
                    priority=10,
                    available=True,
                )
            ]
        )
        wiim_player.set_active_output_protocol("airplay_wiim")
        wiim_player.set_protocol_parent_id("airplay_wiim")

        # Wire up mock_mass.players to controller so get_linked_protocol works
        mock_mass.players = controller

        controller._players = {
            "sonos_123": sonos_a,
            "sonos_456": sonos_b,
            "wiim_789": wiim_player,
            "airplay_sonos": sonos_airplay,
            "airplay_wiim": wiim_airplay,
        }
        controller._player_throttlers = {
            "sonos_123": Throttler(1, 0.05),
            "sonos_456": Throttler(1, 0.05),
            "wiim_789": Throttler(1, 0.05),
            "airplay_sonos": Throttler(1, 0.05),
            "airplay_wiim": Throttler(1, 0.05),
        }

        # Group Sonos B (native) + WiiM (via AirPlay) to Sonos A
        protocol_members, native_members, _protocol_player, _ = (
            controller._translate_members_for_protocols(
                parent_player=sonos_a,
                player_ids=["sonos_456", "wiim_789"],
                parent_protocol_player=sonos_airplay,
                parent_protocol_domain="airplay",
            )
        )

        # Should have hybrid group: native Sonos B + protocol WiiM
        assert len(native_members) == 1
        assert "sonos_456" in native_members
        assert len(protocol_members) == 1
        assert "airplay_wiim" in protocol_members

    def test_protocol_selection_requires_set_members(self, mock_mass: MagicMock) -> None:
        """Test that only protocols with SET_MEMBERS support are selected for grouping."""
        controller = PlayerController(mock_mass)

        sonos_provider = MockProvider("sonos", mass=mock_mass)
        wiim_provider = MockProvider("wiim", mass=mock_mass)
        dlna_provider = MockProvider("dlna", mass=mock_mass)
        airplay_provider = MockProvider("airplay", mass=mock_mass)

        # Sonos player
        sonos_player = MockPlayer(
            sonos_provider,
            "sonos_123",
            "Living Room",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:01"},
        )
        sonos_player._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        sonos_player._cache.clear()

        # WiiM player
        wiim_player = MockPlayer(
            wiim_provider,
            "wiim_456",
            "Bedroom",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:02"},
        )
        wiim_player._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        wiim_player._cache.clear()

        # DLNA protocol (does NOT support SET_MEMBERS)
        sonos_dlna = MockPlayer(
            dlna_provider,
            "dlna_sonos",
            "Living Room (DLNA)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:01"},
        )
        # Note: NO SET_MEMBERS feature

        wiim_dlna = MockPlayer(
            dlna_provider,
            "dlna_wiim",
            "Bedroom (DLNA)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:02"},
        )

        # AirPlay protocol (DOES support SET_MEMBERS)
        sonos_airplay = MockPlayer(
            airplay_provider,
            "airplay_sonos",
            "Living Room (AirPlay)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:01"},
        )
        sonos_airplay._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        sonos_airplay._attr_can_group_with = {"airplay_wiim"}
        sonos_airplay._cache.clear()

        wiim_airplay = MockPlayer(
            airplay_provider,
            "airplay_wiim",
            "Bedroom (AirPlay)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:02"},
        )
        wiim_airplay._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        wiim_airplay._attr_can_group_with = {"airplay_sonos"}
        wiim_airplay._cache.clear()

        # Link protocols (DLNA has lower priority than AirPlay)
        sonos_player.set_linked_output_protocols(
            [
                OutputProtocol(
                    output_protocol_id="dlna_sonos",
                    name="DLNA",
                    protocol_domain="dlna",
                    priority=30,  # Lower priority (higher number)
                    available=True,
                ),
                OutputProtocol(
                    output_protocol_id="airplay_sonos",
                    name="AirPlay",
                    protocol_domain="airplay",
                    priority=10,  # Higher priority (lower number)
                    available=True,
                ),
            ]
        )
        wiim_player.set_linked_output_protocols(
            [
                OutputProtocol(
                    output_protocol_id="dlna_wiim",
                    name="DLNA",
                    protocol_domain="dlna",
                    priority=30,
                    available=True,
                ),
                OutputProtocol(
                    output_protocol_id="airplay_wiim",
                    name="AirPlay",
                    protocol_domain="airplay",
                    priority=10,
                    available=True,
                ),
            ]
        )

        controller._players = {
            "sonos_123": sonos_player,
            "wiim_456": wiim_player,
            "dlna_sonos": sonos_dlna,
            "dlna_wiim": wiim_dlna,
            "airplay_sonos": sonos_airplay,
            "airplay_wiim": wiim_airplay,
        }
        controller._player_throttlers = {
            "sonos_123": Throttler(1, 0.05),
            "wiim_456": Throttler(1, 0.05),
            "dlna_sonos": Throttler(1, 0.05),
            "dlna_wiim": Throttler(1, 0.05),
            "airplay_sonos": Throttler(1, 0.05),
            "airplay_wiim": Throttler(1, 0.05),
        }

        # Update state after modifying attributes
        sonos_airplay.update_state(signal_event=False)
        wiim_airplay.update_state(signal_event=False)

        # Translate members - should skip DLNA (no SET_MEMBERS) and select AirPlay
        protocol_members, _native_members, protocol_player, protocol_domain = (
            controller._translate_members_for_protocols(
                parent_player=sonos_player,
                player_ids=["wiim_456"],
                parent_protocol_player=None,
                parent_protocol_domain=None,
            )
        )

        # Should select AirPlay (supports SET_MEMBERS) not DLNA
        assert len(protocol_members) == 1
        assert "airplay_wiim" in protocol_members
        assert protocol_domain == "airplay"
        assert protocol_player == sonos_airplay


class TestCanGroupWith:
    """Tests for can_group_with property with three scenarios."""

    def test_scenario_1_native_active_only_native_players(self, mock_mass: MagicMock) -> None:
        """Test Scenario 1: Native playback active -> all protocols shown (new behavior)."""
        controller = PlayerController(mock_mass)

        sonos_provider = MockProvider("sonos", instance_id="sonos_instance", mass=mock_mass)
        airplay_provider = MockProvider("airplay", instance_id="airplay_instance", mass=mock_mass)

        # Create Sonos player with native and AirPlay support
        sonos_player = MockPlayer(
            sonos_provider,
            "sonos_123",
            "Living Room",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:01"},
        )
        sonos_player._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        sonos_player._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        sonos_player._attr_can_group_with = {"sonos_456"}
        sonos_player._cache.clear()
        sonos_player.set_active_output_protocol("native")

        # Create another Sonos player
        sonos_player_b = MockPlayer(
            sonos_provider,
            "sonos_456",
            "Kitchen",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:02"},
        )

        # Create AirPlay protocol player
        sonos_airplay = MockPlayer(
            airplay_provider,
            "airplay_sonos",
            "Living Room (AirPlay)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:01"},
        )
        sonos_airplay._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        sonos_airplay._attr_can_group_with = {"airplay_other"}
        sonos_airplay._cache.clear()
        sonos_airplay.set_protocol_parent_id("sonos_123")

        sonos_player.set_linked_output_protocols(
            [
                OutputProtocol(
                    output_protocol_id="airplay_sonos",
                    name="AirPlay",
                    protocol_domain="airplay",
                    priority=10,
                    available=True,
                )
            ]
        )

        # Wire up mock_mass.players to controller so get_linked_protocol works
        mock_mass.players = controller

        controller._players = {
            "sonos_123": sonos_player,
            "sonos_456": sonos_player_b,
            "airplay_sonos": sonos_airplay,
        }
        controller._player_throttlers = {
            "sonos_123": Throttler(1, 0.05),
            "sonos_456": Throttler(1, 0.05),
            "airplay_sonos": Throttler(1, 0.05),
        }

        # Update state after modifying attributes and registering with controller
        sonos_player.update_state(signal_event=False)
        sonos_player_b.update_state(signal_event=False)
        sonos_airplay.update_state(signal_event=False)

        # Get can_group_with while native is active
        groupable = sonos_player.state.can_group_with

        # NEW BEHAVIOR: Should show both native AND protocol players
        # even when native protocol is active
        assert "sonos_456" in groupable  # Native Sonos player
        # Note: airplay_other is not registered in controller._players, so it won't appear
        # But the logic should still allow showing AirPlay options if they were registered

    def test_scenario_2_protocol_active_hybrid_groups(self, mock_mass: MagicMock) -> None:
        """Test Scenario 2: Protocol active -> show all protocols (new behavior)."""
        controller = PlayerController(mock_mass)

        sonos_provider = MockProvider("sonos", instance_id="sonos_instance", mass=mock_mass)
        airplay_provider = MockProvider("airplay", instance_id="airplay_instance", mass=mock_mass)

        # Create Sonos player with AirPlay active
        sonos_player = MockPlayer(
            sonos_provider,
            "sonos_123",
            "Living Room",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:01"},
        )
        sonos_player._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        sonos_player._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        sonos_player._attr_can_group_with = {"sonos_456"}
        sonos_player._cache.clear()

        # Create another Sonos player
        sonos_player_b = MockPlayer(
            sonos_provider,
            "sonos_456",
            "Kitchen",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:02"},
        )

        # Create AirPlay protocol player
        sonos_airplay = MockPlayer(
            airplay_provider,
            "airplay_sonos",
            "Living Room (AirPlay)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:01"},
        )
        sonos_airplay._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        sonos_airplay._attr_can_group_with = {"airplay_other"}
        sonos_airplay._cache.clear()
        sonos_airplay.set_protocol_parent_id("sonos_123")

        # Create another device with AirPlay
        wiim_player = MockPlayer(
            sonos_provider,
            "wiim_789",
            "Bedroom",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:03"},
        )

        airplay_other = MockPlayer(
            airplay_provider,
            "airplay_other",
            "Bedroom (AirPlay)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:03"},
        )
        airplay_other._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        airplay_other._attr_can_group_with = {"airplay_sonos"}
        airplay_other._cache.clear()
        airplay_other.set_protocol_parent_id("wiim_789")

        wiim_player.set_linked_output_protocols(
            [
                OutputProtocol(
                    output_protocol_id="airplay_other",
                    name="AirPlay",
                    protocol_domain="airplay",
                    priority=10,
                    available=True,
                ),
            ]
        )

        sonos_player.set_linked_output_protocols(
            [
                OutputProtocol(
                    output_protocol_id="airplay_sonos",
                    name="AirPlay",
                    protocol_domain="airplay",
                    priority=10,
                    available=True,
                )
            ]
        )
        sonos_player.set_active_output_protocol("airplay_sonos")

        # Wire up mock_mass.players to controller so get_linked_protocol works
        mock_mass.players = controller

        controller._players = {
            "sonos_123": sonos_player,
            "sonos_456": sonos_player_b,
            "wiim_789": wiim_player,
            "airplay_sonos": sonos_airplay,
            "airplay_other": airplay_other,
        }
        controller._player_throttlers = {
            "sonos_123": Throttler(1, 0.05),
            "sonos_456": Throttler(1, 0.05),
            "wiim_789": Throttler(1, 0.05),
            "airplay_sonos": Throttler(1, 0.05),
            "airplay_other": Throttler(1, 0.05),
        }

        # Clear cache after setting linked protocols
        sonos_player._cache.clear()
        wiim_player._cache.clear()

        # Update state after modifying attributes and registering with controller
        # IMPORTANT: Update protocol players FIRST, then parent players
        sonos_airplay.update_state(signal_event=False)
        airplay_other.update_state(signal_event=False)
        sonos_player.update_state(signal_event=False)
        sonos_player_b.update_state(signal_event=False)
        wiim_player.update_state(signal_event=False)

        # Get can_group_with while AirPlay is active
        groupable = sonos_player.state.can_group_with

        # NEW BEHAVIOR: Should show ALL protocols + native players
        # regardless of which protocol is active
        assert "sonos_456" in groupable  # Native Sonos player
        assert "wiim_789" in groupable  # Via airplay_other protocol

    def test_scenario_3_no_active_output_all_protocols_shown(self, mock_mass: MagicMock) -> None:
        """Test Scenario 3: No active output -> show all compatible protocols + native."""
        controller = PlayerController(mock_mass)

        sonos_provider = MockProvider("sonos", instance_id="sonos_instance", mass=mock_mass)
        airplay_provider = MockProvider("airplay", instance_id="airplay_instance", mass=mock_mass)
        dlna_provider = MockProvider("dlna", instance_id="dlna_instance", mass=mock_mass)

        # Create Sonos player (no active protocol)
        sonos_player = MockPlayer(
            sonos_provider,
            "sonos_123",
            "Living Room",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:01"},
        )
        sonos_player._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        sonos_player._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        sonos_player._attr_can_group_with = {"sonos_456"}
        sonos_player._cache.clear()
        # No active output protocol set

        # Create another Sonos player
        sonos_player_b = MockPlayer(
            sonos_provider,
            "sonos_456",
            "Kitchen",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:02"},
        )

        # Create AirPlay protocol player (supports SET_MEMBERS)
        sonos_airplay = MockPlayer(
            airplay_provider,
            "airplay_sonos",
            "Living Room (AirPlay)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:01"},
        )
        sonos_airplay._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        sonos_airplay._attr_can_group_with = {"airplay_other"}
        sonos_airplay._cache.clear()
        sonos_airplay.set_protocol_parent_id("sonos_123")

        # Create DLNA protocol player (does NOT support SET_MEMBERS)
        sonos_dlna = MockPlayer(
            dlna_provider,
            "dlna_sonos",
            "Living Room (DLNA)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:01"},
        )
        # No SET_MEMBERS support
        sonos_dlna._attr_can_group_with = {"dlna_other"}
        sonos_dlna.set_protocol_parent_id("sonos_123")

        # Another device
        wiim_player = MockPlayer(
            sonos_provider,
            "wiim_789",
            "Bedroom",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:03"},
        )

        airplay_other = MockPlayer(
            airplay_provider,
            "airplay_other",
            "Bedroom (AirPlay)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:03"},
        )
        airplay_other._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        airplay_other._attr_can_group_with = {"airplay_sonos"}
        airplay_other._cache.clear()
        airplay_other.set_protocol_parent_id("wiim_789")

        sonos_player.set_linked_output_protocols(
            [
                OutputProtocol(
                    output_protocol_id="airplay_sonos",
                    name="AirPlay",
                    protocol_domain="airplay",
                    priority=10,
                    available=True,
                ),
                OutputProtocol(
                    output_protocol_id="dlna_sonos",
                    name="DLNA",
                    protocol_domain="dlna",
                    priority=30,
                    available=True,
                ),
            ]
        )

        wiim_player.set_linked_output_protocols(
            [
                OutputProtocol(
                    output_protocol_id="airplay_other",
                    name="AirPlay",
                    protocol_domain="airplay",
                    priority=10,
                    available=True,
                ),
            ]
        )

        # Clear cache after setting linked protocols (output_protocols is cached)
        sonos_player._cache.clear()
        wiim_player._cache.clear()

        # Wire up mock_mass.players to controller so get_linked_protocol works
        mock_mass.players = controller

        controller._players = {
            "sonos_123": sonos_player,
            "sonos_456": sonos_player_b,
            "wiim_789": wiim_player,
            "airplay_sonos": sonos_airplay,
            "airplay_other": airplay_other,
            "dlna_sonos": sonos_dlna,
        }
        controller._player_throttlers = {
            "sonos_123": Throttler(1, 0.05),
            "sonos_456": Throttler(1, 0.05),
            "wiim_789": Throttler(1, 0.05),
            "airplay_sonos": Throttler(1, 0.05),
            "airplay_other": Throttler(1, 0.05),
            "dlna_sonos": Throttler(1, 0.05),
        }

        # Update state after modifying attributes and registering with controller
        # Note: set_linked_output_protocols calls trigger_player_update, but since mass.players
        # is a MagicMock, we need to manually call update_state
        # IMPORTANT: Update protocol players FIRST, then parent players, because parent players
        # access protocol_player.state.can_group_with during their update_state()
        sonos_airplay.update_state(signal_event=False)
        airplay_other.update_state(signal_event=False)
        sonos_dlna.update_state(signal_event=False)
        sonos_player.update_state(signal_event=False)
        sonos_player_b.update_state(signal_event=False)
        wiim_player.update_state(signal_event=False)

        # Get can_group_with with no active protocol
        groupable = sonos_player.state.can_group_with

        # Should show native players + AirPlay players (supports SET_MEMBERS)
        # but NOT DLNA players (no SET_MEMBERS support)
        assert "sonos_456" in groupable
        assert "wiim_789" in groupable  # Via AirPlay protocol
        # DLNA players should not be shown since DLNA doesn't support SET_MEMBERS


class TestProtocolSwitchingDuringPlayback:
    """Tests for dynamic protocol switching when group members change during playback."""

    async def test_no_protocol_set_during_grouping_without_playback(
        self, mock_mass: MagicMock
    ) -> None:
        """Test that no protocol is set when grouping players without active playback."""
        controller = PlayerController(mock_mass)

        sonos_provider = MockProvider("sonos", instance_id="sonos_instance", mass=mock_mass)
        airplay_provider = MockProvider("airplay", instance_id="airplay_instance", mass=mock_mass)

        # Create Sonos player with AirPlay support
        sonos_player = MockPlayer(
            sonos_provider,
            "sonos_123",
            "Living Room",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:01"},
        )
        sonos_player._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        sonos_player._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        sonos_player._attr_can_group_with = {"sonos_456"}

        # Create another Sonos player
        sonos_player_b = MockPlayer(
            sonos_provider,
            "sonos_456",
            "Kitchen",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:02"},
        )
        sonos_player_b._attr_supported_features.add(PlayerFeature.SET_MEMBERS)

        # Create AirPlay protocol player
        sonos_airplay = MockPlayer(
            airplay_provider,
            "airplay_sonos",
            "Living Room (AirPlay)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:01"},
        )
        sonos_airplay._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        sonos_airplay.set_protocol_parent_id("sonos_123")

        sonos_player.set_linked_output_protocols(
            [
                OutputProtocol(
                    output_protocol_id="airplay_sonos",
                    name="AirPlay",
                    protocol_domain="airplay",
                    priority=10,
                    available=True,
                )
            ]
        )

        mock_mass.players = controller
        controller._players = {
            "sonos_123": sonos_player,
            "sonos_456": sonos_player_b,
            "airplay_sonos": sonos_airplay,
        }
        controller._player_throttlers = {
            "sonos_123": Throttler(1, 0.05),
            "sonos_456": Throttler(1, 0.05),
            "airplay_sonos": Throttler(1, 0.05),
        }

        # Group players via protocol (simulate grouping through AirPlay)
        # This should NOT set active_output_protocol anymore
        await controller._forward_protocol_set_members(
            parent_player=sonos_player,
            parent_protocol_player=sonos_airplay,
            protocol_members_to_add=["airplay_other"],  # Add a protocol member
            protocol_members_to_remove=[],
        )

        # NEW BEHAVIOR: Protocol should NOT be set during grouping without playback
        # After grouping, protocol should not be activated until playback starts
        assert sonos_player.active_output_protocol is None

    async def test_protocol_selected_at_playback_time(self, mock_mass: MagicMock) -> None:
        """Test that protocol is selected when playback starts, not during grouping."""
        controller = PlayerController(mock_mass)

        sonos_provider = MockProvider("sonos", instance_id="sonos_instance", mass=mock_mass)
        airplay_provider = MockProvider("airplay", instance_id="airplay_instance", mass=mock_mass)

        # Create Sonos player with AirPlay support
        sonos_player = MockPlayer(
            sonos_provider,
            "sonos_123",
            "Living Room",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:01"},
        )
        sonos_player._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        sonos_player._attr_supported_features.add(PlayerFeature.SET_MEMBERS)

        # Create AirPlay protocol player with group members
        sonos_airplay = MockPlayer(
            airplay_provider,
            "airplay_sonos",
            "Living Room (AirPlay)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:01"},
        )
        sonos_airplay._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        sonos_airplay._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        sonos_airplay.set_protocol_parent_id("sonos_123")
        # Simulate that AirPlay protocol has group members (needs >1 for grouping check)
        sonos_airplay._attr_group_members = ["airplay_sonos", "airplay_other"]

        sonos_player.set_linked_output_protocols(
            [
                OutputProtocol(
                    output_protocol_id="airplay_sonos",
                    name="AirPlay",
                    protocol_domain="airplay",
                    priority=10,
                    available=True,
                )
            ]
        )

        mock_mass.players = controller
        controller._players = {
            "sonos_123": sonos_player,
            "airplay_sonos": sonos_airplay,
        }

        # Update state to apply group members to state
        sonos_airplay.update_state(signal_event=False)
        sonos_player.update_state(signal_event=False)

        # Protocol should not be set yet
        assert sonos_player.active_output_protocol is None

        # Select protocol for playback
        selected_player, output_protocol = controller._select_best_output_protocol(sonos_player)

        # Should select AirPlay protocol because it has group members (Priority 1)
        assert selected_player == sonos_airplay
        assert output_protocol is not None
        assert output_protocol.output_protocol_id == "airplay_sonos"
