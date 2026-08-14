"""Tests for protocol player linking and universal player creation."""

import logging
from collections.abc import Awaitable
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.config_entries import PlayerConfig
from music_assistant_models.enums import (
    EventType,
    IdentifierType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
)
from music_assistant_models.player import PlayerMedia

from music_assistant.constants import (
    ATTR_ENABLED,
    CONF_PLAYER_DSP,
    CONF_PLAYER_QUEUES,
    CONF_PLAYERS,
    CONF_PREFERRED_OUTPUT_PROTOCOL,
)
from music_assistant.controllers.config import ConfigController
from music_assistant.controllers.config.migrations import migrate
from music_assistant.controllers.players import PlayerController
from music_assistant.helpers.util import enrich_device_mac_address
from music_assistant.models.player import DeviceInfo, LinkedOutputProtocol, Player
from music_assistant.models.player_provider import PlayerProvider
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
        # the controller iterates these when a group related field changes
        self.players: list[Player] = []


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


class SessionBoundMockPlayer(MockPlayer):
    """Mock player whose native grouping attaches members to its own stream (e.g. AirPlay)."""

    @property
    def native_grouping_requires_own_stream(self) -> bool:
        """Return True: native members ride this player's own stream session."""
        return True


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance."""
    mass = MagicMock()
    mass.closing = False
    mass.config = MagicMock()
    mass.config.get = MagicMock(return_value=[])

    def _get_raw_player_config_value(
        _player_id: str, key: str, default: str | int | None = None
    ) -> str | int | None:
        """Return appropriate defaults for player config values."""
        if key == "min_volume":
            return 0
        if key == "max_volume":
            return 100
        return default

    mass.config.get_raw_player_config_value = MagicMock(side_effect=_get_raw_player_config_value)
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
        """
        Test that MAC addresses differing only in locally-administered bit match.

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
        """
        Test that different devices with locally-administered MACs don't match.

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

    def test_ip_address_fallback_with_no_mac(self, mock_mass: MagicMock) -> None:
        """Test that same IP matches when no MAC is available (ARP couldn't help)."""
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

        # Same IP with no MAC at all = match (likely same device, ARP unavailable)
        assert controller._identifiers_match(player_a, player_b) is True

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
        """
        Test that CAST_UUID identifiers match for cross-protocol linking.

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
        """
        Test that AIRPLAY_ID identifiers match for cross-protocol linking.

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

    def test_ip_fallback_with_la_mac(self, mock_mass: MagicMock) -> None:
        """Test IP fallback matching when one player has a locally-administered MAC."""
        controller = PlayerController(mock_mass)

        provider = MockProvider("test")
        player_a = MockPlayer(
            provider,
            "player_a",
            "AirPlay Device",
            player_type=PlayerType.PROTOCOL,
            identifiers={
                IdentifierType.MAC_ADDRESS: "C0:F5:35:7B:6D:A0",
                IdentifierType.IP_ADDRESS: "192.168.1.48",
            },
        )
        player_b = MockPlayer(
            provider,
            "player_b",
            "DLNA Device",
            player_type=PlayerType.PROTOCOL,
            identifiers={
                # Locally-administered MAC from ARP (device uses MAC randomization)
                IdentifierType.MAC_ADDRESS: "02:4E:52:A1:BC:6B",
                IdentifierType.IP_ADDRESS: "192.168.1.48",
            },
        )

        assert controller._identifiers_match(player_a, player_b) is True

    def test_ip_fallback_with_missing_mac(self, mock_mass: MagicMock) -> None:
        """Test IP fallback matching when one player has no MAC at all."""
        controller = PlayerController(mock_mass)

        provider = MockProvider("test")
        player_a = MockPlayer(
            provider,
            "player_a",
            "AirPlay Device",
            player_type=PlayerType.PROTOCOL,
            identifiers={
                IdentifierType.MAC_ADDRESS: "C0:F5:35:7B:6D:A0",
                IdentifierType.IP_ADDRESS: "192.168.1.48",
            },
        )
        player_b = MockPlayer(
            provider,
            "player_b",
            "DLNA Device",
            player_type=PlayerType.PROTOCOL,
            identifiers={
                IdentifierType.IP_ADDRESS: "192.168.1.48",
            },
        )

        assert controller._identifiers_match(player_a, player_b) is True

    def test_ip_fallback_protocol_players_with_real_macs(self, mock_mass: MagicMock) -> None:
        """
        Test that IP matches protocol players even when both have real MACs.

        Some devices (e.g., Yamaha MusicCast) report different valid globally-unique
        MACs per protocol (DLNA vs AirPlay differ by 1 in the last octet).
        IP matching is safe here because two devices on a LAN can't share an IP.
        """
        controller = PlayerController(mock_mass)

        provider = MockProvider("test")
        player_a = MockPlayer(
            provider,
            "player_a",
            "Device A",
            player_type=PlayerType.PROTOCOL,
            identifiers={
                IdentifierType.MAC_ADDRESS: "C0:F5:35:7B:6D:A0",
                IdentifierType.IP_ADDRESS: "192.168.1.48",
            },
        )
        player_b = MockPlayer(
            provider,
            "player_b",
            "Device B",
            player_type=PlayerType.PROTOCOL,
            identifiers={
                # A8 first octet = 10101000, LA bit (0x02) is clear -> real MAC
                IdentifierType.MAC_ADDRESS: "A8:BB:CC:DD:EE:FF",
                IdentifierType.IP_ADDRESS: "192.168.1.48",
            },
        )

        # Protocol players with real MACs on same IP -> match (same physical device)
        assert controller._identifiers_match(player_a, player_b) is True

    def test_ip_no_fallback_native_players_with_real_macs(self, mock_mass: MagicMock) -> None:
        """Test that IP alone does NOT match two native PLAYER-type players with real MACs."""
        controller = PlayerController(mock_mass)

        provider = MockProvider("test")
        player_a = MockPlayer(
            provider,
            "player_a",
            "Device A",
            player_type=PlayerType.PLAYER,
            identifiers={
                IdentifierType.MAC_ADDRESS: "C0:F5:35:7B:6D:A0",
                IdentifierType.IP_ADDRESS: "192.168.1.48",
            },
        )
        player_b = MockPlayer(
            provider,
            "player_b",
            "Device B",
            player_type=PlayerType.PLAYER,
            identifiers={
                IdentifierType.MAC_ADDRESS: "A8:BB:CC:DD:EE:FF",
                IdentifierType.IP_ADDRESS: "192.168.1.48",
            },
        )

        # Two native players with real MACs that don't match - IP should NOT be used
        assert controller._identifiers_match(player_a, player_b) is False

    def test_ip_no_fallback_different_ips(self, mock_mass: MagicMock) -> None:
        """Test that different IPs don't match even with LA MAC."""
        controller = PlayerController(mock_mass)

        provider = MockProvider("test")
        player_a = MockPlayer(
            provider,
            "player_a",
            "Device A",
            player_type=PlayerType.PROTOCOL,
            identifiers={
                IdentifierType.MAC_ADDRESS: "C0:F5:35:7B:6D:A0",
                IdentifierType.IP_ADDRESS: "192.168.1.48",
            },
        )
        player_b = MockPlayer(
            provider,
            "player_b",
            "Device B",
            player_type=PlayerType.PROTOCOL,
            identifiers={
                IdentifierType.MAC_ADDRESS: "02:4E:52:A1:BC:6B",
                IdentifierType.IP_ADDRESS: "192.168.1.99",
            },
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

        # Find matching players for first Snapcast player
        matches = controller._find_matching_protocol_players(snapcast_player_1)

        # Should only match itself, NOT the other Snapcast player (same protocol domain)
        assert len(matches) == 1
        assert snapcast_player_1 in matches
        assert snapcast_player_2 not in matches


class TestGetDeviceKeyFromPlayers:
    """Tests for device key generation."""

    def test_device_key_from_mac(self, mock_mass: MagicMock) -> None:
        """
        Test device key generation from MAC address.

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
        """
        Test that device key normalizes locally-administered MACs.

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

        # Try to link protocol to native - should load cached parent_id
        controller._try_link_protocol_to_native(protocol_player)

        # Verify protocol_parent_id was set
        assert protocol_player.protocol_parent_id == "native_player_id"

        # Verify protocol was linked to native player
        assert any(
            link.output_protocol_id == protocol_player.player_id
            for link in native_player.linked_output_protocols
        )

    def test_missing_cached_parent_schedules_protocol_evaluation(
        self, mock_mass: MagicMock
    ) -> None:
        """Test that a missing cached parent does not block delayed protocol evaluation."""
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

        with patch.object(controller, "_schedule_protocol_evaluation") as mock_schedule:
            controller._try_link_protocol_to_native(protocol_player)

        # A cached parent that is not registered yet should be handled by the
        # delayed evaluation path, not by assigning a dangling in-memory parent.
        assert protocol_player.protocol_parent_id is None
        mock_schedule.assert_called_once_with(protocol_player)

    @pytest.mark.asyncio
    async def test_cached_parent_registers_before_delayed_eval_links_without_universal(
        self, mock_mass: MagicMock
    ) -> None:
        """Test cached parent startup grace still links when native registers during delay."""
        controller = PlayerController(mock_mass)

        def mock_config_get(
            key: str, default: str | list[str] | None = None
        ) -> str | list[str] | None:
            if "protocol_parent_id" in str(key):
                return "native_player_id"
            return default

        mock_mass.config.get.side_effect = mock_config_get

        dlna_provider = MockProvider("dlna", mass=mock_mass)
        protocol_player = MockPlayer(
            dlna_provider,
            "uuid:RINCON_AABBCCDDEEFF_MR",
            "Sonos DLNA",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        protocol_player.set_initialized()
        controller._players = {protocol_player.player_id: protocol_player}

        controller._try_link_protocol_to_native(protocol_player)

        assert protocol_player.protocol_parent_id is None
        assert mock_mass.loop.call_later.call_args.args[0] == 45.0

        native_provider = MockProvider("sonos", mass=mock_mass)
        native_player = MockPlayer(
            native_provider,
            "native_player_id",
            "Sonos Speaker",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        native_player.set_initialized()
        controller._players[native_player.player_id] = native_player

        controller._try_link_protocols_to_native(native_player)

        assert protocol_player.protocol_parent_id == native_player.player_id
        assert any(  # type: ignore[unreachable]
            link.output_protocol_id == protocol_player.player_id
            for link in native_player.linked_output_protocols
        )

        with patch.object(controller, "_create_or_update_universal_player") as mock_create_up:
            await controller._delayed_protocol_evaluation(protocol_player.player_id)

        mock_create_up.assert_not_called()


class TestSelectBestOutputProtocol:
    """Tests for output protocol selection logic."""

    def test_select_native_when_preferred_is_native(self, mock_mass: MagicMock) -> None:
        """Test that native protocol is selected when user prefers native."""

        # Mock config to return "native" as preferred, but still handle min/max volume
        def _get_raw_config(
            _player_id: str, key: str, _default: str | int | None = None
        ) -> str | int | None:
            if key == "min_volume":
                return 0
            if key == "max_volume":
                return 100
            return "native"

        mock_mass.config.get_raw_player_config_value = MagicMock(side_effect=_get_raw_config)

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

        # Select protocol
        selected_player, output_protocol = controller._select_best_output_protocol(native_player)

        # Should select native player
        assert selected_player == native_player
        assert output_protocol is None  # None means native playback

    def test_select_dlna_when_preferred_is_dlna(self, mock_mass: MagicMock) -> None:
        """Test that DLNA protocol is selected when user prefers DLNA."""

        # Mock config to return the full player ID as preferred, but still handle min/max volume
        def _get_raw_config(
            _player_id: str, key: str, _default: str | int | None = None
        ) -> str | int | None:
            if key == "min_volume":
                return 0
            if key == "max_volume":
                return 100
            return "dlna_AABBCCDDEEFF"

        mock_mass.config.get_raw_player_config_value = MagicMock(side_effect=_get_raw_config)

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

        # Link DLNA protocol to native player
        native_player.set_linked_output_protocols(
            [
                LinkedOutputProtocol(
                    output_protocol_id="dlna_AABBCCDDEEFF",
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

        # Mock config to return the full player ID as preferred, but still handle min/max volume
        def _get_raw_config(
            _player_id: str, key: str, _default: str | int | None = None
        ) -> str | int | None:
            if key == "min_volume":
                return 0
            if key == "max_volume":
                return 100
            return "airplay_AABBCCDDEEFF"

        mock_mass.config.get_raw_player_config_value = MagicMock(side_effect=_get_raw_config)

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

        # Link protocols to native player
        native_player.set_linked_output_protocols(
            [
                LinkedOutputProtocol(
                    output_protocol_id="airplay_AABBCCDDEEFF",
                    protocol_domain="airplay",
                    priority=10,
                ),
                LinkedOutputProtocol(
                    output_protocol_id="dlna_AABBCCDDEEFF",
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

        # Mock config to return "auto" as preferred, but still handle min/max volume
        def _get_raw_config(
            _player_id: str, key: str, _default: str | int | None = None
        ) -> str | int | None:
            if key == "min_volume":
                return 0
            if key == "max_volume":
                return 100
            return "auto"

        mock_mass.config.get_raw_player_config_value = MagicMock(side_effect=_get_raw_config)

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

        # Select protocol with auto preference
        selected_player, output_protocol = controller._select_best_output_protocol(native_player)

        # Should select native player
        assert selected_player == native_player
        assert output_protocol is None  # None means native playback

    def test_skip_protocol_that_needs_setup(self, mock_mass: MagicMock) -> None:
        """Test that a protocol player which still needs setup is never selected."""
        controller = PlayerController(mock_mass)

        # Universal player wrapping the protocols of a device without native playback
        universal_provider = MockProvider("universal_player", mass=mock_mass)
        universal_player = MockPlayer(
            universal_provider,
            "up_AABBCCDDEEFF",
            "Living Room TV",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )

        # AirPlay protocol player that is discovered but still awaits pairing
        airplay_provider = MockProvider("airplay", mass=mock_mass)
        airplay_player = MockPlayer(
            airplay_provider,
            "airplay_AABBCCDDEEFF",
            "Living Room TV AirPlay",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        airplay_player._attr_needs_setup = True

        dlna_provider = MockProvider("dlna", mass=mock_mass)
        dlna_player = MockPlayer(
            dlna_provider,
            "dlna_AABBCCDDEEFF",
            "Living Room TV DLNA",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )

        # Wire up mock_mass.players to controller
        mock_mass.players = controller

        # Register players
        controller._players = {
            "up_AABBCCDDEEFF": universal_player,
            "airplay_AABBCCDDEEFF": airplay_player,
            "dlna_AABBCCDDEEFF": dlna_player,
        }

        # Link both protocols, AirPlay with the better priority
        universal_player.set_linked_output_protocols(
            [
                LinkedOutputProtocol(
                    output_protocol_id="airplay_AABBCCDDEEFF",
                    protocol_domain="airplay",
                    priority=10,
                ),
                LinkedOutputProtocol(
                    output_protocol_id="dlna_AABBCCDDEEFF",
                    protocol_domain="dlna",
                    priority=50,
                ),
            ]
        )

        selected_player, output_protocol = controller._select_best_output_protocol(universal_player)

        # Should skip the unpaired AirPlay output and fall through to DLNA
        assert selected_player == dlna_player
        assert output_protocol is not None
        assert output_protocol.output_protocol_id == "dlna_AABBCCDDEEFF"

        # and the unpaired output must be reported as unavailable
        universal_player.update_state(signal_event=False)
        airplay_output = next(
            protocol
            for protocol in universal_player.output_protocols
            if protocol.output_protocol_id == "airplay_AABBCCDDEEFF"
        )
        assert airplay_output.available is False


class TestGetControlTarget:
    """Tests for resolving the target of an audio-path command."""

    def test_fallback_follows_protocol_priority(self, mock_mass: MagicMock) -> None:
        """
        Without an active output the fallback picks the highest priority protocol.

        An announcement on an idle player reaches this path, so it has to land on the
        same output that regular playback selection would have chosen.
        """
        controller = PlayerController(mock_mass)
        mock_mass.players = controller

        universal_provider = MockProvider("universal_player", mass=mock_mass)
        universal_player = MockPlayer(universal_provider, "universal_123", "Kantoor")
        universal_player._attr_supported_features = set()

        sendspin_player = MockPlayer(
            MockProvider("sendspin", mass=mock_mass),
            "sendspin_AABBCCDDEEFF",
            "Kantoor Sendspin",
            player_type=PlayerType.PROTOCOL,
        )
        sendspin_player._attr_supported_features.add(PlayerFeature.PLAY_ANNOUNCEMENT)
        airplay_player = MockPlayer(
            MockProvider("airplay", mass=mock_mass),
            "airplay_AABBCCDDEEFF",
            "Kantoor AirPlay",
            player_type=PlayerType.PROTOCOL,
        )
        airplay_player._attr_supported_features.add(PlayerFeature.PLAY_ANNOUNCEMENT)

        controller._players = {
            "universal_123": universal_player,
            "sendspin_AABBCCDDEEFF": sendspin_player,
            "airplay_AABBCCDDEEFF": airplay_player,
        }
        # sendspin links first, but airplay outranks it
        universal_player.set_linked_output_protocols(
            [
                LinkedOutputProtocol(
                    output_protocol_id="sendspin_AABBCCDDEEFF",
                    protocol_domain="sendspin",
                    priority=40,
                ),
                LinkedOutputProtocol(
                    output_protocol_id="airplay_AABBCCDDEEFF",
                    protocol_domain="airplay",
                    priority=10,
                ),
            ]
        )

        target = controller._get_control_target(
            universal_player, PlayerFeature.PLAY_ANNOUNCEMENT, require_active=False
        )
        assert target == airplay_player

    def test_fallback_follows_preferred_output_protocol(self, mock_mass: MagicMock) -> None:
        """An explicitly preferred output beats the priority order."""
        controller = PlayerController(mock_mass)
        mock_mass.players = controller

        def _get_raw_player_config_value(
            _player_id: str, key: str, default: str | int | None = None
        ) -> str | int | None:
            if key == CONF_PREFERRED_OUTPUT_PROTOCOL:
                return "sendspin_AABBCCDDEEFF"
            if key == "min_volume":
                return 0
            if key == "max_volume":
                return 100
            return default

        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_get_raw_player_config_value
        )

        universal_player = MockPlayer(
            MockProvider("universal_player", mass=mock_mass), "universal_123", "Kantoor"
        )
        universal_player._attr_supported_features = set()
        sendspin_player = MockPlayer(
            MockProvider("sendspin", mass=mock_mass),
            "sendspin_AABBCCDDEEFF",
            "Kantoor Sendspin",
            player_type=PlayerType.PROTOCOL,
        )
        sendspin_player._attr_supported_features.add(PlayerFeature.PLAY_ANNOUNCEMENT)
        airplay_player = MockPlayer(
            MockProvider("airplay", mass=mock_mass),
            "airplay_AABBCCDDEEFF",
            "Kantoor AirPlay",
            player_type=PlayerType.PROTOCOL,
        )
        airplay_player._attr_supported_features.add(PlayerFeature.PLAY_ANNOUNCEMENT)

        controller._players = {
            "universal_123": universal_player,
            "sendspin_AABBCCDDEEFF": sendspin_player,
            "airplay_AABBCCDDEEFF": airplay_player,
        }
        # airplay outranks sendspin, but the user picked sendspin
        universal_player.set_linked_output_protocols(
            [
                LinkedOutputProtocol(
                    output_protocol_id="airplay_AABBCCDDEEFF",
                    protocol_domain="airplay",
                    priority=10,
                ),
                LinkedOutputProtocol(
                    output_protocol_id="sendspin_AABBCCDDEEFF",
                    protocol_domain="sendspin",
                    priority=40,
                ),
            ]
        )

        target = controller._get_control_target(
            universal_player, PlayerFeature.PLAY_ANNOUNCEMENT, require_active=False
        )
        assert target == sendspin_player

    def test_fallback_ignores_preference_for_another_players_output(
        self, mock_mass: MagicMock
    ) -> None:
        """A preference left behind by a relink does not steal another speaker's output."""
        controller = PlayerController(mock_mass)
        mock_mass.players = controller

        def _get_raw_player_config_value(
            _player_id: str, key: str, default: str | int | None = None
        ) -> str | int | None:
            if key == CONF_PREFERRED_OUTPUT_PROTOCOL:
                return "airplay_998877665544"
            if key == "min_volume":
                return 0
            if key == "max_volume":
                return 100
            return default

        mock_mass.config.get_raw_player_config_value = MagicMock(
            side_effect=_get_raw_player_config_value
        )

        universal_player = MockPlayer(
            MockProvider("universal_player", mass=mock_mass), "universal_123", "Kantoor"
        )
        universal_player._attr_supported_features = set()
        own_player = MockPlayer(
            MockProvider("sendspin", mass=mock_mass),
            "sendspin_AABBCCDDEEFF",
            "Kantoor Sendspin",
            player_type=PlayerType.PROTOCOL,
        )
        own_player._attr_supported_features.add(PlayerFeature.PLAY_ANNOUNCEMENT)
        # the preference now points at an output belonging to a different speaker
        other_player = MockPlayer(
            MockProvider("airplay", mass=mock_mass),
            "airplay_998877665544",
            "Woonkamer AirPlay",
            player_type=PlayerType.PROTOCOL,
        )
        other_player._attr_supported_features.add(PlayerFeature.PLAY_ANNOUNCEMENT)

        controller._players = {
            "universal_123": universal_player,
            "sendspin_AABBCCDDEEFF": own_player,
            "airplay_998877665544": other_player,
        }
        universal_player.set_linked_output_protocols(
            [
                LinkedOutputProtocol(
                    output_protocol_id="sendspin_AABBCCDDEEFF",
                    protocol_domain="sendspin",
                    priority=40,
                )
            ]
        )

        target = controller._get_control_target(
            universal_player, PlayerFeature.PLAY_ANNOUNCEMENT, require_active=False
        )
        assert target == own_player

    def test_require_active_skips_the_fallback(self, mock_mass: MagicMock) -> None:
        """With require_active there is no fallback to an idle linked protocol."""
        controller = PlayerController(mock_mass)
        mock_mass.players = controller

        universal_player = MockPlayer(
            MockProvider("universal_player", mass=mock_mass),
            "universal_123",
            "Kantoor",
        )
        universal_player._attr_supported_features = set()
        airplay_player = MockPlayer(
            MockProvider("airplay", mass=mock_mass),
            "airplay_AABBCCDDEEFF",
            "Kantoor AirPlay",
            player_type=PlayerType.PROTOCOL,
        )
        airplay_player._attr_supported_features.add(PlayerFeature.PLAY_ANNOUNCEMENT)

        controller._players = {
            "universal_123": universal_player,
            "airplay_AABBCCDDEEFF": airplay_player,
        }
        universal_player.set_linked_output_protocols(
            [
                LinkedOutputProtocol(
                    output_protocol_id="airplay_AABBCCDDEEFF",
                    protocol_domain="airplay",
                    priority=10,
                )
            ]
        )

        assert (
            controller._get_control_target(
                universal_player, PlayerFeature.PLAY_ANNOUNCEMENT, require_active=True
            )
            is None
        )


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
                LinkedOutputProtocol(
                    output_protocol_id="airplay_sonos",
                    protocol_domain="airplay",
                    priority=10,
                )
            ]
        )
        wiim_player.set_linked_output_protocols(
            [
                LinkedOutputProtocol(
                    output_protocol_id="airplay_wiim",
                    protocol_domain="airplay",
                    priority=10,
                )
            ]
        )

        controller._players = {
            "sonos_123": sonos_player,
            "wiim_456": wiim_player,
            "airplay_sonos": sonos_airplay,
            "airplay_wiim": wiim_airplay,
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
                LinkedOutputProtocol(
                    output_protocol_id="airplay_sonos",
                    protocol_domain="airplay",
                    priority=10,
                )
            ]
        )
        wiim_player.set_linked_output_protocols(
            [
                LinkedOutputProtocol(
                    output_protocol_id="airplay_wiim",
                    protocol_domain="airplay",
                    priority=10,
                )
            ]
        )
        wiim_player.set_active_output_protocol("airplay_wiim")
        wiim_player.set_protocol_parent_id("airplay_wiim")

        # Wire up mock_mass.players to controller so player lookups resolve
        mock_mass.players = controller

        controller._players = {
            "sonos_123": sonos_a,
            "sonos_456": sonos_b,
            "wiim_789": wiim_player,
            "airplay_sonos": sonos_airplay,
            "airplay_wiim": wiim_airplay,
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
                LinkedOutputProtocol(
                    output_protocol_id="dlna_sonos",
                    protocol_domain="dlna",
                    priority=50,  # Lower priority (higher number)
                ),
                LinkedOutputProtocol(
                    output_protocol_id="airplay_sonos",
                    protocol_domain="airplay",
                    priority=10,  # Higher priority (lower number)
                ),
            ]
        )
        wiim_player.set_linked_output_protocols(
            [
                LinkedOutputProtocol(
                    output_protocol_id="dlna_wiim",
                    protocol_domain="dlna",
                    priority=50,
                ),
                LinkedOutputProtocol(
                    output_protocol_id="airplay_wiim",
                    protocol_domain="airplay",
                    priority=10,
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

        # A group already on DLNA must be moved off it as well, because DLNA cannot
        # group members itself
        protocol_members, _native_members, protocol_player, protocol_domain = (
            controller._translate_members_for_protocols(
                parent_player=sonos_player,
                player_ids=["wiim_456"],
                parent_protocol_player=sonos_dlna,
                parent_protocol_domain="dlna",
            )
        )

        assert protocol_members == ["airplay_wiim"]
        assert protocol_domain == "airplay"
        assert protocol_player == sonos_airplay


class TestJoinActiveNativeSession:
    """
    Grouping a child onto a parent that is already playing natively.

    A child's preferred output protocol must only steer protocol selection when the child
    initiates its own playback. When the child joins a parent that already holds an active
    native session, it should adopt native grouping if compatible, instead of forcing the
    whole group onto the child's preferred protocol.
    """

    @staticmethod
    def _mark_native_playing(player: MockPlayer) -> None:
        """Put the player into a live native playback session."""
        player._attr_playback_state = PlaybackState.PLAYING
        player._cache.clear()
        player.set_active_output_protocol("native")

    @staticmethod
    def _link_airplay(native_player: MockPlayer, airplay_id: str) -> None:
        """Link an AirPlay protocol player to a native player."""
        native_player.set_linked_output_protocols(
            [
                LinkedOutputProtocol(
                    output_protocol_id=airplay_id,
                    protocol_domain="airplay",
                    priority=10,
                )
            ]
        )

    def _build_topology(
        self, mock_mass: MagicMock
    ) -> tuple[PlayerController, dict[str, MockPlayer]]:
        """
        Build parent Sonos A + child Sonos B, both AirPlay-capable and natively groupable.

        The parent also exposes an AirPlay protocol player that supports SET_MEMBERS, so the
        child's preferred AirPlay protocol *would* be selectable if it were given priority.
        """
        controller = PlayerController(mock_mass)
        sonos_provider = MockProvider("sonos", instance_id="sonos_instance", mass=mock_mass)
        airplay_provider = MockProvider("airplay", instance_id="airplay_instance", mass=mock_mass)

        sonos_a = MockPlayer(
            sonos_provider,
            "sonos_a",
            "Office",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:01"},
        )
        sonos_a._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        sonos_a._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        sonos_a._attr_can_group_with = {"sonos_b"}
        sonos_a._cache.clear()

        sonos_b = MockPlayer(
            sonos_provider,
            "sonos_b",
            "Bathroom",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:02"},
        )
        sonos_b._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        sonos_b._attr_can_group_with = {"sonos_a"}
        sonos_b._cache.clear()

        airplay_a = MockPlayer(
            airplay_provider,
            "airplay_a",
            "Office (AirPlay)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:01"},
        )
        airplay_a._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        airplay_a._attr_can_group_with = {"airplay_b"}
        airplay_a._cache.clear()

        airplay_b = MockPlayer(
            airplay_provider,
            "airplay_b",
            "Bathroom (AirPlay)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:02"},
        )
        airplay_b._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        airplay_b._attr_can_group_with = {"airplay_a"}
        airplay_b._cache.clear()

        self._link_airplay(sonos_a, "airplay_a")
        self._link_airplay(sonos_b, "airplay_b")

        mock_mass.players = controller
        players = {
            "sonos_a": sonos_a,
            "sonos_b": sonos_b,
            "airplay_a": airplay_a,
            "airplay_b": airplay_b,
        }
        controller._players = dict(players)
        for player in players.values():
            player.update_state(signal_event=False)
        return controller, players

    def _add_airplay_only_child(
        self, controller: PlayerController, players: dict[str, MockPlayer], mock_mass: MagicMock
    ) -> None:
        """
        Add a WiiM child that can only group via AirPlay (no native grouping with Sonos).

        Different provider instance and absent from the parent's can_group_with, so
        _can_use_native_grouping returns False for it.
        """
        wiim_provider = MockProvider("wiim", instance_id="wiim_instance", mass=mock_mass)
        airplay_provider = MockProvider("airplay", instance_id="airplay_instance", mass=mock_mass)
        wiim_c = MockPlayer(
            wiim_provider,
            "wiim_c",
            "Bedroom",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:03"},
        )
        wiim_c._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        wiim_c._cache.clear()
        airplay_c = MockPlayer(
            airplay_provider,
            "airplay_c",
            "Bedroom (AirPlay)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:03"},
        )
        airplay_c._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        airplay_c._attr_can_group_with = {"airplay_a"}
        airplay_c._cache.clear()
        self._link_airplay(wiim_c, "airplay_c")
        # Allow the parent's AirPlay protocol to group with the new AirPlay child.
        players["airplay_a"]._attr_can_group_with = {"airplay_b", "airplay_c"}
        players["airplay_a"]._cache.clear()
        controller._players["wiim_c"] = wiim_c
        controller._players["airplay_c"] = airplay_c
        wiim_c.update_state(signal_event=False)
        airplay_c.update_state(signal_event=False)

    def test_native_active_parent_ignores_child_preferred_protocol(
        self, mock_mass: MagicMock
    ) -> None:
        """A child joining a natively-playing parent groups natively, not via preferred AirPlay."""

        def _get_raw(
            player_id: str, key: str, default: str | int | None = None
        ) -> str | int | None:
            if key == CONF_PREFERRED_OUTPUT_PROTOCOL and player_id == "sonos_b":
                return "airplay_b"
            if key == "min_volume":
                return 0
            if key == "max_volume":
                return 100
            return default

        mock_mass.config.get_raw_player_config_value = MagicMock(side_effect=_get_raw)

        controller, players = self._build_topology(mock_mass)
        # Parent A is already playing natively.
        self._mark_native_playing(players["sonos_a"])

        protocol_members, native_members, _, _ = controller._translate_members_for_protocols(
            parent_player=players["sonos_a"],
            player_ids=["sonos_b"],
            parent_protocol_player=None,
            parent_protocol_domain=None,
        )

        # Native grouping is used; the child's AirPlay preference is ignored on join.
        assert native_members == ["sonos_b"]
        assert protocol_members == []

    def test_fresh_group_still_honors_child_preferred_protocol(self, mock_mass: MagicMock) -> None:
        """When the parent is not playing, the child's preferred protocol still steers selection."""

        def _get_raw(
            player_id: str, key: str, default: str | int | None = None
        ) -> str | int | None:
            if key == CONF_PREFERRED_OUTPUT_PROTOCOL and player_id == "sonos_b":
                return "airplay_b"
            if key == "min_volume":
                return 0
            if key == "max_volume":
                return 100
            return default

        mock_mass.config.get_raw_player_config_value = MagicMock(side_effect=_get_raw)

        controller, players = self._build_topology(mock_mass)
        # Parent A has no active session (active_output_protocol stays None).
        assert players["sonos_a"].active_output_protocol is None

        protocol_members, native_members, _, protocol_domain = (
            controller._translate_members_for_protocols(
                parent_player=players["sonos_a"],
                player_ids=["sonos_b"],
                parent_protocol_player=None,
                parent_protocol_domain=None,
            )
        )

        # Same topology as the active-native case, but here the preference applies.
        assert protocol_members == ["airplay_b"]
        assert native_members == []
        assert protocol_domain == "airplay"

    def test_idle_parent_with_stale_native_protocol_honors_child_preferred(
        self, mock_mass: MagicMock
    ) -> None:
        """A stopped parent (active protocol lingers briefly) does not force the native-join path."""

        def _get_raw(
            player_id: str, key: str, default: str | int | None = None
        ) -> str | int | None:
            if key == CONF_PREFERRED_OUTPUT_PROTOCOL and player_id == "sonos_b":
                return "airplay_b"
            if key == "min_volume":
                return 0
            if key == "max_volume":
                return 100
            return default

        mock_mass.config.get_raw_player_config_value = MagicMock(side_effect=_get_raw)

        controller, players = self._build_topology(mock_mass)
        # Parent A stopped: active protocol still reads "native" but playback is idle.
        players["sonos_a"].set_active_output_protocol("native")
        assert players["sonos_a"].state.playback_state == PlaybackState.IDLE

        protocol_members, native_members, _, protocol_domain = (
            controller._translate_members_for_protocols(
                parent_player=players["sonos_a"],
                player_ids=["sonos_b"],
                parent_protocol_player=None,
                parent_protocol_domain=None,
            )
        )

        # No live session to join, so the child's preference applies.
        assert protocol_members == ["airplay_b"]
        assert native_members == []
        assert protocol_domain == "airplay"

    def test_native_active_parent_airplay_only_child_uses_protocol(
        self, mock_mass: MagicMock
    ) -> None:
        """An AirPlay-only child (cannot group natively) still joins via its preferred protocol."""

        def _get_raw(
            player_id: str, key: str, default: str | int | None = None
        ) -> str | int | None:
            if key == CONF_PREFERRED_OUTPUT_PROTOCOL and player_id == "wiim_c":
                return "airplay_c"
            if key == "min_volume":
                return 0
            if key == "max_volume":
                return 100
            return default

        mock_mass.config.get_raw_player_config_value = MagicMock(side_effect=_get_raw)

        controller, players = self._build_topology(mock_mass)
        self._add_airplay_only_child(controller, players, mock_mass)
        self._mark_native_playing(players["sonos_a"])

        protocol_members, native_members, _, protocol_domain = (
            controller._translate_members_for_protocols(
                parent_player=players["sonos_a"],
                player_ids=["wiim_c"],
                parent_protocol_player=None,
                parent_protocol_domain=None,
            )
        )

        # No native path exists for the WiiM, so it falls back to AirPlay grouping.
        assert protocol_members == ["airplay_c"]
        assert native_members == []
        assert protocol_domain == "airplay"

    def test_native_active_parent_protocol_only_child_switches_via_common_protocol(
        self, mock_mass: MagicMock
    ) -> None:
        """
        A child with no native path (and no explicit preference) still forces the switch.

        Even without a configured preferred protocol, a child that can only reach the group via
        a shared protocol (here AirPlay, which the natively-playing parent also supports) is
        grouped via that protocol - moving the group onto it.
        """
        controller, players = self._build_topology(mock_mass)
        self._add_airplay_only_child(controller, players, mock_mass)
        self._mark_native_playing(players["sonos_a"])

        # No preferred_output_protocol override - the WiiM resolves via the common-protocol path.
        protocol_members, native_members, _, protocol_domain = (
            controller._translate_members_for_protocols(
                parent_player=players["sonos_a"],
                player_ids=["wiim_c"],
                parent_protocol_player=None,
                parent_protocol_domain=None,
            )
        )

        assert protocol_members == ["airplay_c"]
        assert native_members == []
        assert protocol_domain == "airplay"

    @pytest.mark.parametrize(
        "member_order",
        [["wiim_c", "sonos_b"], ["sonos_b", "wiim_c"]],
    )
    def test_native_active_parent_keeps_mixed_batch_cohesive(
        self, mock_mass: MagicMock, member_order: list[str]
    ) -> None:
        """
        A mixed batch stays cohesive on one protocol regardless of member order.

        When an AirPlay-only child forces the group onto AirPlay, the natively-groupable Sonos
        child adopts that same protocol rather than splitting off into a native sub-group -
        whether it is added before or after the AirPlay-only child.
        """
        controller, players = self._build_topology(mock_mass)
        self._add_airplay_only_child(controller, players, mock_mass)
        self._mark_native_playing(players["sonos_a"])

        protocol_members, native_members, _, protocol_domain = (
            controller._translate_members_for_protocols(
                parent_player=players["sonos_a"],
                player_ids=member_order,
                parent_protocol_player=None,
                parent_protocol_domain=None,
            )
        )

        assert native_members == []
        assert set(protocol_members) == {"airplay_c", "airplay_b"}
        assert protocol_domain == "airplay"


class TestSessionBoundNativeGrouping:
    """
    Grouping a parent whose native grouping only works while it renders its own stream.

    Such native members are attached to that very stream, so they cannot stay natively
    grouped once the group is moved onto one of the parent's output protocols. Parents
    with device-side native grouping are unaffected and keep their hybrid groups.
    """

    @staticmethod
    def _link_bridge(player: MockPlayer, bridge_id: str) -> None:
        """Link a bridge protocol player to a native player."""
        player.set_linked_output_protocols(
            [
                LinkedOutputProtocol(
                    output_protocol_id=bridge_id,
                    protocol_domain="sendspin",
                    priority=40,
                )
            ]
        )

    def _build_topology(
        self,
        mock_mass: MagicMock,
        parent_class: type[MockPlayer],
        with_orphan: bool = False,
    ) -> tuple[PlayerController, dict[str, MockPlayer]]:
        """
        Build a leader with a natively groupable member and a bridge-only member.

        The leader and its member speak the same (speaker) provider, so they group natively.
        Both also expose a bridge protocol player, which is the only way to reach the third
        member: a native player of the bridge domain.

        :param mock_mass: The mocked MusicAssistant instance.
        :param parent_class: The player class to build the leader from.
        :param with_orphan: Add a second natively groupable member without a bridge protocol.
        """
        controller = PlayerController(mock_mass)
        speaker_provider = MockProvider("speaker", instance_id="speaker_instance", mass=mock_mass)
        bridge_provider = MockProvider("sendspin", instance_id="sendspin_instance", mass=mock_mass)

        leader = parent_class(speaker_provider, "speaker_leader", "Living Room")
        leader._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        leader._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        leader._cache.clear()

        member = MockPlayer(speaker_provider, "speaker_member", "Kitchen")
        member._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        member._cache.clear()

        bridge_leader = MockPlayer(
            bridge_provider,
            "bridge_leader",
            "Living Room (Bridge)",
            player_type=PlayerType.PROTOCOL,
        )
        bridge_leader._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        bridge_leader._cache.clear()
        bridge_leader.set_protocol_parent_id("speaker_leader")

        bridge_member = MockPlayer(
            bridge_provider,
            "bridge_member",
            "Kitchen (Bridge)",
            player_type=PlayerType.PROTOCOL,
        )
        bridge_member._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        bridge_member._cache.clear()
        bridge_member.set_protocol_parent_id("speaker_member")

        # native player of the bridge domain: only reachable through the bridge protocol
        bridge_only = MockPlayer(bridge_provider, "bridge_only", "Voice Assistant")
        bridge_only._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        bridge_only._cache.clear()

        self._link_bridge(leader, "bridge_leader")
        self._link_bridge(member, "bridge_member")

        players: dict[str, MockPlayer] = {
            "speaker_leader": leader,
            "speaker_member": member,
            "bridge_leader": bridge_leader,
            "bridge_member": bridge_member,
            "bridge_only": bridge_only,
        }
        if with_orphan:
            orphan = MockPlayer(speaker_provider, "speaker_orphan", "Bedroom")
            orphan._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
            orphan._cache.clear()
            players["speaker_orphan"] = orphan

        mock_mass.players = controller
        controller._players = dict(players)
        for player in players.values():
            player.update_state(signal_event=False)
        return controller, players

    @pytest.mark.parametrize(
        "member_order",
        [
            ["speaker_member", "bridge_only"],
            ["bridge_only", "speaker_member"],
        ],
    )
    def test_native_members_follow_the_group_protocol(
        self, mock_mass: MagicMock, member_order: list[str]
    ) -> None:
        """A natively groupable member moves onto the protocol the group ends up on."""
        controller, players = self._build_topology(mock_mass, SessionBoundMockPlayer)

        protocol_members, native_members, _, protocol_domain = (
            controller._translate_members_for_protocols(
                parent_player=players["speaker_leader"],
                player_ids=member_order,
                parent_protocol_player=None,
                parent_protocol_domain=None,
            )
        )

        assert native_members == []
        assert set(protocol_members) == {"bridge_only", "bridge_member"}
        assert protocol_domain == "sendspin"

    def test_member_without_the_group_protocol_is_refused(
        self, mock_mass: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A native member that lacks the group's protocol is dropped, the others still move."""
        controller, players = self._build_topology(
            mock_mass, SessionBoundMockPlayer, with_orphan=True
        )

        with caplog.at_level(logging.WARNING):
            protocol_members, native_members, _, _ = controller._translate_members_for_protocols(
                parent_player=players["speaker_leader"],
                player_ids=["speaker_member", "speaker_orphan", "bridge_only"],
                parent_protocol_player=None,
                parent_protocol_domain=None,
            )

        assert native_members == []
        assert set(protocol_members) == {"bridge_only", "bridge_member"}
        assert (
            "Cannot group Bedroom with Living Room: the group plays through the sendspin "
            "protocol, which Bedroom does not support" in caplog.text
        )

    def test_device_side_parent_keeps_its_hybrid_group(self, mock_mass: MagicMock) -> None:
        """A parent with device-side native grouping keeps its members natively grouped."""
        controller, players = self._build_topology(mock_mass, MockPlayer)

        protocol_members, native_members, _, protocol_domain = (
            controller._translate_members_for_protocols(
                parent_player=players["speaker_leader"],
                player_ids=["speaker_member", "bridge_only"],
                parent_protocol_player=None,
                parent_protocol_domain=None,
            )
        )

        assert native_members == ["speaker_member"]
        assert protocol_members == ["bridge_only"]
        assert protocol_domain == "sendspin"


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
                LinkedOutputProtocol(
                    output_protocol_id="airplay_sonos",
                    protocol_domain="airplay",
                    priority=10,
                )
            ]
        )

        # Wire up mock_mass.players to controller so player lookups resolve
        mock_mass.players = controller

        controller._players = {
            "sonos_123": sonos_player,
            "sonos_456": sonos_player_b,
            "airplay_sonos": sonos_airplay,
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
                LinkedOutputProtocol(
                    output_protocol_id="airplay_other",
                    protocol_domain="airplay",
                    priority=10,
                ),
            ]
        )

        sonos_player.set_linked_output_protocols(
            [
                LinkedOutputProtocol(
                    output_protocol_id="airplay_sonos",
                    protocol_domain="airplay",
                    priority=10,
                )
            ]
        )
        sonos_player.set_active_output_protocol("airplay_sonos")

        # Wire up mock_mass.players to controller so player lookups resolve
        mock_mass.players = controller

        controller._players = {
            "sonos_123": sonos_player,
            "sonos_456": sonos_player_b,
            "wiim_789": wiim_player,
            "airplay_sonos": sonos_airplay,
            "airplay_other": airplay_other,
        }

        # Clear cache after setting linked protocols
        sonos_player._cache.clear()
        wiim_player._cache.clear()

        # Refresh state after modifying attributes and registering with controller
        # (refresh_state: registration changed cross-player state without the
        # production fan-out running, as events are suppressed in this test)
        # IMPORTANT: Update protocol players FIRST, then parent players
        sonos_airplay.refresh_state(signal_event=False)
        airplay_other.refresh_state(signal_event=False)
        sonos_player.refresh_state(signal_event=False)
        sonos_player_b.refresh_state(signal_event=False)
        wiim_player.refresh_state(signal_event=False)

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
                LinkedOutputProtocol(
                    output_protocol_id="airplay_sonos",
                    protocol_domain="airplay",
                    priority=10,
                ),
                LinkedOutputProtocol(
                    output_protocol_id="dlna_sonos",
                    protocol_domain="dlna",
                    priority=50,
                ),
            ]
        )

        wiim_player.set_linked_output_protocols(
            [
                LinkedOutputProtocol(
                    output_protocol_id="airplay_other",
                    protocol_domain="airplay",
                    priority=10,
                ),
            ]
        )

        # Clear cache after setting linked protocols (output_protocols is cached)
        sonos_player._cache.clear()
        wiim_player._cache.clear()

        # Wire up mock_mass.players to controller so player lookups resolve
        mock_mass.players = controller

        controller._players = {
            "sonos_123": sonos_player,
            "sonos_456": sonos_player_b,
            "wiim_789": wiim_player,
            "airplay_sonos": sonos_airplay,
            "airplay_other": airplay_other,
            "dlna_sonos": sonos_dlna,
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
                LinkedOutputProtocol(
                    output_protocol_id="airplay_sonos_1",
                    protocol_domain="airplay",
                    priority=10,
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
                LinkedOutputProtocol(
                    output_protocol_id="airplay_sonos_1",
                    protocol_domain="airplay",
                    priority=10,
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

    def _build_session_bound_group(
        self, mock_mass: MagicMock, playback_state: PlaybackState
    ) -> tuple[PlayerController, dict[str, MockPlayer]]:
        """
        Build a leader that renders its own stream, one native member and a bridge-only player.

        :param mock_mass: The mocked MusicAssistant instance.
        :param playback_state: The state the leader is in when the switch happens.
        :return: The controller and its players by ID.
        """
        controller = PlayerController(mock_mass)
        speaker_provider = MockProvider("speaker", instance_id="speaker_instance", mass=mock_mass)
        bridge_provider = MockProvider("sendspin", instance_id="sendspin_instance", mass=mock_mass)

        leader = SessionBoundMockPlayer(speaker_provider, "speaker_leader", "Living Room")
        leader._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        leader._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        leader._attr_group_members = ["speaker_leader", "speaker_member"]
        leader._attr_playback_state = playback_state
        leader._cache.clear()

        member = MockPlayer(speaker_provider, "speaker_member", "Kitchen")
        member._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        member._cache.clear()

        bridge_only = MockPlayer(bridge_provider, "bridge_only", "Voice Assistant")
        bridge_only._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        bridge_only._cache.clear()

        players: dict[str, MockPlayer] = {
            "speaker_leader": leader,
            "speaker_member": member,
            "bridge_only": bridge_only,
        }
        for parent_id, bridge_id, bridge_name in (
            ("speaker_leader", "bridge_leader", "Living Room (Bridge)"),
            ("speaker_member", "bridge_member", "Kitchen (Bridge)"),
        ):
            bridge = MockPlayer(
                bridge_provider, bridge_id, bridge_name, player_type=PlayerType.PROTOCOL
            )
            bridge._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
            bridge._cache.clear()
            bridge.set_protocol_parent_id(parent_id)
            players[bridge_id] = bridge
            players[parent_id].set_linked_output_protocols(
                [
                    LinkedOutputProtocol(
                        output_protocol_id=bridge_id,
                        protocol_domain="sendspin",
                        priority=40,
                    )
                ]
            )

        mock_mass.players = controller
        controller._players = dict(players)
        for registered_player in players.values():
            registered_player.update_state(signal_event=False)
        leader.set_active_output_protocol("native")
        return controller, players

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
                LinkedOutputProtocol(
                    output_protocol_id="airplay_sonos",
                    protocol_domain="airplay",
                    priority=10,
                )
            ]
        )

        mock_mass.players = controller
        controller._players = {
            "sonos_123": sonos_player,
            "sonos_456": sonos_player_b,
            "airplay_sonos": sonos_airplay,
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
                LinkedOutputProtocol(
                    output_protocol_id="airplay_sonos",
                    protocol_domain="airplay",
                    priority=10,
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
        """
        Test that _handle_set_members does NOT restart playback.

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
                LinkedOutputProtocol(
                    output_protocol_id="airplay_sonos",
                    protocol_domain="airplay",
                    priority=10,
                )
            ]
        )

        mock_mass.players = controller
        controller._players = {
            "sonos_123": sonos_player,
            "sonos_456": sonos_player_b,
            "airplay_sonos": sonos_airplay,
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

    @pytest.mark.parametrize("playback_state", [PlaybackState.PLAYING, PlaybackState.PAUSED])
    async def test_native_session_is_stopped_and_members_migrate(
        self, mock_mass: MagicMock, playback_state: PlaybackState
    ) -> None:
        """
        A parent that renders its own stream hands its native members over to the new protocol.

        The members join the protocol group in the same call as the new member, after which
        the native session releases them and stops. Playback resumes only for a player that
        was playing, so adding a member to a paused group does not start it.
        """
        controller, players = self._build_session_bound_group(mock_mass, playback_state)
        leader = players["speaker_leader"]
        bridge_leader = players["bridge_leader"]

        # record the sequence of provider level calls the switch performs
        calls: list[tuple[str, Any]] = []
        leader_set_members = leader.set_members
        bridge_set_members = bridge_leader.set_members

        async def record_leader_set_members(
            player_ids_to_add: list[str] | None = None,
            player_ids_to_remove: list[str] | None = None,
        ) -> None:
            calls.append(("leader_set_members", player_ids_to_remove))
            await leader_set_members(player_ids_to_add, player_ids_to_remove)

        async def record_bridge_set_members(
            player_ids_to_add: list[str] | None = None,
            player_ids_to_remove: list[str] | None = None,
        ) -> None:
            calls.append(("bridge_set_members", player_ids_to_add))
            await bridge_set_members(player_ids_to_add, player_ids_to_remove)

        async def record_leader_stop() -> None:
            calls.append(("leader_stop", None))

        leader.set_members = record_leader_set_members  # type: ignore[method-assign]
        leader.stop = record_leader_stop  # type: ignore[method-assign]
        bridge_leader.set_members = record_bridge_set_members  # type: ignore[method-assign]
        controller.cmd_resume = AsyncMock()  # type: ignore[method-assign]
        controller._handle_set_members = AsyncMock()  # type: ignore[method-assign]

        await controller._forward_protocol_set_members(
            parent_player=leader,
            parent_protocol_player=bridge_leader,
            protocol_members_to_add=["bridge_only"],
            protocol_members_to_remove=[],
        )

        assert calls == [
            # the native member joins the protocol group together with the new member
            ("bridge_set_members", ["bridge_only", "bridge_member"]),
            ("leader_set_members", ["speaker_member"]),
            ("leader_stop", None),
        ]
        assert leader.active_output_protocol == "bridge_leader"
        # Only a player that was playing resumes: adding a member to a paused group
        # hands the output over without starting playback.
        if playback_state == PlaybackState.PLAYING:
            controller.cmd_resume.assert_awaited_once_with("speaker_leader")
        else:
            controller.cmd_resume.assert_not_awaited()
        controller._handle_set_members.assert_not_awaited()

    async def test_joining_member_protocol_is_active_before_its_stream_starts(
        self, mock_mass: MagicMock
    ) -> None:
        """
        A joining member's parent knows which protocol carries its audio before its stream starts.

        The member's stream is started from inside set_members, and providers resolve the
        member's volume control while starting it. A device that also exposes a higher
        priority sibling interface (e.g. a soundbar that is both Chromecast and AirPlay)
        would otherwise resolve volume to that sibling and apply its volume semantics to
        a stream it does not attenuate.
        """
        controller = PlayerController(mock_mass)

        airplay_provider = MockProvider("airplay", instance_id="airplay", mass=mock_mass)
        cast_provider = MockProvider("chromecast", instance_id="chromecast", mass=mock_mass)

        leader = MockPlayer(airplay_provider, "leader", "Kitchen")
        leader_airplay = MockPlayer(
            airplay_provider,
            "leader_airplay",
            "Kitchen (AirPlay)",
            player_type=PlayerType.PROTOCOL,
        )
        leader_airplay._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        leader_airplay.set_protocol_parent_id("leader")

        # the joining device exposes both a Chromecast and an AirPlay interface, and has no
        # native volume of its own, so its volume control is one of the two protocol players
        joiner = MockPlayer(cast_provider, "joiner", "Living Room")
        joiner._attr_supported_features = set()
        joiner_cast = MockPlayer(
            cast_provider,
            "joiner_cast",
            "Living Room (Cast)",
            player_type=PlayerType.PROTOCOL,
        )
        joiner_cast.set_protocol_parent_id("joiner")
        joiner_airplay = MockPlayer(
            airplay_provider,
            "joiner_airplay",
            "Living Room (AirPlay)",
            player_type=PlayerType.PROTOCOL,
        )
        joiner_airplay._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        joiner_airplay.set_protocol_parent_id("joiner")

        leader.set_linked_output_protocols(
            [
                LinkedOutputProtocol(
                    output_protocol_id="leader_airplay",
                    protocol_domain="airplay",
                    priority=10,
                )
            ]
        )
        joiner.set_linked_output_protocols(
            [
                LinkedOutputProtocol(
                    output_protocol_id="joiner_cast",
                    protocol_domain="chromecast",
                    priority=10,
                ),
                LinkedOutputProtocol(
                    output_protocol_id="joiner_airplay",
                    protocol_domain="airplay",
                    priority=20,
                ),
            ]
        )

        mock_mass.players = controller
        controller._players = {
            player.player_id: player
            for player in (leader, leader_airplay, joiner, joiner_cast, joiner_airplay)
        }
        for player in controller._players.values():
            player.update_state(signal_event=False)

        # While the device sits idle its Chromecast interface owns the volume: the winner is
        # decided by the hardcoded per-domain control priority, not by the link priorities.
        assert joiner.volume_control == "joiner_cast"

        observed_volume_controls: list[str] = []

        async def record_set_members(
            player_ids_to_add: list[str] | None = None,  # noqa: ARG001
            player_ids_to_remove: list[str] | None = None,  # noqa: ARG001
        ) -> None:
            observed_volume_controls.append(joiner.volume_control)

        leader_airplay.set_members = record_set_members  # type: ignore[method-assign]
        controller._handle_set_members = AsyncMock()  # type: ignore[method-assign]

        await controller._forward_protocol_set_members(
            parent_player=leader,
            parent_protocol_player=leader_airplay,
            protocol_members_to_add=["joiner_airplay"],
            protocol_members_to_remove=[],
        )

        assert observed_volume_controls == ["joiner_airplay"]


class TestNativeProtocolPlayerGrouping:
    """Tests for grouping with native protocol players (e.g., native AirPlay like Apple TV)."""

    def test_native_airplay_groups_with_protocol_linked_player(self, mock_mass: MagicMock) -> None:
        """
        Test grouping a native AirPlay player (Apple TV) with a protocol-linked player (Sonos).

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
                LinkedOutputProtocol(
                    output_protocol_id="airplay_sonos",
                    protocol_domain="airplay",
                    priority=10,
                )
            ]
        )

        mock_mass.players = controller
        controller._players = {
            "apple_tv_1": apple_tv,
            "sonos_badkamer": sonos_player,
            "airplay_sonos": sonos_airplay,
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
        """
        Test that __final_group_members translates protocol player IDs to visible IDs.

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

        sonos_airplay.update_state(signal_event=False)
        sonos_player.update_state(signal_event=False)
        apple_tv.update_state(signal_event=False)

        # Final group_members should show visible player IDs
        final_members = apple_tv.state.group_members
        assert "apple_tv_1" in final_members
        assert "sonos_1" in final_members
        # Protocol player ID should NOT appear in final state
        assert "airplay_sonos" not in final_members


class TestFinalActiveGroupTranslation:
    """Tests for __final_active_group with translated protocol player IDs."""

    def test_active_group_uses_translated_group_members(self, mock_mass: MagicMock) -> None:
        """Test that visible players resolve active_group from translated group members."""
        controller = PlayerController(mock_mass)

        group_provider = MockProvider(
            "universal_group", instance_id="universal_group", mass=mock_mass
        )
        airplay_provider = MockProvider("airplay", instance_id="airplay", mass=mock_mass)
        sonos_provider = MockProvider("sonos", instance_id="sonos", mass=mock_mass)

        group_player = MockPlayer(
            group_provider,
            "group_1",
            "House Group",
            player_type=PlayerType.GROUP,
        )
        group_player._attr_playback_state = PlaybackState.PLAYING
        group_player._attr_group_members = ["airplay_sonos"]
        group_player._cache.clear()

        sonos_player = MockPlayer(sonos_provider, "sonos_1", "Sonos")
        sonos_player._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        sonos_player._cache.clear()

        sonos_airplay = MockPlayer(
            airplay_provider,
            "airplay_sonos",
            "Sonos (AirPlay)",
            player_type=PlayerType.PROTOCOL,
        )
        sonos_airplay.set_protocol_parent_id("sonos_1")
        sonos_airplay._cache.clear()

        mock_mass.players = controller
        controller._players = {
            "group_1": group_player,
            "sonos_1": sonos_player,
            "airplay_sonos": sonos_airplay,
        }

        group_player.set_initialized()
        sonos_player.set_initialized()
        sonos_airplay.set_initialized()

        group_player.update_state(signal_event=False)
        sonos_airplay.update_state(signal_event=False)
        sonos_player.update_state(signal_event=False)

        assert sonos_player.state.active_group == "group_1"
        assert sonos_airplay.state.active_group is None


class TestFinalSyncedToWithNativeProtocolParent:
    """Tests for __final_synced_to when sync parent is a native protocol player."""

    def test_synced_to_native_airplay_player(self, mock_mass: MagicMock) -> None:
        """
        Test that synced_to correctly shows native AirPlay player as parent.

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
        apple_tv._attr_group_members = ["apple_tv_1", "airplay_sonos"]
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
                LinkedOutputProtocol(
                    output_protocol_id="airplay_sonos",
                    protocol_domain="airplay",
                    priority=10,
                )
            ]
        )

        mock_mass.players = controller
        controller._players = {
            "apple_tv_1": apple_tv,
            "sonos_1": sonos_player,
            "airplay_sonos": sonos_airplay,
        }

        apple_tv.set_initialized()
        sonos_player.set_initialized()
        sonos_airplay.set_initialized()

        apple_tv.update_state(signal_event=False)
        sonos_airplay.update_state(signal_event=False)
        sonos_player.update_state(signal_event=False)

        # Sonos's final synced_to should be Apple TV (visible player)
        assert sonos_player.state.synced_to == "apple_tv_1"


class TestUngroupTranslation:
    """Tests for translation when ungrouping from native protocol players."""

    def test_ungroup_translates_visible_to_protocol_id(self, mock_mass: MagicMock) -> None:
        """
        Test that ungrouping correctly translates visible ID to protocol ID.

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
                LinkedOutputProtocol(
                    output_protocol_id="airplay_sonos",
                    protocol_domain="airplay",
                    priority=10,
                )
            ]
        )

        mock_mass.players = controller
        controller._players = {
            "apple_tv_1": apple_tv,
            "sonos_1": sonos_player,
            "airplay_sonos": sonos_airplay,
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
    """
    Tests for grouping with native protocol-domain players.

    This tests the scenario where a player's native provider domain IS the protocol
    domain (e.g., a sendspin web player with PlayerType.PLAYER and provider.domain="sendspin"),
    rather than having the protocol as a linked protocol player.
    """

    def test_native_protocol_player_groups_via_active_protocol(self, mock_mass: MagicMock) -> None:
        """
        Test grouping a native protocol player via the parent's active protocol.

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
                LinkedOutputProtocol(
                    output_protocol_id="sendspin_kantoor",
                    protocol_domain="sendspin",
                    priority=40,
                )
            ]
        )

        mock_mass.players = controller
        controller._players = {
            "sonos_kantoor": kantoor,
            "sendspin_kantoor": sendspin_kantoor,
            "sendspin_web": web_player,
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
        """
        Test grouping a native protocol player via common protocol search (Priority 4).

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
                LinkedOutputProtocol(
                    output_protocol_id="sendspin_kantoor",
                    protocol_domain="sendspin",
                    priority=40,
                )
            ]
        )

        mock_mass.players = controller
        controller._players = {
            "sonos_kantoor": kantoor,
            "sendspin_kantoor": sendspin_kantoor,
            "sendspin_web": web_player,
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
        """
        Test ungrouping a native protocol player from a protocol-linked parent.

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
                LinkedOutputProtocol(
                    output_protocol_id="sendspin_kantoor",
                    protocol_domain="sendspin",
                    priority=40,
                )
            ]
        )

        mock_mass.players = controller
        controller._players = {
            "sonos_kantoor": kantoor,
            "sendspin_kantoor": sendspin_kantoor,
            "sendspin_web": web_player,
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
        """
        Test that _filter_protocol_members accepts native protocol-domain players.

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

    def test_sendspin_visualizer_groups_via_common_protocol(self, mock_mass: MagicMock) -> None:
        """
        Test grouping a sendspin visualizer (e.g. Hue light) with a sendspin-bridged parent.

        Visualizer players have no PLAY_MEDIA so they are not native players, but the
        Sendspin provider advertises a self-referential sendspin output protocol on them
        (is_native=True) so the common-protocol search (Priority 4) can match them against
        any parent that has a sendspin bridge linked.
        """
        controller = PlayerController(mock_mass)

        sonos_provider = MockProvider("sonos", instance_id="sonos", mass=mock_mass)
        sendspin_provider = MockProvider("sendspin", instance_id="sendspin", mass=mock_mass)

        # Mancave: native Sonos parent with sendspin bridge linked
        mancave = MockPlayer(sonos_provider, "sonos_mancave", "Mancave")
        mancave._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        mancave._cache.clear()

        # Sendspin bridge protocol player linked to Mancave
        sendspin_bridge = MockPlayer(
            sendspin_provider,
            "spb_mancave",
            "Mancave (AirPlay)",
            player_type=PlayerType.PROTOCOL,
        )
        sendspin_bridge._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        sendspin_bridge._attr_can_group_with = {"sendspin"}
        sendspin_bridge._cache.clear()
        sendspin_bridge.set_protocol_parent_id("sonos_mancave")

        # Hue visualizer: sendspin provider, no PLAY_MEDIA, has SET_MEMBERS, type LIGHT
        hue_light = MockPlayer(
            sendspin_provider,
            "hue-mancave",
            "Hue: Mancave",
            player_type=PlayerType.LIGHT,
        )
        hue_light._attr_supported_features = {PlayerFeature.SET_MEMBERS}
        hue_light._cache.clear()

        mancave.set_linked_output_protocols(
            [
                LinkedOutputProtocol(
                    output_protocol_id="spb_mancave",
                    protocol_domain="sendspin",
                    priority=40,
                )
            ]
        )

        mock_mass.players = controller
        controller._players = {
            "sonos_mancave": mancave,
            "spb_mancave": sendspin_bridge,
            "hue-mancave": hue_light,
        }

        sendspin_bridge.update_state(signal_event=False)
        mancave.update_state(signal_event=False)
        hue_light.update_state(signal_event=False)

        protocol_members, native_members, protocol_player, protocol_domain = (
            controller._translate_members_for_protocols(
                parent_player=mancave,
                player_ids=["hue-mancave"],
                parent_protocol_player=None,
                parent_protocol_domain=None,
            )
        )

        # Priority 4 should match sendspin↔sendspin and route via the bridge,
        # using the visualizer's player_id directly (is_native=True).
        assert protocol_members == ["hue-mancave"]
        assert native_members == []
        assert protocol_domain == "sendspin"
        assert protocol_player == sendspin_bridge


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
        """
        Test that ARP MAC replaces reported MAC when only the locally-administered bit differs.

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
        """
        Test that ARP result always replaces a locally-administered MAC.

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
    """
    Tests for Fix 1: identifier-based fallback matching for Universal Players.

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

        airplay_player.set_initialized()

        controller._try_link_protocol_to_native(airplay_player)

        # Should be linked via the existing stored ID path
        assert airplay_player.protocol_parent_id == "up_aabbccddeeff"


class TestCachedParentIdentifierCopying:
    """
    Tests for Fix 2: identifier copying when restoring cached parent links.

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

        # Link protocol player via cached parent path
        controller._try_link_protocol_to_native(airplay_player)

        # Should be linked
        assert airplay_player.protocol_parent_id == "up_62e5974593d3"

        # Universal player should now have the MAC from the protocol player
        assert (
            universal.device_info.identifiers.get(IdentifierType.MAC_ADDRESS) == "62:E5:97:45:93:D3"
        )

    def test_sendspin_matches_after_identifier_copy(self, mock_mass: MagicMock) -> None:
        """
        Test the full scenario: AirPlay restores, copies identifiers, Sendspin matches.

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

        controller._try_link_protocol_to_native(dlna_player)

        # Should be linked
        assert dlna_player.protocol_parent_id == "sonos_123"

        # Native player's MAC should NOT be overwritten
        assert sonos_player.device_info.identifiers[IdentifierType.MAC_ADDRESS] == original_mac


class TestEnrichAndMatchIntegration:
    """
    Integration tests for the interaction between MAC enrichment and identifier matching.

    These tests verify that Fix 3 (preserving original MAC when ARP resolves a
    completely different address) works correctly with the identifier matching system.
    """

    @pytest.mark.asyncio
    async def test_apple_device_sendspin_matches_via_airplay_id(self, mock_mass: MagicMock) -> None:
        """
        Test the full Apple device scenario end-to-end.

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
        """
        Test that WiiM devices match after ARP enrichment.

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
    """
    Tests for preventing duplicate protocol domain linking.

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

    def test_add_protocol_link_allows_replacing_removed_player(self, mock_mass: MagicMock) -> None:
        """Test that a new link can replace a stale one after the old player is removed."""
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
        ap1.set_initialized()
        ap2.set_initialized()
        sonos_player.set_initialized()

        # Link first AirPlay
        controller._add_protocol_link(sonos_player, ap1, "airplay")
        assert ap1.protocol_parent_id == "sonos_1"

        # Remove first AirPlay from registry (provider cleaned up stale player)
        del controller._players["ap_1"]

        # Link second AirPlay - should succeed since first was removed
        controller._add_protocol_link(sonos_player, ap2, "airplay")
        assert ap2.protocol_parent_id == "sonos_1"

    def test_add_protocol_link_blocks_when_existing_still_registered(
        self, mock_mass: MagicMock
    ) -> None:
        """Test that a new link is blocked when the old player is still registered."""
        controller = PlayerController(mock_mass)

        sonos_provider = MockProvider("sonos")
        sonos_player = MockPlayer(
            sonos_provider, "sonos_1", "Sonos Speaker", player_type=PlayerType.PLAYER
        )

        snap_provider = MockProvider("snapcast")
        snap1 = MockPlayer(
            snap_provider,
            "snap_1",
            "Snapcast 1",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "D0:11:E5:00:67:2F"},
        )
        snap2 = MockPlayer(
            snap_provider,
            "snap_2",
            "Snapcast 2",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "D0:11:E5:00:67:2F"},
        )

        # Both unavailable
        snap1._attr_available = False
        snap1._cache.clear()
        snap1.update_state(signal_event=False)
        snap2._attr_available = False
        snap2._cache.clear()
        snap2.update_state(signal_event=False)

        controller._players = {
            "sonos_1": sonos_player,
            "snap_1": snap1,
            "snap_2": snap2,
        }
        snap1.set_initialized()
        snap2.set_initialized()
        sonos_player.set_initialized()

        # Link first snapcast
        controller._add_protocol_link(sonos_player, snap1, "snapcast")
        assert snap1.protocol_parent_id == "sonos_1"

        # Try to link second snapcast - should be BLOCKED (snap1 still registered)
        controller._add_protocol_link(sonos_player, snap2, "snapcast")
        assert snap2.protocol_parent_id is None

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
        """
        Test that two universal players merge when they share a MAC after identifier copy.

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

        # Both UPs have one linked protocol -> equal-count merge falls through to
        # the deterministic tiebreaker on player_id. "up50411c2f00ee" (up2) is
        # lex-smaller than "upf15fff97f002" (up1), so up2 absorbs up1.

        # up2 (lex-smaller player_id) should have absorbed up1's DLNA link
        protocol_domains = {link.protocol_domain for link in up2.linked_output_protocols}
        assert "dlna" in protocol_domains
        assert "airplay" in protocol_domains

        # up1 should have no more protocol links
        assert len(up1.linked_output_protocols) == 0

        # DLNA player should now be linked to up2 (was on up1 before merge)
        assert dlna_player.protocol_parent_id == "up50411c2f00ee"

        # AirPlay player should still be linked to up2
        assert ap_player.protocol_parent_id == "up50411c2f00ee"

        # up2 should have protocol player IDs from both
        assert "dlna_uuid_1" in up2._protocol_player_ids
        assert "ap_1" in up2._protocol_player_ids

        # Unregister should have been called for up1
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

    def test_merge_deterministic_tiebreaker_on_equal_link_counts(
        self, mock_mass: MagicMock
    ) -> None:
        """Test that equal-link-count merges resolve to the lex-smaller player_id."""
        controller = PlayerController(mock_mass)
        up_provider = create_mock_universal_provider(mock_mass)

        # up_a: lex-smaller player_id, one protocol link (DLNA)
        up_a = UniversalPlayer(
            provider=up_provider,
            player_id="up_aaa",
            name="Player A",
            device_info=DeviceInfo(model="Test", manufacturer="Test"),
            protocol_player_ids=["dlna_1"],
        )
        up_a._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, "AA:BB:CC:DD:EE:FF")
        up_a._cache.clear()
        up_a.update_state(signal_event=False)
        up_a.set_initialized()

        # up_b: lex-larger player_id, one protocol link (AirPlay)
        up_b = UniversalPlayer(
            provider=up_provider,
            player_id="up_bbb",
            name="Player B",
            device_info=DeviceInfo(model="Test", manufacturer="Test"),
            protocol_player_ids=["ap_1"],
        )
        up_b._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, "AA:BB:CC:DD:EE:FF")
        up_b._cache.clear()
        up_b.update_state(signal_event=False)
        up_b.set_initialized()

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

        controller._players = {
            "up_aaa": up_a,
            "up_bbb": up_b,
            "dlna_1": dlna_player,
            "ap_1": ap_player,
        }

        controller._add_protocol_link(up_a, dlna_player, "dlna")
        controller._add_protocol_link(up_b, ap_player, "airplay")

        # Call merge from the lex-larger side. Under the old non-deterministic
        # behavior the caller (up_b) would have won. With the tiebreaker the
        # lex-smaller player_id (up_a) wins regardless of which side calls.
        controller._check_merge_universal_players(up_b)

        # up_a (lex-smaller) should have absorbed up_b's AirPlay link
        protocol_domains = {link.protocol_domain for link in up_a.linked_output_protocols}
        assert "dlna" in protocol_domains
        assert "airplay" in protocol_domains

        # AirPlay player should now point to up_aaa
        assert ap_player.protocol_parent_id == "up_aaa"

        # up_b should have no more links
        assert len(up_b.linked_output_protocols) == 0

    async def test_merge_preserves_moved_protocols_during_parent_cleanup(
        self, mock_mass: MagicMock
    ) -> None:
        """Moved protocol ownership must survive the removed parent's permanent cleanup."""
        controller = PlayerController(mock_mass)
        up_provider = create_mock_universal_provider(mock_mass)

        config_store: dict[str, object] = {
            "players/up_keep": {"enabled": True},
            "players/up_remove": {"enabled": True},
            "players/ap_keep": {"enabled": True},
            "players/dlna_live": {"enabled": True},
            "players/dlna_cached": {
                "enabled": True,
                "provider": "dlna--test",
                "player_type": "protocol",
                "values": {"protocol_parent_id": "up_remove"},
            },
        }

        def config_get(key: str, default: object = None) -> object:
            return config_store.get(key, default)

        def config_set(key: str, value: object) -> None:
            config_store[key] = value

        def config_remove(key: str) -> None:
            config_store.pop(key, None)

        scheduled_tasks: list[Awaitable[object]] = []

        def capture_task(task: Awaitable[object], *_args: Any, **_kwargs: Any) -> None:
            scheduled_tasks.append(task)

        mock_mass.config.get = MagicMock(side_effect=config_get)
        mock_mass.config.set = MagicMock(side_effect=config_set)
        mock_mass.config.remove = MagicMock(side_effect=config_remove)
        mock_mass.players = controller
        mock_mass.player_queues = MagicMock()
        mock_mass.call_later = MagicMock()
        mock_mass.loop = MagicMock()
        mock_mass.create_task = MagicMock(side_effect=capture_task)

        keep = UniversalPlayer(
            provider=up_provider,
            player_id="up_keep",
            name="Keep",
            device_info=DeviceInfo(model="Test", manufacturer="Test"),
            protocol_player_ids=["ap_keep"],
        )
        keep._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, "AA:BB:CC:DD:EE:FF")
        keep._cache.clear()
        keep.update_state(signal_event=False)
        keep.set_initialized()

        remove = UniversalPlayer(
            provider=up_provider,
            player_id="up_remove",
            name="Remove",
            device_info=DeviceInfo(model="Test", manufacturer="Test"),
            protocol_player_ids=["dlna_live", "dlna_cached"],
        )
        remove._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, "AA:BB:CC:DD:EE:FF")
        remove._cache.clear()
        remove.update_state(signal_event=False)
        remove.set_initialized()

        airplay_provider = MockProvider("airplay", mass=mock_mass)
        ap_keep = MockPlayer(
            airplay_provider,
            "ap_keep",
            "AirPlay",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        ap_keep.set_initialized()

        dlna_provider = MockProvider("dlna", mass=mock_mass)
        dlna_live = MockPlayer(
            dlna_provider,
            "dlna_live",
            "DLNA",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        dlna_live.set_initialized()

        controller._players = {
            "up_keep": keep,
            "up_remove": remove,
            "ap_keep": ap_keep,
            "dlna_live": dlna_live,
        }

        controller._add_protocol_link(keep, ap_keep, "airplay")
        controller._add_protocol_link(remove, dlna_live, "dlna")

        with patch.object(controller, "_save_universal_player_data"):
            controller._check_merge_universal_players(keep)

        assert dlna_live.protocol_parent_id == "up_keep"
        assert config_store["players/dlna_cached/values/protocol_parent_id"] == "up_keep"
        unregister_tasks = [t for t in scheduled_tasks if "unregister" in repr(t)]
        assert len(unregister_tasks) == 1

        await unregister_tasks[0]

        assert "up_remove" not in controller._players
        assert dlna_live.protocol_parent_id == "up_keep"
        assert "dlna_cached" in keep._protocol_player_ids
        assert config_store["players/dlna_cached/values/protocol_parent_id"] == "up_keep"

    def test_no_merge_same_domain_protocols_on_same_ip(self, mock_mass: MagicMock) -> None:
        """
        Test that UPs with same-domain protocols on the same IP are NOT merged.

        Scenario: 3 squeezelite instances on the same VM (same IP, different real MACs).
        Each gets its own universal player. The merge check must NOT merge them,
        because merging would combine protocols from the same domain.

        Reproduces: https://github.com/music-assistant/support/issues/5266
        """
        controller = PlayerController(mock_mass)
        up_provider = create_mock_universal_provider(mock_mass)

        # Create 3 universal players, each with one squeezelite protocol
        squeeze_provider = MockProvider("squeezelite", mass=mock_mass)
        ups = []
        protocols = []
        for i in range(1, 4):
            mac = f"00:00:00:00:00:{i:02X}"
            up = UniversalPlayer(
                provider=up_provider,
                player_id=f"up00000000000{i}",
                name=f"Zone {i}",
                device_info=DeviceInfo(model="Squeezelite", manufacturer="Test"),
                protocol_player_ids=[mac],
            )
            up._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, mac)
            up._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, "192.168.1.50")
            up._cache.clear()
            up.update_state(signal_event=False)
            up.set_initialized()
            ups.append(up)

            proto = MockPlayer(
                squeeze_provider,
                mac,
                f"squeezeplay: {mac}",
                player_type=PlayerType.PROTOCOL,
                identifiers={
                    IdentifierType.MAC_ADDRESS: mac,
                    IdentifierType.IP_ADDRESS: "192.168.1.50",
                },
            )
            proto.set_initialized()
            protocols.append(proto)

        controller._players = {}
        for up in ups:
            controller._players[up.player_id] = up
        for proto in protocols:
            controller._players[proto.player_id] = proto

        # Link each protocol to its UP
        for up, proto in zip(ups, protocols, strict=True):
            controller._add_protocol_link(up, proto, "squeezelite")

        # Verify initial state: each protocol linked to its own UP
        for up, proto in zip(ups, protocols, strict=True):
            assert proto.protocol_parent_id == up.player_id

        # Now run merge check on each UP - none should merge
        with patch.object(controller, "_save_universal_player_data"):
            for up in ups:
                controller._check_merge_universal_players(up)

        # All 3 UPs must still exist with their original protocol links
        for up, proto in zip(ups, protocols, strict=True):
            assert proto.protocol_parent_id == up.player_id, (
                f"Protocol {proto.player_id} should still be linked to {up.player_id}, "
                f"but is linked to {proto.protocol_parent_id}"
            )
            assert len(up.linked_output_protocols) == 1, (
                f"UP {up.player_id} should have exactly 1 protocol, "
                f"but has {len(up.linked_output_protocols)}"
            )

    @staticmethod
    def _setup_merge_scenario(
        mock_mass: MagicMock, config_store: dict[str, Any]
    ) -> tuple[PlayerController, UniversalPlayer, list[Awaitable[object]]]:
        """
        Wire a merge scenario for the config carry-over tests.

        Universal players 'up_keep' and 'up_remove' share a MAC; with equal
        link counts the deterministic tiebreaker keeps 'up_keep' and removes
        'up_remove', so 'up_remove' is the loser whose config is carried over.
        """
        controller = PlayerController(mock_mass)
        up_provider = create_mock_universal_provider(mock_mass)

        def config_get(key: str, default: object = None) -> object:
            return config_store.get(key, default)

        def config_set(key: str, value: object) -> None:
            config_store[key] = value

        def config_remove(key: str) -> None:
            config_store.pop(key, None)

        scheduled_tasks: list[Awaitable[object]] = []

        def capture_task(task: Awaitable[object], *_args: Any, **_kwargs: Any) -> None:
            scheduled_tasks.append(task)

        mock_mass.config.get = MagicMock(side_effect=config_get)
        mock_mass.config.set = MagicMock(side_effect=config_set)
        mock_mass.config.remove = MagicMock(side_effect=config_remove)
        mock_mass.players = controller
        mock_mass.player_queues = MagicMock()
        mock_mass.call_later = MagicMock()
        mock_mass.loop = MagicMock()
        mock_mass.create_task = MagicMock(side_effect=capture_task)

        keep = UniversalPlayer(
            provider=up_provider,
            player_id="up_keep",
            name="Keep (Universal)",
            device_info=DeviceInfo(model="Test", manufacturer="Test"),
            protocol_player_ids=["ap_keep"],
        )
        keep._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, "AA:BB:CC:DD:EE:FF")
        keep._cache.clear()
        keep.update_state(signal_event=False)
        keep.set_initialized()

        remove = UniversalPlayer(
            provider=up_provider,
            player_id="up_remove",
            name="Remove (Universal)",
            device_info=DeviceInfo(model="Test", manufacturer="Test"),
            protocol_player_ids=["dlna_live"],
        )
        remove._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, "AA:BB:CC:DD:EE:FF")
        remove._cache.clear()
        remove.update_state(signal_event=False)
        remove.set_initialized()

        ap_keep = MockPlayer(
            MockProvider("airplay", mass=mock_mass),
            "ap_keep",
            "AirPlay",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        ap_keep.set_initialized()

        dlna_live = MockPlayer(
            MockProvider("dlna", mass=mock_mass),
            "dlna_live",
            "DLNA",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        dlna_live.set_initialized()

        controller._players = {
            "up_keep": keep,
            "up_remove": remove,
            "ap_keep": ap_keep,
            "dlna_live": dlna_live,
        }
        controller._add_protocol_link(keep, ap_keep, "airplay")
        controller._add_protocol_link(remove, dlna_live, "dlna")
        return controller, keep, scheduled_tasks

    async def test_merge_carries_over_universal_config(self, mock_mass: MagicMock) -> None:
        """Merge migrates name, values, DSP and queue settings from loser to keeper."""
        config_store: dict[str, Any] = {
            "players/up_keep": {
                "enabled": True,
                "name": "Keep (Universal)",
                "default_name": "Keep (Universal)",
                "values": {"volume_control": "keep_ctrl"},
            },
            "players/up_remove": {
                "enabled": True,
                "name": "Living Room",
                "default_name": "Remove (Universal)",
                "values": {
                    "hide_in_ui": True,
                    "volume_control": "remove_ctrl",
                    "device_info": {"model": "Test", "manufacturer": "Test"},
                },
            },
            "players/ap_keep": {"enabled": True},
            "players/dlna_live": {"enabled": True},
            "player_dsp/up_remove": {"enabled": True, "filters": []},
            "player_queues/up_remove": {
                "queue_id": "up_remove",
                "values": {"crossfade_duration": 5},
            },
        }
        controller, keep, scheduled_tasks = self._setup_merge_scenario(mock_mass, config_store)

        with patch.object(controller, "_save_universal_player_data"):
            controller._check_merge_universal_players(keep)

        # the custom display name and non-conflicting values moved onto the keeper
        assert config_store["players/up_keep/name"] == "Living Room"
        assert config_store["players/up_keep/values/hide_in_ui"] is True
        # values the keeper has explicitly set are not overwritten
        assert "players/up_keep/values/volume_control" not in config_store
        # wrapper bookkeeping is not carried over
        assert "players/up_keep/values/device_info" not in config_store
        # DSP moved wholesale and queue settings moved and re-keyed onto the keeper
        assert config_store["player_dsp/up_keep"] == {"enabled": True, "filters": []}
        assert config_store["player_queues/up_keep"] == {
            "queue_id": "up_keep",
            "values": {"crossfade_duration": 5},
        }
        assert "player_queues/up_remove" not in config_store
        # the keeper's in-place config gets reloaded to apply the migrated values
        assert any("_reapply_player_config" in repr(t) for t in scheduled_tasks)

        # and the losing universal player is still permanently removed
        unregister_tasks = [t for t in scheduled_tasks if "unregister" in repr(t)]
        assert len(unregister_tasks) == 1
        await unregister_tasks[0]
        assert "up_remove" not in controller._players

    def test_merge_keeps_keeper_explicit_values(self, mock_mass: MagicMock) -> None:
        """Values explicitly set on the keeper win over the losing player's."""
        config_store: dict[str, Any] = {
            "players/up_keep": {
                "enabled": True,
                "name": "Kitchen",
                "default_name": "Keep (Universal)",
                "values": {"hide_in_ui": False},
            },
            "players/up_remove": {
                "enabled": True,
                "name": "Living Room",
                "default_name": "Remove (Universal)",
                "values": {"hide_in_ui": True, "flow_mode": True},
            },
            "players/ap_keep": {"enabled": True},
            "players/dlna_live": {"enabled": True},
            "player_dsp/up_keep": {"enabled": False, "filters": ["keep"]},
            "player_dsp/up_remove": {"enabled": True, "filters": ["remove"]},
        }
        controller, keep, _ = self._setup_merge_scenario(mock_mass, config_store)

        with patch.object(controller, "_save_universal_player_data"):
            controller._check_merge_universal_players(keep)

        # the keeper's own custom name and DSP config win
        assert "players/up_keep/name" not in config_store
        assert config_store["players/up_keep"]["name"] == "Kitchen"
        assert config_store["player_dsp/up_keep"] == {"enabled": False, "filters": ["keep"]}
        # the keeper's explicit value is not overwritten
        assert "players/up_keep/values/hide_in_ui" not in config_store
        # non-conflicting values still migrate
        assert config_store["players/up_keep/values/flow_mode"] is True

    def test_merge_repoints_group_memberships(self, mock_mass: MagicMock) -> None:
        """Merge re-points group memberships from the removed wrapper to the keeper."""
        config_store: dict[str, Any] = {
            "players/up_keep": {
                "enabled": True,
                "name": "Keep (Universal)",
                "default_name": "Keep (Universal)",
                "values": {},
            },
            "players/up_remove": {
                "enabled": True,
                "name": "Remove (Universal)",
                "default_name": "Remove (Universal)",
                "values": {},
            },
            "players/ap_keep": {"enabled": True},
            "players/dlna_live": {"enabled": True},
            # nested players dict scanned by _repoint_group_memberships
            "players": {
                "group_1": {
                    "enabled": True,
                    "values": {
                        "group_members": ["up_remove", "other"],
                        "allowed_members": ["up_remove"],
                    },
                },
                "group_2": {
                    "enabled": True,
                    # keeper already listed -> no duplicate after re-point
                    "values": {"group_members": ["up_keep", "up_remove"]},
                },
            },
        }
        controller, keep, _ = self._setup_merge_scenario(mock_mass, config_store)

        with patch.object(controller, "_save_universal_player_data"):
            controller._check_merge_universal_players(keep)

        assert config_store["players/group_1/values/group_members"] == ["up_keep", "other"]
        assert config_store["players/group_1/values/allowed_members"] == ["up_keep"]
        # no duplicate when the keeper is already a member
        assert config_store["players/group_2/values/group_members"] == ["up_keep"]

    async def test_merge_stops_playing_loser_before_unregister(self, mock_mass: MagicMock) -> None:
        """A playing losing wrapper is stopped before it is permanently removed."""
        config_store: dict[str, Any] = {
            "players/up_keep": {"enabled": True},
            "players/up_remove": {"enabled": True},
            "players/ap_keep": {"enabled": True},
            "players/dlna_live": {"enabled": True},
        }
        controller, keep, scheduled_tasks = self._setup_merge_scenario(mock_mass, config_store)
        controller._players["up_remove"]._attr_playback_state = PlaybackState.PLAYING

        call_order: list[str] = []
        mock_mass.player_queues.stop = AsyncMock(
            side_effect=lambda *_a, **_k: call_order.append("stop")
        )

        with patch.object(controller, "_save_universal_player_data"):
            controller._check_merge_universal_players(keep)

        task = next(t for t in scheduled_tasks if "unregister" in repr(t))

        def _record_unregister(*_a: Any, **_k: Any) -> None:
            call_order.append("unregister")

        with patch.object(controller, "unregister", new=AsyncMock(side_effect=_record_unregister)):
            await task

        assert call_order == ["stop", "unregister"]
        mock_mass.player_queues.stop.assert_awaited_once_with("up_remove")

    async def test_merge_idle_loser_not_stopped(self, mock_mass: MagicMock) -> None:
        """An idle losing wrapper is removed without a stop command."""
        config_store: dict[str, Any] = {
            "players/up_keep": {"enabled": True},
            "players/up_remove": {"enabled": True},
            "players/ap_keep": {"enabled": True},
            "players/dlna_live": {"enabled": True},
        }
        controller, keep, scheduled_tasks = self._setup_merge_scenario(mock_mass, config_store)
        mock_mass.player_queues.stop = AsyncMock()

        with patch.object(controller, "_save_universal_player_data"):
            controller._check_merge_universal_players(keep)

        task = next(t for t in scheduled_tasks if "unregister" in repr(t))
        await task

        mock_mass.player_queues.stop.assert_not_awaited()
        assert "up_remove" not in controller._players


class TestUniversalPlayerReplacement:
    """Tests for replacing a universal player with a native player."""

    async def test_replace_preserves_moved_protocols_during_parent_cleanup(
        self, mock_mass: MagicMock
    ) -> None:
        """Moved protocol ownership must survive the removed universal cleanup."""
        controller = PlayerController(mock_mass)
        up_provider = create_mock_universal_provider(mock_mass)

        config_store: dict[str, object] = {
            "players/sonos_1": {"enabled": True},
            "players/up_old": {"enabled": True},
            "players/ap_live": {"enabled": True},
            "players/dlna_cached": {
                "enabled": True,
                "provider": "dlna--test",
                "player_type": "protocol",
                "values": {"protocol_parent_id": "up_old"},
            },
        }

        def config_get(key: str, default: object = None) -> object:
            return config_store.get(key, default)

        def config_set(key: str, value: object) -> None:
            config_store[key] = value

        def config_remove(key: str) -> None:
            config_store.pop(key, None)

        scheduled_tasks: list[Awaitable[object]] = []

        def capture_task(task: Awaitable[object], *_args: Any, **_kwargs: Any) -> None:
            scheduled_tasks.append(task)

        mock_mass.config.get = MagicMock(side_effect=config_get)
        mock_mass.config.set = MagicMock(side_effect=config_set)
        mock_mass.config.remove = MagicMock(side_effect=config_remove)
        mock_mass.players = controller
        mock_mass.player_queues = MagicMock()
        mock_mass.call_later = MagicMock()
        mock_mass.loop = MagicMock()
        mock_mass.create_task = MagicMock(side_effect=capture_task)

        universal = UniversalPlayer(
            provider=up_provider,
            player_id="up_old",
            name="Old Universal",
            device_info=DeviceInfo(model="Test", manufacturer="Test"),
            protocol_player_ids=["ap_live", "dlna_cached"],
        )
        universal._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, "AA:BB:CC:DD:EE:FF")
        universal._cache.clear()
        universal.update_state(signal_event=False)
        universal.set_initialized()

        native_provider = MockProvider("sonos", mass=mock_mass)
        native = MockPlayer(
            native_provider,
            "sonos_1",
            "Sonos",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        native.set_initialized()

        airplay_provider = MockProvider("airplay", mass=mock_mass)
        ap_live = MockPlayer(
            airplay_provider,
            "ap_live",
            "AirPlay",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        ap_live.set_initialized()

        controller._players = {
            "up_old": universal,
            "sonos_1": native,
            "ap_live": ap_live,
        }

        controller._add_protocol_link(universal, ap_live, "airplay")

        controller._check_replace_universal_player(native)

        assert ap_live.protocol_parent_id == "sonos_1"
        linked_ids = cast("list[str]", config_store["players/sonos_1/values/linked_protocol_ids"])
        assert set(linked_ids) == {
            "ap_live",
            "dlna_cached",
        }
        assert config_store["players/dlna_cached/values/protocol_parent_id"] == "sonos_1"
        unregister_tasks = [t for t in scheduled_tasks if "unregister" in repr(t)]
        assert len(unregister_tasks) == 1

        await unregister_tasks[0]

        assert "up_old" not in controller._players
        assert ap_live.protocol_parent_id == "sonos_1"
        assert config_store["players/dlna_cached/values/protocol_parent_id"] == "sonos_1"
        linked_ids = cast("list[str]", config_store["players/sonos_1/values/linked_protocol_ids"])
        assert set(linked_ids) == {
            "ap_live",
            "dlna_cached",
        }

    def test_replace_keeps_universal_when_all_links_refused(self, mock_mass: MagicMock) -> None:
        """
        Keep the universal player when no link could migrate.

        If the native player already has an active link from the same protocol
        domain as every protocol on the universal player, each `_add_protocol_link`
        call refuses and leaves the protocol's parent unchanged. The universal player
        must NOT be permanently unregistered in that case, otherwise it disappears
        from the UI on restart.
        """
        controller = PlayerController(mock_mass)
        up_provider = create_mock_universal_provider(mock_mass)

        config_store: dict[str, object] = {
            "players/cast_1": {"enabled": True},
            "players/up_old": {"enabled": True},
            "players/spb_old": {"enabled": True},
            "players/spb_new": {"enabled": True},
        }

        def config_get(key: str, default: object = None) -> object:
            return config_store.get(key, default)

        def config_set(key: str, value: object) -> None:
            config_store[key] = value

        def config_remove(key: str) -> None:
            config_store.pop(key, None)

        scheduled_tasks: list[Awaitable[object]] = []

        def capture_task(task: Awaitable[object], *_args: Any, **_kwargs: Any) -> None:
            scheduled_tasks.append(task)

        mock_mass.config.get = MagicMock(side_effect=config_get)
        mock_mass.config.set = MagicMock(side_effect=config_set)
        mock_mass.config.remove = MagicMock(side_effect=config_remove)
        mock_mass.players = controller
        mock_mass.player_queues = MagicMock()
        mock_mass.call_later = MagicMock()
        mock_mass.loop = MagicMock()
        mock_mass.create_task = MagicMock(side_effect=capture_task)

        universal = UniversalPlayer(
            provider=up_provider,
            player_id="up_old",
            name="Soundbar (Universal)",
            device_info=DeviceInfo(model="Test", manufacturer="Test"),
            protocol_player_ids=["spb_old"],
        )
        universal._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, "AA:BB:CC:DD:EE:FF")
        universal._cache.clear()
        universal.update_state(signal_event=False)
        universal.set_initialized()

        cast_provider = MockProvider("cast", mass=mock_mass)
        native = MockPlayer(
            cast_provider,
            "cast_1",
            "Soundbar",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        native.set_initialized()

        sendspin_provider = MockProvider("sendspin", mass=mock_mass)
        spb_old = MockPlayer(
            sendspin_provider,
            "spb_old",
            "Soundbar (Sendspin)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        spb_old.set_initialized()
        spb_new = MockPlayer(
            sendspin_provider,
            "spb_new",
            "Soundbar (Sendspin v2)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        spb_new.set_initialized()

        controller._players = {
            "up_old": universal,
            "cast_1": native,
            "spb_old": spb_old,
            "spb_new": spb_new,
        }

        controller._add_protocol_link(universal, spb_old, "sendspin")
        controller._add_protocol_link(native, spb_new, "sendspin")

        controller._check_replace_universal_player(native)

        unregister_tasks = [t for t in scheduled_tasks if "unregister" in repr(t)]
        assert unregister_tasks == []
        assert "up_old" in controller._players
        assert spb_old.protocol_parent_id == "up_old"
        assert any(
            link.output_protocol_id == "spb_old" for link in universal.linked_output_protocols
        )

    def test_replace_keeps_universal_when_some_links_refused(self, mock_mass: MagicMock) -> None:
        """
        Keep the universal player when only some of its links could migrate.

        The native player already owns the sendspin domain, so the universal player's
        sendspin link is refused while its airplay link migrates. The universal player
        must be kept (holding the refused link) instead of being unregistered, which
        would orphan the refused protocol during cleanup.
        """
        controller = PlayerController(mock_mass)
        up_provider = create_mock_universal_provider(mock_mass)

        config_store: dict[str, object] = {
            "players/cast_1": {"enabled": True},
            "players/up_old": {"enabled": True},
            "players/spb_old": {"enabled": True},
            "players/spb_new": {"enabled": True},
            "players/ap_old": {"enabled": True},
        }

        def config_get(key: str, default: object = None) -> object:
            return config_store.get(key, default)

        def config_set(key: str, value: object) -> None:
            config_store[key] = value

        def config_remove(key: str) -> None:
            config_store.pop(key, None)

        scheduled_tasks: list[Awaitable[object]] = []

        def capture_task(task: Awaitable[object], *_args: Any, **_kwargs: Any) -> None:
            scheduled_tasks.append(task)

        mock_mass.config.get = MagicMock(side_effect=config_get)
        mock_mass.config.set = MagicMock(side_effect=config_set)
        mock_mass.config.remove = MagicMock(side_effect=config_remove)
        mock_mass.players = controller
        mock_mass.player_queues = MagicMock()
        mock_mass.call_later = MagicMock()
        mock_mass.loop = MagicMock()
        mock_mass.create_task = MagicMock(side_effect=capture_task)

        universal = UniversalPlayer(
            provider=up_provider,
            player_id="up_old",
            name="Soundbar (Universal)",
            device_info=DeviceInfo(model="Test", manufacturer="Test"),
            protocol_player_ids=["spb_old", "ap_old"],
        )
        universal._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, "AA:BB:CC:DD:EE:FF")
        universal._cache.clear()
        universal.update_state(signal_event=False)
        universal.set_initialized()

        cast_provider = MockProvider("cast", mass=mock_mass)
        native = MockPlayer(
            cast_provider,
            "cast_1",
            "Soundbar",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        native.set_initialized()

        sendspin_provider = MockProvider("sendspin", mass=mock_mass)
        spb_old = MockPlayer(
            sendspin_provider,
            "spb_old",
            "Soundbar (Sendspin)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        spb_old.set_initialized()
        spb_new = MockPlayer(
            sendspin_provider,
            "spb_new",
            "Soundbar (Sendspin v2)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        spb_new.set_initialized()

        airplay_provider = MockProvider("airplay", mass=mock_mass)
        ap_old = MockPlayer(
            airplay_provider,
            "ap_old",
            "Soundbar (AirPlay)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        ap_old.set_initialized()

        controller._players = {
            "up_old": universal,
            "cast_1": native,
            "spb_old": spb_old,
            "spb_new": spb_new,
            "ap_old": ap_old,
        }

        controller._add_protocol_link(universal, spb_old, "sendspin")
        controller._add_protocol_link(universal, ap_old, "airplay")
        controller._add_protocol_link(native, spb_new, "sendspin")

        controller._check_replace_universal_player(native)

        unregister_tasks = [t for t in scheduled_tasks if "unregister" in repr(t)]
        assert unregister_tasks == []
        assert "up_old" in controller._players
        # The refused sendspin link stays owned by the universal player.
        assert spb_old.protocol_parent_id == "up_old"
        assert any(
            link.output_protocol_id == "spb_old" for link in universal.linked_output_protocols
        )
        # The airplay link migrated to the native player and left the universal player.
        assert ap_old.protocol_parent_id == "cast_1"
        assert any(link.output_protocol_id == "ap_old" for link in native.linked_output_protocols)
        assert all(
            link.output_protocol_id != "ap_old" for link in universal.linked_output_protocols
        )

    @staticmethod
    def _setup_carry_over_scenario(
        mock_mass: MagicMock, config_store: dict[str, Any]
    ) -> tuple[PlayerController, MockPlayer, list[Awaitable[object]]]:
        """
        Wire a replacement scenario for the config carry-over tests.

        Universal player 'up_old' (holding protocol player 'ap_live') is
        replaceable by native player 'cast_1'.
        """
        controller = PlayerController(mock_mass)
        up_provider = create_mock_universal_provider(mock_mass)

        def config_get(key: str, default: object = None) -> object:
            return config_store.get(key, default)

        def config_set(key: str, value: object) -> None:
            config_store[key] = value

        def config_remove(key: str) -> None:
            config_store.pop(key, None)

        scheduled_tasks: list[Awaitable[object]] = []

        def capture_task(task: Awaitable[object], *_args: Any, **_kwargs: Any) -> None:
            scheduled_tasks.append(task)

        mock_mass.config.get = MagicMock(side_effect=config_get)
        mock_mass.config.set = MagicMock(side_effect=config_set)
        mock_mass.config.remove = MagicMock(side_effect=config_remove)
        mock_mass.players = controller
        mock_mass.player_queues = MagicMock()
        mock_mass.call_later = MagicMock()
        mock_mass.loop = MagicMock()
        mock_mass.create_task = MagicMock(side_effect=capture_task)

        universal = UniversalPlayer(
            provider=up_provider,
            player_id="up_old",
            name="Soundbar (Universal)",
            device_info=DeviceInfo(model="Test", manufacturer="Test"),
            protocol_player_ids=["ap_live"],
        )
        universal._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, "AA:BB:CC:DD:EE:FF")
        universal._cache.clear()
        universal.update_state(signal_event=False)
        universal.set_initialized()

        cast_provider = MockProvider("cast", mass=mock_mass)
        native = MockPlayer(
            cast_provider,
            "cast_1",
            "Soundbar",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        native.set_initialized()

        airplay_provider = MockProvider("airplay", mass=mock_mass)
        ap_live = MockPlayer(
            airplay_provider,
            "ap_live",
            "Soundbar (AirPlay)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        ap_live.set_initialized()

        controller._players = {"up_old": universal, "cast_1": native, "ap_live": ap_live}
        controller._add_protocol_link(universal, ap_live, "airplay")
        return controller, native, scheduled_tasks

    async def test_replace_carries_over_universal_config(self, mock_mass: MagicMock) -> None:
        """Replacement migrates name, values, DSP and queue settings to the native player."""
        config_store: dict[str, Any] = {
            # fresh native configs store name == default_name; that must not
            # count as a user override blocking the name carry-over
            "players/cast_1": {
                "enabled": True,
                "name": "Soundbar",
                "default_name": "Soundbar",
                "values": {"volume_control": "native"},
            },
            "players/up_old": {
                "enabled": True,
                "name": "Living Room",
                "default_name": "Soundbar (Universal)",
                "values": {
                    "hide_in_ui": True,
                    "volume_control": "up_volume_control",
                    "linked_protocol_ids": ["ap_live", "ghost_proto"],
                    "device_identifiers": {"mac_address": "AA:BB:CC:DD:EE:FF"},
                    "device_info": {"model": "Test", "manufacturer": "Test"},
                    "ap_live||protocol||output_codec": "flac",
                },
            },
            "players/ap_live": {"enabled": True},
            "player_dsp/up_old": {"enabled": True, "filters": []},
            "player_queues/up_old": {"queue_id": "up_old", "values": {"crossfade_duration": 5}},
        }
        controller, native, scheduled_tasks = self._setup_carry_over_scenario(
            mock_mass, config_store
        )

        controller._check_replace_universal_player(native)

        # the custom display name and user-set values moved onto the native player
        assert config_store["players/cast_1/name"] == "Living Room"
        assert config_store["players/cast_1/values/hide_in_ui"] is True
        # values the native player has explicitly set are not overwritten
        assert "players/cast_1/values/volume_control" not in config_store
        # wrapper bookkeeping and protocol-mirror values are not carried over
        assert "players/cast_1/values/device_identifiers" not in config_store
        assert "players/cast_1/values/device_info" not in config_store
        assert "players/cast_1/values/ap_live||protocol||output_codec" not in config_store
        # linked ids come from the protocol move machinery, not the universal's raw list
        assert config_store["players/cast_1/values/linked_protocol_ids"] == ["ap_live"]
        # DSP settings moved wholesale
        assert config_store["player_dsp/cast_1"] == {"enabled": True, "filters": []}
        # queue settings moved and re-keyed onto the native player's queue
        assert config_store["player_queues/cast_1"] == {
            "queue_id": "cast_1",
            "values": {"crossfade_duration": 5},
        }
        assert "player_queues/up_old" not in config_store
        # the native player's in-place config gets reloaded to apply the new values
        assert any("_reapply_player_config" in repr(t) for t in scheduled_tasks)

        # and the universal player is still permanently removed
        unregister_tasks = [t for t in scheduled_tasks if "unregister" in repr(t)]
        assert len(unregister_tasks) == 1
        await unregister_tasks[0]
        assert "up_old" not in controller._players
        # migrated values survive the permanent cleanup of the universal player
        assert config_store["players/cast_1/name"] == "Living Room"
        assert "players/up_old" not in config_store
        assert "player_dsp/up_old" not in config_store

    def test_replace_carries_over_nothing_without_user_config(self, mock_mass: MagicMock) -> None:
        """A non-customized universal player migrates nothing (no default-name carry)."""
        config_store: dict[str, Any] = {
            "players/cast_1": {"enabled": True},
            "players/up_old": {
                "enabled": True,
                "name": "Soundbar (Universal)",
                "default_name": "Soundbar (Universal)",
                "values": {"linked_protocol_ids": ["ap_live"]},
            },
            "players/ap_live": {"enabled": True},
        }
        controller, native, scheduled_tasks = self._setup_carry_over_scenario(
            mock_mass, config_store
        )

        controller._check_replace_universal_player(native)

        assert "players/cast_1/name" not in config_store
        assert "player_dsp/cast_1" not in config_store
        assert "player_queues/cast_1" not in config_store
        # nothing migrated, so no config reload is scheduled
        assert not any("_reapply_player_config" in repr(t) for t in scheduled_tasks)
        assert any("unregister" in repr(t) for t in scheduled_tasks)

    def test_replace_keeps_native_name_dsp_and_queue_values(self, mock_mass: MagicMock) -> None:
        """Values explicitly set on the native player win over the universal player's."""
        config_store: dict[str, Any] = {
            "players/cast_1": {"enabled": True, "name": "Kitchen", "default_name": "Soundbar"},
            "players/up_old": {
                "enabled": True,
                "name": "Living Room",
                "default_name": "Soundbar (Universal)",
                "values": {"hide_in_ui": True},
            },
            "players/ap_live": {"enabled": True},
            "player_dsp/cast_1": {"enabled": False, "filters": ["native"]},
            "player_dsp/up_old": {"enabled": True, "filters": ["universal"]},
            "player_queues/cast_1": {"queue_id": "cast_1", "values": {"crossfade_duration": 2}},
            "player_queues/up_old": {
                "queue_id": "up_old",
                "values": {"crossfade_duration": 8, "autoplay_mode": "smart"},
            },
        }
        controller, native, _ = self._setup_carry_over_scenario(mock_mass, config_store)

        controller._check_replace_universal_player(native)

        # the native player's own custom name and DSP config win
        assert "players/cast_1/name" not in config_store
        assert config_store["players/cast_1"]["name"] == "Kitchen"
        assert config_store["player_dsp/cast_1"] == {"enabled": False, "filters": ["native"]}
        # queue values merge without overwriting the native queue's own values
        assert config_store["player_queues/cast_1"] == {
            "queue_id": "cast_1",
            "values": {"crossfade_duration": 2, "autoplay_mode": "smart"},
        }
        assert "player_queues/up_old" not in config_store
        # non-conflicting values still migrate
        assert config_store["players/cast_1/values/hide_in_ui"] is True

    async def test_reapply_player_config_refreshes_native_player(
        self, mock_mass: MagicMock
    ) -> None:
        """Reloading the config applies a migrated custom name to the live player."""
        controller = PlayerController(mock_mass)
        mock_mass.players = controller
        mock_mass.player_queues = MagicMock()
        mock_mass.call_later = MagicMock()
        provider = MockProvider("cast", mass=mock_mass)
        native = MockPlayer(provider, "cast_1", "Soundbar")
        native.set_initialized()
        controller._players = {"cast_1": native}

        new_config = PlayerConfig.parse(
            [],
            {
                "player_id": "cast_1",
                "provider": "cast",
                "name": "Living Room",
                "default_name": "Soundbar",
            },
        )
        mock_mass.config.get_player_config = AsyncMock(return_value=new_config)

        await controller._reapply_player_config("cast_1")

        assert native.config is new_config
        assert native.display_name == "Living Room"
        signaled_events = [call.args[0] for call in mock_mass.signal_event.call_args_list]
        assert EventType.PLAYER_CONFIG_UPDATED in signaled_events

    def test_replace_repoints_group_memberships(self, mock_mass: MagicMock) -> None:
        """Replacement re-points group memberships from the wrapper to the native player."""
        config_store: dict[str, Any] = {
            "players/cast_1": {"enabled": True},
            "players/up_old": {
                "enabled": True,
                "name": "Soundbar (Universal)",
                "default_name": "Soundbar (Universal)",
                "values": {"linked_protocol_ids": ["ap_live"]},
            },
            "players/ap_live": {"enabled": True},
            # nested players dict scanned by _repoint_group_memberships
            "players": {
                "group_1": {
                    "enabled": True,
                    "values": {
                        "group_members": ["up_old", "other"],
                        "allowed_members": ["up_old"],
                    },
                },
                "group_2": {
                    "enabled": True,
                    # native id already listed -> no duplicate after re-point
                    "values": {"group_members": ["up_old", "cast_1", "extra"]},
                },
            },
        }
        controller, native, _ = self._setup_carry_over_scenario(mock_mass, config_store)
        group_1 = MockPlayer(MockProvider("player_group", mass=mock_mass), "group_1", "Group")
        group_1.set_initialized()
        controller._players["group_1"] = group_1

        with patch.object(group_1, "refresh_state") as mock_refresh:
            controller._check_replace_universal_player(native)

        # the wrapper id is replaced by the native id on both membership keys
        assert config_store["players/group_1/values/group_members"] == ["cast_1", "other"]
        assert config_store["players/group_1/values/allowed_members"] == ["cast_1"]
        # no duplicate when the native id is already a member
        assert config_store["players/group_2/values/group_members"] == ["cast_1", "extra"]
        # a registered player whose membership changed is refreshed
        mock_refresh.assert_called()

    async def test_replace_stops_playing_universal_before_unregister(
        self, mock_mass: MagicMock
    ) -> None:
        """A playing universal player is stopped before it is permanently removed."""
        config_store: dict[str, Any] = {
            "players/cast_1": {"enabled": True},
            "players/up_old": {
                "enabled": True,
                "name": "Soundbar (Universal)",
                "default_name": "Soundbar (Universal)",
                "values": {"linked_protocol_ids": ["ap_live"]},
            },
            "players/ap_live": {"enabled": True},
        }
        controller, native, scheduled_tasks = self._setup_carry_over_scenario(
            mock_mass, config_store
        )
        controller._players["up_old"]._attr_playback_state = PlaybackState.PLAYING

        call_order: list[str] = []
        mock_mass.player_queues.stop = AsyncMock(
            side_effect=lambda *_a, **_k: call_order.append("stop")
        )

        controller._check_replace_universal_player(native)
        task = next(t for t in scheduled_tasks if "unregister" in repr(t))

        def _record_unregister(*_a: Any, **_k: Any) -> None:
            call_order.append("unregister")

        with patch.object(controller, "unregister", new=AsyncMock(side_effect=_record_unregister)):
            await task

        assert call_order == ["stop", "unregister"]
        mock_mass.player_queues.stop.assert_awaited_once_with("up_old")

    async def test_replace_idle_universal_not_stopped(self, mock_mass: MagicMock) -> None:
        """An idle universal player is removed without a stop command."""
        config_store: dict[str, Any] = {
            "players/cast_1": {"enabled": True},
            "players/up_old": {
                "enabled": True,
                "name": "Soundbar (Universal)",
                "default_name": "Soundbar (Universal)",
                "values": {"linked_protocol_ids": ["ap_live"]},
            },
            "players/ap_live": {"enabled": True},
        }
        controller, native, scheduled_tasks = self._setup_carry_over_scenario(
            mock_mass, config_store
        )
        mock_mass.player_queues.stop = AsyncMock()

        controller._check_replace_universal_player(native)
        task = next(t for t in scheduled_tasks if "unregister" in repr(t))
        await task

        mock_mass.player_queues.stop.assert_not_awaited()
        assert "up_old" not in controller._players


class TestEndToEndDuplicateProtocol:
    """
    End-to-end integration tests for duplicate protocol → separate universal player flow.

    These tests exercise the full async flow through _delayed_protocol_evaluation,
    ensure_universal_player_for_protocols, and _create_separate_universal_player,
    verifying that a second instance of the same protocol domain on the same host
    results in a separate universal player.
    """

    @staticmethod
    def _setup_e2e_controller(
        mock_mass: MagicMock,
    ) -> tuple[PlayerController, UniversalPlayerProvider]:
        """
        Set up a PlayerController and UniversalPlayerProvider wired for E2E testing.

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
        """
        Path A: _delayed_protocol_evaluation creates separate universal player for duplicate.

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
        """
        Path A: _delayed_protocol_evaluation correctly joins when domain is different.

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
        """
        Path B: ensure_universal_player_for_protocols separates can_join vs rejected.

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
        """
        Path B: First protocol player creates universal, second gets separate.

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
        """
        Path C: Two Sendspin bridge instances on same host get separate universal players.

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
        """
        Test that three AirPlay instances each get their own universal player.

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
    async def test_same_domain_same_ip_get_separate_universals(self, mock_mass: MagicMock) -> None:
        """
        Test that multiple instances of the same protocol on the same host stay separate.

        Scenario: 3 shairport-sync AirPlay instances on the same Raspberry Pi.
        All share the same IP and have locally-administered MACs.
        The IP fallback must NOT merge them — domain dedup takes precedence.
        """
        controller, _ = self._setup_e2e_controller(mock_mass)

        ap_provider = MockProvider("airplay", mass=mock_mass)
        players = []
        for i in range(1, 4):
            p = MockPlayer(
                ap_provider,
                f"ap_{i}",
                f"Shairport Zone {i}",
                player_type=PlayerType.PROTOCOL,
                identifiers={
                    # Each has a distinct LA MAC (as shairport-sync generates)
                    IdentifierType.MAC_ADDRESS: f"02:AA:BB:CC:DD:{i:02X}",
                    IdentifierType.IP_ADDRESS: "192.168.1.10",
                },
            )
            p.set_initialized()
            players.append(p)

        controller._players = {p.player_id: p for p in players}

        for p in players:
            await controller._delayed_protocol_evaluation(p.player_id)

        # Each should have its own universal player (NOT merged)
        parent_ids = {p.protocol_parent_id for p in players}
        assert None not in parent_ids
        assert len(parent_ids) == 3

    @pytest.mark.asyncio
    async def test_same_domain_same_mac_get_separate_universals(self, mock_mass: MagicMock) -> None:
        """
        Test that two snapcast instances with the same ARP-resolved MAC stay separate.

        Scenario: Two snapcast clients on the same Mac mini. Both have distinct
        locally-administered MACs but ARP resolves both to the same real hardware MAC.
        They should each get their own universal player, not be merged.
        """
        controller, _ = self._setup_e2e_controller(mock_mass)

        snap_provider = MockProvider("snapcast", mass=mock_mass)
        snap1 = MockPlayer(
            snap_provider,
            "ma_cea0b4ed2221",
            "Mac-mini-15269.local",
            player_type=PlayerType.PROTOCOL,
            identifiers={
                # ARP-enriched: both resolve to same real MAC
                IdentifierType.MAC_ADDRESS: "D0:11:E5:00:67:2F",
                IdentifierType.IP_ADDRESS: "192.168.1.42",
            },
        )
        snap2 = MockPlayer(
            snap_provider,
            "ma_cea0b4ed2220",
            "Mac-mini-63197.local",
            player_type=PlayerType.PROTOCOL,
            identifiers={
                IdentifierType.MAC_ADDRESS: "D0:11:E5:00:67:2F",
                IdentifierType.IP_ADDRESS: "192.168.1.42",
            },
        )
        snap1._attr_available = False
        snap1._cache.clear()
        snap1.update_state(signal_event=False)
        snap2._attr_available = False
        snap2._cache.clear()
        snap2.update_state(signal_event=False)
        snap1.set_initialized()
        snap2.set_initialized()

        controller._players = {"ma_cea0b4ed2221": snap1, "ma_cea0b4ed2220": snap2}

        await controller._delayed_protocol_evaluation("ma_cea0b4ed2221")
        await controller._delayed_protocol_evaluation("ma_cea0b4ed2220")

        # Each should have its own universal player
        assert snap1.protocol_parent_id is not None
        assert snap2.protocol_parent_id is not None
        assert snap1.protocol_parent_id != snap2.protocol_parent_id

    @pytest.mark.asyncio
    async def test_mixed_protocols_plus_duplicate_creates_correct_topology(
        self, mock_mass: MagicMock
    ) -> None:
        """
        Test that mixed protocols + duplicate create correct player topology.

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
    """
    Tests for sibling protocol matching via _match_via_linked_protocols.

    When a native player (e.g., HEOS) doesn't have its own MAC/serial identifiers
    but has protocol players (e.g., AirPlay) already linked, a new protocol player
    (e.g., Sendspin bridge) should be matched to the native player through the
    shared identifiers of sibling protocols.
    """

    def test_sendspin_matches_heos_via_airplay_sibling(self, mock_mass: MagicMock) -> None:
        """
        Test Sendspin links to HEOS through AirPlay's shared AIRPLAY_ID.

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

        # _try_link_to_existing_player should succeed via sibling matching
        result = controller._try_link_to_existing_player(spb_player, "sendspin")
        assert result is True
        assert spb_player.protocol_parent_id == "2140037800"


class TestDelayedEvalRetry:
    """
    Tests for the retry of _try_link_to_existing_player in _delayed_protocol_evaluation.

    When a protocol player's delayed evaluation fires, native players may have
    registered during the delay period. The retry ensures they are checked before
    falling through to universal player creation.
    """

    @pytest.mark.asyncio
    async def test_delayed_eval_links_to_native_registered_during_delay(
        self, mock_mass: MagicMock
    ) -> None:
        """
        Test delayed eval links to a native player that registered during the delay.

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

        # Should return early without creating universal player
        await controller._delayed_protocol_evaluation("spb_1")
        assert spb_player.protocol_parent_id == "some_parent"


class TestNativePlayerSiblingLinking:
    """
    Tests for the second pass in _try_link_protocols_to_native using sibling matching.

    After a native player registers and recovers cached protocol links, the second
    pass should match remaining unlinked protocol players via sibling identifiers.
    """

    def test_native_registration_links_sendspin_via_sibling(self, mock_mass: MagicMock) -> None:
        """
        Test HEOS registration links Sendspin via AirPlay sibling identifiers.

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


class TestCachedProtocolLinkRecovery:
    """Tests for recovering protocol links from config."""

    def test_recovered_links_are_applied_through_the_setter(self, mock_mass: MagicMock) -> None:
        """Recovery applies a single link per protocol domain through the setter."""
        controller = PlayerController(mock_mass)

        protocol_configs = {
            "ap_first": {
                "provider": "airplay--first",
                "player_type": "protocol",
                "values": {"protocol_parent_id": "heos_player"},
            },
            "ap_second": {
                "provider": "airplay--second",
                "player_type": "protocol",
                "values": {"protocol_parent_id": "heos_player"},
            },
        }

        def config_get_side_effect(key: str, default: object = None) -> object:
            if key == "players/heos_player/values/linked_protocol_ids":
                return ["ap_first", "ap_second"]
            if key == "players":
                return protocol_configs
            if key.startswith("players/"):
                return protocol_configs.get(key.split("/", maxsplit=1)[1])
            return default

        mock_mass.config.get = MagicMock(side_effect=config_get_side_effect)

        heos_player = MockPlayer(
            MockProvider("heos", mass=mock_mass),
            "heos_player",
            "Denon AVR-X2700H",
        )

        controller._recover_cached_protocol_links(heos_player)

        recovered = [link.output_protocol_id for link in heos_player.linked_output_protocols]
        assert recovered == ["ap_first"]
        mock_mass.players.trigger_player_update.assert_called_once_with("heos_player")

        # a repeat pass has nothing left to recover, so no update is triggered
        controller._recover_cached_protocol_links(heos_player)
        mock_mass.players.trigger_player_update.assert_called_once_with("heos_player")


class TestMultiMACMatching:
    """Tests for multi-MAC matching (reported MAC vs ARP MAC)."""

    def test_match_on_reported_mac_when_arp_mac_differs(self, mock_mass: MagicMock) -> None:
        """Two players match when one's reported MAC equals the other's ARP-resolved MAC."""
        controller = PlayerController(mock_mass)

        provider = MockProvider("test")
        # Player A has ARP-resolved MAC in identifiers, reported MAC saved in extra_data
        player_a = MockPlayer(
            provider,
            "player_a",
            "Player A",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:01"},
        )
        player_a.extra_data["reported_mac"] = "AA:BB:CC:DD:EE:02"

        # Player B has a MAC that matches player_a's reported MAC
        player_b = MockPlayer(
            provider,
            "player_b",
            "Player B",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:02"},
        )

        assert controller._identifiers_match(player_a, player_b) is True

    def test_match_on_reported_mac_reverse(self, mock_mass: MagicMock) -> None:
        """Match when player_b's reported MAC matches player_a's ARP MAC."""
        controller = PlayerController(mock_mass)

        provider = MockProvider("test")
        player_a = MockPlayer(
            provider,
            "player_a",
            "Player A",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:02"},
        )

        player_b = MockPlayer(
            provider,
            "player_b",
            "Player B",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:01"},
        )
        player_b.extra_data["reported_mac"] = "AA:BB:CC:DD:EE:02"

        assert controller._identifiers_match(player_a, player_b) is True

    def test_no_match_when_both_macs_differ(self, mock_mass: MagicMock) -> None:
        """No match when all MACs differ between players."""
        controller = PlayerController(mock_mass)

        provider = MockProvider("test")
        player_a = MockPlayer(
            provider,
            "player_a",
            "Player A",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:01"},
        )
        player_a.extra_data["reported_mac"] = "AA:BB:CC:DD:EE:02"

        player_b = MockPlayer(
            provider,
            "player_b",
            "Player B",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:03"},
        )
        player_b.extra_data["reported_mac"] = "AA:BB:CC:DD:EE:04"

        assert controller._identifiers_match(player_a, player_b) is False

    def test_match_reported_mac_with_la_bit_normalization(self, mock_mass: MagicMock) -> None:
        """Reported MAC matching also applies LA bit normalization."""
        controller = PlayerController(mock_mass)

        provider = MockProvider("test")
        # Player A has ARP MAC, reported MAC has LA bit set
        player_a = MockPlayer(
            provider,
            "player_a",
            "Player A",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:01"},
        )
        player_a.extra_data["reported_mac"] = "56:78:C9:E6:0D:A0"  # LA variant

        # Player B has the hardware MAC
        player_b = MockPlayer(
            provider,
            "player_b",
            "Player B",
            identifiers={IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A0"},  # real MAC
        )

        assert controller._identifiers_match(player_a, player_b) is True


class TestProtocolParentIdPersistence:
    """Tests for protocol_parent_id persistence for all parent types."""

    def test_parent_id_saved_for_universal_parent(self, mock_mass: MagicMock) -> None:
        """protocol_parent_id should be saved when linking to a universal player."""
        controller = PlayerController(mock_mass)

        def _config_get(key: str, default: object = None) -> object:
            # Protocol player config must exist for _save_protocol_parent_id to write
            if key == "players/airplay_test":
                return {"enabled": True}
            return default if default is not None else []

        mock_mass.config.get = MagicMock(side_effect=_config_get)

        universal_provider = create_mock_universal_provider(mock_mass)
        parent = UniversalPlayer(
            provider=universal_provider,
            player_id="up_test",
            name="Test Universal",
            device_info=DeviceInfo(),
            protocol_player_ids=[],
        )
        parent.update_state(signal_event=False)

        protocol_provider = MockProvider("airplay")
        protocol_player = MockPlayer(
            protocol_provider,
            "airplay_test",
            "AirPlay Test",
            player_type=PlayerType.PROTOCOL,
        )

        controller._players = {
            "up_test": parent,
            "airplay_test": protocol_player,
        }

        controller._add_protocol_link(parent, protocol_player, "airplay")

        # Verify _save_protocol_parent_id was called (config.set with parent ID)
        config_set_calls = [
            call
            for call in mock_mass.config.set.call_args_list
            if "protocol_parent_id" in str(call)
        ]
        assert len(config_set_calls) >= 1, (
            "protocol_parent_id should be saved for universal player parents"
        )

    def test_parent_id_cleared_for_universal_parent(self, mock_mass: MagicMock) -> None:
        """protocol_parent_id should be cleared when unlinking from a universal player."""
        controller = PlayerController(mock_mass)
        mock_mass.config.get = MagicMock(return_value={"enabled": True})

        universal_provider = create_mock_universal_provider(mock_mass)
        parent = UniversalPlayer(
            provider=universal_provider,
            player_id="up_test",
            name="Test Universal",
            device_info=DeviceInfo(),
            protocol_player_ids=["airplay_test"],
        )
        parent.update_state(signal_event=False)

        protocol_provider = MockProvider("airplay")
        protocol_player = MockPlayer(
            protocol_provider,
            "airplay_test",
            "AirPlay Test",
            player_type=PlayerType.PROTOCOL,
        )
        protocol_player.set_protocol_parent_id("up_test")

        # Set up linked protocols
        parent.set_linked_output_protocols(
            [
                LinkedOutputProtocol(
                    output_protocol_id="airplay_test",
                    protocol_domain="airplay",
                    priority=10,
                )
            ]
        )

        controller._players = {
            "up_test": parent,
            "airplay_test": protocol_player,
        }

        controller._remove_protocol_link(parent, "airplay_test")

        # Verify protocol_parent_id was cleared in config
        config_set_calls = [
            call
            for call in mock_mass.config.set.call_args_list
            if "protocol_parent_id" in str(call) and call.args[1] is None
        ]
        assert len(config_set_calls) >= 1, (
            "protocol_parent_id should be cleared for universal player parents"
        )


class TestCleanupProtocolLinks:
    """Tests for protocol link cleanup on player removal."""

    def test_cleanup_clears_config_parent_id(self, mock_mass: MagicMock) -> None:
        """When a universal player is removed, protocol parent_ids are cleared in config."""
        controller = PlayerController(mock_mass)
        mock_mass.config.get = MagicMock(return_value={"enabled": True})

        universal_provider = create_mock_universal_provider(mock_mass)
        parent = UniversalPlayer(
            provider=universal_provider,
            player_id="up_test",
            name="Test Universal",
            device_info=DeviceInfo(),
            protocol_player_ids=["airplay_test"],
        )
        parent.update_state(signal_event=False)

        protocol_provider = MockProvider("airplay")
        protocol_player = MockPlayer(
            protocol_provider,
            "airplay_test",
            "AirPlay Test",
            player_type=PlayerType.PROTOCOL,
        )
        protocol_player.set_protocol_parent_id("up_test")

        parent.set_linked_output_protocols(
            [
                LinkedOutputProtocol(
                    output_protocol_id="airplay_test",
                    protocol_domain="airplay",
                    priority=10,
                )
            ]
        )

        controller._players = {
            "up_test": parent,
            "airplay_test": protocol_player,
        }

        controller._cleanup_protocol_links(parent)

        # Verify protocol_parent_id was cleared in config
        config_set_calls = [
            call
            for call in mock_mass.config.set.call_args_list
            if "airplay_test" in str(call) and "protocol_parent_id" in str(call)
        ]
        assert len(config_set_calls) >= 1, (
            "protocol_parent_id should be cleared in config on parent removal"
        )
        # Verify in-memory parent cleared
        assert protocol_player.protocol_parent_id is None

    def test_cleanup_native_parent_schedules_protocol_re_evaluation(
        self, mock_mass: MagicMock
    ) -> None:
        """Permanent native parent removal should reset links and reschedule protocols."""
        controller = PlayerController(mock_mass)

        config_store: dict[str, object] = {
            "players/sonos_1": {"enabled": True},
            "players/sonos_1/values/linked_protocol_ids": ["airplay_live", "dlna_cached"],
            "players/airplay_live": {"enabled": True},
            "players/dlna_cached": {"enabled": True},
        }

        def config_get(key: str, default: object = None) -> object:
            return config_store.get(key, default)

        def config_set(key: str, value: object) -> None:
            config_store[key] = value

        mock_mass.config.get = MagicMock(side_effect=config_get)
        mock_mass.config.set = MagicMock(side_effect=config_set)

        native_provider = MockProvider("sonos", mass=mock_mass)
        parent = MockPlayer(
            native_provider,
            "sonos_1",
            "Sonos",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )

        protocol_provider = MockProvider("airplay", mass=mock_mass)
        protocol_player = MockPlayer(
            protocol_provider,
            "airplay_live",
            "AirPlay Test",
            player_type=PlayerType.PROTOCOL,
        )
        protocol_player.set_protocol_parent_id("sonos_1")

        parent.set_linked_output_protocols(
            [
                LinkedOutputProtocol(
                    output_protocol_id="airplay_live",
                    protocol_domain="airplay",
                    priority=10,
                )
            ]
        )

        controller._players = {
            "sonos_1": parent,
            "airplay_live": protocol_player,
        }

        with patch.object(controller, "_schedule_protocol_evaluation") as mock_schedule:
            controller._cleanup_protocol_links(parent)

        assert protocol_player.protocol_parent_id is None
        assert config_store["players/airplay_live/values/protocol_parent_id"] is None
        assert config_store["players/dlna_cached/values/protocol_parent_id"] is None
        mock_schedule.assert_called_once_with(protocol_player)

    def test_cleanup_universal_parent_clears_cached_only_protocol_ids(
        self, mock_mass: MagicMock
    ) -> None:
        """Universal cleanup should also clear cached protocol ids that are not active."""
        controller = PlayerController(mock_mass)

        config_store: dict[str, object] = {
            "players/up_test": {"enabled": True},
            "players/airplay_live": {"enabled": True},
            "players/dlna_cached": {"enabled": True},
        }

        def config_get(key: str, default: object = None) -> object:
            return config_store.get(key, default)

        def config_set(key: str, value: object) -> None:
            config_store[key] = value

        mock_mass.config.get = MagicMock(side_effect=config_get)
        mock_mass.config.set = MagicMock(side_effect=config_set)

        universal_provider = create_mock_universal_provider(mock_mass)
        parent = UniversalPlayer(
            provider=universal_provider,
            player_id="up_test",
            name="Test Universal",
            device_info=DeviceInfo(),
            protocol_player_ids=["airplay_live", "dlna_cached"],
        )
        parent.update_state(signal_event=False)

        protocol_provider = MockProvider("airplay", mass=mock_mass)
        protocol_player = MockPlayer(
            protocol_provider,
            "airplay_live",
            "AirPlay Test",
            player_type=PlayerType.PROTOCOL,
        )
        protocol_player.set_protocol_parent_id("up_test")

        parent.set_linked_output_protocols(
            [
                LinkedOutputProtocol(
                    output_protocol_id="airplay_live",
                    protocol_domain="airplay",
                    priority=10,
                )
            ]
        )

        controller._players = {
            "up_test": parent,
            "airplay_live": protocol_player,
        }

        with patch.object(controller, "_schedule_protocol_evaluation") as mock_schedule:
            controller._cleanup_protocol_links(parent)

        assert protocol_player.protocol_parent_id is None
        assert config_store["players/airplay_live/values/protocol_parent_id"] is None
        assert config_store["players/dlna_cached/values/protocol_parent_id"] is None
        mock_schedule.assert_called_once_with(protocol_player)


class TestDetachProtocolChildren:
    """Tests for detaching protocol players of a removed player that is not registered."""

    @staticmethod
    def _setup(
        mock_mass: MagicMock, live_parent_id: str | None
    ) -> tuple[PlayerController, MockPlayer, dict[str, object]]:
        """Set up a registered protocol player whose (unregistered) parent is sonos_1."""
        controller = PlayerController(mock_mass)

        config_store: dict[str, object] = {
            "players": {"sonos_1": {}, "airplay_live": {}},
            "players/sonos_1": {"enabled": True},
            "players/airplay_live": {"enabled": True},
            "players/airplay_live/values/protocol_parent_id": "sonos_1",
        }

        mock_mass.config.get = MagicMock(
            side_effect=lambda key, default=None: config_store.get(key, default)
        )
        mock_mass.config.set = MagicMock(
            side_effect=lambda key, value: config_store.__setitem__(key, value)
        )

        protocol_player = MockPlayer(
            MockProvider("airplay", mass=mock_mass),
            "airplay_live",
            "AirPlay Test",
            player_type=PlayerType.PROTOCOL,
        )
        protocol_player.set_protocol_parent_id(live_parent_id)
        controller._players = {"airplay_live": protocol_player}
        return controller, protocol_player, config_store

    def test_detach_on_removal_of_an_unregistered_parent(self, mock_mass: MagicMock) -> None:
        """Removing a player that is no longer registered detaches its protocol player."""
        controller, protocol_player, config_store = self._setup(mock_mass, "sonos_1")

        with patch.object(controller, "_schedule_protocol_evaluation") as mock_schedule:
            controller.delete_player_config("sonos_1")

        assert protocol_player.protocol_parent_id is None
        assert config_store["players/airplay_live/values/protocol_parent_id"] is None
        mock_schedule.assert_called_once_with(protocol_player)

    def test_detach_of_a_protocol_player_waiting_for_its_parent(self, mock_mass: MagicMock) -> None:
        """A protocol player that only has the parent link in its config is detached too."""
        controller, protocol_player, config_store = self._setup(mock_mass, None)

        with patch.object(controller, "_schedule_protocol_evaluation") as mock_schedule:
            controller.delete_player_config("sonos_1")

        assert config_store["players/airplay_live/values/protocol_parent_id"] is None
        mock_schedule.assert_called_once_with(protocol_player)

    def test_detach_skips_a_protocol_player_of_another_parent(self, mock_mass: MagicMock) -> None:
        """A protocol player that already moved to another parent keeps its link."""
        controller, protocol_player, config_store = self._setup(mock_mass, "cast_1")

        with patch.object(controller, "_schedule_protocol_evaluation") as mock_schedule:
            controller.delete_player_config("sonos_1")

        assert protocol_player.protocol_parent_id == "cast_1"
        assert config_store["players/airplay_live/values/protocol_parent_id"] == "sonos_1"
        mock_schedule.assert_not_called()


class TestStaleConfigMigration:
    """Tests for protocol parent link repair on startup."""

    def test_clears_stale_parent_ids(self, mock_mass: MagicMock) -> None:
        """Stale parent_ids pointing to deleted players are cleared on startup."""
        controller = PlayerController(mock_mass)

        # Set up config with a protocol player pointing to a deleted universal player
        mock_mass.config.get = MagicMock(
            return_value={
                "airplay_test": {
                    "player_type": "protocol",
                    "values": {
                        "protocol_parent_id": "up_deleted_player",
                    },
                },
                "heos_player": {
                    "player_type": "player",
                    "values": {},
                },
            }
        )

        controller._repair_protocol_parent_links()

        # Verify the stale parent_id was cleared
        mock_mass.config.set.assert_any_call(
            "players/airplay_test/values/protocol_parent_id",
            None,
        )
        mock_mass.config.set_player_type.assert_not_called()

    def test_keeps_valid_parent_ids(self, mock_mass: MagicMock) -> None:
        """Valid parent_ids pointing to existing players are preserved."""
        controller = PlayerController(mock_mass)

        mock_mass.config.get = MagicMock(
            return_value={
                "airplay_test": {
                    "player_type": "protocol",
                    "values": {
                        "protocol_parent_id": "up_valid_player",
                    },
                },
                "up_valid_player": {
                    "player_type": "group",
                    "provider": "universal_player",
                    "values": {},
                },
            }
        )

        controller._repair_protocol_parent_links()

        # Verify no set calls were made to clear the parent_id
        for call in mock_mass.config.set.call_args_list:
            assert "protocol_parent_id" not in str(call), "Valid parent_id should not be cleared"
        mock_mass.config.set_player_type.assert_not_called()

    def test_heals_stale_player_type_of_linked_child(self, mock_mass: MagicMock) -> None:
        """A parented child whose player_type was corrupted is healed back to protocol."""
        controller = PlayerController(mock_mass)

        mock_mass.config.get = MagicMock(
            return_value={
                "squeezelite_child": {
                    "player_type": "player",
                    "values": {
                        "protocol_parent_id": "up_valid_player",
                    },
                },
                "up_valid_player": {
                    "player_type": "group",
                    "provider": "universal_player",
                    "values": {},
                },
            }
        )

        controller._repair_protocol_parent_links()

        mock_mass.config.set_player_type.assert_called_once_with(
            "squeezelite_child", PlayerType.PROTOCOL
        )

    def test_native_registration_clears_stale_parent_link(self, mock_mass: MagicMock) -> None:
        """A player registering with a non-protocol type drops its leftover parent link."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("sendspin", mass=mock_mass)
        player = MockPlayer(provider, "web_player", "Web Player", player_type=PlayerType.PLAYER)

        config_values = {
            f"{CONF_PLAYERS}/web_player": {"player_type": "player"},
            f"{CONF_PLAYERS}/web_player/values/protocol_parent_id": "up_old_parent",
        }
        mock_mass.config.get = MagicMock(
            side_effect=lambda key, default=None: config_values.get(key, default)
        )

        with patch.object(controller, "_try_link_protocols_to_native"):
            controller._evaluate_protocol_links(player)

        mock_mass.config.set.assert_any_call(
            f"{CONF_PLAYERS}/web_player/values/protocol_parent_id",
            None,
        )


class TestUniversalPlayerRestoreOrphanCleanup:
    """Tests for UniversalPlayerProvider._restore_player membership repair behavior."""

    @staticmethod
    def _setup_config_get(
        mock_mass: MagicMock,
        universal_id: str,
        linked_protocol_ids: list[str],
        protocol_configs: dict[str, dict[str, object]],
    ) -> None:
        """Wire mass.config.get to return universal/protocol configs by key."""
        universal_conf = {
            "values": {
                "linked_protocol_ids": list(linked_protocol_ids),
                "device_identifiers": {},
                "device_info": {},
            },
            "name": "Test Universal",
        }
        all_configs: dict[str, object] = {universal_id: universal_conf, **protocol_configs}

        def _get(key: str, default: object = None) -> object:
            if key == CONF_PLAYERS:
                return all_configs
            if key.startswith(f"{CONF_PLAYERS}/"):
                pid = key.split("/", 1)[1]
                return all_configs.get(pid, default)
            return default

        mock_mass.config.get.side_effect = _get

    @staticmethod
    def _wire_nested_config(mock_mass: MagicMock, store: dict[str, Any]) -> None:
        """Wire mass.config get/set/remove to a nested, path-addressable store."""

        def _get(key: str, default: object = None) -> object:
            node: Any = store
            for part in key.split("/"):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return node

        def _set(key: str, value: object) -> None:
            parts = key.split("/")
            node: dict[str, Any] = store
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value

        def _remove(key: str) -> None:
            parts = key.split("/")
            node: Any = store
            for part in parts[:-1]:
                node = node.get(part) if isinstance(node, dict) else None
                if node is None:
                    return
            if isinstance(node, dict):
                node.pop(parts[-1], None)

        mock_mass.config.get = MagicMock(side_effect=_get)
        mock_mass.config.set = MagicMock(side_effect=_set)
        mock_mass.config.remove = MagicMock(side_effect=_remove)

    @pytest.mark.asyncio
    async def test_orphan_protocol_dropped_but_configs_kept(self, mock_mass: MagicMock) -> None:
        """Orphan protocol is dropped from membership without deleting any config."""
        provider = create_mock_universal_provider(mock_mass)
        universal_id = "up_test"
        orphan_id = "spb_orphan"

        self._setup_config_get(
            mock_mass,
            universal_id,
            [orphan_id],
            {
                orphan_id: {
                    "player_type": "protocol",
                    "values": {"protocol_parent_id": None},
                },
            },
        )
        mock_mass.players = MagicMock()
        mock_mass.players.get_player = MagicMock(return_value=None)
        mock_mass.players.delete_player_config = MagicMock()
        mock_mass.players.unregister = AsyncMock()
        mock_mass.players.register_or_update = AsyncMock()
        mock_mass.config.remove_player_config = AsyncMock()

        await provider._restore_player(universal_id)

        # Neither the orphan's config nor the universal's config is deleted
        mock_mass.players.delete_player_config.assert_not_called()
        mock_mass.players.unregister.assert_not_called()
        mock_mass.config.remove_player_config.assert_not_called()
        # With no valid protocols left, the universal is simply not restored
        mock_mass.players.register_or_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_protocol_kept_and_no_cleanup(self, mock_mass: MagicMock) -> None:
        """Protocol with a valid protocol_parent_id is kept and no cleanup runs."""
        provider = create_mock_universal_provider(mock_mass)
        universal_id = "up_test"
        protocol_id = "spb_valid"

        self._setup_config_get(
            mock_mass,
            universal_id,
            [protocol_id],
            {
                protocol_id: {
                    "player_type": "protocol",
                    "values": {"protocol_parent_id": universal_id},
                },
            },
        )
        mock_mass.players = MagicMock()
        mock_mass.players.get_player = MagicMock(return_value=None)
        mock_mass.players.delete_player_config = MagicMock()
        mock_mass.players.unregister = AsyncMock()
        mock_mass.players.register_or_update = AsyncMock()
        mock_mass.config.remove_player_config = AsyncMock()
        mock_mass.config.get_base_player_config.return_value = create_mock_config("Test Universal")

        await provider._restore_player(universal_id)

        mock_mass.players.delete_player_config.assert_not_called()
        mock_mass.players.unregister.assert_not_called()
        mock_mass.config.remove_player_config.assert_not_called()
        # Universal is constructed and registered with the valid protocol kept
        mock_mass.players.register_or_update.assert_awaited_once()
        registered = mock_mass.players.register_or_update.call_args.args[0]
        assert protocol_id in registered._protocol_player_ids

    @pytest.mark.asyncio
    async def test_restore_keeps_linked_protocol_after_startup_type_rewrite(
        self, mock_mass: MagicMock
    ) -> None:
        """A linked protocol whose type was rewritten must not delete its universal config."""
        provider = create_mock_universal_provider(mock_mass)
        universal_id = "upe45f0170ef67"
        protocol_id = "e4:5f:01:70:ef:67"
        universal_config: dict[str, Any] = {
            "player_id": universal_id,
            "provider": "universal_player",
            "player_type": "player",
            "enabled": True,
            "name": "WC-Player",
            "default_name": "solarium-bath-sl",
            "values": {
                "hide_in_ui": True,
                "announce_volume_min": 55,
                "announce_volume_max": 98,
                "play_media_overrides_group": False,
                "linked_protocol_ids": [protocol_id],
                "device_identifiers": {},
                "device_info": {},
            },
        }
        configs: dict[str, dict[str, Any]] = {
            universal_id: universal_config,
            protocol_id: {
                "player_id": protocol_id,
                "provider": "squeezelite",
                "player_type": "player",
                "enabled": True,
                "values": {"protocol_parent_id": universal_id},
            },
        }

        def _config_get(key: str, default: object = None) -> object:
            if key == CONF_PLAYERS:
                return configs
            if key.startswith(f"{CONF_PLAYERS}/"):
                pid = key.split("/", 1)[1]
                return configs.get(pid, default)
            return default

        mock_mass.config.get.side_effect = _config_get
        mock_mass.config.remove_player_config = AsyncMock()
        mock_mass.config.get_base_player_config.return_value = create_mock_config("WC-Player")
        mock_mass.players = MagicMock()
        mock_mass.players.get_player = MagicMock(return_value=None)
        mock_mass.players.delete_player_config = MagicMock()
        mock_mass.players.unregister = AsyncMock()
        mock_mass.players.register_or_update = AsyncMock()

        await provider._restore_player(universal_id)

        mock_mass.config.remove_player_config.assert_not_called()
        mock_mass.players.delete_player_config.assert_not_called()
        mock_mass.players.unregister.assert_not_called()
        mock_mass.players.register_or_update.assert_awaited_once()
        registered = mock_mass.players.register_or_update.call_args.args[0]
        assert protocol_id in registered._protocol_player_ids
        assert configs[universal_id]["values"]["hide_in_ui"] is True
        assert configs[universal_id]["values"]["announce_volume_min"] == 55

    @pytest.mark.asyncio
    async def test_membership_augmented_from_child_parent_link(self, mock_mass: MagicMock) -> None:
        """A child whose parent link points at the universal is re-added to membership."""
        provider = create_mock_universal_provider(mock_mass)
        universal_id = "up_test"
        stored_id = "spb_stored"
        missing_id = "spb_missing"

        self._setup_config_get(
            mock_mass,
            universal_id,
            [stored_id],
            {
                stored_id: {
                    "player_type": "protocol",
                    "values": {"protocol_parent_id": universal_id},
                },
                missing_id: {
                    "player_type": "protocol",
                    "values": {"protocol_parent_id": universal_id},
                },
            },
        )
        mock_mass.players = MagicMock()
        mock_mass.players.get_player = MagicMock(return_value=None)
        mock_mass.players.register_or_update = AsyncMock()
        mock_mass.config.remove_player_config = AsyncMock()
        mock_mass.config.get_base_player_config.return_value = create_mock_config("Test Universal")

        await provider._restore_player(universal_id)

        # The missing child is restored to membership and persisted
        mock_mass.config.set.assert_any_call(
            f"{CONF_PLAYERS}/{universal_id}/values/linked_protocol_ids",
            [stored_id, missing_id],
        )
        registered = mock_mass.players.register_or_update.call_args.args[0]
        assert registered._protocol_player_ids == [stored_id, missing_id]

    @pytest.mark.asyncio
    async def test_universal_config_kept_when_protocol_moved_to_other_parent(
        self, mock_mass: MagicMock
    ) -> None:
        """A protocol that moved to another parent is dropped, universal config is kept."""
        provider = create_mock_universal_provider(mock_mass)
        universal_id = "up_test"
        protocol_id = "spb_moved"

        self._setup_config_get(
            mock_mass,
            universal_id,
            [protocol_id],
            {
                protocol_id: {
                    "player_type": "protocol",
                    "values": {"protocol_parent_id": "up_other"},
                },
            },
        )
        mock_mass.players = MagicMock()
        mock_mass.players.get_player = MagicMock(return_value=None)
        mock_mass.players.register_or_update = AsyncMock()
        mock_mass.config.remove_player_config = AsyncMock()

        await provider._restore_player(universal_id)

        mock_mass.config.remove_player_config.assert_not_called()
        mock_mass.players.register_or_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_disabled_parent_reparents_and_disables_orphaned_protocols(
        self, mock_mass: MagicMock
    ) -> None:
        """
        Self-heal: stale UP whose protocols belong to a disabled native parent.

        Restoring such a UP should skip the wrapper, restore each protocol's
        parent_id back to the rightful (disabled) native parent, cascade-disable
        each protocol so the next registration cycle doesn't rebuild the wrapper,
        and hand the wrapper's config over to the native parent before deleting it.
        """
        provider = create_mock_universal_provider(mock_mass)
        universal_id = "up_test"
        native_parent_id = "sonos_disabled"
        ap_id = "airplay_1"
        dlna_id = "dlna_1"

        all_configs = {
            universal_id: {
                "values": {
                    "linked_protocol_ids": [ap_id, dlna_id],
                    "device_identifiers": {},
                    "device_info": {},
                },
                "name": "Test UP",
            },
            native_parent_id: {
                "enabled": False,
                "provider": "sonos",
                "values": {"linked_protocol_ids": [ap_id, dlna_id]},
            },
            ap_id: {
                "player_type": "protocol",
                "enabled": True,
                "values": {"protocol_parent_id": universal_id},
            },
            dlna_id: {
                "player_type": "protocol",
                "enabled": True,
                "values": {"protocol_parent_id": universal_id},
            },
        }

        def _config_get(key: str, default: object = None) -> object:
            if key == CONF_PLAYERS:
                return all_configs
            if key.startswith(f"{CONF_PLAYERS}/"):
                pid = key.split("/", 1)[1]
                return all_configs.get(pid, default)
            return default

        mock_mass.config.get.side_effect = _config_get
        mock_mass.config.set = MagicMock()
        mock_mass.config.save_player_config = AsyncMock()
        mock_mass.config.remove_player_config = AsyncMock()
        mock_mass.players = MagicMock()
        mock_mass.players.get_player = MagicMock(return_value=None)

        await provider._restore_player(universal_id)

        # The wrapper is not restored; its settings are handed over to the
        # native parent and its now-obsolete config is deleted
        mock_mass.players._migrate_universal_player_config.assert_called_once_with(
            universal_id, native_parent_id
        )
        mock_mass.players._repoint_group_memberships.assert_called_once_with(
            universal_id, native_parent_id
        )
        mock_mass.players.delete_player_config.assert_called_once_with(universal_id)

        # Each protocol's parent_id is restored to the disabled native parent
        parent_restorations = {
            call.args[0]: call.args[1]
            for call in mock_mass.config.set.call_args_list
            if "protocol_parent_id" in call.args[0]
        }
        assert parent_restorations == {
            f"{CONF_PLAYERS}/{ap_id}/values/protocol_parent_id": native_parent_id,
            f"{CONF_PLAYERS}/{dlna_id}/values/protocol_parent_id": native_parent_id,
        }

        # Each protocol is cascade-disabled
        disabled_ids = {
            call.args[0] for call in mock_mass.config.save_player_config.await_args_list
        }
        assert disabled_ids == {ap_id, dlna_id}

    @pytest.mark.asyncio
    async def test_protocols_reparented_to_their_own_native_claimer(
        self, mock_mass: MagicMock
    ) -> None:
        """Each protocol is reparented to the native player that claims it, not the first found."""
        provider = create_mock_universal_provider(mock_mass)
        universal_id = "up_test"
        ap_id = "airplay_1"
        slimproto_id = "slimproto_1"

        all_configs = {
            universal_id: {
                "values": {
                    "linked_protocol_ids": [ap_id, slimproto_id],
                    "device_identifiers": {},
                    "device_info": {},
                },
                "name": "Test UP",
            },
            "native_a": {
                "provider": "dlna",
                "values": {"linked_protocol_ids": [ap_id]},
            },
            "native_b": {
                "provider": "squeezelite",
                "values": {"linked_protocol_ids": [slimproto_id]},
            },
            ap_id: {
                "player_type": "protocol",
                "values": {"protocol_parent_id": universal_id},
            },
            slimproto_id: {
                "player_type": "protocol",
                "values": {"protocol_parent_id": universal_id},
            },
        }

        def _config_get(key: str, default: object = None) -> object:
            if key == CONF_PLAYERS:
                return all_configs
            if key.startswith(f"{CONF_PLAYERS}/"):
                pid = key.split("/", 1)[1]
                return all_configs.get(pid, default)
            return default

        mock_mass.config.get.side_effect = _config_get
        mock_mass.config.set = MagicMock()
        mock_mass.config.save_player_config = AsyncMock()
        mock_mass.config.remove_player_config = AsyncMock()
        mock_mass.players = MagicMock()
        mock_mass.players.get_player = MagicMock(return_value=None)
        mock_mass.players.register_or_update = AsyncMock()

        await provider._restore_player(universal_id)

        mock_mass.players.register_or_update.assert_not_called()
        parent_restorations = {
            call.args[0]: call.args[1]
            for call in mock_mass.config.set.call_args_list
            if "protocol_parent_id" in call.args[0]
        }
        assert parent_restorations == {
            f"{CONF_PLAYERS}/{ap_id}/values/protocol_parent_id": "native_a",
            f"{CONF_PLAYERS}/{slimproto_id}/values/protocol_parent_id": "native_b",
        }
        for call in mock_mass.config.save_player_config.await_args_list:
            assert call.args[1] == {ATTR_ENABLED: False}
        # The wrapper's settings are handed to the first claimer, then its
        # config is deleted
        mock_mass.players._migrate_universal_player_config.assert_called_once_with(
            universal_id, "native_a"
        )
        mock_mass.players._repoint_group_memberships.assert_called_once_with(
            universal_id, "native_a"
        )
        mock_mass.players.delete_player_config.assert_called_once_with(universal_id)

    @pytest.mark.asyncio
    async def test_restore_native_claims_carries_config_and_deletes_wrapper(
        self, mock_mass: MagicMock
    ) -> None:
        """The wrapper's user settings move to the native claimer and its config is deleted."""
        universal_id = "up_old"
        native_id = "cast_1"
        ap_id = "ap_1"
        store: dict[str, Any] = {
            CONF_PLAYERS: {
                universal_id: {
                    "provider": "universal_player",
                    "enabled": True,
                    "name": "Living Room",
                    "default_name": "Soundbar (Universal)",
                    "values": {
                        "hide_in_ui": True,
                        "volume_control": "up_volume_control",
                        "linked_protocol_ids": [ap_id],
                        "device_identifiers": {"mac_address": "AA:BB:CC:DD:EE:FF"},
                        "device_info": {"model": "Test", "manufacturer": "Test"},
                    },
                },
                native_id: {
                    "provider": "cast",
                    "enabled": True,
                    "name": "Soundbar",
                    "default_name": "Soundbar",
                    "values": {
                        "volume_control": "native",
                        "linked_protocol_ids": [ap_id],
                    },
                },
                ap_id: {
                    "player_type": "protocol",
                    "enabled": True,
                    "values": {"protocol_parent_id": universal_id},
                },
                "group_1": {
                    "provider": "sync_group",
                    "enabled": True,
                    "values": {
                        "group_members": [universal_id, "other"],
                        "allowed_members": [universal_id],
                    },
                },
            },
            CONF_PLAYER_DSP: {universal_id: {"enabled": True, "filters": []}},
            CONF_PLAYER_QUEUES: {
                universal_id: {"queue_id": universal_id, "values": {"crossfade_duration": 5}}
            },
        }
        self._wire_nested_config(mock_mass, store)
        mock_mass.config.save_player_config = AsyncMock()
        scheduled_tasks: list[Awaitable[object]] = []
        mock_mass.create_task = MagicMock(
            side_effect=lambda task, *_a, **_kw: scheduled_tasks.append(task)
        )
        controller = PlayerController(mock_mass)
        controller._players = {}
        mock_mass.players = controller
        mock_mass.player_queues = MagicMock()
        provider = create_mock_universal_provider(mock_mass)

        await provider._restore_player(universal_id)

        players_tree = store[CONF_PLAYERS]
        # the protocol player is reparented to the native claimer
        assert players_tree[ap_id]["values"]["protocol_parent_id"] == native_id
        # the custom display name and user-set values moved onto the native player
        assert players_tree[native_id]["name"] == "Living Room"
        assert players_tree[native_id]["values"]["hide_in_ui"] is True
        # values the native player has explicitly set are not overwritten
        assert players_tree[native_id]["values"]["volume_control"] == "native"
        # wrapper bookkeeping values are not carried over
        assert "device_identifiers" not in players_tree[native_id]["values"]
        assert "device_info" not in players_tree[native_id]["values"]
        # DSP and queue settings moved onto the native player
        assert store[CONF_PLAYER_DSP] == {native_id: {"enabled": True, "filters": []}}
        assert store[CONF_PLAYER_QUEUES] == {
            native_id: {"queue_id": native_id, "values": {"crossfade_duration": 5}}
        }
        # group memberships that referenced the wrapper now point at the native player
        assert players_tree["group_1"]["values"]["group_members"] == [native_id, "other"]
        assert players_tree["group_1"]["values"]["allowed_members"] == [native_id]
        # the wrapper's own config entries are all gone
        assert universal_id not in players_tree
        # cached queue state of the wrapper is purged as well
        mock_mass.player_queues.purge_saved_queue.assert_called_once_with(universal_id)
        # the wrapper itself is not registered as a player
        assert universal_id not in controller._players
        for task in scheduled_tasks:
            await task

    @pytest.mark.asyncio
    async def test_restore_native_claims_keeps_native_name_dsp_and_queue_values(
        self, mock_mass: MagicMock
    ) -> None:
        """Values explicitly set on the native player win over the wrapper's on restore."""
        universal_id = "up_old"
        native_id = "cast_1"
        ap_id = "ap_1"
        store: dict[str, Any] = {
            CONF_PLAYERS: {
                universal_id: {
                    "provider": "universal_player",
                    "enabled": True,
                    "name": "Living Room",
                    "default_name": "Soundbar (Universal)",
                    "values": {"hide_in_ui": True, "linked_protocol_ids": [ap_id]},
                },
                native_id: {
                    "provider": "cast",
                    "enabled": True,
                    "name": "Kitchen",
                    "default_name": "Soundbar",
                    "values": {"linked_protocol_ids": [ap_id]},
                },
                ap_id: {
                    "player_type": "protocol",
                    "enabled": True,
                    "values": {"protocol_parent_id": universal_id},
                },
            },
            CONF_PLAYER_DSP: {
                universal_id: {"enabled": True, "filters": ["universal"]},
                native_id: {"enabled": False, "filters": ["native"]},
            },
            CONF_PLAYER_QUEUES: {
                universal_id: {
                    "queue_id": universal_id,
                    "values": {"crossfade_duration": 8, "autoplay_mode": "smart"},
                },
                native_id: {"queue_id": native_id, "values": {"crossfade_duration": 2}},
            },
        }
        self._wire_nested_config(mock_mass, store)
        mock_mass.config.save_player_config = AsyncMock()
        scheduled_tasks: list[Awaitable[object]] = []
        mock_mass.create_task = MagicMock(
            side_effect=lambda task, *_a, **_kw: scheduled_tasks.append(task)
        )
        controller = PlayerController(mock_mass)
        controller._players = {}
        mock_mass.players = controller
        mock_mass.player_queues = MagicMock()
        provider = create_mock_universal_provider(mock_mass)

        await provider._restore_player(universal_id)

        players_tree = store[CONF_PLAYERS]
        # the native player's own custom name and DSP config win
        assert players_tree[native_id]["name"] == "Kitchen"
        assert store[CONF_PLAYER_DSP] == {native_id: {"enabled": False, "filters": ["native"]}}
        # queue values merge without overwriting the native queue's own values
        assert store[CONF_PLAYER_QUEUES] == {
            native_id: {
                "queue_id": native_id,
                "values": {"crossfade_duration": 2, "autoplay_mode": "smart"},
            }
        }
        # non-conflicting values still migrate and the wrapper config is gone
        assert players_tree[native_id]["values"]["hide_in_ui"] is True
        assert universal_id not in players_tree
        for task in scheduled_tasks:
            await task

    @pytest.mark.asyncio
    async def test_restore_native_claims_keeps_config_while_wrapper_registered(
        self, mock_mass: MagicMock
    ) -> None:
        """No config carry-over/deletion while the wrapper is still registered."""
        provider = create_mock_universal_provider(mock_mass)
        universal_id = "up_test"
        ap_id = "airplay_1"

        all_configs = {
            universal_id: {
                "values": {
                    "linked_protocol_ids": [ap_id],
                    "device_identifiers": {},
                    "device_info": {},
                },
                "name": "Test UP",
            },
            "native_a": {
                "provider": "dlna",
                "values": {"linked_protocol_ids": [ap_id]},
            },
            ap_id: {
                "player_type": "protocol",
                "values": {"protocol_parent_id": universal_id},
            },
        }

        def _config_get(key: str, default: object = None) -> object:
            if key == CONF_PLAYERS:
                return all_configs
            if key.startswith(f"{CONF_PLAYERS}/"):
                pid = key.split("/", 1)[1]
                return all_configs.get(pid, default)
            return default

        mock_mass.config.get.side_effect = _config_get
        mock_mass.config.set = MagicMock()
        mock_mass.config.save_player_config = AsyncMock()
        mock_mass.players = MagicMock()
        mock_mass.players.get_player = MagicMock(return_value=MagicMock())

        await provider._restore_player(universal_id)

        # the protocol link repair still runs
        parent_restorations = {
            call.args[0]: call.args[1]
            for call in mock_mass.config.set.call_args_list
            if "protocol_parent_id" in call.args[0]
        }
        assert parent_restorations == {
            f"{CONF_PLAYERS}/{ap_id}/values/protocol_parent_id": "native_a",
        }
        # but the registered wrapper keeps its config and group memberships
        mock_mass.players._migrate_universal_player_config.assert_not_called()
        mock_mass.players._repoint_group_memberships.assert_not_called()
        mock_mass.players.delete_player_config.assert_not_called()
        mock_mass.player_queues.on_player_remove.assert_not_called()


class TestParentDisableCascade:
    """Tests for cascading enable/disable from a native parent to its protocols."""

    @staticmethod
    def _make_config(player_id: str, *, enabled: bool, provider: str = "sonos") -> MagicMock:
        """Build a minimal PlayerConfig mock for on_player_config_change."""
        config = MagicMock()
        config.player_id = player_id
        config.provider = provider
        config.enabled = enabled
        config.values = {}
        return config

    @pytest.mark.asyncio
    async def test_disabling_native_parent_cascades_disable_to_linked_protocols(
        self, mock_mass: MagicMock
    ) -> None:
        """Disabling a native parent saves enabled=False for each linked protocol."""
        controller = PlayerController(mock_mass)

        native_provider = MockProvider("sonos", mass=mock_mass)
        native_player = MockPlayer(
            native_provider,
            "sonos_native",
            "Native Speaker",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        # Bypass the power-off branch in on_player_config_change
        native_player._attr_available = False
        native_player.set_linked_output_protocols(
            [
                LinkedOutputProtocol(
                    output_protocol_id="ap_1",
                    protocol_domain="airplay",
                    priority=10,
                ),
                LinkedOutputProtocol(
                    output_protocol_id="dlna_1",
                    protocol_domain="dlna",
                    priority=20,
                ),
            ]
        )
        controller._players = {native_player.player_id: native_player}

        protocol_configs = {
            "ap_1": {"enabled": True, "player_type": "protocol"},
            "dlna_1": {"enabled": True, "player_type": "protocol"},
        }

        def _config_get(key: str, default: object = None) -> object:
            if key.startswith(f"{CONF_PLAYERS}/"):
                pid = key.split("/", 1)[1]
                return protocol_configs.get(pid, default)
            return default

        mock_mass.config.get = MagicMock(side_effect=_config_get)
        mock_mass.config.save_player_config = MagicMock()
        mock_mass.create_task = MagicMock()
        mock_mass.get_provider = MagicMock(return_value=MagicMock(spec=PlayerProvider))

        config = self._make_config(native_player.player_id, enabled=False)

        await controller.on_player_config_change(config, {ATTR_ENABLED})

        called = mock_mass.config.save_player_config.call_args_list
        assert {call.args[0] for call in called} == {"ap_1", "dlna_1"}
        for call in called:
            assert call.args[1] == {ATTR_ENABLED: False}

    @pytest.mark.asyncio
    async def test_enabling_native_parent_cascades_enable_to_linked_protocols(
        self, mock_mass: MagicMock
    ) -> None:
        """Re-enabling a native parent saves enabled=True for each (currently disabled) protocol."""
        controller = PlayerController(mock_mass)

        native_provider = MockProvider("sonos", mass=mock_mass)
        native_player = MockPlayer(
            native_provider,
            "sonos_native",
            "Native Speaker",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        native_player._attr_available = False
        native_player.set_linked_output_protocols(
            [
                LinkedOutputProtocol(
                    output_protocol_id="ap_1",
                    protocol_domain="airplay",
                    priority=10,
                ),
            ]
        )
        controller._players = {native_player.player_id: native_player}

        protocol_configs = {
            "ap_1": {"enabled": False, "player_type": "protocol"},
        }

        def _config_get(key: str, default: object = None) -> object:
            if key.startswith(f"{CONF_PLAYERS}/"):
                pid = key.split("/", 1)[1]
                return protocol_configs.get(pid, default)
            return default

        mock_mass.config.get = MagicMock(side_effect=_config_get)
        mock_mass.config.save_player_config = MagicMock()
        mock_mass.create_task = MagicMock()
        mock_mass.get_provider = MagicMock(return_value=MagicMock(spec=PlayerProvider))

        config = self._make_config(native_player.player_id, enabled=True)

        await controller.on_player_config_change(config, {ATTR_ENABLED})

        mock_mass.config.save_player_config.assert_called_once_with("ap_1", {ATTR_ENABLED: True})

    @pytest.mark.asyncio
    async def test_disabling_native_parent_falls_back_to_cached_protocol_ids(
        self, mock_mass: MagicMock
    ) -> None:
        """When the parent isn't registered, cascade uses cached linked_protocol_ids."""
        controller = PlayerController(mock_mass)

        # Parent is not in _players (e.g. provider didn't register a disabled parent)
        controller._players = {}

        cached_protocol_ids = ["ap_cached", "dlna_cached"]
        protocol_configs = {
            "ap_cached": {"enabled": True, "player_type": "protocol"},
            "dlna_cached": {"enabled": True, "player_type": "protocol"},
        }

        def _config_get(key: str, default: object = None) -> object:
            if key == f"{CONF_PLAYERS}/unregistered_native/values/linked_protocol_ids":
                return list(cached_protocol_ids)
            if key.startswith(f"{CONF_PLAYERS}/"):
                pid = key.split("/", 1)[1]
                return protocol_configs.get(pid, default)
            return default

        mock_mass.config.get = MagicMock(side_effect=_config_get)
        mock_mass.config.save_player_config = MagicMock()
        mock_mass.create_task = MagicMock()
        mock_mass.get_provider = MagicMock(return_value=MagicMock(spec=PlayerProvider))

        config = self._make_config("unregistered_native", enabled=False)

        await controller.on_player_config_change(config, {ATTR_ENABLED})

        called_ids = {call.args[0] for call in mock_mass.config.save_player_config.call_args_list}
        assert called_ids == set(cached_protocol_ids)

    @pytest.mark.asyncio
    async def test_toggling_protocol_player_does_not_cascade(self, mock_mass: MagicMock) -> None:
        """A PROTOCOL-type player flipping enabled must not trigger any cascade."""
        controller = PlayerController(mock_mass)

        ap_provider = MockProvider("airplay", mass=mock_mass)
        ap_player = MockPlayer(
            ap_provider,
            "ap_1",
            "AirPlay",
            player_type=PlayerType.PROTOCOL,
        )
        ap_player._attr_available = False
        controller._players = {ap_player.player_id: ap_player}

        mock_mass.config.save_player_config = MagicMock()
        mock_mass.create_task = MagicMock()
        mock_mass.get_provider = MagicMock(return_value=MagicMock(spec=PlayerProvider))

        config = self._make_config(ap_player.player_id, enabled=False, provider="airplay")

        await controller.on_player_config_change(config, {ATTR_ENABLED})

        mock_mass.config.save_player_config.assert_not_called()


class TestDelayedEvalDisabledParentGuard:
    """Tests for the guard in _delayed_protocol_evaluation against disabled parents."""

    @pytest.mark.asyncio
    async def test_skips_universal_creation_when_cached_parent_is_disabled(
        self, mock_mass: MagicMock
    ) -> None:
        """No universal player is created when the protocol's cached parent is disabled."""
        controller = PlayerController(mock_mass)

        dlna_provider = MockProvider("dlna", mass=mock_mass)
        protocol_player = MockPlayer(
            dlna_provider,
            "dlna_orphan",
            "Orphan DLNA",
            player_type=PlayerType.PROTOCOL,
        )
        protocol_player.set_initialized()
        controller._players = {protocol_player.player_id: protocol_player}

        cached_parent_id = "sonos_disabled"

        def _config_get(key: str, default: object = None) -> object:
            if key.endswith("/protocol_parent_id"):
                return cached_parent_id
            if key == f"{CONF_PLAYERS}/{cached_parent_id}":
                return {"enabled": False, "provider": "sonos"}
            return default

        mock_mass.config.get = MagicMock(side_effect=_config_get)

        with (
            patch.object(controller, "_try_link_to_existing_player", return_value=False),
            patch.object(controller, "_find_matching_universal_player", return_value=None),
            patch.object(controller, "_create_or_update_universal_player") as mock_create_up,
        ):
            await controller._delayed_protocol_evaluation(protocol_player.player_id)

        mock_create_up.assert_not_called()

    @pytest.mark.asyncio
    async def test_creates_universal_when_cached_parent_is_enabled_but_offline(
        self, mock_mass: MagicMock
    ) -> None:
        """Sanity check: an enabled-but-unregistered cached parent still falls through."""
        controller = PlayerController(mock_mass)

        dlna_provider = MockProvider("dlna", mass=mock_mass)
        protocol_player = MockPlayer(
            dlna_provider,
            "dlna_orphan",
            "Orphan DLNA",
            player_type=PlayerType.PROTOCOL,
        )
        protocol_player.set_initialized()
        controller._players = {protocol_player.player_id: protocol_player}

        cached_parent_id = "sonos_offline"

        def _config_get(key: str, default: object = None) -> object:
            if key.endswith("/protocol_parent_id"):
                return cached_parent_id
            if key == f"{CONF_PLAYERS}/{cached_parent_id}":
                return {"enabled": True, "provider": "sonos"}
            return default

        mock_mass.config.get = MagicMock(side_effect=_config_get)

        with (
            patch.object(controller, "_try_link_to_existing_player", return_value=False),
            patch.object(controller, "_find_matching_universal_player", return_value=None),
            patch.object(controller, "_create_or_update_universal_player") as mock_create_up,
            patch.object(
                controller,
                "_find_matching_protocol_players",
                return_value=[protocol_player],
            ),
        ):
            await controller._delayed_protocol_evaluation(protocol_player.player_id)

        mock_create_up.assert_called_once()


class TestUniversalPlayerCurrentMedia:
    """Tests for current_media resolution while an output protocol is active."""

    def test_current_media_surfaces_active_output_protocol(self, mock_mass: MagicMock) -> None:
        """
        While outputting via a protocol player, surface that player's raw current_media.

        A sync group mirrors its leader's raw current_media to resolve the active queue
        item. When the leader is a universal player outputting via AirPlay, returning None
        here strands the group with a frozen current item (regression guard for grouped
        AirPlay live-metadata never refreshing).
        """
        protocol_provider = MockProvider("airplay", mass=mock_mass)
        protocol_player = MockPlayer(
            protocol_provider, "ap_1", "AirPlay", player_type=PlayerType.PROTOCOL
        )
        media = PlayerMedia(
            uri="http://x/flow/sess/queue_1/item_1/ap_1.flac", queue_item_id="item_1"
        )
        protocol_player._attr_current_media = media

        universal = _create_universal_player(mock_mass, "up_1", "Universal", ["ap_1"])
        players = {"up_1": universal, "ap_1": protocol_player}
        mock_mass.players.get_player = MagicMock(side_effect=lambda pid: players.get(pid))

        # without an active output protocol there is nothing to surface
        assert universal.current_media is None

        universal.set_active_output_protocol("ap_1")
        assert universal.current_media is media

    def test_current_media_none_for_native_output(self, mock_mass: MagicMock) -> None:
        """A native output protocol has no separate protocol player to surface."""
        universal = _create_universal_player(mock_mass, "up_1", "Universal", [])
        universal.set_active_output_protocol("native")
        assert universal.current_media is None


class TestSelfReferentialProtocolLinks:
    """
    Tests for the self-link bug when a Sendspin device changes player type.

    A Sendspin device keeps a stable client_id (the MA player_id) across reflashes.
    When it first registers as a protocol player it gets wrapped in a universal
    player that caches that id as one of its protocols. If the device later
    reconnects as a non-protocol player (e.g. its roles changed to display-only)
    with the same id, the replace logic could link it to itself, hiding it.
    """

    def test_add_protocol_link_refuses_self(self, mock_mass: MagicMock) -> None:
        """A player must never become its own protocol parent."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("sendspin", mass=mock_mass)
        player = MockPlayer(provider, "esp_client", "ESP", player_type=PlayerType.PROTOCOL)
        mock_mass.players = controller
        controller._players = {"esp_client": player}

        controller._add_protocol_link(player, player, "sendspin")

        assert player.protocol_parent_id is None
        assert all(
            link.output_protocol_id != "esp_client" for link in player.linked_output_protocols
        )

    def test_type_change_replaces_universal_without_self_link(self, mock_mass: MagicMock) -> None:
        """Replacing a universal player with the same-id native player must not self-link."""
        universal = _create_universal_player(
            mock_mass, "upespclient", "ESP", protocol_player_ids=["esp_client"]
        )
        display_provider = MockProvider("sendspin", mass=mock_mass)
        display_player = MockPlayer(
            display_provider, "esp_client", "ESP", player_type=PlayerType.DISPLAY
        )

        store: dict[str, Any] = {"players/esp_client": {"enabled": True}}
        mock_mass.config.get.side_effect = lambda key, default=None: store.get(key, default)
        mock_mass.config.set.side_effect = lambda key, value: store.__setitem__(key, value)
        mock_mass.create_task = MagicMock()
        mock_mass.players = controller = PlayerController(mock_mass)
        controller._players = {"upespclient": universal, "esp_client": display_player}

        controller._check_replace_universal_player(display_player)

        assert display_player.protocol_parent_id is None
        assert store.get("players/esp_client/values/protocol_parent_id") != "esp_client"
        assert "esp_client" not in store.get("players/esp_client/values/linked_protocol_ids", [])
        # the obsolete universal player must not keep listing the native player,
        # or its permanent cleanup would re-evaluate it as an orphaned protocol
        assert "esp_client" not in universal._protocol_player_ids

    async def test_migrate_clears_persisted_self_referential_link(self) -> None:
        """Config migration scrubs a self-referential link left by an older version."""
        config = ConfigController.__new__(ConfigController)
        config._data = {
            CONF_PLAYERS: {
                "esp_client": {
                    "provider": "sendspin",
                    "player_id": "esp_client",
                    "values": {
                        "protocol_parent_id": "esp_client",
                        "linked_protocol_ids": ["esp_client"],
                    },
                }
            }
        }
        await migrate(config._data)

        values = config._data[CONF_PLAYERS]["esp_client"]["values"]
        assert values["protocol_parent_id"] is None
        assert "esp_client" not in values["linked_protocol_ids"]


class TestDerivedProtocolLinking:
    """
    Tests for derived protocol players (players with underlying_player_id set).

    Derived protocol players (e.g. Sendspin bridges riding on an AirPlay player)
    resolve their parent deterministically via the underlying player instead of
    device identifier matching, and never seed universal players themselves.
    """

    @staticmethod
    def _make_derived_player(
        mock_mass: MagicMock, player_id: str, underlying_player_id: str
    ) -> MockPlayer:
        """Create a derived protocol player without any device identifiers."""
        provider = MockProvider("sendspin", mass=mock_mass)
        player = MockPlayer(
            provider,
            player_id,
            f"Bridge {player_id}",
            player_type=PlayerType.PROTOCOL,
        )
        player._attr_underlying_player_id = underlying_player_id
        player.set_initialized()
        return player

    def test_derived_links_to_parent_of_linked_underlying(self, mock_mass: MagicMock) -> None:
        """Test a derived player joins the parent of its (linked) underlying player."""
        controller = PlayerController(mock_mass)
        mock_mass.config.get = MagicMock(return_value=[])

        native = MockPlayer(
            MockProvider("heos", mass=mock_mass), "native_1", "AVR", player_type=PlayerType.PLAYER
        )
        native.set_initialized()
        ap_player = MockPlayer(
            MockProvider("airplay", mass=mock_mass),
            "ap_1",
            "AVR AirPlay",
            player_type=PlayerType.PROTOCOL,
        )
        ap_player.set_initialized()
        controller._players = {"native_1": native, "ap_1": ap_player}
        controller._add_protocol_link(native, ap_player, "airplay")

        # The derived player has NO identifiers - the edge alone must resolve it
        derived = self._make_derived_player(mock_mass, "spb_1", "ap_1")
        controller._players["spb_1"] = derived
        controller._try_link_protocol_to_native(derived)

        assert derived.protocol_parent_id == "native_1"
        # the derived link carries its base output, the non-derived link does not
        links = {link.output_protocol_id: link for link in native.linked_output_protocols}
        assert links["spb_1"].derived_from == "ap_1"
        assert links["ap_1"].derived_from is None

    def test_waiting_derived_follows_when_underlying_links(self, mock_mass: MagicMock) -> None:
        """Test a derived player that registered early follows once its underlying links."""
        controller = PlayerController(mock_mass)
        mock_mass.config.get = MagicMock(return_value=[])

        native = MockPlayer(
            MockProvider("heos", mass=mock_mass), "native_1", "AVR", player_type=PlayerType.PLAYER
        )
        native.set_initialized()
        ap_player = MockPlayer(
            MockProvider("airplay", mass=mock_mass),
            "ap_1",
            "AVR AirPlay",
            player_type=PlayerType.PROTOCOL,
        )
        ap_player.set_initialized()
        derived = self._make_derived_player(mock_mass, "spb_1", "ap_1")
        controller._players = {"native_1": native, "ap_1": ap_player, "spb_1": derived}

        # Underlying is not linked yet: derived stays unlinked
        controller._try_link_protocol_to_native(derived)
        assert derived.protocol_parent_id is None

        # Linking the underlying player propagates to the waiting derived player
        controller._add_protocol_link(native, ap_player, "airplay")
        assert derived.protocol_parent_id == "native_1"

    def test_derived_links_directly_to_non_protocol_underlying(self, mock_mass: MagicMock) -> None:
        """Test a derived player parents to the underlying player itself when not a protocol."""
        controller = PlayerController(mock_mass)
        mock_mass.config.get = MagicMock(side_effect=lambda _key, default=None: default)

        local_player = MockPlayer(
            MockProvider("local_audio", mass=mock_mass),
            "local_1",
            "Dummy Output",
            player_type=PlayerType.PLAYER,
        )
        local_player.set_initialized()
        derived = self._make_derived_player(mock_mass, "spb_1", "local_1")
        controller._players = {"spb_1": derived, "local_1": local_player}

        # The final derived pass of _try_link_protocols_to_native picks it up
        controller._try_link_protocols_to_native(local_player)

        assert derived.protocol_parent_id == "local_1"
        # riding on the parent player itself normalizes the base output to "native"
        links = {link.output_protocol_id: link for link in local_player.linked_output_protocols}
        assert links["spb_1"].derived_from == "native"

    @pytest.mark.asyncio
    async def test_derived_delayed_eval_never_creates_universal_player(
        self, mock_mass: MagicMock
    ) -> None:
        """Test delayed evaluation of a derived player never wraps it in a universal player."""
        controller, _ = TestEndToEndDuplicateProtocol._setup_e2e_controller(mock_mass)
        mock_mass.config.get = MagicMock(return_value=[])

        derived = self._make_derived_player(mock_mass, "spb_1", "ap_missing")
        controller._players = {"spb_1": derived}

        await controller._delayed_protocol_evaluation("spb_1")

        assert derived.protocol_parent_id is None
        assert not any(pid.startswith("up") for pid in controller._players)

    def test_derived_link_refused_on_domain_duplicate(self, mock_mass: MagicMock) -> None:
        """Test a derived player stays unlinked when the parent already has its domain."""
        controller = PlayerController(mock_mass)
        mock_mass.config.get = MagicMock(return_value=[])

        native = MockPlayer(
            MockProvider("heos", mass=mock_mass), "native_1", "AVR", player_type=PlayerType.PLAYER
        )
        native.set_initialized()
        ap_player = MockPlayer(
            MockProvider("airplay", mass=mock_mass),
            "ap_1",
            "AVR AirPlay",
            player_type=PlayerType.PROTOCOL,
        )
        ap_player.set_initialized()
        other_sendspin = MockPlayer(
            MockProvider("sendspin", mass=mock_mass),
            "sendspin_native_client",
            "AVR Sendspin",
            player_type=PlayerType.PROTOCOL,
        )
        other_sendspin.set_initialized()
        controller._players = {
            "native_1": native,
            "ap_1": ap_player,
            "sendspin_native_client": other_sendspin,
        }
        controller._add_protocol_link(native, ap_player, "airplay")
        controller._add_protocol_link(native, other_sendspin, "sendspin")

        derived = self._make_derived_player(mock_mass, "spb_1", "ap_1")
        controller._players["spb_1"] = derived
        controller._try_link_protocol_to_native(derived)

        assert derived.protocol_parent_id is None

    def test_derived_joins_universal_parent(self, mock_mass: MagicMock) -> None:
        """Test a derived player joins the universal player of its underlying player."""
        controller = PlayerController(mock_mass)
        mock_mass.config.get = MagicMock(return_value=[])

        def _close_coro(coro: Any, *_args: Any, **_kwargs: Any) -> None:
            if hasattr(coro, "close"):
                coro.close()

        mock_mass.create_task = MagicMock(side_effect=_close_coro)

        universal = _create_universal_player(
            mock_mass,
            "up_aabbccddeeff",
            "Living Room Speaker",
            protocol_player_ids=["ap_1"],
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        ap_player = MockPlayer(
            MockProvider("airplay", mass=mock_mass),
            "ap_1",
            "AirPlay Speaker",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        ap_player.set_initialized()
        controller._players = {"up_aabbccddeeff": universal, "ap_1": ap_player}
        controller._add_protocol_link(universal, ap_player, "airplay")

        derived = self._make_derived_player(mock_mass, "spb_1", "ap_1")
        controller._players["spb_1"] = derived
        controller._try_link_protocol_to_native(derived)

        assert derived.protocol_parent_id == "up_aabbccddeeff"
        assert "spb_1" in universal._protocol_player_ids

    def test_save_underlying_player_id_persists_and_clears(self, mock_mass: MagicMock) -> None:
        """Test the derived edge is persisted, cleared when revoked and skipped otherwise."""
        controller = PlayerController(mock_mass)
        derived = self._make_derived_player(mock_mass, "spb_1", "ap_1")
        conf_key = "players/spb_1/values/underlying_player_id"
        stored: dict[str, Any] = {"players/spb_1": {"values": {}}}
        mock_mass.config.get = MagicMock(
            side_effect=lambda key, default=None: stored.get(key, default)
        )
        mock_mass.config.set = MagicMock(side_effect=stored.__setitem__)

        # derived player: edge gets persisted
        controller._save_underlying_player_id(derived)
        assert stored[conf_key] == "ap_1"

        # edge revoked (e.g. bridge client turned web player): stale value is cleared
        derived._attr_underlying_player_id = None
        controller._save_underlying_player_id(derived)
        assert stored[conf_key] is None

        # nothing persisted and no edge: no write at all
        mock_mass.config.set.reset_mock()
        del stored[conf_key]
        controller._save_underlying_player_id(derived)
        mock_mass.config.set.assert_not_called()

        # player config gone: never create partial entries
        derived._attr_underlying_player_id = "ap_1"
        del stored["players/spb_1"]
        controller._save_underlying_player_id(derived)
        mock_mass.config.set.assert_not_called()


class TestLocalAudioPlayerPromotion:
    """
    Tests for the local_audio reshape: attribution stub -> regular native player.

    The local_audio device player (bare device-uuid player_id) is a regular,
    visible PLAYER that parents its Sendspin bridge directly. On upgrade, the
    previously auto-created universal player wrapper (up<uuid-hex>) must be
    absorbed by the native player without re-wrapping it.
    """

    DEVICE_UUID = "aabbccdd-1122-3344-5566-77889900aabb"
    UNIVERSAL_ID = "upaabbccdd11223344556677889900aabb"

    def test_native_local_player_absorbs_universal_wrapper(self, mock_mass: MagicMock) -> None:
        """The native player takes over the bridge protocol and the wrapper is emptied."""
        universal = _create_universal_player(
            mock_mass,
            self.UNIVERSAL_ID,
            "Dummy Output",
            protocol_player_ids=[self.DEVICE_UUID, "spb_1"],
        )
        native = MockPlayer(
            MockProvider("local_audio", mass=mock_mass),
            self.DEVICE_UUID,
            "Dummy Output",
            player_type=PlayerType.PLAYER,
            identifiers={IdentifierType.UUID: self.DEVICE_UUID},
        )
        native.set_initialized()
        spb = MockPlayer(
            MockProvider("sendspin", mass=mock_mass),
            "spb_1",
            "Dummy Output Sendspin",
            player_type=PlayerType.PROTOCOL,
        )
        spb._attr_underlying_player_id = self.DEVICE_UUID
        spb.set_initialized()

        store: dict[str, Any] = {
            f"players/{self.DEVICE_UUID}": {"enabled": True},
            "players/spb_1": {"enabled": True},
        }
        mock_mass.config.get = MagicMock(
            side_effect=lambda key, default=None: store.get(key, default)
        )
        mock_mass.config.set = MagicMock(side_effect=store.__setitem__)

        def _close_coro(coro: Any, *_args: Any, **_kwargs: Any) -> None:
            if hasattr(coro, "close"):
                coro.close()

        mock_mass.create_task = MagicMock(side_effect=_close_coro)
        mock_mass.players = controller = PlayerController(mock_mass)
        controller._players = {
            self.UNIVERSAL_ID: universal,
            self.DEVICE_UUID: native,
            "spb_1": spb,
        }
        controller._add_protocol_link(universal, spb, "sendspin")

        controller._try_link_protocols_to_native(native)

        assert spb.protocol_parent_id == self.DEVICE_UUID
        assert [link.output_protocol_id for link in native.linked_output_protocols] == ["spb_1"]
        # the obsolete wrapper keeps neither the bridge nor the native player itself,
        # so its permanent cleanup has no orphaned protocols to re-evaluate
        assert universal._protocol_player_ids == []
        assert store.get("players/spb_1/values/protocol_parent_id") == self.DEVICE_UUID

    async def test_delayed_eval_skips_type_changed_player(self, mock_mass: MagicMock) -> None:
        """A pending evaluation for a player that is not (or no longer) a protocol is a no-op."""
        controller, _ = TestEndToEndDuplicateProtocol._setup_e2e_controller(mock_mass)
        mock_mass.config.get = MagicMock(return_value=[])

        native = MockPlayer(
            MockProvider("local_audio", mass=mock_mass),
            self.DEVICE_UUID,
            "Dummy Output",
            player_type=PlayerType.PLAYER,
        )
        native.set_initialized()
        controller._players = {self.DEVICE_UUID: native}

        await controller._delayed_protocol_evaluation(self.DEVICE_UUID)

        assert native.protocol_parent_id is None
        assert not any(pid.startswith("up") for pid in controller._players)


class TestLocalAudioStubMigration:
    """Tests for the config migration that promotes local_audio stubs to regular players."""

    DEVICE_UUID = "aabbccdd-1122-3344-5566-77889900aabb"
    UNIVERSAL_ID = "upaabbccdd11223344556677889900aabb"

    def _make_data(self) -> dict[str, Any]:
        """Build a settings dict as persisted by the previous local_audio setup."""
        return {
            CONF_PLAYERS: {
                self.DEVICE_UUID: {
                    "player_id": self.DEVICE_UUID,
                    "provider": "local_audio",
                    "player_type": "protocol",
                    "enabled": True,
                    "values": {"protocol_parent_id": self.UNIVERSAL_ID},
                },
                self.UNIVERSAL_ID: {
                    "player_id": self.UNIVERSAL_ID,
                    "provider": "universal_player",
                    "player_type": "group",
                    "enabled": False,
                    "name": "Kitchen Output",
                    "values": {
                        # ghost_spb has no config anymore (device gone) - must not crash
                        "linked_protocol_ids": [self.DEVICE_UUID, "spb_1", "ghost_spb"],
                        "device_identifiers": {"uuid": self.DEVICE_UUID},
                        "device_info": {"model": "Dummy Output", "manufacturer": "Local Audio"},
                        "expose_to_ha": False,
                        "hide_in_ui": True,
                    },
                },
                "spb_1": {
                    "player_id": "spb_1",
                    "provider": "sendspin",
                    "player_type": "protocol",
                    "enabled": True,
                    "values": {"protocol_parent_id": self.UNIVERSAL_ID},
                },
                "syncgroup_1": {
                    "player_id": "syncgroup_1",
                    "provider": "sync_group",
                    "player_type": "group",
                    "enabled": True,
                    "values": {
                        "group_members": [self.UNIVERSAL_ID, "other_player"],
                        "allowed_members": [self.UNIVERSAL_ID],
                    },
                },
            },
            CONF_PLAYER_QUEUES: {
                self.UNIVERSAL_ID: {
                    "queue_id": self.UNIVERSAL_ID,
                    "values": {"autoplay_mode": "smart"},
                },
            },
            CONF_PLAYER_DSP: {
                self.UNIVERSAL_ID: {"enabled": True},
            },
        }

    async def test_stub_promoted_and_universal_absorbed(self) -> None:
        """The stub becomes a regular player carrying the universal player's settings."""
        data = self._make_data()

        assert await migrate(data) is True

        stub_cfg = data[CONF_PLAYERS][self.DEVICE_UUID]
        assert stub_cfg["player_type"] == "player"
        # the universal player was the device toggle the user operated
        assert stub_cfg["enabled"] is False
        assert stub_cfg["name"] == "Kitchen Output"
        values = stub_cfg["values"]
        assert "protocol_parent_id" not in values
        assert values["expose_to_ha"] is False
        assert values["hide_in_ui"] is True
        assert values["linked_protocol_ids"] == ["spb_1", "ghost_spb"]
        assert "device_identifiers" not in values
        assert "device_info" not in values

        # the wrapper is gone and the bridge protocol re-parented
        assert self.UNIVERSAL_ID not in data[CONF_PLAYERS]
        assert data[CONF_PLAYERS]["spb_1"]["values"]["protocol_parent_id"] == self.DEVICE_UUID
        assert data[CONF_PLAYERS]["spb_1"]["player_type"] == "protocol"

        # queue settings, DSP config and group memberships follow the new player_id
        queue_cfg = data[CONF_PLAYER_QUEUES][self.DEVICE_UUID]
        assert queue_cfg["queue_id"] == self.DEVICE_UUID
        assert queue_cfg["values"]["autoplay_mode"] == "smart"
        assert self.UNIVERSAL_ID not in data[CONF_PLAYER_QUEUES]
        assert data[CONF_PLAYER_DSP] == {self.DEVICE_UUID: {"enabled": True}}
        group_values = data[CONF_PLAYERS]["syncgroup_1"]["values"]
        assert group_values["group_members"] == [self.DEVICE_UUID, "other_player"]
        assert group_values["allowed_members"] == [self.DEVICE_UUID]

    async def test_stub_without_universal_wrapper(self) -> None:
        """A stub without a universal player wrapper is still promoted."""
        data = self._make_data()
        del data[CONF_PLAYERS][self.UNIVERSAL_ID]

        assert await migrate(data) is True

        stub_cfg = data[CONF_PLAYERS][self.DEVICE_UUID]
        assert stub_cfg["player_type"] == "player"
        assert stub_cfg["enabled"] is True
        assert "protocol_parent_id" not in stub_cfg["values"]

    async def test_migration_is_idempotent(self) -> None:
        """A second run finds nothing left to migrate."""
        data = self._make_data()

        assert await migrate(data) is True
        assert await migrate(data) is False
