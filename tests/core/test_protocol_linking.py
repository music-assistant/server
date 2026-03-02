"""Tests for protocol player linking and universal player creation."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import IdentifierType, PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.player import OutputProtocol, PlayerMedia

from music_assistant.controllers.players import PlayerController
from music_assistant.helpers.throttle_retry import Throttler
from music_assistant.helpers.util import enrich_device_mac_address
from music_assistant.models.player import DeviceInfo, Player
from music_assistant.providers.universal_player.player import UniversalPlayer
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
    # Add config mock for instance_id property
    config = MagicMock()
    config.instance_id = "universal_player"
    config.name = None
    provider.config = config
    # Initialize the locks dict that would normally be set in handle_async_init
    provider._universal_player_locks = {}
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

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Mock implementation of set_members."""
        current_members = set(getattr(self, "_attr_group_members", []))

        if player_ids_to_add:
            current_members.update(player_ids_to_add)

        if player_ids_to_remove:
            current_members.difference_update(player_ids_to_remove)

        # Always include self as first member if there are members
        if current_members:
            self._attr_group_members = [self.player_id] + [
                pid for pid in current_members if pid != self.player_id
            ]
        else:
            self._attr_group_members = []

        # Clear cache to reflect changes
        self._cache.clear()


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

    def test_mac_address_locally_administered_bit_match(self, mock_mass: MagicMock) -> None:
        """Test that MAC addresses differing only in locally-administered bit match.

        Some protocols (like AirPlay) report a MAC with the locally-administered
        bit set (bit 1 of first octet), while other protocols report the real
        hardware MAC. These should match as the same device.

        Example: 54:78:C9:E6:0D:A0 (hardware) vs 56:78:C9:E6:0D:A0 (AirPlay)
        """
        controller = PlayerController(mock_mass)

        provider = MockProvider("test")
        # Real hardware MAC (first byte 0x54 = 01010100, bit 1 = 0)
        player_a = MockPlayer(
            provider,
            "player_a",
            "WiiM Pro (DLNA)",
            identifiers={IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A0"},
        )
        # AirPlay MAC with locally-administered bit set (first byte 0x56 = 01010110, bit 1 = 1)
        player_b = MockPlayer(
            provider,
            "player_b",
            "WiiM Pro (AirPlay)",
            identifiers={IdentifierType.MAC_ADDRESS: "56:78:C9:E6:0D:A0"},
        )

        # These should match because they differ only in the locally-administered bit
        assert controller._identifiers_match(player_a, player_b) is True

    def test_mac_address_locally_administered_bit_different_devices_no_match(
        self, mock_mass: MagicMock
    ) -> None:
        """Test that different devices with locally-administered MACs don't match.

        Only the locally-administered bit should be ignored, not other differences.
        """
        controller = PlayerController(mock_mass)

        provider = MockProvider("test")
        player_a = MockPlayer(
            provider,
            "player_a",
            "Device A",
            identifiers={IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A0"},
        )
        player_b = MockPlayer(
            provider,
            "player_b",
            "Device B",
            identifiers={IdentifierType.MAC_ADDRESS: "56:78:C9:E6:0D:A1"},  # Different last byte
        )

        # These should NOT match - they differ in more than just the locally-administered bit
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

    def test_cast_uuid_match(self, mock_mass: MagicMock) -> None:
        """Test that CAST_UUID identifiers match for cross-protocol linking.

        When a Chromecast device has no valid MAC (non-Google devices like SEI Robotics),
        the CAST_UUID identifier allows matching the Chromecast player with its
        Sendspin bridge player.
        """
        controller = PlayerController(mock_mass)

        provider = MockProvider("test")
        # Chromecast player with UUID and CAST_UUID but no MAC
        player_a = MockPlayer(
            provider,
            "player_a",
            "Android TV (Cast)",
            player_type=PlayerType.PROTOCOL,
            identifiers={
                IdentifierType.UUID: "88ef6168-67d7-3d08-fae8-b1b1709c1ed7",
                IdentifierType.CAST_UUID: "88ef6168-67d7-3d08-fae8-b1b1709c1ed7",
                IdentifierType.IP_ADDRESS: "192.168.1.228",
            },
        )
        # Sendspin bridge player with only MAC and CAST_UUID (no UUID, no IP)
        player_b = MockPlayer(
            provider,
            "player_b",
            "Android TV (Sendspin)",
            player_type=PlayerType.PROTOCOL,
            identifiers={
                IdentifierType.MAC_ADDRESS: "D4:CF:F9:D9:DA:FD",
                IdentifierType.CAST_UUID: "88ef6168-67d7-3d08-fae8-b1b1709c1ed7",
            },
        )

        # Should match via CAST_UUID even though they share no MAC, UUID, or IP
        assert controller._identifiers_match(player_a, player_b) is True

    def test_cast_uuid_no_match(self, mock_mass: MagicMock) -> None:
        """Test that different CAST_UUIDs don't match."""
        controller = PlayerController(mock_mass)

        provider = MockProvider("test")
        player_a = MockPlayer(
            provider,
            "player_a",
            "Device A",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.CAST_UUID: "88ef6168-67d7-3d08-fae8-b1b1709c1ed7"},
        )
        player_b = MockPlayer(
            provider,
            "player_b",
            "Device B",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.CAST_UUID: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},
        )

        assert controller._identifiers_match(player_a, player_b) is False

    def test_airplay_id_match(self, mock_mass: MagicMock) -> None:
        """Test that AIRPLAY_ID identifiers match for cross-protocol linking.

        When an AirPlay device's MAC has locally-administered bit differences,
        AIRPLAY_ID provides a reliable secondary match path between the AirPlay
        player and its Sendspin bridge player.
        """
        controller = PlayerController(mock_mass)

        provider = MockProvider("test")
        # AirPlay player with its player_id as AIRPLAY_ID
        player_a = MockPlayer(
            provider,
            "player_a",
            "Apple TV (AirPlay)",
            player_type=PlayerType.PROTOCOL,
            identifiers={
                IdentifierType.MAC_ADDRESS: "54:78:1A:D4:FF:44",
                IdentifierType.AIRPLAY_ID: "ap54781ad4ff44",
                IdentifierType.IP_ADDRESS: "192.168.1.50",
            },
        )
        # Sendspin bridge player with MAC and AIRPLAY_ID
        player_b = MockPlayer(
            provider,
            "player_b",
            "Apple TV (Sendspin)",
            player_type=PlayerType.PROTOCOL,
            identifiers={
                IdentifierType.MAC_ADDRESS: "54:78:1A:D4:FF:44",
                IdentifierType.AIRPLAY_ID: "ap54781ad4ff44",
            },
        )

        # Should match via both MAC and AIRPLAY_ID
        assert controller._identifiers_match(player_a, player_b) is True

    def test_airplay_id_no_match(self, mock_mass: MagicMock) -> None:
        """Test that different AIRPLAY_IDs don't match."""
        controller = PlayerController(mock_mass)

        provider = MockProvider("test")
        player_a = MockPlayer(
            provider,
            "player_a",
            "Device A",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.AIRPLAY_ID: "ap54781ad4ff44"},
        )
        player_b = MockPlayer(
            provider,
            "player_b",
            "Device B",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.AIRPLAY_ID: "apaabbccddeeff"},
        )

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

        # Mark players as initialized so they are returned by all_players()
        airplay_player.set_initialized()
        chromecast_player.set_initialized()

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
        """Test device key generation from MAC address.

        Note: Device keys are normalized to clear the locally-administered bit
        (bit 1 of first octet) to ensure consistent keys across protocols.
        """
        universal_provider = create_mock_universal_provider(mock_mass)

        provider = MockProvider("airplay")
        # Use a MAC without locally-administered bit set for cleaner test
        # 00:BB:CC:DD:EE:FF has first byte 0x00, bit 1 = 0
        player = MockPlayer(
            provider,
            "ap_123456",
            "Test Player",
            identifiers={IdentifierType.MAC_ADDRESS: "00:BB:CC:DD:EE:FF"},
        )

        device_key = universal_provider._get_device_key_from_players([player])

        assert device_key == "00bbccddeeff"

    def test_device_key_normalizes_locally_administered_mac(self, mock_mass: MagicMock) -> None:
        """Test that device key normalizes locally-administered MACs.

        A device with hardware MAC 54:78:C9:E6:0D:A0 and AirPlay MAC 56:78:C9:E6:0D:A0
        should generate the same device key, allowing them to be merged into
        the same universal player.
        """
        universal_provider = create_mock_universal_provider(mock_mass)

        provider_dlna = MockProvider("dlna")
        provider_airplay = MockProvider("airplay")

        # DLNA player with real hardware MAC
        player_dlna = MockPlayer(
            provider_dlna,
            "dlna_123456",
            "WiiM Pro (DLNA)",
            identifiers={IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A0"},
        )

        # AirPlay player with locally-administered MAC (bit 1 set)
        player_airplay = MockPlayer(
            provider_airplay,
            "ap_123456",
            "WiiM Pro (AirPlay)",
            identifiers={IdentifierType.MAC_ADDRESS: "56:78:C9:E6:0D:A0"},
        )

        # Both should generate the same device key
        key_dlna = universal_provider._get_device_key_from_players([player_dlna])
        key_airplay = universal_provider._get_device_key_from_players([player_airplay])

        # Keys should be identical (both normalized to clear locally-administered bit)
        assert key_dlna == key_airplay
        # The normalized MAC should have bit 1 cleared (0x54 not 0x56)
        assert key_dlna == "5478c9e60da0"

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
                    priority=50,
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
                    priority=50,
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
                    priority=50,  # Lower priority (higher number)
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
                    priority=50,
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

        mock_mass.players = controller
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
        sonos_dlna.update_state(signal_event=False)
        wiim_dlna.update_state(signal_event=False)
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
                    priority=50,
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


class TestNativePlayerProtocolGrouping:
    """Tests for grouping between native PLAYER type and PROTOCOL type AirPlay players."""

    def test_native_airplay_player_sees_protocol_players_as_visible_parents(
        self, mock_mass: MagicMock
    ) -> None:
        """Test that a native PLAYER type translates protocol players to visible parents."""
        controller = PlayerController(mock_mass)

        airplay_provider = MockProvider("airplay", instance_id="airplay", mass=mock_mass)
        sonos_provider = MockProvider("sonos", instance_id="sonos", mass=mock_mass)

        # HomePod: native AirPlay PLAYER (not PROTOCOL)
        homepod = MockPlayer(airplay_provider, "homepod_1", "Office")
        homepod._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        homepod._attr_can_group_with = {"airplay"}  # Provider instance ID
        homepod._cache.clear()

        # Sonos native player (visible to the user)
        sonos_player = MockPlayer(sonos_provider, "sonos_1", "Kitchen")
        sonos_player._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        sonos_player._cache.clear()

        # AirPlay protocol player for the Sonos (hidden, linked to sonos_player)
        sonos_airplay = MockPlayer(
            airplay_provider,
            "airplay_sonos_1",
            "Kitchen (AirPlay)",
            player_type=PlayerType.PROTOCOL,
        )
        sonos_airplay._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        sonos_airplay._attr_can_group_with = {"airplay"}
        sonos_airplay._cache.clear()
        sonos_airplay.set_protocol_parent_id("sonos_1")

        sonos_player.set_linked_output_protocols(
            [
                OutputProtocol(
                    output_protocol_id="airplay_sonos_1",
                    name="AirPlay",
                    protocol_domain="airplay",
                    priority=10,
                    available=True,
                )
            ]
        )

        mock_mass.players = controller
        mock_mass.get_provider = MagicMock(return_value=airplay_provider)

        controller._players = {
            "homepod_1": homepod,
            "sonos_1": sonos_player,
            "airplay_sonos_1": sonos_airplay,
        }
        controller._player_throttlers = {
            "homepod_1": Throttler(1, 0.05),
            "sonos_1": Throttler(1, 0.05),
            "airplay_sonos_1": Throttler(1, 0.05),
        }

        # Mark players as initialized so they are returned by all_players()
        homepod.set_initialized()
        sonos_player.set_initialized()
        sonos_airplay.set_initialized()

        # Update protocol players first, then parents
        sonos_airplay.update_state(signal_event=False)
        sonos_player.update_state(signal_event=False)
        homepod.update_state(signal_event=False)

        groupable = homepod.state.can_group_with

        # HomePod should see Sonos's VISIBLE player, not the hidden protocol player
        assert "sonos_1" in groupable
        assert "airplay_sonos_1" not in groupable  # Hidden protocol ID must NOT appear

    def test_protocol_linked_player_sees_native_airplay_player(self, mock_mass: MagicMock) -> None:
        """Test that a player with linked AirPlay protocol sees native PLAYER type players."""
        controller = PlayerController(mock_mass)

        airplay_provider = MockProvider("airplay", instance_id="airplay", mass=mock_mass)
        sonos_provider = MockProvider("sonos", instance_id="sonos", mass=mock_mass)

        # HomePod: native AirPlay PLAYER
        homepod = MockPlayer(airplay_provider, "homepod_1", "Office")
        homepod._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        homepod._attr_can_group_with = {"airplay"}
        homepod._cache.clear()

        # Sonos native player (visible to the user)
        sonos_player = MockPlayer(sonos_provider, "sonos_1", "Kitchen")
        sonos_player._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        sonos_player._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        sonos_player._attr_can_group_with = set()  # No native Sonos grouping peers
        sonos_player._cache.clear()

        # AirPlay protocol player for the Sonos (hidden, linked to sonos_player)
        sonos_airplay = MockPlayer(
            airplay_provider,
            "airplay_sonos_1",
            "Kitchen (AirPlay)",
            player_type=PlayerType.PROTOCOL,
        )
        sonos_airplay._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        sonos_airplay._attr_can_group_with = {"airplay"}  # Provider instance ID
        sonos_airplay._cache.clear()
        sonos_airplay.set_protocol_parent_id("sonos_1")

        sonos_player.set_linked_output_protocols(
            [
                OutputProtocol(
                    output_protocol_id="airplay_sonos_1",
                    name="AirPlay",
                    protocol_domain="airplay",
                    priority=10,
                    available=True,
                )
            ]
        )

        mock_mass.players = controller
        mock_mass.get_provider = MagicMock(return_value=airplay_provider)

        controller._players = {
            "homepod_1": homepod,
            "sonos_1": sonos_player,
            "airplay_sonos_1": sonos_airplay,
        }
        controller._player_throttlers = {
            "homepod_1": Throttler(1, 0.05),
            "sonos_1": Throttler(1, 0.05),
            "airplay_sonos_1": Throttler(1, 0.05),
        }

        # Mark players as initialized so they are returned by all_players()
        homepod.set_initialized()
        sonos_player.set_initialized()
        sonos_airplay.set_initialized()

        # Update protocol players first, then parents
        sonos_airplay.update_state(signal_event=False)
        homepod.update_state(signal_event=False)
        sonos_player.update_state(signal_event=False)

        groupable = sonos_player.state.can_group_with

        # Sonos should see HomePod via its linked AirPlay protocol's can_group_with
        assert "homepod_1" in groupable


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

    async def test_no_restart_from_handle_set_members(self, mock_mass: MagicMock) -> None:
        """Test that _handle_set_members does NOT restart playback.

        Protocol switching and playback restarts are handled in _forward_protocol_set_members,
        not in _handle_set_members. This test verifies that _handle_set_members doesn't
        trigger any redundant playback restarts.
        """
        controller = PlayerController(mock_mass)

        sonos_provider = MockProvider("sonos", instance_id="sonos_instance", mass=mock_mass)
        airplay_provider = MockProvider("airplay", instance_id="airplay_instance", mass=mock_mass)

        # Create Sonos player currently playing via AirPlay
        sonos_player = MockPlayer(
            sonos_provider,
            "sonos_123",
            "Living Room",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:01"},
        )
        sonos_player._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        sonos_player._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        sonos_player._attr_playback_state = PlaybackState.PLAYING
        sonos_player._attr_group_members = ["sonos_123", "sonos_456"]

        # Create another Sonos player in the group (member of sonos_123's group)
        sonos_player_b = MockPlayer(
            sonos_provider,
            "sonos_456",
            "Kitchen",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:02"},
        )
        sonos_player_b._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        # sonos_player_b's synced_to is derived from group_members, not a direct attribute

        # Create AirPlay protocol player (was used for grouping)
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

        # Update state and set active output protocol AFTER registering with controller
        sonos_player.update_state(signal_event=False)
        sonos_player_b.update_state(signal_event=False)
        sonos_airplay.update_state(signal_event=False)

        # Set active output protocol (must be done after controller is set up)
        sonos_player.set_active_output_protocol("airplay_sonos")

        # Track if cmd_resume was called
        resume_called = False

        async def mock_cmd_resume(
            player_id: str,  # noqa: ARG001
            source: str | None = None,  # noqa: ARG001
            media: PlayerMedia | None = None,  # noqa: ARG001
        ) -> None:
            nonlocal resume_called
            resume_called = True

        controller.cmd_resume = mock_cmd_resume  # type: ignore[method-assign]

        # Remove member - now only the parent player is left
        # After removal, _select_best_output_protocol would return native
        sonos_player._attr_group_members = ["sonos_123"]
        sonos_player._cache.clear()

        # Call _handle_set_members directly to trigger the protocol change check
        await controller._handle_set_members(
            sonos_player,
            player_ids_to_add=None,
            player_ids_to_remove=["sonos_456"],
        )

        # Playback should NOT have been restarted because we're going back to native
        assert not resume_called, "cmd_resume should not be called when switching to native"


class TestNativeProtocolPlayerGrouping:
    """Tests for grouping with native protocol players (e.g., native AirPlay like Apple TV)."""

    def test_native_airplay_groups_with_protocol_linked_player(self, mock_mass: MagicMock) -> None:
        """Test grouping a native AirPlay player (Apple TV) with a protocol-linked player (Sonos).

        This tests the scenario where:
        - Apple TV is a native AirPlay PLAYER (not PROTOCOL type)
        - Sonos has AirPlay as a linked protocol
        - Apple TV groups with Sonos via the common AirPlay protocol
        """
        controller = PlayerController(mock_mass)

        airplay_provider = MockProvider("airplay", instance_id="airplay", mass=mock_mass)
        sonos_provider = MockProvider("sonos", instance_id="sonos", mass=mock_mass)

        # Apple TV: native AirPlay PLAYER (supports grouping via AirPlay)
        apple_tv = MockPlayer(airplay_provider, "apple_tv_1", "Apple TV Slaapkamer")
        apple_tv._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        apple_tv._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        apple_tv._attr_can_group_with = {"airplay"}  # Provider instance ID
        apple_tv._cache.clear()

        # Sonos native player (visible)
        sonos_player = MockPlayer(sonos_provider, "sonos_badkamer", "Badkamer")
        sonos_player._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        sonos_player._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        sonos_player._cache.clear()

        # AirPlay protocol player for Sonos (hidden, linked to sonos)
        sonos_airplay = MockPlayer(
            airplay_provider,
            "airplay_sonos",
            "Badkamer (AirPlay)",
            player_type=PlayerType.PROTOCOL,
        )
        sonos_airplay._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        sonos_airplay._attr_can_group_with = {"airplay"}
        sonos_airplay._cache.clear()
        sonos_airplay.set_protocol_parent_id("sonos_badkamer")

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
            "apple_tv_1": apple_tv,
            "sonos_badkamer": sonos_player,
            "airplay_sonos": sonos_airplay,
        }
        controller._player_throttlers = {
            "apple_tv_1": Throttler(1, 0.05),
            "sonos_badkamer": Throttler(1, 0.05),
            "airplay_sonos": Throttler(1, 0.05),
        }

        # Update states
        sonos_airplay.update_state(signal_event=False)
        sonos_player.update_state(signal_event=False)
        apple_tv.update_state(signal_event=False)

        # Translate members for grouping Sonos to Apple TV
        protocol_members, _native_members, protocol_player, protocol_domain = (
            controller._translate_members_for_protocols(
                parent_player=apple_tv,
                player_ids=["sonos_badkamer"],
                parent_protocol_player=None,
                parent_protocol_domain=None,
            )
        )

        # Should find common AirPlay protocol
        assert len(protocol_members) == 1
        assert "airplay_sonos" in protocol_members
        assert protocol_domain == "airplay"
        # For native AirPlay player, protocol_player should be the Apple TV itself
        assert protocol_player == apple_tv

    def test_get_output_protocol_by_domain_finds_native(self, mock_mass: MagicMock) -> None:
        """Test that get_output_protocol_by_domain finds native protocol."""
        controller = PlayerController(mock_mass)

        airplay_provider = MockProvider("airplay", instance_id="airplay", mass=mock_mass)

        # Native AirPlay player
        apple_tv = MockPlayer(airplay_provider, "apple_tv_1", "Apple TV")
        apple_tv._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        apple_tv._cache.clear()

        mock_mass.players = controller
        controller._players = {"apple_tv_1": apple_tv}

        apple_tv.update_state(signal_event=False)

        # Should find native AirPlay protocol
        protocol = apple_tv.get_output_protocol_by_domain("airplay")
        assert protocol is not None
        assert protocol.output_protocol_id == "native"
        assert protocol.protocol_domain == "airplay"
        assert protocol.is_native is True


