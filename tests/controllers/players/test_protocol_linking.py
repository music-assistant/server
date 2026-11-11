"""Tests for protocol player linking and universal player creation."""

import asyncio
import inspect
import logging
import re
from collections.abc import Awaitable, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from music_assistant_models.enums import (
    IdentifierType,
    PlayerFeature,
    PlayerType,
)

from music_assistant.constants import (
    CONF_PLAYER_DSP,
    CONF_PLAYER_QUEUES,
    CONF_PLAYERS,
    CONF_PROTOCOL_PARENT_ID,
)
from music_assistant.controllers.config.migrations import migrate
from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.players.matching import DeviceIdentity, identifiers_match
from music_assistant.helpers.util import enrich_device_mac_address
from music_assistant.models.player import DeviceInfo, Player
from music_assistant.providers.universal_player.constants import CONF_CREATED_AT
from music_assistant.providers.universal_player.player import (
    UniversalPlayer,
    get_clean_player_name,
)
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
    # Initialize the lock that would normally be set in handle_async_init
    provider._lock = asyncio.Lock()
    return provider


# Player id of the universal player a device was wrapped in earlier, as minted once
# by UniversalPlayerProvider.mint_player_id.
STORED_UNIVERSAL_ID = "up1a2b3c4d"


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


def _fake_create_default_player_config(
    store: dict[str, Any],
    player_id: str,
    provider: str,
    player_type: PlayerType,
    name: str | None = None,
    enabled: bool = True,
    values: dict[str, Any] | None = None,
) -> None:
    """
    Mirror ConfigController.create_default_player_config against a plain dict store.

    Only the parts the universal player flows depend on: a fresh config keeps the
    creation-time name as the default name only, an existing config keeps its type.
    """
    players: dict[str, Any] = store.setdefault(CONF_PLAYERS, {})
    if existing := players.get(player_id):
        if name and name != existing.get("default_name"):
            existing["default_name"] = name
        return
    players[player_id] = {
        "player_id": player_id,
        "provider": provider,
        "player_type": player_type.value,
        "enabled": enabled,
        "name": None,
        "default_name": name,
        "values": dict(values or {}),
    }


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
        self.protocols: list[Player] = []


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
def mock_mass() -> Iterator[MagicMock]:
    """Create a mock MusicAssistant instance."""
    mass = MagicMock()
    mass.closing = False
    mass.config = MagicMock()
    mass.config.get = MagicMock(return_value=[])

    def _get_player_value(
        _player_id: str, key: str, default: str | int | None = None
    ) -> str | int | None:
        """Return appropriate defaults for player config values."""
        if key == "min_volume":
            return 0
        if key == "max_volume":
            return 100
        return default

    mass.config.get_player_value = MagicMock(side_effect=_get_player_value)
    # Return "GLOBAL" for log level config (standard default)
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    mass.config.set = MagicMock()
    mass.signal_event = MagicMock()
    mass.get_providers = MagicMock(return_value=[])
    # awaited by the tests that replay the tasks scheduled during a config wipe
    mass.webserver.auth.remove_from_user_filters = AsyncMock()
    mass.webserver.auth.replace_player_in_user_filters = AsyncMock()
    yield mass
    for call in mass.create_task.call_args_list:
        if call.args and inspect.iscoroutine(coro := call.args[0]):
            coro.close()