class TestFinalGroupMembersTranslation:
    """Tests for __final_group_members translation of protocol player IDs."""

    def test_final_group_members_translates_protocol_ids(self, mock_mass: MagicMock) -> None:
        """Test that __final_group_members translates protocol player IDs to visible IDs.

        When a native AirPlay player (Apple TV) has protocol players in its group_members,
        the final state should show the visible parent player IDs instead.
        """
        controller = PlayerController(mock_mass)

        airplay_provider = MockProvider("airplay", instance_id="airplay", mass=mock_mass)
        sonos_provider = MockProvider("sonos", instance_id="sonos", mass=mock_mass)

        # Apple TV with group members containing a protocol player ID
        apple_tv = MockPlayer(airplay_provider, "apple_tv_1", "Apple TV")
        apple_tv._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        apple_tv._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        apple_tv._attr_group_members = ["apple_tv_1", "airplay_sonos"]
        apple_tv._cache.clear()

        # Sonos visible player
        sonos_player = MockPlayer(sonos_provider, "sonos_1", "Sonos")
        sonos_player._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        sonos_player._cache.clear()

        # AirPlay protocol player for Sonos
        sonos_airplay = MockPlayer(
            airplay_provider,
            "airplay_sonos",
            "Sonos (AirPlay)",
            player_type=PlayerType.PROTOCOL,
        )
        sonos_airplay._cache.clear()
        sonos_airplay.set_protocol_parent_id("sonos_1")

        mock_mass.players = controller
        controller._players = {
            "apple_tv_1": apple_tv,
            "sonos_1": sonos_player,
            "airplay_sonos": sonos_airplay,
        }
        controller._player_throttlers = {
            "apple_tv_1": Throttler(1, 0.05),
            "sonos_1": Throttler(1, 0.05),
            "airplay_sonos": Throttler(1, 0.05),
        }

        sonos_airplay.update_state(signal_event=False)
        sonos_player.update_state(signal_event=False)
        apple_tv.update_state(signal_event=False)

        # Final group_members should show visible player IDs
        final_members = apple_tv.state.group_members
        assert "apple_tv_1" in final_members
        assert "sonos_1" in final_members
        # Protocol player ID should NOT appear in final state
        assert "airplay_sonos" not in final_members


class TestFinalSyncedToWithNativeProtocolParent:
    """Tests for __final_synced_to when sync parent is a native protocol player."""

    def test_synced_to_native_airplay_player(self, mock_mass: MagicMock) -> None:
        """Test that synced_to correctly shows native AirPlay player as parent.

        When a Sonos player's AirPlay protocol player is synced to a native AirPlay
        player (Apple TV), the Sonos's final synced_to should show the Apple TV.
        """
        controller = PlayerController(mock_mass)

        airplay_provider = MockProvider("airplay", instance_id="airplay", mass=mock_mass)
        sonos_provider = MockProvider("sonos", instance_id="sonos", mass=mock_mass)

        # Apple TV: native AirPlay PLAYER (the group leader)
        apple_tv = MockPlayer(airplay_provider, "apple_tv_1", "Apple TV")
        apple_tv._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        apple_tv._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        apple_tv._cache.clear()

        # Sonos visible player
        sonos_player = MockPlayer(sonos_provider, "sonos_1", "Sonos")
        sonos_player._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        sonos_player._cache.clear()

        # AirPlay protocol player for Sonos - synced to Apple TV
        sonos_airplay = MockPlayer(
            airplay_provider,
            "airplay_sonos",
            "Sonos (AirPlay)",
            player_type=PlayerType.PROTOCOL,
        )
        # Set group_members with Apple TV first to indicate synced_to Apple TV
        sonos_airplay._attr_group_members = ["apple_tv_1", "airplay_sonos"]
        sonos_airplay._cache.clear()
        sonos_airplay.set_protocol_parent_id("sonos_1")

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
            "apple_tv_1": apple_tv,
            "sonos_1": sonos_player,
            "airplay_sonos": sonos_airplay,
        }
        controller._player_throttlers = {
            "apple_tv_1": Throttler(1, 0.05),
            "sonos_1": Throttler(1, 0.05),
            "airplay_sonos": Throttler(1, 0.05),
        }

        apple_tv.update_state(signal_event=False)
        sonos_airplay.update_state(signal_event=False)
        sonos_player.update_state(signal_event=False)

        # Sonos's final synced_to should be Apple TV (visible player)
        assert sonos_player.state.synced_to == "apple_tv_1"


class TestUngroupTranslation:
    """Tests for translation when ungrouping from native protocol players."""

    def test_ungroup_translates_visible_to_protocol_id(self, mock_mass: MagicMock) -> None:
        """Test that ungrouping correctly translates visible ID to protocol ID.

        When ungrouping Sonos from Apple TV, the visible Sonos ID should be
        translated to its AirPlay protocol player ID for the removal.
        """
        controller = PlayerController(mock_mass)

        airplay_provider = MockProvider("airplay", instance_id="airplay", mass=mock_mass)
        sonos_provider = MockProvider("sonos", instance_id="sonos", mass=mock_mass)

        # Apple TV with Sonos's AirPlay protocol player in group_members
        apple_tv = MockPlayer(airplay_provider, "apple_tv_1", "Apple TV")
        apple_tv._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        apple_tv._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        apple_tv._attr_group_members = ["apple_tv_1", "airplay_sonos"]
        apple_tv._cache.clear()

        # Sonos visible player
        sonos_player = MockPlayer(sonos_provider, "sonos_1", "Sonos")
        sonos_player._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        sonos_player._cache.clear()

        # AirPlay protocol player for Sonos
        sonos_airplay = MockPlayer(
            airplay_provider,
            "airplay_sonos",
            "Sonos (AirPlay)",
            player_type=PlayerType.PROTOCOL,
        )
        sonos_airplay._cache.clear()
        sonos_airplay.set_protocol_parent_id("sonos_1")

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
            "apple_tv_1": apple_tv,
            "sonos_1": sonos_player,
            "airplay_sonos": sonos_airplay,
        }
        controller._player_throttlers = {
            "apple_tv_1": Throttler(1, 0.05),
            "sonos_1": Throttler(1, 0.05),
            "airplay_sonos": Throttler(1, 0.05),
        }

        sonos_airplay.update_state(signal_event=False)
        sonos_player.update_state(signal_event=False)
        apple_tv.update_state(signal_event=False)

        # Translate members for removal - visible ID should become protocol ID
        _protocol_members, native_members = controller._translate_members_to_remove_for_protocols(
            parent_player=apple_tv,
            player_ids=["sonos_1"],  # Visible player ID
            parent_protocol_player=None,
            parent_protocol_domain=None,
        )

        # Should translate to the protocol player ID for native removal
        assert "airplay_sonos" in native_members
        assert "sonos_1" not in native_members


class TestNativeProtocolDomainPlayerGrouping:
    """Tests for grouping with native protocol-domain players.

    This tests the scenario where a player's native provider domain IS the protocol
    domain (e.g., a sendspin web player with PlayerType.PLAYER and provider.domain="sendspin"),
    rather than having the protocol as a linked protocol player.
    """

    def test_native_protocol_player_groups_via_active_protocol(self, mock_mass: MagicMock) -> None:
        """Test grouping a native protocol player via the parent's active protocol.

        Scenario: Kantoor (has sendspin linked protocol, active) groups with
        Web player (native sendspin PlayerType.PLAYER).
        This should use Priority 2 (parent's active protocol).
        """
        controller = PlayerController(mock_mass)

        sonos_provider = MockProvider("sonos", instance_id="sonos", mass=mock_mass)
        sendspin_provider = MockProvider("sendspin", instance_id="sendspin", mass=mock_mass)

        # Kantoor: native Sonos player with sendspin as linked protocol
        kantoor = MockPlayer(sonos_provider, "sonos_kantoor", "Kantoor")
        kantoor._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        kantoor._cache.clear()

        # Sendspin protocol player linked to Kantoor
        sendspin_kantoor = MockPlayer(
            sendspin_provider,
            "sendspin_kantoor",
            "Kantoor (Sendspin)",
            player_type=PlayerType.PROTOCOL,
        )
        sendspin_kantoor._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        sendspin_kantoor._attr_can_group_with = {"sendspin"}
        sendspin_kantoor._cache.clear()
        sendspin_kantoor.set_protocol_parent_id("sonos_kantoor")

        # Web player: native sendspin player (PlayerType.PLAYER, standalone)
        web_player = MockPlayer(sendspin_provider, "sendspin_web", "Web (Chrome on Mac)")
        web_player._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        web_player._cache.clear()

        # Link sendspin protocol to Kantoor
        kantoor.set_linked_output_protocols(
            [
                OutputProtocol(
                    output_protocol_id="sendspin_kantoor",
                    name="Sendspin",
                    protocol_domain="sendspin",
                    priority=40,
                    available=True,
                )
            ]
        )

        mock_mass.players = controller
        controller._players = {
            "sonos_kantoor": kantoor,
            "sendspin_kantoor": sendspin_kantoor,
            "sendspin_web": web_player,
        }
        controller._player_throttlers = {
            "sonos_kantoor": Throttler(1, 0.05),
            "sendspin_kantoor": Throttler(1, 0.05),
            "sendspin_web": Throttler(1, 0.05),
        }

        # Update states
        sendspin_kantoor.update_state(signal_event=False)
        kantoor.update_state(signal_event=False)
        web_player.update_state(signal_event=False)

        # Group Web player with Kantoor, with sendspin as active protocol
        protocol_members, native_members, protocol_player, _ = (
            controller._translate_members_for_protocols(
                parent_player=kantoor,
                player_ids=["sendspin_web"],
                parent_protocol_player=sendspin_kantoor,
                parent_protocol_domain="sendspin",
            )
        )

        # Web player's own player_id should be in protocol_members
        assert len(protocol_members) == 1
        assert "sendspin_web" in protocol_members
        assert len(native_members) == 0
        assert protocol_player == sendspin_kantoor

    def test_native_protocol_player_groups_via_common_protocol(self, mock_mass: MagicMock) -> None:
        """Test grouping a native protocol player via common protocol search (Priority 4).

        Same scenario but without a pre-set active protocol — the common protocol
        search should find sendspin as the shared protocol.
        """
        controller = PlayerController(mock_mass)

        sonos_provider = MockProvider("sonos", instance_id="sonos", mass=mock_mass)
        sendspin_provider = MockProvider("sendspin", instance_id="sendspin", mass=mock_mass)

        # Kantoor: native Sonos player with sendspin as linked protocol
        kantoor = MockPlayer(sonos_provider, "sonos_kantoor", "Kantoor")
        kantoor._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        kantoor._cache.clear()

        # Sendspin protocol player linked to Kantoor
        sendspin_kantoor = MockPlayer(
            sendspin_provider,
            "sendspin_kantoor",
            "Kantoor (Sendspin)",
            player_type=PlayerType.PROTOCOL,
        )
        sendspin_kantoor._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        sendspin_kantoor._attr_can_group_with = {"sendspin"}
        sendspin_kantoor._cache.clear()
        sendspin_kantoor.set_protocol_parent_id("sonos_kantoor")

        # Web player: native sendspin player (PlayerType.PLAYER, standalone)
        web_player = MockPlayer(sendspin_provider, "sendspin_web", "Web (Chrome on Mac)")
        web_player._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        web_player._cache.clear()

        # Link sendspin protocol to Kantoor
        kantoor.set_linked_output_protocols(
            [
                OutputProtocol(
                    output_protocol_id="sendspin_kantoor",
                    name="Sendspin",
                    protocol_domain="sendspin",
                    priority=40,
                    available=True,
                )
            ]
        )

        mock_mass.players = controller
        controller._players = {
            "sonos_kantoor": kantoor,
            "sendspin_kantoor": sendspin_kantoor,
            "sendspin_web": web_player,
        }
        controller._player_throttlers = {
            "sonos_kantoor": Throttler(1, 0.05),
            "sendspin_kantoor": Throttler(1, 0.05),
            "sendspin_web": Throttler(1, 0.05),
        }

        # Update states
        sendspin_kantoor.update_state(signal_event=False)
        kantoor.update_state(signal_event=False)
        web_player.update_state(signal_event=False)

        # Group Web player with Kantoor, without pre-set active protocol
        protocol_members, native_members, protocol_player, protocol_domain = (
            controller._translate_members_for_protocols(
                parent_player=kantoor,
                player_ids=["sendspin_web"],
                parent_protocol_player=None,
                parent_protocol_domain=None,
            )
        )

        # Should find common sendspin protocol via Priority 4
        assert len(protocol_members) == 1
        assert "sendspin_web" in protocol_members
        assert len(native_members) == 0
        assert protocol_domain == "sendspin"
        assert protocol_player == sendspin_kantoor

    def test_ungroup_native_protocol_player(self, mock_mass: MagicMock) -> None:
        """Test ungrouping a native protocol player from a protocol-linked parent.

        When ungrouping a native sendspin web player from Kantoor's sendspin group,
        the web player's own player_id should be used for removal.
        """
        controller = PlayerController(mock_mass)

        sonos_provider = MockProvider("sonos", instance_id="sonos", mass=mock_mass)
        sendspin_provider = MockProvider("sendspin", instance_id="sendspin", mass=mock_mass)

        # Kantoor with sendspin linked protocol
        kantoor = MockPlayer(sonos_provider, "sonos_kantoor", "Kantoor")
        kantoor._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        kantoor._cache.clear()

        # Sendspin protocol player linked to Kantoor, with web player in group
        sendspin_kantoor = MockPlayer(
            sendspin_provider,
            "sendspin_kantoor",
            "Kantoor (Sendspin)",
            player_type=PlayerType.PROTOCOL,
        )
        sendspin_kantoor._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        sendspin_kantoor._attr_group_members = ["sendspin_kantoor", "sendspin_web"]
        sendspin_kantoor._cache.clear()
        sendspin_kantoor.set_protocol_parent_id("sonos_kantoor")

        # Web player: native sendspin player
        web_player = MockPlayer(sendspin_provider, "sendspin_web", "Web (Chrome on Mac)")
        web_player._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        web_player._cache.clear()

        kantoor.set_linked_output_protocols(
            [
                OutputProtocol(
                    output_protocol_id="sendspin_kantoor",
                    name="Sendspin",
                    protocol_domain="sendspin",
                    priority=40,
                    available=True,
                )
            ]
        )

        mock_mass.players = controller
        controller._players = {
            "sonos_kantoor": kantoor,
            "sendspin_kantoor": sendspin_kantoor,
            "sendspin_web": web_player,
        }
        controller._player_throttlers = {
            "sonos_kantoor": Throttler(1, 0.05),
            "sendspin_kantoor": Throttler(1, 0.05),
            "sendspin_web": Throttler(1, 0.05),
        }

        sendspin_kantoor.update_state(signal_event=False)
        kantoor.update_state(signal_event=False)
        web_player.update_state(signal_event=False)

        # Translate removal — web player's own player_id should be used
        protocol_members, native_members = controller._translate_members_to_remove_for_protocols(
            parent_player=kantoor,
            player_ids=["sendspin_web"],
            parent_protocol_player=sendspin_kantoor,
            parent_protocol_domain="sendspin",
        )

        # Web player's player_id should be in protocol removal list
        assert "sendspin_web" in protocol_members
        assert len(native_members) == 0

    def test_filter_protocol_members_accepts_native_protocol_player(
        self, mock_mass: MagicMock
    ) -> None:
        """Test that _filter_protocol_members accepts native protocol-domain players.

        A PlayerType.PLAYER with matching provider domain should pass through the
        filter, not just PlayerType.PROTOCOL players.
        """
        controller = PlayerController(mock_mass)

        sendspin_provider = MockProvider("sendspin", instance_id="sendspin", mass=mock_mass)

        # Protocol player (the parent's linked sendspin)
        sendspin_parent = MockPlayer(
            sendspin_provider,
            "sendspin_parent",
            "Parent (Sendspin)",
            player_type=PlayerType.PROTOCOL,
        )
        sendspin_parent._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        sendspin_parent._cache.clear()

        # Native sendspin web player (PlayerType.PLAYER)
        web_player = MockPlayer(sendspin_provider, "sendspin_web", "Web (Chrome on Mac)")
        web_player._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        web_player._cache.clear()

        # Another protocol type sendspin player
        sendspin_other = MockPlayer(
            sendspin_provider,
            "sendspin_other",
            "Other (Sendspin)",
            player_type=PlayerType.PROTOCOL,
        )
        sendspin_other._cache.clear()

        mock_mass.players = controller
        controller._players = {
            "sendspin_parent": sendspin_parent,
            "sendspin_web": web_player,
            "sendspin_other": sendspin_other,
        }
        controller._player_throttlers = {
            "sendspin_parent": Throttler(1, 0.05),
            "sendspin_web": Throttler(1, 0.05),
            "sendspin_other": Throttler(1, 0.05),
        }

        sendspin_parent.update_state(signal_event=False)
        web_player.update_state(signal_event=False)
        sendspin_other.update_state(signal_event=False)

        # Both native and protocol players should pass through the filter
        filtered = controller._filter_protocol_members(
            ["sendspin_web", "sendspin_other"],
            sendspin_parent,
        )

        assert "sendspin_web" in filtered
        assert "sendspin_other" in filtered
        assert len(filtered) == 2


class TestEnrichPlayerIdentifiers:
    """Tests for MAC address enrichment via ARP lookup."""

    @pytest.mark.asyncio
    async def test_no_ip_skips_enrichment(self, mock_mass: MagicMock) -> None:
        """Test that enrichment is skipped when no IP address is available."""
        provider = MockProvider("sendspin", mass=mock_mass)
        # Sendspin bridge player has MAC but no IP
        player = MockPlayer(
            provider,
            "spb_62e5974593d3",
            "Apple TV (Sendspin)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "62:E5:97:45:93:D3"},
        )

        await enrich_device_mac_address(player.device_info)

        # MAC should remain unchanged (no IP = no ARP lookup)
        assert player.device_info.identifiers[IdentifierType.MAC_ADDRESS] == "62:E5:97:45:93:D3"

    @pytest.mark.asyncio
    async def test_valid_hardware_mac_skips_enrichment(self, mock_mass: MagicMock) -> None:
        """Test that a valid, non-locally-administered MAC skips enrichment."""
        provider = MockProvider("sonos", mass=mock_mass)
        player = MockPlayer(
            provider,
            "sonos_123",
            "Sonos Speaker",
            player_type=PlayerType.PLAYER,
            identifiers={
                IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A0",
                IdentifierType.IP_ADDRESS: "192.168.1.100",
            },
        )

        await enrich_device_mac_address(player.device_info)

        # MAC should remain unchanged (valid hardware MAC, not locally administered)
        assert player.device_info.identifiers[IdentifierType.MAC_ADDRESS] == "54:78:C9:E6:0D:A0"

    @pytest.mark.asyncio
    async def test_locally_administered_bit_difference_replaces_mac(
        self, mock_mass: MagicMock
    ) -> None:
        """Test that ARP MAC replaces reported MAC when only the locally-administered bit differs.

        Example: AirPlay reports 56:78:C9:E6:0D:A0, ARP resolves 54:78:C9:E6:0D:A0.
        These differ only in bit 1 of the first octet (locally-administered bit).
        """
        provider = MockProvider("airplay", mass=mock_mass)
        player = MockPlayer(
            provider,
            "ap_5678c9e60da0",
            "WiiM Pro (AirPlay)",
            player_type=PlayerType.PROTOCOL,
            identifiers={
                IdentifierType.MAC_ADDRESS: "56:78:C9:E6:0D:A0",
                IdentifierType.IP_ADDRESS: "192.168.1.50",
            },
        )

        with patch(
            "music_assistant.helpers.util.resolve_real_mac_address",
            new_callable=AsyncMock,
            return_value="54:78:C9:E6:0D:A0",
        ):
            await enrich_device_mac_address(player.device_info)

        # MAC should be replaced with the ARP-resolved hardware MAC
        assert player.device_info.identifiers[IdentifierType.MAC_ADDRESS] == "54:78:C9:E6:0D:A0"

    @pytest.mark.asyncio
    async def test_completely_different_mac_replaced_by_arp(self, mock_mass: MagicMock) -> None:
        """Test that ARP result always replaces a locally-administered MAC.

        Even when the ARP MAC is completely different from the reported MAC
        (e.g., Apple devices with random private MACs), the ARP result is used
        because it's the hardware truth that reliably unifies protocols.
        Sendspin bridges match via AIRPLAY_ID/CAST_UUID, not MAC.
        """
        provider = MockProvider("airplay", mass=mock_mass)
        player = MockPlayer(
            provider,
            "ap62e5974593d3",
            "Apple TV (AirPlay)",
            player_type=PlayerType.PLAYER,
            identifiers={
                IdentifierType.MAC_ADDRESS: "62:E5:97:45:93:D3",
                IdentifierType.IP_ADDRESS: "192.168.1.200",
            },
        )

        with patch(
            "music_assistant.helpers.util.resolve_real_mac_address",
            new_callable=AsyncMock,
            return_value="C0:95:6D:51:34:E0",
        ):
            await enrich_device_mac_address(player.device_info)

        # MAC should be replaced with ARP result (hardware truth)
        assert player.device_info.identifiers[IdentifierType.MAC_ADDRESS] == "C0:95:6D:51:34:E0"

    @pytest.mark.asyncio
    async def test_no_reported_mac_uses_arp_result(self, mock_mass: MagicMock) -> None:
        """Test that ARP MAC is used when no MAC was reported at all."""
        provider = MockProvider("chromecast", mass=mock_mass)
        player = MockPlayer(
            provider,
            "cc_123",
            "Chromecast",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.IP_ADDRESS: "192.168.1.75"},
        )

        with patch(
            "music_assistant.helpers.util.resolve_real_mac_address",
            new_callable=AsyncMock,
            return_value="AA:BB:CC:DD:EE:FF",
        ):
            await enrich_device_mac_address(player.device_info)

        # MAC should be set from ARP since there was none before
        assert player.device_info.identifiers[IdentifierType.MAC_ADDRESS] == "AA:BB:CC:DD:EE:FF"

    @pytest.mark.asyncio
    async def test_invalid_mac_replaced_by_arp(self, mock_mass: MagicMock) -> None:
        """Test that an invalid MAC (00:00:00:00:00:00) is replaced by ARP result."""
        provider = MockProvider("dlna", mass=mock_mass)
        player = MockPlayer(
            provider,
            "dlna_123",
            "DLNA Device",
            player_type=PlayerType.PROTOCOL,
            identifiers={
                IdentifierType.MAC_ADDRESS: "00:00:00:00:00:00",
                IdentifierType.IP_ADDRESS: "192.168.1.60",
            },
        )

        with patch(
            "music_assistant.helpers.util.resolve_real_mac_address",
            new_callable=AsyncMock,
            return_value="11:22:33:44:55:66",
        ):
            await enrich_device_mac_address(player.device_info)

        # Invalid MAC should be replaced
        assert player.device_info.identifiers[IdentifierType.MAC_ADDRESS] == "11:22:33:44:55:66"

    @pytest.mark.asyncio
    async def test_arp_returns_none_no_change(self, mock_mass: MagicMock) -> None:
        """Test that MAC is unchanged when ARP lookup returns None."""
        provider = MockProvider("airplay", mass=mock_mass)
        player = MockPlayer(
            provider,
            "ap_123",
            "AirPlay Device",
            player_type=PlayerType.PROTOCOL,
            identifiers={
                IdentifierType.MAC_ADDRESS: "62:E5:97:45:93:D3",
                IdentifierType.IP_ADDRESS: "192.168.1.100",
            },
        )

        with patch(
            "music_assistant.helpers.util.resolve_real_mac_address",
            new_callable=AsyncMock,
            return_value=None,
        ):
            await enrich_device_mac_address(player.device_info)

        # MAC should remain unchanged
        assert player.device_info.identifiers[IdentifierType.MAC_ADDRESS] == "62:E5:97:45:93:D3"

    @pytest.mark.asyncio
    async def test_ipv6_mapped_ipv4_normalized(self, mock_mass: MagicMock) -> None:
        """Test that IPv6-mapped IPv4 addresses are normalized."""
        provider = MockProvider("airplay", mass=mock_mass)
        player = MockPlayer(
            provider,
            "ap_123",
            "AirPlay Device",
            player_type=PlayerType.PROTOCOL,
            identifiers={
                IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A0",
                IdentifierType.IP_ADDRESS: "::ffff:192.168.1.100",
            },
        )

        await enrich_device_mac_address(player.device_info)

        # IP should be normalized to IPv4
        assert player.device_info.identifiers[IdentifierType.IP_ADDRESS] == "192.168.1.100"