class TestIdentityOf:
    """Tests for what a player reports about its device to the identifier matcher."""

    def test_native_player(self, mock_mass: MagicMock) -> None:
        """A native player reports its identifiers, its own domain and that it is native."""
        controller = PlayerController(mock_mass)
        player = MockPlayer(
            MockProvider("sonos", mass=mock_mass),
            "RINCON_000E58123456",
            "Sonos",
            identifiers={
                IdentifierType.MAC_ADDRESS: "00:0E:58:12:34:56",
                IdentifierType.UUID: "RINCON_000E58123456",
            },
        )

        assert controller._identity_of(player) == DeviceIdentity(
            identifiers=frozenset(
                {
                    (IdentifierType.MAC_ADDRESS, "00:0E:58:12:34:56"),
                    (IdentifierType.UUID, "RINCON_000E58123456"),
                }
            ),
            domains=frozenset({"sonos"}),
            native=True,
        )

    def test_protocol_player(self, mock_mass: MagicMock) -> None:
        """A protocol player is not the native protocol of its device."""
        controller = PlayerController(mock_mass)
        player = MockPlayer(
            MockProvider("airplay", mass=mock_mass),
            "ap000e58123456",
            "Speaker (AirPlay)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.AIRPLAY_ID: "ap000e58123456"},
        )

        assert controller._identity_of(player) == DeviceIdentity(
            identifiers=frozenset({(IdentifierType.AIRPLAY_ID, "ap000e58123456")}),
            domains=frozenset({"airplay"}),
            native=False,
        )

    def test_universal_player(self, mock_mass: MagicMock) -> None:
        """
        A universal player reports for the protocols it holds.

        It reports their identifiers next to the ones it stored, so each of them stays its
        device whichever UUID was stored last, and their domains also while they are away,
        so a second renderer of one protocol on the host is told apart.
        """
        controller = PlayerController(mock_mass)
        mock_mass.players = controller
        _wire_nested_config(
            mock_mass,
            {
                CONF_PLAYERS: {
                    "up_tv": {"values": {"linked_protocol_ids": ["cast_tv", "dlna_tv", "ap_tv"]}},
                    "cast_tv": {"provider": "chromecast--one"},
                    "dlna_tv": {"provider": "dlna--one"},
                    "ap_tv": {"provider": "airplay--one"},
                }
            },
        )
        tv = {
            IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A0",
            IdentifierType.IP_ADDRESS: "192.168.1.60",
        }
        universal = _create_universal_player(
            mock_mass,
            "up_tv",
            "TV",
            protocol_player_ids=["cast_tv", "dlna_tv", "ap_tv"],
            identifiers={**tv, IdentifierType.UUID: "dlna-udn"},
        )
        cast = MockPlayer(
            MockProvider("chromecast", mass=mock_mass),
            "cast_tv",
            "TV (Cast)",
            player_type=PlayerType.PROTOCOL,
            identifiers={**tv, IdentifierType.UUID: "cast-uuid"},
        )
        dlna = MockPlayer(
            MockProvider("dlna", mass=mock_mass),
            "dlna_tv",
            "TV (DLNA)",
            player_type=PlayerType.PROTOCOL,
            identifiers={**tv, IdentifierType.UUID: "dlna-udn"},
        )
        other_renderer = MockPlayer(
            MockProvider("dlna", mass=mock_mass),
            "dlna_other",
            "Other renderer on the TV",
            player_type=PlayerType.PROTOCOL,
            identifiers={**tv, IdentifierType.UUID: "other-udn"},
        )
        # the AirPlay protocol is away: only its stored config names its domain
        controller._players = {"up_tv": universal, "cast_tv": cast, "dlna_tv": dlna}

        identity = controller._identity_of(universal)

        assert identity == DeviceIdentity(
            identifiers=frozenset(
                {*tv.items(), (IdentifierType.UUID, "dlna-udn"), (IdentifierType.UUID, "cast-uuid")}
            ),
            domains=frozenset({"chromecast", "dlna", "airplay"}),
            native=False,
        )
        assert identifiers_match(identity, controller._identity_of(cast))
        assert identifiers_match(identity, controller._identity_of(dlna))
        assert not identifiers_match(identity, controller._identity_of(other_renderer))


class TestMintPlayerId:
    """Tests for minting the opaque player id of a new universal player."""

    def test_minted_id_is_free_as_a_player_and_as_a_config(self, mock_mass: MagicMock) -> None:
        """A minted id is never one that is already taken."""
        universal_provider = create_mock_universal_provider(mock_mass)
        _wire_nested_config(
            mock_mass,
            {CONF_PLAYERS: {"upbbbbbbbb": {"player_id": "upbbbbbbbb", "enabled": False}}},
        )
        registered = {"upaaaaaaaa": MagicMock()}
        mock_mass.players = MagicMock()
        mock_mass.players.get_player = MagicMock(
            side_effect=lambda pid, *_args, **_kwargs: registered.get(pid)
        )
        candidates = [
            UUID("aaaaaaaa-0000-0000-0000-000000000000"),  # taken by a registered player
            UUID("bbbbbbbb-0000-0000-0000-000000000000"),  # taken by a (stale) config
            UUID("cccccccc-0000-0000-0000-000000000000"),
        ]

        with patch(
            "music_assistant.providers.universal_player.provider.uuid4", side_effect=candidates
        ):
            assert universal_provider.mint_player_id() == "upcccccccc"