def _create_universal_player(
    mock_mass: MagicMock,
    player_id: str,
    name: str,
    protocol_player_ids: list[str],
    identifiers: dict[IdentifierType, str] | None = None,
) -> UniversalPlayer:
    """Create a UniversalPlayer for testing."""
    universal_provider = create_mock_universal_provider(mock_mass)
    # Set up config so provider.instance_id works
    provider_config = MagicMock()
    provider_config.instance_id = "universal_player"
    provider_config.name = None
    universal_provider.config = provider_config
    mock_mass.config.get_base_player_config.return_value = create_mock_config(name)

    device_info = DeviceInfo(
        model="Universal Player",
        manufacturer="Music Assistant",
    )
    if identifiers:
        for conn_type, value in identifiers.items():
            device_info.add_identifier(conn_type, value)

    player = UniversalPlayer(
        provider=universal_provider,
        player_id=player_id,
        name=name,
        device_info=device_info,
        protocol_player_ids=protocol_player_ids,
    )
    player._attr_available = True
    player._cache.clear()
    player.set_initialized()
    return player


class TestProtocolToUniversalIdentifierFallback:
    """Tests for Fix 1: identifier-based fallback matching for Universal Players.

    When a new protocol player (like Sendspin bridge) registers and its ID isn't
    in the Universal Player's stored _protocol_player_ids list, the system should
    fall back to identifier matching (MAC address) to find the correct parent.
    """

    def test_new_protocol_matches_universal_by_mac(self, mock_mass: MagicMock) -> None:
        """Test that a new protocol player matches a Universal Player by MAC address."""
        controller = PlayerController(mock_mass)

        # No cached parent_id for the sendspin player
        def mock_config_get(_key: str, default: str | None = None) -> str | None:
            return default

        mock_mass.config.get.side_effect = mock_config_get

        # Create existing universal player with known MAC (from AirPlay)
        universal = _create_universal_player(
            mock_mass,
            "up_62e5974593d3",
            "Apple TV",
            protocol_player_ids=["ap62e5974593d3"],
            identifiers={IdentifierType.MAC_ADDRESS: "62:E5:97:45:93:D3"},
        )

        # Create AirPlay protocol player (already linked)
        airplay_provider = MockProvider("airplay", mass=mock_mass)
        airplay_player = MockPlayer(
            airplay_provider,
            "ap62e5974593d3",
            "Apple TV (AirPlay)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "62:E5:97:45:93:D3"},
        )
        airplay_player.set_protocol_parent_id("up_62e5974593d3")

        # Create NEW Sendspin bridge player with same MAC (not yet in _protocol_player_ids)
        sendspin_provider = MockProvider("sendspin", mass=mock_mass)
        sendspin_player = MockPlayer(
            sendspin_provider,
            "spb_62e5974593d3",
            "Apple TV (Sendspin)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "62:E5:97:45:93:D3"},
        )

        mock_mass.players = controller
        controller._players = {
            "up_62e5974593d3": universal,
            "ap62e5974593d3": airplay_player,
            "spb_62e5974593d3": sendspin_player,
        }
        controller._player_throttlers = {
            "up_62e5974593d3": Throttler(1, 0.05),
            "ap62e5974593d3": Throttler(1, 0.05),
            "spb_62e5974593d3": Throttler(1, 0.05),
        }

        # Initialize players so all_players() returns them
        airplay_player.set_initialized()
        sendspin_player.set_initialized()

        # Try to link Sendspin player
        controller._try_link_protocol_to_native(sendspin_player)

        # Should be linked to the universal player via identifier matching
        assert sendspin_player.protocol_parent_id == "up_62e5974593d3"

        # Should be added to universal player's protocol list
        assert "spb_62e5974593d3" in universal._protocol_player_ids

        # Should have a linked output protocol
        assert any(
            link.output_protocol_id == "spb_62e5974593d3"
            for link in universal.linked_output_protocols
        )

    def test_new_protocol_no_match_skips_universal(self, mock_mass: MagicMock) -> None:
        """Test that a protocol player with different MAC doesn't match the wrong Universal."""
        controller = PlayerController(mock_mass)

        # No cached parent_id
        def mock_config_get(_key: str, default: str | None = None) -> str | None:
            return default

        mock_mass.config.get.side_effect = mock_config_get

        # Create universal player for Device A
        universal = _create_universal_player(
            mock_mass,
            "up_aabbccddeeff",
            "Device A",
            protocol_player_ids=["ap_aabbccddeeff"],
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )

        # Create protocol player for a completely different device
        sendspin_provider = MockProvider("sendspin", mass=mock_mass)
        sendspin_player = MockPlayer(
            sendspin_provider,
            "spb_112233445566",
            "Device B (Sendspin)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "11:22:33:44:55:66"},
        )

        mock_mass.players = controller
        controller._players = {
            "up_aabbccddeeff": universal,
            "spb_112233445566": sendspin_player,
        }
        controller._player_throttlers = {
            "up_aabbccddeeff": Throttler(1, 0.05),
            "spb_112233445566": Throttler(1, 0.05),
        }

        sendspin_player.set_initialized()

        controller._try_link_protocol_to_native(sendspin_player)

        # Should NOT be linked to the wrong universal player
        assert sendspin_player.protocol_parent_id != "up_aabbccddeeff"

    def test_known_protocol_id_still_works(self, mock_mass: MagicMock) -> None:
        """Test that the existing path (player_id in _protocol_player_ids) still works."""
        controller = PlayerController(mock_mass)

        # No cached parent_id
        def mock_config_get(_key: str, default: str | None = None) -> str | None:
            return default

        mock_mass.config.get.side_effect = mock_config_get

        airplay_provider = MockProvider("airplay", mass=mock_mass)
        airplay_player = MockPlayer(
            airplay_provider,
            "ap_aabbccddeeff",
            "AirPlay Device",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )

        # Universal player already has this player_id in its list
        universal = _create_universal_player(
            mock_mass,
            "up_aabbccddeeff",
            "Device",
            protocol_player_ids=["ap_aabbccddeeff"],
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )

        mock_mass.players = controller
        controller._players = {
            "up_aabbccddeeff": universal,
            "ap_aabbccddeeff": airplay_player,
        }
        controller._player_throttlers = {
            "up_aabbccddeeff": Throttler(1, 0.05),
            "ap_aabbccddeeff": Throttler(1, 0.05),
        }

        airplay_player.set_initialized()

        controller._try_link_protocol_to_native(airplay_player)

        # Should be linked via the existing stored ID path
        assert airplay_player.protocol_parent_id == "up_aabbccddeeff"


class TestCachedParentIdentifierCopying:
    """Tests for Fix 2: identifier copying when restoring cached parent links.

    When a protocol player reconnects and links to a Universal Player via the
    cached_parent_id fast path, identifiers must be copied to the Universal Player.
    Restored Universal Players start with empty identifiers, so without this copy,
    subsequent protocol players (like Sendspin bridges) cannot match by identifiers.
    """

    def test_identifiers_copied_on_cached_parent_restore(self, mock_mass: MagicMock) -> None:
        """Test that identifiers are copied from protocol player to Universal Player on restore."""
        controller = PlayerController(mock_mass)

        # Mock config to return cached parent_id
        def mock_config_get(key: str, default: str | None = None) -> str | None:
            if "protocol_parent_id" in str(key):
                return "up_62e5974593d3"
            return default

        mock_mass.config.get.side_effect = mock_config_get

        # Create restored universal player with EMPTY identifiers (simulates restart)
        universal = _create_universal_player(
            mock_mass,
            "up_62e5974593d3",
            "Apple TV",
            protocol_player_ids=["ap62e5974593d3"],
            # No identifiers - simulates restored state
        )

        # Create AirPlay protocol player reconnecting with its identifiers
        airplay_provider = MockProvider("airplay", mass=mock_mass)
        airplay_player = MockPlayer(
            airplay_provider,
            "ap62e5974593d3",
            "Apple TV (AirPlay)",
            player_type=PlayerType.PROTOCOL,
            identifiers={
                IdentifierType.MAC_ADDRESS: "62:E5:97:45:93:D3",
                IdentifierType.IP_ADDRESS: "192.168.1.200",
            },
        )

        mock_mass.players = controller
        controller._players = {
            "up_62e5974593d3": universal,
            "ap62e5974593d3": airplay_player,
        }
        controller._player_throttlers = {
            "up_62e5974593d3": Throttler(1, 0.05),
            "ap62e5974593d3": Throttler(1, 0.05),
        }

        # Link protocol player via cached parent path
        controller._try_link_protocol_to_native(airplay_player)

        # Should be linked
        assert airplay_player.protocol_parent_id == "up_62e5974593d3"

        # Universal player should now have the MAC from the protocol player
        assert (
            universal.device_info.identifiers.get(IdentifierType.MAC_ADDRESS) == "62:E5:97:45:93:D3"
        )

    def test_sendspin_matches_after_identifier_copy(self, mock_mass: MagicMock) -> None:
        """Test the full scenario: AirPlay restores, copies identifiers, Sendspin matches.

        AirPlay restores via cache, copies identifiers to the Universal Player,
        then the Sendspin bridge matches the Universal Player by MAC.
        This is the end-to-end test for the combined Fix 2 + Fix 1 interaction.
        """
        controller = PlayerController(mock_mass)

        # Step 1: Mock config - AirPlay has cached parent, Sendspin does not
        def mock_config_get(key: str, default: str | None = None) -> str | None:
            if "ap62e5974593d3" in str(key) and "protocol_parent_id" in str(key):
                return "up_62e5974593d3"
            return default

        mock_mass.config.get.side_effect = mock_config_get

        # Create restored universal player with empty identifiers
        universal = _create_universal_player(
            mock_mass,
            "up_62e5974593d3",
            "Apple TV",
            protocol_player_ids=["ap62e5974593d3"],
        )

        # Create AirPlay protocol player
        airplay_provider = MockProvider("airplay", mass=mock_mass)
        airplay_player = MockPlayer(
            airplay_provider,
            "ap62e5974593d3",
            "Apple TV (AirPlay)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "62:E5:97:45:93:D3"},
        )

        # Create Sendspin bridge player with same MAC
        sendspin_provider = MockProvider("sendspin", mass=mock_mass)
        sendspin_player = MockPlayer(
            sendspin_provider,
            "spb_62e5974593d3",
            "Apple TV (Sendspin)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "62:E5:97:45:93:D3"},
        )

        mock_mass.players = controller
        controller._players = {
            "up_62e5974593d3": universal,
            "ap62e5974593d3": airplay_player,
            "spb_62e5974593d3": sendspin_player,
        }
        controller._player_throttlers = {
            "up_62e5974593d3": Throttler(1, 0.05),
            "ap62e5974593d3": Throttler(1, 0.05),
            "spb_62e5974593d3": Throttler(1, 0.05),
        }

        # Initialize players so all_players() returns them
        airplay_player.set_initialized()
        sendspin_player.set_initialized()

        # Step 2: AirPlay reconnects first - links via cached parent_id
        controller._try_link_protocol_to_native(airplay_player)

        # Verify AirPlay linked and identifiers copied
        assert airplay_player.protocol_parent_id == "up_62e5974593d3"
        assert (
            universal.device_info.identifiers.get(IdentifierType.MAC_ADDRESS) == "62:E5:97:45:93:D3"
        )

        # Step 3: Sendspin registers next - should match Universal Player by MAC
        controller._try_link_protocol_to_native(sendspin_player)

        # Sendspin should be linked to the SAME universal player
        assert sendspin_player.protocol_parent_id == "up_62e5974593d3"
        assert "spb_62e5974593d3" in universal._protocol_player_ids

    def test_non_universal_parent_skips_identifier_copy(self, mock_mass: MagicMock) -> None:
        """Test that identifier copy is only done for Universal Players, not native players."""
        controller = PlayerController(mock_mass)

        # Mock config to return cached parent_id pointing to a native player
        def mock_config_get(key: str, default: str | None = None) -> str | None:
            if "protocol_parent_id" in str(key):
                return "sonos_123"
            return default

        mock_mass.config.get.side_effect = mock_config_get

        # Create native Sonos player
        sonos_provider = MockProvider("sonos", mass=mock_mass)
        sonos_player = MockPlayer(
            sonos_provider,
            "sonos_123",
            "Sonos Speaker",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        original_mac = sonos_player.device_info.identifiers[IdentifierType.MAC_ADDRESS]

        # Create DLNA protocol player with different MAC
        dlna_provider = MockProvider("dlna", mass=mock_mass)
        dlna_player = MockPlayer(
            dlna_provider,
            "dlna_123",
            "Sonos DLNA",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "11:22:33:44:55:66"},
        )

        mock_mass.players = controller
        controller._players = {
            "sonos_123": sonos_player,
            "dlna_123": dlna_player,
        }
        controller._player_throttlers = {
            "sonos_123": Throttler(1, 0.05),
            "dlna_123": Throttler(1, 0.05),
        }

        controller._try_link_protocol_to_native(dlna_player)

        # Should be linked
        assert dlna_player.protocol_parent_id == "sonos_123"

        # Native player's MAC should NOT be overwritten
        assert sonos_player.device_info.identifiers[IdentifierType.MAC_ADDRESS] == original_mac


class TestEnrichAndMatchIntegration:
    """Integration tests for the interaction between MAC enrichment and identifier matching.

    These tests verify that Fix 3 (preserving original MAC when ARP resolves a
    completely different address) works correctly with the identifier matching system.
    """

    @pytest.mark.asyncio
    async def test_apple_device_sendspin_matches_via_airplay_id(self, mock_mass: MagicMock) -> None:
        """Test the full Apple device scenario end-to-end.

        1. AirPlay player registers with private MAC 62:E5:97:45:93:D3
        2. ARP resolves to hardware MAC C0:95:6D:51:34:E0 (always used)
        3. Sendspin bridge matches via AIRPLAY_ID, not MAC
        """
        controller = PlayerController(mock_mass)

        # Create AirPlay player (Apple TV - native PLAYER type)
        airplay_provider = MockProvider("airplay", mass=mock_mass)
        airplay_player = MockPlayer(
            airplay_provider,
            "ap62e5974593d3",
            "Apple TV",
            player_type=PlayerType.PLAYER,
            identifiers={
                IdentifierType.MAC_ADDRESS: "62:E5:97:45:93:D3",
                IdentifierType.AIRPLAY_ID: "62:E5:97:45:93:D3",
                IdentifierType.IP_ADDRESS: "192.168.1.200",
            },
        )

        # ARP enrichment replaces MAC with hardware truth
        with patch(
            "music_assistant.helpers.util.resolve_real_mac_address",
            new_callable=AsyncMock,
            return_value="C0:95:6D:51:34:E0",
        ):
            await enrich_device_mac_address(airplay_player.device_info)

        # MAC replaced with ARP result
        assert (
            airplay_player.device_info.identifiers[IdentifierType.MAC_ADDRESS]
            == "C0:95:6D:51:34:E0"
        )

        # Create Sendspin bridge player with AIRPLAY_ID (how Sendspin actually matches)
        sendspin_provider = MockProvider("sendspin", mass=mock_mass)
        sendspin_player = MockPlayer(
            sendspin_provider,
            "spb_62e5974593d3",
            "Apple TV (Sendspin)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.AIRPLAY_ID: "62:E5:97:45:93:D3"},
        )

        # Identifiers match via AIRPLAY_ID
        assert controller._identifiers_match(airplay_player, sendspin_player, "sendspin") is True

    @pytest.mark.asyncio
    async def test_wiim_device_mac_replaced_still_matches(self, mock_mass: MagicMock) -> None:
        """Test that WiiM devices match after ARP enrichment.

        WiiM AirPlay reports 56:78:C9:E6:0D:A0 (locally administered), ARP resolves to
        54:78:C9:E6:0D:A0 (hardware). The MAC is always replaced with the ARP result.
        """
        controller = PlayerController(mock_mass)

        # Create AirPlay player (WiiM - PROTOCOL type)
        airplay_provider = MockProvider("airplay", mass=mock_mass)
        airplay_player = MockPlayer(
            airplay_provider,
            "ap_5678c9e60da0",
            "WiiM Pro (AirPlay)",
            player_type=PlayerType.PROTOCOL,
            identifiers={
                IdentifierType.MAC_ADDRESS: "56:78:C9:E6:0D:A0",
                IdentifierType.IP_ADDRESS: "192.168.1.50",
            },
        )

        # ARP resolves MAC that differs only in locally-administered bit
        with patch(
            "music_assistant.helpers.util.resolve_real_mac_address",
            new_callable=AsyncMock,
            return_value="54:78:C9:E6:0D:A0",
        ):
            await enrich_device_mac_address(airplay_player.device_info)

        # MAC should be replaced (only bit difference)
        assert (
            airplay_player.device_info.identifiers[IdentifierType.MAC_ADDRESS]
            == "54:78:C9:E6:0D:A0"
        )

        # DLNA player also reports hardware MAC
        dlna_provider = MockProvider("dlna", mass=mock_mass)
        dlna_player = MockPlayer(
            dlna_provider,
            "dlna_5478c9e60da0",
            "WiiM Pro (DLNA)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A0"},
        )

        # Should match by MAC
        assert controller._identifiers_match(airplay_player, dlna_player, "dlna") is True