class TestGetCleanPlayerName:
    """Tests for player name selection."""

    def test_prefers_chromecast_name(self) -> None:
        """Test that Chromecast names are preferred over other protocols."""
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
        clean_name = get_clean_player_name([airplay_player, chromecast_player])
        assert clean_name == "Living Room Speaker"

    def test_filters_mac_address_names(self) -> None:
        """Test that MAC address-like names are filtered out."""
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
        clean_name = get_clean_player_name([sq_player, ap_player])
        assert clean_name == "Kitchen Speaker"

    def test_filters_player_id_names(self) -> None:
        """Test that player ID-like names are filtered out."""
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
        clean_name = get_clean_player_name([ss_player, dlna_player])
        assert clean_name == "Bedroom TV"

    def test_valid_name_unchanged(self) -> None:
        """Test that valid names are returned unchanged."""
        provider = MockProvider("airplay")
        player = MockPlayer(
            provider,
            "ap_123456",
            "HomePod Mini",
            player_type=PlayerType.PLAYER,
        )

        clean_name = get_clean_player_name([player])
        assert clean_name == "HomePod Mini"

    def test_no_name_without_a_usable_one(self) -> None:
        """Test that no name is returned when every name looks like a technical id."""
        provider = MockProvider("squeezelite")
        player = MockPlayer(
            provider,
            "sq_123456",
            "AA:BB:CC:DD:EE:FF",
            player_type=PlayerType.PROTOCOL,
        )

        assert get_clean_player_name([player]) is None


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

    def test_stale_cache_does_not_claim_a_protocol_waiting_for_its_owner(
        self, mock_mass: MagicMock
    ) -> None:
        """A protocol whose persisted owner is still starting up is not claimed by another parent."""
        controller = PlayerController(mock_mass)

        store: dict[str, Any] = {
            CONF_PLAYERS: {
                "ap_shared": {
                    "provider": "airplay--x",
                    "player_type": "protocol",
                    "values": {CONF_PROTOCOL_PARENT_ID: "sonos_owner"},
                },
                "heos_player": {
                    "provider": "heos--y",
                    "player_type": "player",
                    "values": {"linked_protocol_ids": ["ap_shared"]},
                },
                "sonos_owner": {
                    "provider": "sonos--z",
                    "player_type": "player",
                    "values": {"linked_protocol_ids": ["ap_shared"]},
                },
            }
        }
        _wire_nested_config(mock_mass, store)

        # a native player that kept a stale cached id for a protocol it no longer owns
        heos_player = MockPlayer(
            MockProvider("heos", mass=mock_mass),
            "heos_player",
            "Old Receiver",
            identifiers={IdentifierType.MAC_ADDRESS: "11:22:33:44:55:66"},
        )
        heos_player.set_initialized()
        airplay = MockPlayer(
            MockProvider("airplay", mass=mock_mass),
            "ap_shared",
            "Speaker (AirPlay)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        airplay.set_initialized()
        controller._players = {"heos_player": heos_player, "ap_shared": airplay}

        # the persisted owner has not registered yet
        controller._try_link_protocol_to_native(airplay)

        assert airplay.protocol_parent_id is None
        assert heos_player.linked_output_protocols == []
        assert mock_mass.loop.call_later.call_args.args[0] == 45.0

        # once the owner registers it takes its own protocol
        sonos_owner = MockPlayer(
            MockProvider("sonos", mass=mock_mass),
            "sonos_owner",
            "Speaker",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        sonos_owner.set_initialized()
        controller._players["sonos_owner"] = sonos_owner
        controller._try_link_protocols_to_native(sonos_owner)

        assert airplay.protocol_parent_id == "sonos_owner"
        assert [  # type: ignore[unreachable]
            link.output_protocol_id for link in sonos_owner.linked_output_protocols
        ] == ["ap_shared"]

    def test_identifier_match_does_not_claim_a_protocol_waiting_for_its_owner(
        self, mock_mass: MagicMock
    ) -> None:
        """Identifier matching does not override a protocol's own persisted owner."""
        controller = PlayerController(mock_mass)

        store: dict[str, Any] = {
            CONF_PLAYERS: {
                "ap_shared": {
                    "provider": "airplay--x",
                    "player_type": "protocol",
                    "values": {CONF_PROTOCOL_PARENT_ID: "sonos_owner"},
                },
                "sonos_owner": {"provider": "sonos--z", "player_type": "player", "values": {}},
            }
        }
        _wire_nested_config(mock_mass, store)

        # shares identifiers with the protocol but is not its persisted owner
        other_native = MockPlayer(
            MockProvider("heos", mass=mock_mass),
            "heos_player",
            "Other Receiver",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        other_native.set_initialized()
        airplay = MockPlayer(
            MockProvider("airplay", mass=mock_mass),
            "ap_shared",
            "Speaker (AirPlay)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        airplay.set_initialized()
        controller._players = {"heos_player": other_native, "ap_shared": airplay}

        controller._try_link_protocol_to_native(airplay)

        assert airplay.protocol_parent_id is None
        assert other_native.linked_output_protocols == []

    def test_other_native_does_not_claim_a_protocol_reserved_for_its_owner(
        self, mock_mass: MagicMock
    ) -> None:
        """A native registering before the persisted owner leaves that owner's protocol alone."""
        controller = PlayerController(mock_mass)

        store: dict[str, Any] = {
            CONF_PLAYERS: {
                "ap_shared": {
                    "provider": "airplay--x",
                    "player_type": "protocol",
                    "values": {CONF_PROTOCOL_PARENT_ID: "sonos_owner"},
                },
                "sonos_owner": {"provider": "sonos--z", "player_type": "player", "values": {}},
            }
        }
        _wire_nested_config(mock_mass, store)

        airplay = MockPlayer(
            MockProvider("airplay", mass=mock_mass),
            "ap_shared",
            "Speaker (AirPlay)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        airplay.set_initialized()
        controller._players = {"ap_shared": airplay}

        controller._try_link_protocol_to_native(airplay)
        assert airplay.protocol_parent_id is None

        # another native with a colliding identifier registers before the owner
        other_native = MockPlayer(
            MockProvider("heos", mass=mock_mass),
            "heos_player",
            "Other Receiver",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        other_native.set_initialized()
        controller._players["heos_player"] = other_native
        controller._try_link_protocols_to_native(other_native)

        assert airplay.protocol_parent_id is None
        assert other_native.linked_output_protocols == []

        # the owner still gets its own protocol
        sonos_owner = MockPlayer(
            MockProvider("sonos", mass=mock_mass),
            "sonos_owner",
            "Speaker",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        sonos_owner.set_initialized()
        controller._players["sonos_owner"] = sonos_owner
        controller._try_link_protocols_to_native(sonos_owner)

        assert airplay.protocol_parent_id == "sonos_owner"

    @pytest.mark.asyncio
    async def test_protocol_links_elsewhere_when_its_owner_never_registers(
        self, mock_mass: MagicMock
    ) -> None:
        """A protocol whose persisted owner never registers still links on delayed evaluation."""
        controller = PlayerController(mock_mass)

        store: dict[str, Any] = {
            CONF_PLAYERS: {
                "ap_shared": {
                    "provider": "airplay--x",
                    "player_type": "protocol",
                    "values": {CONF_PROTOCOL_PARENT_ID: "gone_owner"},
                },
                "heos_player": {
                    "provider": "heos--y",
                    "player_type": "player",
                    "values": {"linked_protocol_ids": ["ap_shared"]},
                },
            }
        }
        _wire_nested_config(mock_mass, store)

        heos_player = MockPlayer(
            MockProvider("heos", mass=mock_mass),
            "heos_player",
            "Receiver",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        heos_player.set_initialized()
        airplay = MockPlayer(
            MockProvider("airplay", mass=mock_mass),
            "ap_shared",
            "Speaker (AirPlay)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        airplay.set_initialized()
        controller._players = {"heos_player": heos_player, "ap_shared": airplay}

        controller._try_link_protocol_to_native(airplay)
        assert airplay.protocol_parent_id is None

        await controller._delayed_protocol_evaluation("ap_shared")

        assert airplay.protocol_parent_id == "heos_player"

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

        # DLNA players should not be shown since DLNA doesn't support SET_MEMBERS


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
        assert identifiers_match(
            controller._identity_of(airplay_player), controller._identity_of(sendspin_player)
        )

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
        assert identifiers_match(
            controller._identity_of(airplay_player), controller._identity_of(dlna_player)
        )


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

    @staticmethod
    def _setup_merge_tiebreak(
        mock_mass: MagicMock, created_at: dict[str, int]
    ) -> tuple[PlayerController, UniversalPlayer, UniversalPlayer, MockPlayer, MockPlayer]:
        """
        Set up two universal players of one device, each with a single protocol link.

        :param mock_mass: The mocked MusicAssistant instance to wire.
        :param created_at: Stored creation timestamp per universal player id.
        :return: Tuple of (controller, up_aaa, up_bbb, its DLNA player, its AirPlay player).
        """
        controller = PlayerController(mock_mass)
        up_provider = create_mock_universal_provider(mock_mass)
        _wire_nested_config(
            mock_mass,
            {
                CONF_PLAYERS: {
                    player_id: {
                        "player_id": player_id,
                        "provider": "universal_player",
                        "enabled": True,
                        "values": {CONF_CREATED_AT: timestamp},
                    }
                    for player_id, timestamp in created_at.items()
                }
            },
        )

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

        return controller, up_a, up_b, dlna_player, ap_player

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


class TestUniversalPlayerReplacement:
    """Tests for replacing a universal player with a native player."""

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
        mock_mass.player_queues.get.return_value = None
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

    async def test_provider_discovery_rechecks_registered_native_players(
        self, mock_mass: MagicMock
    ) -> None:
        """A native player that registered before its wrapper was restored is promoted."""
        controller = PlayerController(mock_mass)
        config_store: dict[str, Any] = {
            CONF_PLAYERS: {
                "sonos_1": {"enabled": True, "values": {}},
                "up_old": {"enabled": True, "provider": "universal_player", "values": {}},
                "ap_live": {
                    "enabled": True,
                    "provider": "airplay--test",
                    "player_type": "protocol",
                    "values": {CONF_PROTOCOL_PARENT_ID: "up_old"},
                },
            }
        }
        _wire_nested_config(mock_mass, config_store)
        mock_mass.players = controller
        mock_mass.player_queues = MagicMock()
        mock_mass.create_task = MagicMock()
        mock_mass.config.get_player_configs = AsyncMock(return_value=[])

        provider = create_mock_universal_provider(mock_mass)
        identifiers = {IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"}
        universal = _create_universal_player(
            mock_mass, "up_old", "Old Universal", ["ap_live"], identifiers=identifiers
        )
        native = MockPlayer(
            MockProvider("sonos", mass=mock_mass), "sonos_1", "Sonos", identifiers=identifiers
        )
        native.set_initialized()
        ap_live = MockPlayer(
            MockProvider("airplay", mass=mock_mass),
            "ap_live",
            "AirPlay",
            player_type=PlayerType.PROTOCOL,
            identifiers=identifiers,
        )
        ap_live.set_initialized()
        controller._players = {
            "up_old": universal,
            "sonos_1": native,
            "ap_live": ap_live,
        }
        controller._add_protocol_link(universal, ap_live, "airplay")

        await provider.discover_players()

        assert ap_live.protocol_parent_id == "sonos_1"
        assert native.protocol_parent_id is None

    async def test_provider_discovery_does_not_replace_wrapper_with_group(
        self, mock_mass: MagicMock
    ) -> None:
        """A matching group player must never take over a wrapper's protocols."""
        controller = PlayerController(mock_mass)
        config_store: dict[str, Any] = {
            CONF_PLAYERS: {
                "up_old": {"enabled": True, "provider": "universal_player", "values": {}},
                "ap_owned": {
                    "enabled": True,
                    "provider": "airplay--test",
                    "player_type": "protocol",
                    "values": {CONF_PROTOCOL_PARENT_ID: "up_old"},
                },
            }
        }
        _wire_nested_config(mock_mass, config_store)
        mock_mass.players = controller
        mock_mass.player_queues = MagicMock()
        mock_mass.create_task = MagicMock()
        mock_mass.config.get_player_configs = AsyncMock(return_value=[])

        provider = create_mock_universal_provider(mock_mass)
        identifiers = {IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"}
        universal = _create_universal_player(
            mock_mass, "up_old", "Old Universal", ["ap_owned"], identifiers=identifiers
        )
        group = MockPlayer(
            MockProvider("sync_group", mass=mock_mass),
            "group_1",
            "Group",
            player_type=PlayerType.GROUP,
            identifiers=identifiers,
        )
        group.set_initialized()
        ap_owned = MockPlayer(
            MockProvider("airplay", mass=mock_mass),
            "ap_owned",
            "AirPlay",
            player_type=PlayerType.PROTOCOL,
            identifiers=identifiers,
        )
        ap_owned.set_initialized()
        controller._players = {"up_old": universal, "group_1": group, "ap_owned": ap_owned}
        controller._add_protocol_link(universal, ap_owned, "airplay")

        await provider.discover_players()

        assert ap_owned.protocol_parent_id == "up_old"
        assert not group.linked_output_protocols

    @staticmethod
    def _setup_config_claim_scenario(
        mock_mass: MagicMock, player_configs: dict[str, dict[str, Any]]
    ) -> tuple[PlayerController, dict[str, Any], list[Awaitable[object]]]:
        """Wire a controller to a nested config store and capture the scheduled tasks."""
        controller = PlayerController(mock_mass)
        config_store: dict[str, Any] = {CONF_PLAYERS: player_configs}
        _wire_nested_config(mock_mass, config_store)
        scheduled_tasks: list[Awaitable[object]] = []

        def capture_task(task: Awaitable[object], *_args: Any, **_kwargs: Any) -> None:
            scheduled_tasks.append(task)

        mock_mass.players = controller
        mock_mass.player_queues = MagicMock()
        mock_mass.player_queues.get = MagicMock(return_value=None)
        mock_mass.call_later = MagicMock()
        mock_mass.loop = MagicMock()
        mock_mass.create_task = MagicMock(side_effect=capture_task)
        return controller, config_store, scheduled_tasks

    @staticmethod
    def _protocol_player(mock_mass: MagicMock, domain: str, player_id: str) -> MockPlayer:
        """Create an initialized protocol player carrying the wrapped device's MAC."""
        player = MockPlayer(
            MockProvider(domain, mass=mock_mass),
            player_id,
            player_id,
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        player.set_initialized()
        return player

    async def test_universal_replaced_by_linkplay_shell(self, mock_mass: MagicMock) -> None:
        """A LinkPlay shell that lists the wrapped DLNA+AirPlay in config replaces the Universal."""
        universal_id = "up_edifier"
        shell_id = "wiim_uuid:FF97F002-783E-6505-6579-F15FFF97F002"
        controller, config_store, scheduled_tasks = self._setup_config_claim_scenario(
            mock_mass,
            {
                universal_id: {
                    "enabled": True,
                    "provider": "universal_player",
                    "name": "Edifier MS50A",
                    "default_name": "Edifier (Universal)",
                    "values": {"hide_in_ui": True},
                },
                shell_id: {
                    "enabled": True,
                    "provider": "wiim",
                    "player_type": "player",
                    "values": {"linked_protocol_ids": ["ap_edifier", "dlna_edifier"]},
                },
                "ap_edifier": {
                    "enabled": True,
                    "provider": "airplay--test",
                    "player_type": "protocol",
                    "values": {CONF_PROTOCOL_PARENT_ID: universal_id},
                },
                "dlna_edifier": {
                    "enabled": True,
                    "provider": "dlna--test",
                    "player_type": "protocol",
                    "values": {CONF_PROTOCOL_PARENT_ID: universal_id},
                },
            },
        )
        universal = _create_universal_player(
            mock_mass,
            universal_id,
            "Edifier (Universal)",
            ["ap_edifier", "dlna_edifier"],
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        ap = self._protocol_player(mock_mass, "airplay", "ap_edifier")
        dlna = self._protocol_player(mock_mass, "dlna", "dlna_edifier")
        # the shell exposes no identifiers to match the wrapper by
        shell = MockPlayer(MockProvider("wiim", mass=mock_mass), shell_id, "Edifier MS50A")
        shell.set_initialized()
        controller._players = {
            universal_id: universal,
            "ap_edifier": ap,
            "dlna_edifier": dlna,
            shell_id: shell,
        }
        controller._add_protocol_link(universal, ap, "airplay")
        controller._add_protocol_link(universal, dlna, "dlna")

        controller._check_replace_universal_player(shell)

        assert ap.protocol_parent_id == shell_id
        assert dlna.protocol_parent_id == shell_id
        # the Universal's user settings carry over and the Universal is removed
        assert config_store[CONF_PLAYERS][shell_id]["values"]["hide_in_ui"] is True
        assert any("unregister" in repr(t) for t in scheduled_tasks)


class TestEndToEndDuplicateProtocol:
    """
    End-to-end integration tests for duplicate protocol → separate universal player flow.

    These tests exercise the full async flow through _delayed_protocol_evaluation,
    ensure_universal_players_for_protocols, and _create_separate_universal_player,
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

        # ap2 should have its own (freshly minted) universal player
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
        Path B: ensure_universal_players_for_protocols separates can_join vs rejected.

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
        # _create_or_update_universal_player → ensure_universal_players_for_protocols
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


class TestEndToEndUniversalPlayerId:
    """
    End-to-end tests for the minted player id of a universal player.

    Regression tests for music-assistant/support#5888: the id used to be derived from
    whichever device identifiers happened to be available when the wrapper was created,
    so a re-created wrapper could silently change id and orphan the entities API
    consumers (such as the Home Assistant integration) bind to it. These tests drive
    the real evaluation path against a config store that persists what the flow writes.
    """

    @staticmethod
    def _setup_controller(
        mock_mass: MagicMock,
    ) -> tuple[PlayerController, dict[str, Any]]:
        """
        Wire a controller, the universal player provider and a persisting config store.

        :param mock_mass: The mocked MusicAssistant instance to wire.
        :return: Tuple of (controller, backing config store).
        """
        controller = PlayerController(mock_mass)
        up_provider = create_mock_universal_provider(mock_mass)
        mock_mass.get_providers = MagicMock(return_value=[up_provider])

        store: dict[str, Any] = {CONF_PLAYERS: {}}
        _wire_nested_config(mock_mass, store)
        mock_mass.config.create_default_player_config = MagicMock(
            side_effect=lambda *args, **kwargs: _fake_create_default_player_config(
                store, *args, **kwargs
            )
        )
        mock_mass.config.get_base_player_config.return_value = create_mock_config("Test")

        async def fake_register_or_update(player: Player) -> None:
            controller._players[player.player_id] = player
            player.set_initialized()

        mock_mass.players = MagicMock()
        mock_mass.players.register_or_update = AsyncMock(side_effect=fake_register_or_update)
        mock_mass.players.get_player = lambda pid, *_args, **_kwargs: controller._players.get(pid)
        mock_mass.players.get_protocol = mock_mass.players.get_player
        return controller, store

    @staticmethod
    def _make_airplay_player(
        mock_mass: MagicMock, player_id: str, name: str, identifiers: dict[IdentifierType, str]
    ) -> MockPlayer:
        """Return an unlinked AirPlay protocol player."""
        player = MockPlayer(
            MockProvider("airplay", mass=mock_mass),
            player_id,
            name,
            player_type=PlayerType.PROTOCOL,
            identifiers=identifiers,
        )
        player.set_initialized()
        # registration creates the player config, which holds the parent link
        mock_mass.config.create_default_player_config(
            player_id, player.provider_id, player.type, name
        )
        return player

    async def test_id_survives_recreation_with_other_identifiers(
        self, mock_mass: MagicMock
    ) -> None:
        """
        A re-created wrapper keeps its id when a different set of identifiers is known.

        The first evaluation only knows the device's MAC, the second only its UUID.
        The derived key changed along with that, taking the player id with it.
        """
        controller, store = self._setup_controller(mock_mass)
        first = self._make_airplay_player(
            mock_mass, "ap_1", "Living Room", {IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A0"}
        )
        controller._players = {"ap_1": first}

        await controller._delayed_protocol_evaluation("ap_1")

        universal_id = first.protocol_parent_id
        assert universal_id is not None
        assert re.fullmatch(r"up[0-9a-f]{8}", universal_id)

        # tear the wrapper down (its config is kept, as a non-permanent unregister does)
        # and bring the device back with only a UUID to identify it by
        del controller._players[universal_id]
        second = self._make_airplay_player(
            mock_mass,
            "ap_1",
            "Living Room",
            {IdentifierType.UUID: "12345678-1234-1234-1234-123456789abc"},
        )
        controller._players = {"ap_1": second}

        await controller._delayed_protocol_evaluation("ap_1")

        assert second.protocol_parent_id == universal_id
        assert universal_id in controller._players
        assert store[CONF_PLAYERS]["ap_1"]["values"][CONF_PROTOCOL_PARENT_ID] == universal_id

    async def test_wrappers_created_in_one_second_still_order(self, mock_mass: MagicMock) -> None:
        """
        Two wrappers created back to back get distinct creation timestamps.

        A merge between them must be able to keep the older one, which it cannot do
        when both carry the same timestamp and the tiebreak falls back to the id.
        """
        controller, store = self._setup_controller(mock_mass)
        players = [
            self._make_airplay_player(
                mock_mass, f"ap_{index}", f"Speaker {index}", {IdentifierType.MAC_ADDRESS: mac}
            )
            for index, mac in enumerate(("54:78:C9:E6:0D:A0", "AA:BB:CC:DD:EE:FF"), start=1)
        ]
        controller._players = {player.player_id: player for player in players}

        for player in players:
            await controller._delayed_protocol_evaluation(player.player_id)

        created = [
            store[CONF_PLAYERS][player.protocol_parent_id]["values"][CONF_CREATED_AT]
            for player in players
        ]
        assert all(isinstance(value, int) for value in created)
        assert created[0] < created[1]

    async def test_new_devices_get_distinct_minted_ids(self, mock_mass: MagicMock) -> None:
        """Devices that were never wrapped before each get their own minted id."""
        controller, _ = self._setup_controller(mock_mass)
        players = [
            self._make_airplay_player(
                mock_mass, f"ap_{index}", f"Speaker {index}", {IdentifierType.MAC_ADDRESS: mac}
            )
            for index, mac in enumerate(("54:78:C9:E6:0D:A0", "AA:BB:CC:DD:EE:FF"), start=1)
        ]
        controller._players = {player.player_id: player for player in players}

        for player in players:
            await controller._delayed_protocol_evaluation(player.player_id)

        universal_ids = [player.protocol_parent_id for player in players]
        assert all(pid and re.fullmatch(r"up[0-9a-f]{8}", pid) for pid in universal_ids)
        assert len(set(universal_ids)) == len(players)

    async def test_duplicate_domain_gets_a_second_wrapper_with_a_parent_link(
        self, mock_mass: MagicMock
    ) -> None:
        """
        Two players of one protocol domain end up on two wrappers, both linked.

        The handoff must persist a parent link for the second player as well,
        otherwise it silently stays unwrapped.
        """
        controller, store = self._setup_controller(mock_mass)
        identifiers = {IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A0"}
        ap1 = self._make_airplay_player(mock_mass, "ap_1", "Zone 1", identifiers)
        ap2 = self._make_airplay_player(mock_mass, "ap_2", "Zone 2", identifiers)
        controller._players = {"ap_1": ap1, "ap_2": ap2}

        await controller._create_or_update_universal_player([ap1, ap2])

        assert ap1.protocol_parent_id is not None
        assert ap2.protocol_parent_id is not None
        assert ap1.protocol_parent_id != ap2.protocol_parent_id
        for player in (ap1, ap2):
            stored = store[CONF_PLAYERS][player.player_id]["values"][CONF_PROTOCOL_PARENT_ID]
            assert stored == player.protocol_parent_id


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
        assert not identifiers_match(
            controller._identity_of(heos_player), controller._identity_of(spb_player)
        )

        # Sibling matching should succeed through AirPlay's identifiers
        assert controller._match_via_linked_protocols(heos_player, spb_player, "sendspin") is True
        assert spb_player.protocol_parent_id == "2140037800"

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

        assert identifiers_match(
            controller._identity_of(player_a), controller._identity_of(player_b)
        )

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

        assert identifiers_match(
            controller._identity_of(player_a), controller._identity_of(player_b)
        )

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

        assert not identifiers_match(
            controller._identity_of(player_a), controller._identity_of(player_b)
        )

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

        assert identifiers_match(
            controller._identity_of(player_a), controller._identity_of(player_b)
        )

    def test_match_on_arp_mac_while_the_provider_reports_its_own(
        self, mock_mass: MagicMock
    ) -> None:
        """A player keeps the ARP-resolved MAC when its provider reports its own MAC again."""
        controller = PlayerController(mock_mass)
        _wire_nested_config(
            mock_mass,
            {CONF_PLAYERS: {"player_a": {"values": {"cached_arp_mac": "AA:BB:CC:DD:EE:02"}}}},
        )

        player_a = MockPlayer(
            MockProvider("test", mass=mock_mass),
            "player_a",
            "Player A",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:01"},
        )
        player_b = MockPlayer(
            MockProvider("other", mass=mock_mass),
            "player_b",
            "Player B",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:02"},
        )

        assert player_a.identifiers == frozenset(
            {
                (IdentifierType.MAC_ADDRESS, "AA:BB:CC:DD:EE:01"),
                (IdentifierType.MAC_ADDRESS, "AA:BB:CC:DD:EE:02"),
            }
        )
        assert identifiers_match(
            controller._identity_of(player_a), controller._identity_of(player_b)
        )


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

    @pytest.mark.parametrize("removal", ["config_deleted_while_unregistered", "unregistered"])
    async def test_refused_derived_links_once_its_domain_is_freed(
        self, mock_mass: MagicMock, removal: str
    ) -> None:
        """
        A derived player refused for a taken domain links once the player holding it is removed.

        A device's bridges register in no fixed order, so a bridge that is removed right after
        start can hold the domain while the bridge that stays registers.
        """
        controller = PlayerController(mock_mass)
        mock_mass.players = controller
        mock_mass.config.get = MagicMock(return_value=[])

        native = MockPlayer(
            MockProvider("wiim", mass=mock_mass), "native_1", "WiiM", player_type=PlayerType.PLAYER
        )
        native.set_initialized()
        controller._players = {"native_1": native}
        for player_id, domain in (("ap_1", "airplay"), ("cast_1", "chromecast")):
            base = MockPlayer(
                MockProvider(domain, mass=mock_mass),
                player_id,
                f"WiiM {domain}",
                player_type=PlayerType.PROTOCOL,
            )
            base.set_initialized()
            controller._players[player_id] = base
            controller._add_protocol_link(native, base, domain)
        for player_id, underlying_id in (("spb_cast", "cast_1"), ("spb_ap", "ap_1")):
            bridge = self._make_derived_player(mock_mass, player_id, underlying_id)
            controller._players[player_id] = bridge
            controller._try_link_protocol_to_native(bridge)
        assert controller._players["spb_ap"].protocol_parent_id is None

        if removal == "unregistered":
            await controller.unregister("spb_cast", permanent=True)
        else:
            del controller._players["spb_cast"]
            controller.delete_player_config("spb_cast")

        assert controller._players["spb_ap"].protocol_parent_id == "native_1"

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