class TestDuplicateProtocolPrevention:
    """Tests for preventing duplicate protocol domain linking.

    When multiple instances of the same protocol (e.g., two AirPlay or ShairPort-Sync
    instances, or multi-zone Bluesound players) run on the same host, they share the
    same IP/MAC. These must NOT all bind to the same parent player — each should get
    its own separate universal player.
    """

    def test_parent_has_active_protocol_from_domain_empty(self, mock_mass: MagicMock) -> None:
        """Test helper returns False when parent has no linked protocols."""
        controller = PlayerController(mock_mass)

        sonos_provider = MockProvider("sonos")
        sonos_player = MockPlayer(
            sonos_provider, "sonos_1", "Sonos Speaker", player_type=PlayerType.PLAYER
        )

        assert controller._parent_has_active_protocol_from_domain(sonos_player, "airplay") is False

    def test_parent_has_active_protocol_from_domain_true(self, mock_mass: MagicMock) -> None:
        """Test helper returns True when parent has an active link from the given domain."""
        controller = PlayerController(mock_mass)

        sonos_provider = MockProvider("sonos")
        sonos_player = MockPlayer(
            sonos_provider, "sonos_1", "Sonos Speaker", player_type=PlayerType.PLAYER
        )

        airplay_provider = MockProvider("airplay")
        airplay_player = MockPlayer(
            airplay_provider,
            "ap_1",
            "AirPlay 1",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        airplay_player._attr_available = True
        airplay_player._cache.clear()
        airplay_player.update_state(signal_event=False)

        # Register players
        controller._players = {
            "sonos_1": sonos_player,
            "ap_1": airplay_player,
        }
        controller._player_throttlers = {
            "sonos_1": Throttler(1, 0.05),
            "ap_1": Throttler(1, 0.05),
        }
        airplay_player.set_initialized()
        sonos_player.set_initialized()

        # Add a protocol link
        controller._add_protocol_link(sonos_player, airplay_player, "airplay")

        assert controller._parent_has_active_protocol_from_domain(sonos_player, "airplay") is True
        assert controller._parent_has_active_protocol_from_domain(sonos_player, "dlna") is False

    def test_parent_has_active_protocol_excludes_player_id(self, mock_mass: MagicMock) -> None:
        """Test helper excludes the specified player_id from the check."""
        controller = PlayerController(mock_mass)

        sonos_provider = MockProvider("sonos")
        sonos_player = MockPlayer(
            sonos_provider, "sonos_1", "Sonos Speaker", player_type=PlayerType.PLAYER
        )

        airplay_provider = MockProvider("airplay")
        airplay_player = MockPlayer(
            airplay_provider,
            "ap_1",
            "AirPlay 1",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        airplay_player._attr_available = True
        airplay_player._cache.clear()
        airplay_player.update_state(signal_event=False)

        controller._players = {
            "sonos_1": sonos_player,
            "ap_1": airplay_player,
        }
        controller._player_throttlers = {
            "sonos_1": Throttler(1, 0.05),
            "ap_1": Throttler(1, 0.05),
        }
        airplay_player.set_initialized()
        sonos_player.set_initialized()

        controller._add_protocol_link(sonos_player, airplay_player, "airplay")

        # Should return False when the only match is the excluded player
        assert (
            controller._parent_has_active_protocol_from_domain(
                sonos_player, "airplay", exclude_player_id="ap_1"
            )
            is False
        )

    def test_add_protocol_link_refuses_duplicate_domain(self, mock_mass: MagicMock) -> None:
        """Test that _add_protocol_link refuses when domain already has an active link."""
        controller = PlayerController(mock_mass)

        sonos_provider = MockProvider("sonos")
        sonos_player = MockPlayer(
            sonos_provider, "sonos_1", "Sonos Speaker", player_type=PlayerType.PLAYER
        )

        airplay_provider = MockProvider("airplay")
        ap1 = MockPlayer(
            airplay_provider,
            "ap_1",
            "AirPlay 1",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        ap2 = MockPlayer(
            airplay_provider,
            "ap_2",
            "AirPlay 2",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )

        # Both available
        ap1._attr_available = True
        ap1._cache.clear()
        ap1.update_state(signal_event=False)
        ap2._attr_available = True
        ap2._cache.clear()
        ap2.update_state(signal_event=False)

        controller._players = {
            "sonos_1": sonos_player,
            "ap_1": ap1,
            "ap_2": ap2,
        }
        controller._player_throttlers = {
            "sonos_1": Throttler(1, 0.05),
            "ap_1": Throttler(1, 0.05),
            "ap_2": Throttler(1, 0.05),
        }
        ap1.set_initialized()
        ap2.set_initialized()
        sonos_player.set_initialized()

        # Link first AirPlay - should succeed
        controller._add_protocol_link(sonos_player, ap1, "airplay")
        assert ap1.protocol_parent_id == "sonos_1"
        assert len(sonos_player.linked_output_protocols) == 1

        # Link second AirPlay - should be refused (domain already active)
        controller._add_protocol_link(sonos_player, ap2, "airplay")
        assert ap2.protocol_parent_id is None
        assert len(sonos_player.linked_output_protocols) == 1
        assert sonos_player.linked_output_protocols[0].output_protocol_id == "ap_1"

    def test_add_protocol_link_allows_different_domains(self, mock_mass: MagicMock) -> None:
        """Test that _add_protocol_link allows links from different protocol domains."""
        controller = PlayerController(mock_mass)

        sonos_provider = MockProvider("sonos")
        sonos_player = MockPlayer(
            sonos_provider, "sonos_1", "Sonos Speaker", player_type=PlayerType.PLAYER
        )

        airplay_provider = MockProvider("airplay")
        ap1 = MockPlayer(
            airplay_provider,
            "ap_1",
            "AirPlay",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )

        dlna_provider = MockProvider("dlna")
        dlna1 = MockPlayer(
            dlna_provider,
            "dlna_1",
            "DLNA",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )

        ap1._attr_available = True
        ap1._cache.clear()
        ap1.update_state(signal_event=False)
        dlna1._attr_available = True
        dlna1._cache.clear()
        dlna1.update_state(signal_event=False)

        controller._players = {
            "sonos_1": sonos_player,
            "ap_1": ap1,
            "dlna_1": dlna1,
        }
        controller._player_throttlers = {
            "sonos_1": Throttler(1, 0.05),
            "ap_1": Throttler(1, 0.05),
            "dlna_1": Throttler(1, 0.05),
        }
        ap1.set_initialized()
        dlna1.set_initialized()
        sonos_player.set_initialized()

        # Link AirPlay - should succeed
        controller._add_protocol_link(sonos_player, ap1, "airplay")
        assert ap1.protocol_parent_id == "sonos_1"

        # Link DLNA - should also succeed (different domain)
        controller._add_protocol_link(sonos_player, dlna1, "dlna")
        assert dlna1.protocol_parent_id == "sonos_1"
        assert len(sonos_player.linked_output_protocols) == 2

    def test_add_protocol_link_allows_replacing_inactive_domain(self, mock_mass: MagicMock) -> None:
        """Test that a new link can replace one from the same domain if old one is inactive."""
        controller = PlayerController(mock_mass)

        sonos_provider = MockProvider("sonos")
        sonos_player = MockPlayer(
            sonos_provider, "sonos_1", "Sonos Speaker", player_type=PlayerType.PLAYER
        )

        airplay_provider = MockProvider("airplay")
        ap1 = MockPlayer(
            airplay_provider,
            "ap_1",
            "AirPlay 1",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        ap2 = MockPlayer(
            airplay_provider,
            "ap_2",
            "AirPlay 2",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )

        ap1._attr_available = True
        ap1._cache.clear()
        ap1.update_state(signal_event=False)
        ap2._attr_available = True
        ap2._cache.clear()
        ap2.update_state(signal_event=False)

        controller._players = {
            "sonos_1": sonos_player,
            "ap_1": ap1,
            "ap_2": ap2,
        }
        controller._player_throttlers = {
            "sonos_1": Throttler(1, 0.05),
            "ap_1": Throttler(1, 0.05),
            "ap_2": Throttler(1, 0.05),
        }
        ap1.set_initialized()
        ap2.set_initialized()
        sonos_player.set_initialized()

        # Link first AirPlay
        controller._add_protocol_link(sonos_player, ap1, "airplay")
        assert ap1.protocol_parent_id == "sonos_1"

        # Make first AirPlay unavailable (simulates disconnection)
        ap1._attr_available = False
        ap1._cache.clear()
        ap1.update_state(signal_event=False)

        # Link second AirPlay - should succeed since first is inactive
        controller._add_protocol_link(sonos_player, ap2, "airplay")
        assert ap2.protocol_parent_id == "sonos_1"

    def test_try_link_protocol_to_native_skips_duplicate_domain(self, mock_mass: MagicMock) -> None:
        """Test that _try_link_protocol_to_native falls through when domain is duplicate."""
        controller = PlayerController(mock_mass)

        sonos_provider = MockProvider("sonos")
        sonos_player = MockPlayer(
            sonos_provider,
            "sonos_1",
            "Sonos Speaker",
            player_type=PlayerType.PLAYER,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )

        airplay_provider = MockProvider("airplay")
        ap1 = MockPlayer(
            airplay_provider,
            "ap_1",
            "AirPlay 1",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        ap2 = MockPlayer(
            airplay_provider,
            "ap_2",
            "AirPlay 2",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )

        ap1._attr_available = True
        ap1._cache.clear()
        ap1.update_state(signal_event=False)
        ap2._attr_available = True
        ap2._cache.clear()
        ap2.update_state(signal_event=False)

        controller._players = {
            "sonos_1": sonos_player,
            "ap_1": ap1,
            "ap_2": ap2,
        }
        controller._player_throttlers = {
            "sonos_1": Throttler(1, 0.05),
            "ap_1": Throttler(1, 0.05),
            "ap_2": Throttler(1, 0.05),
        }
        ap1.set_initialized()
        ap2.set_initialized()
        sonos_player.set_initialized()

        # Link first AirPlay to native player
        controller._add_protocol_link(sonos_player, ap1, "airplay")
        assert ap1.protocol_parent_id == "sonos_1"

        # Mock _schedule_protocol_evaluation to track if it's called
        with patch.object(controller, "_schedule_protocol_evaluation") as mock_schedule:
            controller._try_link_protocol_to_native(ap2)

        # ap2 should NOT have been linked to sonos
        assert ap2.protocol_parent_id is None
        # ap2 should be scheduled for delayed evaluation (which will create its own
        # universal player)
        mock_schedule.assert_called_once_with(ap2)

    def test_try_link_protocols_to_native_skips_duplicate_domain(
        self, mock_mass: MagicMock
    ) -> None:
        """Test that _try_link_protocols_to_native skips protocol players with duplicate domain."""
        controller = PlayerController(mock_mass)

        # Config.get returns {} for CONF_PLAYERS dict lookups
        mock_mass.config.get = MagicMock(side_effect=lambda _key, default=None: default or {})

        sonos_provider = MockProvider("sonos")
        sonos_player = MockPlayer(
            sonos_provider,
            "sonos_1",
            "Sonos Speaker",
            player_type=PlayerType.PLAYER,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )

        airplay_provider = MockProvider("airplay")
        ap1 = MockPlayer(
            airplay_provider,
            "ap_1",
            "AirPlay 1",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        ap2 = MockPlayer(
            airplay_provider,
            "ap_2",
            "AirPlay 2",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )

        dlna_provider = MockProvider("dlna")
        dlna_player = MockPlayer(
            dlna_provider,
            "dlna_1",
            "DLNA",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )

        ap1._attr_available = True
        ap1._cache.clear()
        ap1.update_state(signal_event=False)
        ap2._attr_available = True
        ap2._cache.clear()
        ap2.update_state(signal_event=False)
        dlna_player._attr_available = True
        dlna_player._cache.clear()
        dlna_player.update_state(signal_event=False)

        controller._players = {
            "ap_1": ap1,
            "ap_2": ap2,
            "dlna_1": dlna_player,
            "sonos_1": sonos_player,
        }
        controller._player_throttlers = {
            "ap_1": Throttler(1, 0.05),
            "ap_2": Throttler(1, 0.05),
            "dlna_1": Throttler(1, 0.05),
            "sonos_1": Throttler(1, 0.05),
        }
        ap1.set_initialized()
        ap2.set_initialized()
        dlna_player.set_initialized()
        sonos_player.set_initialized()

        # Call _try_link_protocols_to_native for the Sonos player
        # It should link ap1 and dlna but NOT ap2 (duplicate airplay domain)
        controller._try_link_protocols_to_native(sonos_player)

        # First AirPlay should be linked
        assert ap1.protocol_parent_id == "sonos_1"
        # DLNA should be linked (different domain)
        assert dlna_player.protocol_parent_id == "sonos_1"
        # Second AirPlay should NOT be linked (duplicate domain)
        assert ap2.protocol_parent_id is None
        assert len(sonos_player.linked_output_protocols) == 2

    def test_normal_multi_protocol_device_still_works(self, mock_mass: MagicMock) -> None:
        """Test that a device with AirPlay+DLNA (different protocols) still links correctly."""
        controller = PlayerController(mock_mass)

        # Config.get returns {} for CONF_PLAYERS dict lookups
        mock_mass.config.get = MagicMock(side_effect=lambda _key, default=None: default or {})

        sonos_provider = MockProvider("sonos")
        sonos_player = MockPlayer(
            sonos_provider,
            "sonos_1",
            "Sonos Speaker",
            player_type=PlayerType.PLAYER,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )

        airplay_provider = MockProvider("airplay")
        ap = MockPlayer(
            airplay_provider,
            "ap_1",
            "AirPlay",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )

        dlna_provider = MockProvider("dlna")
        dlna = MockPlayer(
            dlna_provider,
            "dlna_1",
            "DLNA",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )

        ap._attr_available = True
        ap._cache.clear()
        ap.update_state(signal_event=False)
        dlna._attr_available = True
        dlna._cache.clear()
        dlna.update_state(signal_event=False)

        controller._players = {
            "sonos_1": sonos_player,
            "ap_1": ap,
            "dlna_1": dlna,
        }
        controller._player_throttlers = {
            "sonos_1": Throttler(1, 0.05),
            "ap_1": Throttler(1, 0.05),
            "dlna_1": Throttler(1, 0.05),
        }
        ap.set_initialized()
        dlna.set_initialized()
        sonos_player.set_initialized()

        # Link both protocols
        controller._try_link_protocols_to_native(sonos_player)

        assert ap.protocol_parent_id == "sonos_1"
        assert dlna.protocol_parent_id == "sonos_1"
        assert len(sonos_player.linked_output_protocols) == 2

    def test_cached_parent_restore_refuses_duplicate_domain(self, mock_mass: MagicMock) -> None:
        """Test that restoring a cached parent is refused when domain already has active link."""
        controller = PlayerController(mock_mass)

        sonos_provider = MockProvider("sonos")
        sonos_player = MockPlayer(
            sonos_provider,
            "sonos_1",
            "Sonos Speaker",
            player_type=PlayerType.PLAYER,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )

        airplay_provider = MockProvider("airplay")
        ap1 = MockPlayer(
            airplay_provider,
            "ap_1",
            "AirPlay 1",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        ap2 = MockPlayer(
            airplay_provider,
            "ap_2",
            "AirPlay 2",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )

        ap1._attr_available = True
        ap1._cache.clear()
        ap1.update_state(signal_event=False)
        ap2._attr_available = True
        ap2._cache.clear()
        ap2.update_state(signal_event=False)

        controller._players = {
            "sonos_1": sonos_player,
            "ap_1": ap1,
            "ap_2": ap2,
        }
        controller._player_throttlers = {
            "sonos_1": Throttler(1, 0.05),
            "ap_1": Throttler(1, 0.05),
            "ap_2": Throttler(1, 0.05),
        }
        ap1.set_initialized()
        ap2.set_initialized()
        sonos_player.set_initialized()

        # Link first AirPlay
        controller._add_protocol_link(sonos_player, ap1, "airplay")
        assert ap1.protocol_parent_id == "sonos_1"

        # Simulate ap2 having a cached parent from previous session
        with (
            patch.object(
                controller,
                "_get_cached_protocol_parent_id",
                return_value="sonos_1",
            ),
            patch.object(controller, "_schedule_protocol_evaluation") as mock_schedule,
        ):
            controller._try_link_protocol_to_native(ap2)

        # ap2 should NOT have been linked (domain already active on sonos_1)
        assert ap2.protocol_parent_id is None
        # ap2 should be scheduled for delayed evaluation
        mock_schedule.assert_called_once_with(ap2)


class TestUniversalPlayerMerging:
    """Tests for merging universal players when identifiers overlap."""

    def test_merge_universal_players_on_identifier_copy(self, mock_mass: MagicMock) -> None:
        """Test that two universal players merge when they share a MAC after identifier copy.

        Simulates the Edifier scenario: DLNA creates a UUID-based universal player,
        AirPlay creates a MAC-based universal player. When ARP enrichment adds the
        real MAC to the DLNA player and it's copied to the DLNA universal player,
        the two universal players should merge because they now share the same MAC.
        """
        controller = PlayerController(mock_mass)
        up_provider = create_mock_universal_provider(mock_mass)

        # Universal player 1: from DLNA (UUID-based, no MAC initially)
        up1 = UniversalPlayer(
            provider=up_provider,
            player_id="upf15fff97f002",
            name="Speaker Liv",
            device_info=DeviceInfo(model="DLNA Speaker", manufacturer="Edifier"),
            protocol_player_ids=["dlna_uuid_1"],
        )
        up1._attr_device_info.add_identifier(
            IdentifierType.UUID, "FF97F002-783E-6505-6579-F15FFF97F002"
        )
        up1._cache.clear()
        up1.update_state(signal_event=False)
        up1.set_initialized()

        # Universal player 2: from AirPlay (MAC-based)
        up2 = UniversalPlayer(
            provider=up_provider,
            player_id="up50411c2f00ee",
            name="Speaker Liv",
            device_info=DeviceInfo(model="AirPlay Speaker", manufacturer="Edifier"),
            protocol_player_ids=["ap_1", "spb_1"],
        )
        up2._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, "50:41:1C:2F:00:EE")
        up2._attr_device_info.add_identifier(IdentifierType.AIRPLAY_ID, "52:41:1C:2F:00:EE")
        up2._cache.clear()
        up2.update_state(signal_event=False)
        up2.set_initialized()

        # Create mock DLNA protocol player (has MAC from ARP enrichment)
        dlna_provider = MockProvider("dlna", mass=mock_mass)
        dlna_player = MockPlayer(
            dlna_provider,
            "dlna_uuid_1",
            "Speaker Liv DLNA",
            player_type=PlayerType.PROTOCOL,
            identifiers={
                IdentifierType.UUID: "FF97F002-783E-6505-6579-F15FFF97F002",
                IdentifierType.MAC_ADDRESS: "50:41:1C:2F:00:EE",  # From ARP
                IdentifierType.IP_ADDRESS: "192.168.1.80",
            },
        )
        dlna_player.set_initialized()

        # Create mock AirPlay protocol player
        airplay_provider = MockProvider("airplay", mass=mock_mass)
        ap_player = MockPlayer(
            airplay_provider,
            "ap_1",
            "Speaker Liv AirPlay",
            player_type=PlayerType.PROTOCOL,
            identifiers={
                IdentifierType.MAC_ADDRESS: "50:41:1C:2F:00:EE",
                IdentifierType.AIRPLAY_ID: "52:41:1C:2F:00:EE",
            },
        )
        ap_player.set_initialized()

        # Link protocols to their respective universal players
        controller._players = {
            "upf15fff97f002": up1,
            "up50411c2f00ee": up2,
            "dlna_uuid_1": dlna_player,
            "ap_1": ap_player,
        }
        controller._player_throttlers = {k: Throttler(1, 0.05) for k in controller._players}

        # Link DLNA to up1
        controller._add_protocol_link(up1, dlna_player, "dlna")
        assert dlna_player.protocol_parent_id == "upf15fff97f002"

        # Link AirPlay to up2
        controller._add_protocol_link(up2, ap_player, "airplay")
        assert ap_player.protocol_parent_id == "up50411c2f00ee"

        # Now simulate what happens when DLNA player copies identifiers to up1
        # (this happens in _try_restore_cached_parent or _try_link_to_existing_player)
        for conn_type, value in dlna_player.device_info.identifiers.items():
            up1.device_info.add_identifier(conn_type, value)

        # up1 now has MAC 50:41:1C:2F:00:EE which matches up2
        assert up1.device_info.identifiers.get(IdentifierType.MAC_ADDRESS) == "50:41:1C:2F:00:EE"

        # Call merge check
        controller._check_merge_universal_players(up1)

        # up1 should have absorbed up2's protocol links (AirPlay)
        protocol_domains = {link.protocol_domain for link in up1.linked_output_protocols}
        assert "dlna" in protocol_domains
        assert "airplay" in protocol_domains

        # up2 should have no more protocol links
        assert len(up2.linked_output_protocols) == 0

        # AirPlay player should now be linked to up1
        assert ap_player.protocol_parent_id == "upf15fff97f002"

        # up1 should have protocol player IDs from both
        assert "dlna_uuid_1" in up1._protocol_player_ids
        assert "ap_1" in up1._protocol_player_ids

        # Unregister should have been called for up2
        mock_mass.create_task.assert_called()

    def test_no_merge_when_identifiers_dont_match(self, mock_mass: MagicMock) -> None:
        """Test that universal players are not merged when identifiers don't overlap."""
        controller = PlayerController(mock_mass)
        up_provider = create_mock_universal_provider(mock_mass)

        up1 = UniversalPlayer(
            provider=up_provider,
            player_id="up_1",
            name="Speaker A",
            device_info=DeviceInfo(model="Model A", manufacturer="Maker A"),
            protocol_player_ids=["dlna_1"],
        )
        up1._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, "AA:BB:CC:DD:EE:01")
        up1._cache.clear()
        up1.update_state(signal_event=False)
        up1.set_initialized()

        up2 = UniversalPlayer(
            provider=up_provider,
            player_id="up_2",
            name="Speaker B",
            device_info=DeviceInfo(model="Model B", manufacturer="Maker B"),
            protocol_player_ids=["ap_1"],
        )
        up2._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, "AA:BB:CC:DD:EE:02")
        up2._cache.clear()
        up2.update_state(signal_event=False)
        up2.set_initialized()

        controller._players = {"up_1": up1, "up_2": up2}
        controller._player_throttlers = {
            "up_1": Throttler(1, 0.05),
            "up_2": Throttler(1, 0.05),
        }

        # Call merge - should do nothing since MACs are different
        controller._check_merge_universal_players(up1)

        # Both players should remain untouched
        assert "up_1" in controller._players
        assert "up_2" in controller._players

    def test_merge_keeps_player_with_more_protocols(self, mock_mass: MagicMock) -> None:
        """Test that the universal player with more protocol links absorbs the other."""
        controller = PlayerController(mock_mass)
        up_provider = create_mock_universal_provider(mock_mass)

        # up1 has only 1 protocol link (DLNA)
        up1 = UniversalPlayer(
            provider=up_provider,
            player_id="up_small",
            name="Small Player",
            device_info=DeviceInfo(model="Test", manufacturer="Test"),
            protocol_player_ids=["dlna_1"],
        )
        up1._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, "AA:BB:CC:DD:EE:FF")
        up1._cache.clear()
        up1.update_state(signal_event=False)
        up1.set_initialized()

        # up2 has 2 protocol links (AirPlay + Sendspin)
        up2 = UniversalPlayer(
            provider=up_provider,
            player_id="up_big",
            name="Big Player",
            device_info=DeviceInfo(model="Test", manufacturer="Test"),
            protocol_player_ids=["ap_1", "spb_1"],
        )
        up2._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, "AA:BB:CC:DD:EE:FF")
        up2._cache.clear()
        up2.update_state(signal_event=False)
        up2.set_initialized()

        # Create protocol players
        dlna_provider = MockProvider("dlna", mass=mock_mass)
        dlna_player = MockPlayer(
            dlna_provider,
            "dlna_1",
            "DLNA",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        dlna_player.set_initialized()

        airplay_provider = MockProvider("airplay", mass=mock_mass)
        ap_player = MockPlayer(
            airplay_provider,
            "ap_1",
            "AirPlay",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        ap_player.set_initialized()

        sendspin_provider = MockProvider("sendspin", mass=mock_mass)
        spb_player = MockPlayer(
            sendspin_provider,
            "spb_1",
            "Sendspin",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        spb_player.set_initialized()

        controller._players = {
            "up_small": up1,
            "up_big": up2,
            "dlna_1": dlna_player,
            "ap_1": ap_player,
            "spb_1": spb_player,
        }
        controller._player_throttlers = {k: Throttler(1, 0.05) for k in controller._players}

        # Link DLNA to small, AirPlay+Sendspin to big
        controller._add_protocol_link(up1, dlna_player, "dlna")
        controller._add_protocol_link(up2, ap_player, "airplay")
        controller._add_protocol_link(up2, spb_player, "sendspin")

        # Merge from the small player's perspective
        controller._check_merge_universal_players(up1)

        # up2 (bigger) should have absorbed up1's DLNA link
        protocol_domains = {link.protocol_domain for link in up2.linked_output_protocols}
        assert "airplay" in protocol_domains
        assert "sendspin" in protocol_domains
        assert "dlna" in protocol_domains

        # DLNA player should now point to up_big
        assert dlna_player.protocol_parent_id == "up_big"

        # up1 should have no more links
        assert len(up1.linked_output_protocols) == 0


class TestEndToEndDuplicateProtocol:
    """End-to-end integration tests for duplicate protocol → separate universal player flow.

    These tests exercise the full async flow through _delayed_protocol_evaluation,
    ensure_universal_player_for_protocols, and _create_separate_universal_player,
    verifying that a second instance of the same protocol domain on the same host
    results in a separate universal player.
    """

    @staticmethod
    def _setup_e2e_controller(
        mock_mass: MagicMock,
    ) -> tuple[PlayerController, UniversalPlayerProvider]:
        """Set up a PlayerController and UniversalPlayerProvider wired for E2E testing.

        The mock_mass is configured so that:
        - get_providers(ProviderType.PLAYER) returns the universal provider
        - register_or_update adds the player to the controller's _players dict
        - config.create_default_player_config does not fail
        - config.set does not fail

        :return: Tuple of (controller, universal_provider).
        """
        controller = PlayerController(mock_mass)
        up_provider = create_mock_universal_provider(mock_mass)

        # Wire get_providers to return our universal provider
        mock_mass.get_providers = MagicMock(return_value=[up_provider])

        # Wire register_or_update to add the player to controller._players
        async def fake_register_or_update(player: Player) -> None:
            controller._players[player.player_id] = player
            controller._player_throttlers[player.player_id] = Throttler(1, 0.05)
            player.set_initialized()

        mock_mass.players = MagicMock()
        mock_mass.players.register_or_update = AsyncMock(side_effect=fake_register_or_update)
        mock_mass.players.get_player = lambda pid: controller._players.get(pid)

        # Wire config methods
        mock_mass.config.create_default_player_config = MagicMock()
        mock_mass.config.set = MagicMock()
        mock_mass.config.get_base_player_config.return_value = create_mock_config("Test")

        return controller, up_provider

    @pytest.mark.asyncio
    async def test_path_a_delayed_eval_creates_separate_universal_for_duplicate(
        self, mock_mass: MagicMock
    ) -> None:
        """Path A: _delayed_protocol_evaluation creates separate universal player for duplicate.

        Scenario: Two AirPlay instances register on the same host.
        1. First AirPlay links to existing universal player
           via _add_protocol_to_existing_universal
        2. Second AirPlay arrives at _delayed_protocol_evaluation
        3. It finds the existing universal player but is refused (domain duplicate)
        4. Falls through to _find_matching_protocol_players (which skips same-domain)
        5. Creates a NEW separate universal player via _create_or_update_universal_player
        """
        controller, _ = self._setup_e2e_controller(mock_mass)

        # Create existing universal player with AirPlay already linked
        universal = _create_universal_player(
            mock_mass,
            "up_aabbccddeeff",
            "Living Room Speaker",
            protocol_player_ids=["ap_1"],
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )

        # Create first AirPlay (already linked to universal)
        ap_provider = MockProvider("airplay", mass=mock_mass)
        ap1 = MockPlayer(
            ap_provider,
            "ap_1",
            "AirPlay Instance 1",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        ap1.set_initialized()
        controller._add_protocol_link(universal, ap1, "airplay")
        assert ap1.protocol_parent_id == "up_aabbccddeeff"

        # Create second AirPlay (same MAC, not yet linked)
        ap2 = MockPlayer(
            ap_provider,
            "ap_2",
            "AirPlay Instance 2",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        ap2.set_initialized()

        controller._players = {
            "up_aabbccddeeff": universal,
            "ap_1": ap1,
            "ap_2": ap2,
        }
        controller._player_throttlers = {k: Throttler(1, 0.05) for k in controller._players}

        # Run delayed evaluation for ap2 - full async flow
        await controller._delayed_protocol_evaluation("ap_2")

        # ap2 should NOT be linked to the existing universal player
        assert ap2.protocol_parent_id != "up_aabbccddeeff"

        # ap2 should have its own universal player (with player_id-based device key)
        assert ap2.protocol_parent_id is not None
        separate_up_id = ap2.protocol_parent_id
        assert separate_up_id in controller._players
        separate_up = controller._players[separate_up_id]
        assert isinstance(separate_up, UniversalPlayer)

        # The original universal player should still have only ap_1
        original_ap_links = [
            link for link in universal.linked_output_protocols if link.protocol_domain == "airplay"
        ]
        assert len(original_ap_links) == 1
        assert original_ap_links[0].output_protocol_id == "ap_1"

    @pytest.mark.asyncio
    async def test_path_a_delayed_eval_joins_existing_universal_different_domain(
        self, mock_mass: MagicMock
    ) -> None:
        """Path A: _delayed_protocol_evaluation correctly joins when domain is different.

        Scenario: AirPlay universal player exists, DLNA arrives via delayed eval.
        DLNA should successfully join the existing universal player (different domain).
        """
        controller, _ = self._setup_e2e_controller(mock_mass)

        # Create existing universal player with AirPlay linked
        universal = _create_universal_player(
            mock_mass,
            "up_aabbccddeeff",
            "Living Room Speaker",
            protocol_player_ids=["ap_1"],
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )

        ap_provider = MockProvider("airplay", mass=mock_mass)
        ap1 = MockPlayer(
            ap_provider,
            "ap_1",
            "AirPlay",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        ap1.set_initialized()
        controller._add_protocol_link(universal, ap1, "airplay")

        # Create DLNA player (same device, different protocol)
        dlna_provider = MockProvider("dlna", mass=mock_mass)
        dlna1 = MockPlayer(
            dlna_provider,
            "dlna_1",
            "DLNA",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        dlna1.set_initialized()

        controller._players = {
            "up_aabbccddeeff": universal,
            "ap_1": ap1,
            "dlna_1": dlna1,
        }
        controller._player_throttlers = {k: Throttler(1, 0.05) for k in controller._players}

        # Run delayed evaluation for DLNA
        await controller._delayed_protocol_evaluation("dlna_1")

        # DLNA should join the existing universal player
        assert dlna1.protocol_parent_id == "up_aabbccddeeff"

        # Universal player should now have both AirPlay and DLNA
        domains = {link.protocol_domain for link in universal.linked_output_protocols}
        assert "airplay" in domains
        assert "dlna" in domains

    @pytest.mark.asyncio
    async def test_path_b_ensure_universal_separates_duplicate_domain(
        self, mock_mass: MagicMock
    ) -> None:
        """Path B: ensure_universal_player_for_protocols separates can_join vs rejected.

        Scenario: Two AirPlay players arrive simultaneously for delayed eval.
        Both are unlinked, both pass to _create_or_update_universal_player.
        The first creates a universal player. The second finds the existing one
        and is rejected (duplicate domain), getting its own separate universal player.
        """
        controller, _ = self._setup_e2e_controller(mock_mass)

        ap_provider = MockProvider("airplay", mass=mock_mass)
        ap1 = MockPlayer(
            ap_provider,
            "ap_1",
            "AirPlay Instance 1",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        ap1.set_initialized()

        ap2 = MockPlayer(
            ap_provider,
            "ap_2",
            "AirPlay Instance 2",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        ap2.set_initialized()

        controller._players = {
            "ap_1": ap1,
            "ap_2": ap2,
        }
        controller._player_throttlers = {k: Throttler(1, 0.05) for k in controller._players}

        # First: ap1 goes through delayed evaluation, creates a universal player
        await controller._delayed_protocol_evaluation("ap_1")

        # ap1 should have a universal player parent
        assert ap1.protocol_parent_id is not None
        first_up_id = ap1.protocol_parent_id
        assert first_up_id in controller._players

        # Second: ap2 goes through delayed evaluation
        # _find_matching_universal_player will find the universal player by MAC
        # _add_protocol_to_existing_universal will refuse (duplicate domain)
        # _find_matching_protocol_players will skip ap1 (same domain)
        # _create_or_update_universal_player → ensure_universal_player_for_protocols
        #   finds existing universal player, separates ap2 as rejected
        await controller._delayed_protocol_evaluation("ap_2")

        # ap2 should have its own separate universal player
        assert ap2.protocol_parent_id is not None
        second_up_id = ap2.protocol_parent_id
        assert second_up_id != first_up_id
        assert second_up_id in controller._players

        # Both universal players should exist
        first_up = controller._players[first_up_id]
        second_up = controller._players[second_up_id]
        assert isinstance(first_up, UniversalPlayer)
        assert isinstance(second_up, UniversalPlayer)

    @pytest.mark.asyncio
    async def test_path_b_first_creates_then_second_gets_separate(
        self, mock_mass: MagicMock
    ) -> None:
        """Path B: First protocol player creates universal, second gets separate.

        Scenario: AirPlay and DLNA create a universal player together.
        Then a second AirPlay arrives and gets its own separate universal player.
        """
        controller, _ = self._setup_e2e_controller(mock_mass)

        ap_provider = MockProvider("airplay", mass=mock_mass)
        dlna_provider = MockProvider("dlna", mass=mock_mass)

        ap1 = MockPlayer(
            ap_provider,
            "ap_1",
            "AirPlay",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        ap1.set_initialized()

        dlna1 = MockPlayer(
            dlna_provider,
            "dlna_1",
            "DLNA",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        dlna1.set_initialized()

        ap2 = MockPlayer(
            ap_provider,
            "ap_2",
            "AirPlay 2",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        ap2.set_initialized()

        controller._players = {
            "ap_1": ap1,
            "dlna_1": dlna1,
            "ap_2": ap2,
        }
        controller._player_throttlers = {k: Throttler(1, 0.05) for k in controller._players}

        # First: ap1 arrives at delayed eval, finds dlna1 as matching (different domain)
        # Together they create a universal player
        await controller._delayed_protocol_evaluation("ap_1")

        assert ap1.protocol_parent_id is not None
        # dlna1 should also be linked (found by _find_matching_protocol_players)
        assert dlna1.protocol_parent_id == ap1.protocol_parent_id
        shared_up_id = ap1.protocol_parent_id

        # Second: ap2 arrives at delayed eval
        await controller._delayed_protocol_evaluation("ap_2")

        # ap2 should get its own universal player
        assert ap2.protocol_parent_id is not None
        assert ap2.protocol_parent_id != shared_up_id
        assert ap2.protocol_parent_id in controller._players

        # Shared universal player should have AirPlay + DLNA
        shared_up = controller._players[shared_up_id]
        shared_domains = {link.protocol_domain for link in shared_up.linked_output_protocols}
        assert "airplay" in shared_domains
        assert "dlna" in shared_domains

    @pytest.mark.asyncio
    async def test_path_c_sendspin_duplicate_gets_separate_universal(
        self, mock_mass: MagicMock
    ) -> None:
        """Path C: Two Sendspin bridge instances on same host get separate universal players.

        Sendspin bridges match via CAST_UUID / AIRPLAY_ID, not MAC. Two bridges
        on the same host share the same MAC (from ARP) but have different CAST_UUIDs.
        Each should get its own universal player.
        """
        controller, _ = self._setup_e2e_controller(mock_mass)

        sendspin_provider = MockProvider("sendspin", mass=mock_mass)

        # First Sendspin bridge
        spb1 = MockPlayer(
            sendspin_provider,
            "spb_1",
            "Sendspin Bridge 1",
            player_type=PlayerType.PROTOCOL,
            identifiers={
                IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF",
                IdentifierType.CAST_UUID: "cast-uuid-111",
            },
        )
        spb1.set_initialized()

        # Second Sendspin bridge (same MAC, different CAST_UUID)
        spb2 = MockPlayer(
            sendspin_provider,
            "spb_2",
            "Sendspin Bridge 2",
            player_type=PlayerType.PROTOCOL,
            identifiers={
                IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF",
                IdentifierType.CAST_UUID: "cast-uuid-222",
            },
        )
        spb2.set_initialized()

        controller._players = {
            "spb_1": spb1,
            "spb_2": spb2,
        }
        controller._player_throttlers = {k: Throttler(1, 0.05) for k in controller._players}

        # First: spb1 goes through delayed evaluation
        await controller._delayed_protocol_evaluation("spb_1")

        assert spb1.protocol_parent_id is not None
        first_up_id = spb1.protocol_parent_id

        # Second: spb2 goes through delayed evaluation
        # _find_matching_protocol_players skips spb1 (same domain: "sendspin")
        # _find_matching_universal_player may find the first universal player by MAC
        # _add_protocol_to_existing_universal refuses (duplicate domain: "sendspin")
        # Falls through to create separate universal player
        await controller._delayed_protocol_evaluation("spb_2")

        assert spb2.protocol_parent_id is not None
        second_up_id = spb2.protocol_parent_id

        # Each should have its own universal player
        assert first_up_id != second_up_id
        assert first_up_id in controller._players
        assert second_up_id in controller._players

        first_up = controller._players[first_up_id]
        second_up = controller._players[second_up_id]
        assert isinstance(first_up, UniversalPlayer)
        assert isinstance(second_up, UniversalPlayer)

    @pytest.mark.asyncio
    async def test_three_airplay_instances_get_separate_universals(
        self, mock_mass: MagicMock
    ) -> None:
        """Test that three AirPlay instances each get their own universal player.

        Exercises the flow when there are 3+ instances of the same protocol.
        """
        controller, _ = self._setup_e2e_controller(mock_mass)

        ap_provider = MockProvider("airplay", mass=mock_mass)
        players = []
        for i in range(1, 4):
            p = MockPlayer(
                ap_provider,
                f"ap_{i}",
                f"AirPlay Instance {i}",
                player_type=PlayerType.PROTOCOL,
                identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
            )
            p.set_initialized()
            players.append(p)

        controller._players = {p.player_id: p for p in players}
        controller._player_throttlers = {k: Throttler(1, 0.05) for k in controller._players}

        # Run delayed evaluation for each in sequence
        for p in players:
            await controller._delayed_protocol_evaluation(p.player_id)

        # Each should have its own universal player
        parent_ids = {p.protocol_parent_id for p in players}
        assert None not in parent_ids
        # All three should have distinct parents
        assert len(parent_ids) == 3

        # All three universal players should exist
        for pid in parent_ids:
            assert pid in controller._players
            assert isinstance(controller._players[pid], UniversalPlayer)

    @pytest.mark.asyncio
    async def test_mixed_protocols_plus_duplicate_creates_correct_topology(
        self, mock_mass: MagicMock
    ) -> None:
        """Test that mixed protocols + duplicate create correct player topology.

        Scenario:
        - Device has AirPlay, DLNA, and Chromecast protocols
        - Plus a second AirPlay instance (duplicate)
        Result:
        - One universal player with AirPlay + DLNA + Chromecast
        - One separate universal player for the second AirPlay
        """
        controller, _ = self._setup_e2e_controller(mock_mass)

        ap_provider = MockProvider("airplay", mass=mock_mass)
        dlna_provider = MockProvider("dlna", mass=mock_mass)
        cc_provider = MockProvider("chromecast", mass=mock_mass)

        ap1 = MockPlayer(
            ap_provider,
            "ap_1",
            "AirPlay",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        ap1.set_initialized()

        dlna1 = MockPlayer(
            dlna_provider,
            "dlna_1",
            "DLNA",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        dlna1.set_initialized()

        cc1 = MockPlayer(
            cc_provider,
            "cc_1",
            "Chromecast",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        cc1.set_initialized()

        ap2 = MockPlayer(
            ap_provider,
            "ap_2",
            "AirPlay Duplicate",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        ap2.set_initialized()

        controller._players = {
            "ap_1": ap1,
            "dlna_1": dlna1,
            "cc_1": cc1,
            "ap_2": ap2,
        }
        controller._player_throttlers = {k: Throttler(1, 0.05) for k in controller._players}

        # ap1 goes through delayed eval - finds dlna1 and cc1 as matching
        # (different domains), creates universal player with all three
        await controller._delayed_protocol_evaluation("ap_1")

        assert ap1.protocol_parent_id is not None
        assert dlna1.protocol_parent_id == ap1.protocol_parent_id
        assert cc1.protocol_parent_id == ap1.protocol_parent_id
        shared_up_id = ap1.protocol_parent_id

        # ap2 goes through delayed eval - finds universal player but is rejected
        await controller._delayed_protocol_evaluation("ap_2")

        assert ap2.protocol_parent_id is not None
        assert ap2.protocol_parent_id != shared_up_id

        # Shared universal player should have 3 protocols
        shared_up = controller._players[shared_up_id]
        shared_domains = {link.protocol_domain for link in shared_up.linked_output_protocols}
        assert shared_domains == {"airplay", "dlna", "chromecast"}


class TestSiblingProtocolMatching:
    """Tests for sibling protocol matching via _match_via_linked_protocols.

    When a native player (e.g., HEOS) doesn't have its own MAC/serial identifiers
    but has protocol players (e.g., AirPlay) already linked, a new protocol player
    (e.g., Sendspin bridge) should be matched to the native player through the
    shared identifiers of sibling protocols.
    """

    def test_sendspin_matches_heos_via_airplay_sibling(self, mock_mass: MagicMock) -> None:
        """Test Sendspin links to HEOS through AirPlay's shared AIRPLAY_ID.

        HEOS has no MAC/UUID identifiers. AirPlay is already linked to HEOS.
        Sendspin has AIRPLAY_ID matching AirPlay. Sibling matching should link
        Sendspin to HEOS.
        """
        controller = PlayerController(mock_mass)

        # HEOS native player - no MAC, no serial, no UUID (just IP from discovery)
        heos_provider = MockProvider("heos", mass=mock_mass)
        heos_player = MockPlayer(
            heos_provider,
            "2140037800",
            "Denon AVR-X2700H",
            player_type=PlayerType.PLAYER,
        )
        heos_player._attr_supported_features = {PlayerFeature.VOLUME_SET, PlayerFeature.PLAY_MEDIA}
        heos_player._cache.clear()
        heos_player.set_initialized()

        # AirPlay protocol player already linked to HEOS
        ap_provider = MockProvider("airplay", mass=mock_mass)
        ap_player = MockPlayer(
            ap_provider,
            "ap000678747b16",
            "Denon AVR-X2700H (AirPlay)",
            player_type=PlayerType.PROTOCOL,
            identifiers={
                IdentifierType.MAC_ADDRESS: "00:06:78:74:7B:16",
                IdentifierType.AIRPLAY_ID: "ap000678747b16",
            },
        )
        ap_player.set_initialized()

        # Link AirPlay to HEOS
        controller._players = {
            "2140037800": heos_player,
            "ap000678747b16": ap_player,
        }
        controller._player_throttlers = {k: Throttler(1, 0.05) for k in controller._players}
        controller._add_protocol_link(heos_player, ap_player, "airplay")
        assert ap_player.protocol_parent_id == "2140037800"

        # Sendspin bridge with AIRPLAY_ID matching AirPlay
        spb_provider = MockProvider("sendspin", mass=mock_mass)
        spb_player = MockPlayer(
            spb_provider,
            "spb_000678747b16",
            "Denon AVR-X2700H (Sendspin)",
            player_type=PlayerType.PROTOCOL,
            identifiers={
                IdentifierType.MAC_ADDRESS: "00:06:78:74:7B:16",
                IdentifierType.AIRPLAY_ID: "ap000678747b16",
            },
        )
        spb_player.set_initialized()
        controller._players["spb_000678747b16"] = spb_player
        controller._player_throttlers["spb_000678747b16"] = Throttler(1, 0.05)

        # Direct identifier match would fail (HEOS has no identifiers)
        assert controller._identifiers_match(heos_player, spb_player, "sendspin") is False

        # Sibling matching should succeed through AirPlay's identifiers
        assert controller._match_via_linked_protocols(heos_player, spb_player, "sendspin") is True
        assert spb_player.protocol_parent_id == "2140037800"

    def test_sibling_match_no_linked_protocols(self, mock_mass: MagicMock) -> None:
        """Test sibling matching returns False when native player has no linked protocols."""
        controller = PlayerController(mock_mass)

        heos_provider = MockProvider("heos", mass=mock_mass)
        heos_player = MockPlayer(
            heos_provider, "heos_1", "HEOS Player", player_type=PlayerType.PLAYER
        )
        heos_player.set_initialized()

        spb_provider = MockProvider("sendspin", mass=mock_mass)
        spb_player = MockPlayer(
            spb_provider,
            "spb_1",
            "Sendspin",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.AIRPLAY_ID: "ap_1"},
        )
        spb_player.set_initialized()

        controller._players = {"heos_1": heos_player, "spb_1": spb_player}
        controller._player_throttlers = {k: Throttler(1, 0.05) for k in controller._players}

        assert controller._match_via_linked_protocols(heos_player, spb_player, "sendspin") is False

    def test_sibling_match_no_identifier_overlap(self, mock_mass: MagicMock) -> None:
        """Test sibling matching returns False when identifiers don't overlap."""
        controller = PlayerController(mock_mass)

        heos_provider = MockProvider("heos", mass=mock_mass)
        heos_player = MockPlayer(
            heos_provider, "heos_1", "HEOS Player", player_type=PlayerType.PLAYER
        )
        heos_player.set_initialized()

        # AirPlay with one MAC
        ap_provider = MockProvider("airplay", mass=mock_mass)
        ap_player = MockPlayer(
            ap_provider,
            "ap_1",
            "AirPlay",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        ap_player.set_initialized()

        controller._players = {"heos_1": heos_player, "ap_1": ap_player}
        controller._player_throttlers = {k: Throttler(1, 0.05) for k in controller._players}
        controller._add_protocol_link(heos_player, ap_player, "airplay")

        # Sendspin with different MAC, no shared identifiers
        spb_provider = MockProvider("sendspin", mass=mock_mass)
        spb_player = MockPlayer(
            spb_provider,
            "spb_1",
            "Sendspin",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "11:22:33:44:55:66"},
        )
        spb_player.set_initialized()
        controller._players["spb_1"] = spb_player
        controller._player_throttlers["spb_1"] = Throttler(1, 0.05)

        assert controller._match_via_linked_protocols(heos_player, spb_player, "sendspin") is False
        assert spb_player.protocol_parent_id is None

    def test_sibling_match_refuses_duplicate_domain(self, mock_mass: MagicMock) -> None:
        """Test sibling matching refuses link when domain is already active on native player."""
        controller = PlayerController(mock_mass)

        heos_provider = MockProvider("heos", mass=mock_mass)
        heos_player = MockPlayer(
            heos_provider, "heos_1", "HEOS Player", player_type=PlayerType.PLAYER
        )
        heos_player.set_initialized()

        # AirPlay already linked
        ap_provider = MockProvider("airplay", mass=mock_mass)
        ap_player = MockPlayer(
            ap_provider,
            "ap_1",
            "AirPlay",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        ap_player.set_initialized()

        controller._players = {"heos_1": heos_player, "ap_1": ap_player}
        controller._player_throttlers = {k: Throttler(1, 0.05) for k in controller._players}
        controller._add_protocol_link(heos_player, ap_player, "airplay")

        # Second AirPlay instance with same MAC - should be refused (duplicate domain)
        ap2 = MockPlayer(
            ap_provider,
            "ap_2",
            "AirPlay 2",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        ap2.set_initialized()
        controller._players["ap_2"] = ap2
        controller._player_throttlers["ap_2"] = Throttler(1, 0.05)

        assert controller._match_via_linked_protocols(heos_player, ap2, "airplay") is False
        assert ap2.protocol_parent_id is None

    def test_try_link_to_existing_uses_sibling_matching(self, mock_mass: MagicMock) -> None:
        """Test _try_link_to_existing_player falls back to sibling matching."""
        controller = PlayerController(mock_mass)

        # No cached protocol IDs for HEOS
        mock_mass.config.get = MagicMock(return_value=[])

        heos_provider = MockProvider("heos", mass=mock_mass)
        heos_player = MockPlayer(
            heos_provider,
            "2140037800",
            "Denon AVR-X2700H",
            player_type=PlayerType.PLAYER,
        )
        heos_player.set_initialized()

        ap_provider = MockProvider("airplay", mass=mock_mass)
        ap_player = MockPlayer(
            ap_provider,
            "ap000678747b16",
            "Denon AirPlay",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.AIRPLAY_ID: "ap000678747b16"},
        )
        ap_player.set_initialized()

        controller._players = {
            "2140037800": heos_player,
            "ap000678747b16": ap_player,
        }
        controller._player_throttlers = {k: Throttler(1, 0.05) for k in controller._players}
        controller._add_protocol_link(heos_player, ap_player, "airplay")

        # Sendspin with matching AIRPLAY_ID
        spb_provider = MockProvider("sendspin", mass=mock_mass)
        spb_player = MockPlayer(
            spb_provider,
            "spb_000678747b16",
            "Denon Sendspin",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.AIRPLAY_ID: "ap000678747b16"},
        )
        spb_player.set_initialized()
        controller._players["spb_000678747b16"] = spb_player
        controller._player_throttlers["spb_000678747b16"] = Throttler(1, 0.05)

        # _try_link_to_existing_player should succeed via sibling matching
        result = controller._try_link_to_existing_player(spb_player, "sendspin")
        assert result is True
        assert spb_player.protocol_parent_id == "2140037800"


class TestDelayedEvalRetry:
    """Tests for the retry of _try_link_to_existing_player in _delayed_protocol_evaluation.

    When a protocol player's delayed evaluation fires, native players may have
    registered during the delay period. The retry ensures they are checked before
    falling through to universal player creation.
    """

    @pytest.mark.asyncio
    async def test_delayed_eval_links_to_native_registered_during_delay(
        self, mock_mass: MagicMock
    ) -> None:
        """Test delayed eval links to a native player that registered during the delay.

        Scenario: Sendspin registers before HEOS. The delayed evaluation fires
        after HEOS has registered and has AirPlay linked. The retry should find
        HEOS via sibling matching and link Sendspin.
        """
        controller, _ = TestEndToEndDuplicateProtocol._setup_e2e_controller(mock_mass)

        # No cached protocol IDs
        mock_mass.config.get = MagicMock(return_value=[])

        # HEOS native player (no device identifiers)
        heos_provider = MockProvider("heos", mass=mock_mass)
        heos_player = MockPlayer(
            heos_provider,
            "2140037800",
            "Denon AVR-X2700H",
            player_type=PlayerType.PLAYER,
        )
        heos_player._attr_supported_features = {PlayerFeature.VOLUME_SET, PlayerFeature.PLAY_MEDIA}
        heos_player._cache.clear()
        heos_player.set_initialized()

        # AirPlay already linked to HEOS
        ap_provider = MockProvider("airplay", mass=mock_mass)
        ap_player = MockPlayer(
            ap_provider,
            "ap000678747b16",
            "Denon AirPlay",
            player_type=PlayerType.PROTOCOL,
            identifiers={
                IdentifierType.MAC_ADDRESS: "00:06:78:74:7B:16",
                IdentifierType.AIRPLAY_ID: "ap000678747b16",
            },
        )
        ap_player.set_initialized()
        ap_player.set_protocol_parent_id("2140037800")

        # Sendspin (not yet linked)
        spb_provider = MockProvider("sendspin", mass=mock_mass)
        spb_player = MockPlayer(
            spb_provider,
            "spb_000678747b16",
            "Denon Sendspin",
            player_type=PlayerType.PROTOCOL,
            identifiers={
                IdentifierType.MAC_ADDRESS: "00:06:78:74:7B:16",
                IdentifierType.AIRPLAY_ID: "ap000678747b16",
            },
        )
        spb_player.set_initialized()

        # Set up players dict (simulating: HEOS registered during the delay)
        controller._players = {
            "2140037800": heos_player,
            "ap000678747b16": ap_player,
            "spb_000678747b16": spb_player,
        }
        controller._player_throttlers = {k: Throttler(1, 0.05) for k in controller._players}

        # Link AirPlay to HEOS
        controller._add_protocol_link(heos_player, ap_player, "airplay")

        # Run delayed evaluation for Sendspin
        await controller._delayed_protocol_evaluation("spb_000678747b16")

        # Sendspin should be linked to HEOS, not a new universal player
        assert spb_player.protocol_parent_id == "2140037800"

    @pytest.mark.asyncio
    async def test_delayed_eval_skips_already_linked(self, mock_mass: MagicMock) -> None:
        """Test delayed eval returns early if protocol player was linked during the delay."""
        controller, _ = TestEndToEndDuplicateProtocol._setup_e2e_controller(mock_mass)

        spb_provider = MockProvider("sendspin", mass=mock_mass)
        spb_player = MockPlayer(
            spb_provider,
            "spb_1",
            "Sendspin",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.AIRPLAY_ID: "ap_1"},
        )
        spb_player.set_initialized()
        # Simulate it was linked during the delay
        spb_player.set_protocol_parent_id("some_parent")

        controller._players = {"spb_1": spb_player}
        controller._player_throttlers = {"spb_1": Throttler(1, 0.05)}

        # Should return early without creating universal player
        await controller._delayed_protocol_evaluation("spb_1")
        assert spb_player.protocol_parent_id == "some_parent"


class TestNativePlayerSiblingLinking:
    """Tests for the second pass in _try_link_protocols_to_native using sibling matching.

    After a native player registers and recovers cached protocol links, the second
    pass should match remaining unlinked protocol players via sibling identifiers.
    """

    def test_native_registration_links_sendspin_via_sibling(self, mock_mass: MagicMock) -> None:
        """Test HEOS registration links Sendspin via AirPlay sibling identifiers.

        Scenario: Sendspin registered before HEOS. When HEOS registers, the first
        pass can't match Sendspin (no direct identifiers). After recovering cached
        AirPlay link, the second pass matches Sendspin via AirPlay's AIRPLAY_ID.
        """
        controller = PlayerController(mock_mass)

        # Set up config to return cached AirPlay link for HEOS
        def config_get_side_effect(key: str, default: object = None) -> object:
            if key == "players/2140037800/values/linked_protocol_ids":
                return ["ap000678747b16"]
            if key == "players/ap000678747b16":
                return {
                    "provider": "airplay--test",
                    "player_type": "protocol",
                    "values": {"protocol_parent_id": "2140037800"},
                }
            if key == "players":
                return {
                    "ap000678747b16": {
                        "provider": "airplay--test",
                        "player_type": "protocol",
                        "values": {"protocol_parent_id": "2140037800"},
                    },
                    "spb_000678747b16": {
                        "provider": "sendspin--test",
                        "player_type": "protocol",
                        "values": {},
                    },
                }
            return default

        mock_mass.config.get = MagicMock(side_effect=config_get_side_effect)
        mock_mass.get_provider = MagicMock(return_value=None)

        # AirPlay protocol player (already registered, waiting for parent)
        ap_provider = MockProvider("airplay", mass=mock_mass)
        ap_player = MockPlayer(
            ap_provider,
            "ap000678747b16",
            "Denon AirPlay",
            player_type=PlayerType.PROTOCOL,
            identifiers={
                IdentifierType.MAC_ADDRESS: "00:06:78:74:7B:16",
                IdentifierType.AIRPLAY_ID: "ap000678747b16",
            },
        )
        ap_player.set_initialized()
        # AirPlay has cached parent set but parent hasn't registered yet
        ap_player.set_protocol_parent_id("2140037800")

        # Sendspin (registered, not linked)
        spb_provider = MockProvider("sendspin", mass=mock_mass)
        spb_player = MockPlayer(
            spb_provider,
            "spb_000678747b16",
            "Denon Sendspin",
            player_type=PlayerType.PROTOCOL,
            identifiers={
                IdentifierType.MAC_ADDRESS: "00:06:78:74:7B:16",
                IdentifierType.AIRPLAY_ID: "ap000678747b16",
            },
        )
        spb_player.set_initialized()

        # HEOS native player registers now
        heos_provider = MockProvider("heos", mass=mock_mass)
        heos_player = MockPlayer(
            heos_provider,
            "2140037800",
            "Denon AVR-X2700H",
            player_type=PlayerType.PLAYER,
        )
        heos_player._attr_supported_features = {PlayerFeature.VOLUME_SET, PlayerFeature.PLAY_MEDIA}
        heos_player._cache.clear()
        heos_player.set_initialized()

        controller._players = {
            "ap000678747b16": ap_player,
            "spb_000678747b16": spb_player,
            "2140037800": heos_player,
        }
        controller._player_throttlers = {k: Throttler(1, 0.05) for k in controller._players}

        # Simulate what happens when HEOS registers
        controller._try_link_protocols_to_native(heos_player)

        # AirPlay should be recovered from cache (linked_output_protocols)
        airplay_linked = any(
            link.output_protocol_id == "ap000678747b16"
            for link in heos_player.linked_output_protocols
        )
        assert airplay_linked, "AirPlay should be recovered from cached protocol links"

        # Sendspin should be linked via sibling matching (second pass)
        assert spb_player.protocol_parent_id == "2140037800"
        sendspin_linked = any(
            link.output_protocol_id == "spb_000678747b16"
            for link in heos_player.linked_output_protocols
        )
        assert sendspin_linked, "Sendspin should be linked via sibling protocol matching"
