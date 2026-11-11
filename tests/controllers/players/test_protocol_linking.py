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
    PlaybackState,
    PlayerFeature,
    PlayerType,
)
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.media_items import AudioSource
from music_assistant_models.media_items.provider_mapping import ProviderMapping
from music_assistant_models.player import PlayerMedia

from music_assistant.constants import (
    CONF_PLAYER_DSP,
    CONF_PLAYER_QUEUES,
    CONF_PLAYERS,
    CONF_PREFERRED_OUTPUT_PROTOCOL,
    CONF_PROTOCOL_PARENT_ID,
)
from music_assistant.controllers.config.migrations import migrate
from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.players.matching import DeviceIdentity, identifiers_match
from music_assistant.helpers.util import enrich_device_mac_address
from music_assistant.models.player import DeviceInfo, LinkedOutputProtocol, Player
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


class SessionBoundMockPlayer(MockPlayer):
    """Mock player whose native grouping attaches members to its own stream (e.g. AirPlay)."""

    @property
    def native_grouping_requires_own_stream(self) -> bool:
        """Return True: native members ride this player's own stream session."""
        return True


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

        mock_mass.config.get_player_value = MagicMock(side_effect=_get_raw)

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

        mock_mass.config.get_player_value = MagicMock(side_effect=_get_raw)

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

        mock_mass.config.get_player_value = MagicMock(side_effect=_get_raw)

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

        mock_mass.config.get_player_value = MagicMock(side_effect=_get_raw)

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
        with_airplay: bool = False,
    ) -> tuple[PlayerController, dict[str, MockPlayer]]:
        """
        Build a leader with a natively groupable member and a bridge-only member.

        The leader and its member speak the same (speaker) provider, so they group natively.
        Both also expose a bridge protocol player, which is the only way to reach the third
        member: a native player of the bridge domain.

        :param mock_mass: The mocked MusicAssistant instance.
        :param parent_class: The player class to build the leader from.
        :param with_orphan: Add a second natively groupable member without a bridge protocol.
        :param with_airplay: Also link an AirPlay protocol player to the leader and its member.
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
        if with_airplay:
            airplay_provider = MockProvider(
                "airplay", instance_id="airplay_instance", mass=mock_mass
            )
            for parent, airplay_id, airplay_name in (
                (leader, "airplay_leader", "Living Room (AirPlay)"),
                (member, "airplay_member", "Kitchen (AirPlay)"),
            ):
                airplay = MockPlayer(
                    airplay_provider, airplay_id, airplay_name, player_type=PlayerType.PROTOCOL
                )
                airplay._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
                airplay._cache.clear()
                airplay.set_protocol_parent_id(parent.player_id)
                parent.set_linked_output_protocols(
                    [
                        *parent.linked_output_protocols,
                        LinkedOutputProtocol(
                            output_protocol_id=airplay_id, protocol_domain="airplay", priority=10
                        ),
                    ]
                )
                players[airplay_id] = airplay

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


class TestRegroupOffUnneededProtocol:
    """
    A group that was moved onto a bridge protocol for one member leaves it once that member left.

    The regrouping only happens on a fresh playback start, so running playback is never
    interrupted.
    """

    async def _build_left_on_bridge(
        self, mock_mass: MagicMock, with_airplay: bool = False
    ) -> tuple[PlayerController, dict[str, MockPlayer]]:
        """
        Build two session-bound speakers left grouped on the bridge after the bridge-only member left.

        The bridge-only member joins the native group (moving it onto the bridge) and leaves it
        again, exactly as the grouping commands do it.
        """
        controller, players = TestSessionBoundNativeGrouping()._build_topology(
            mock_mass, SessionBoundMockPlayer, with_airplay=with_airplay
        )
        leader = players["speaker_leader"]
        for player in players.values():
            self._update_state_on_set_members(player)
            player.play_media = AsyncMock()  # type: ignore[method-assign]
        await leader.set_members(player_ids_to_add=["speaker_member"])
        leader.set_active_output_protocol("native")

        await controller._handle_set_members_with_protocols(leader, ["bridge_only"], [])
        await controller._handle_set_members_with_protocols(leader, [], ["bridge_only"])

        assert players["bridge_leader"].group_members == ["bridge_leader", "bridge_member"]
        assert "speaker_member" not in leader.group_members
        return controller, players

    @staticmethod
    def _update_state_on_set_members(player: MockPlayer) -> None:
        """Recalculate the player's state after each member change, as a real provider does."""
        set_members = player.set_members

        async def _set_members(
            player_ids_to_add: list[str] | None = None,
            player_ids_to_remove: list[str] | None = None,
        ) -> None:
            await set_members(player_ids_to_add, player_ids_to_remove)
            player.update_state(signal_event=False)

        player.set_members = _set_members  # type: ignore[method-assign]

    @pytest.mark.parametrize("active_protocol", ["bridge_leader", None])
    async def test_fresh_start_regroups_natively(
        self, mock_mass: MagicMock, active_protocol: str | None
    ) -> None:
        """
        The remaining speakers are grouped natively again and the group plays natively.

        The leader's active protocol may still point at the bridge, or already be cleared
        once it went idle.
        """
        controller, players = await self._build_left_on_bridge(mock_mass)
        leader = players["speaker_leader"]
        leader.set_active_output_protocol(active_protocol)
        media = PlayerMedia(uri="http://test/stream")

        await controller._handle_play_media("speaker_leader", media)

        assert players["bridge_leader"].group_members == ["bridge_leader"]
        assert leader.group_members == ["speaker_leader", "speaker_member"]
        assert players["speaker_member"].active_output_protocol is None
        assert leader.active_output_protocol == "native"
        leader.play_media.assert_awaited_once_with(media)  # type: ignore[attr-defined]
        players["bridge_leader"].play_media.assert_not_awaited()  # type: ignore[attr-defined]

    async def test_group_that_still_needs_the_bridge_is_left_alone(
        self, mock_mass: MagicMock
    ) -> None:
        """A member that can only be reached through the bridge keeps the group on it."""
        controller, players = await self._build_left_on_bridge(mock_mass)
        leader = players["speaker_leader"]
        await controller._handle_set_members_with_protocols(leader, ["bridge_only"], [])
        bridge_members = list(players["bridge_leader"].group_members)
        bridge_set_members = AsyncMock(wraps=players["bridge_leader"].set_members)
        players["bridge_leader"].set_members = bridge_set_members  # type: ignore[method-assign]
        media = PlayerMedia(uri="http://test/stream")

        await controller._handle_play_media("speaker_leader", media)

        bridge_set_members.assert_not_awaited()
        assert players["bridge_leader"].group_members == bridge_members
        assert set(bridge_members) == {"bridge_leader", "bridge_member", "bridge_only"}
        assert "speaker_member" not in leader.group_members
        players["bridge_leader"].play_media.assert_awaited_once_with(media)  # type: ignore[attr-defined]
        leader.play_media.assert_not_awaited()  # type: ignore[attr-defined]

    async def test_leader_that_prefers_the_bridge_keeps_its_group(
        self, mock_mass: MagicMock
    ) -> None:
        """A leader that plays through its preferred bridge keeps its members on that bridge."""
        controller, players = await self._build_left_on_bridge(mock_mass)
        mock_mass.config.get_player_value = MagicMock(
            side_effect=lambda player_id, key, default=None: (
                "bridge_leader"
                if key == CONF_PREFERRED_OUTPUT_PROTOCOL and player_id == "speaker_leader"
                else default
            )
        )
        media = PlayerMedia(uri="http://test/stream")

        await controller._handle_play_media("speaker_leader", media)

        assert players["bridge_leader"].group_members == ["bridge_leader", "bridge_member"]
        players["bridge_leader"].play_media.assert_awaited_once_with(media)  # type: ignore[attr-defined]

    async def test_group_that_prefers_another_protocol_regroups_onto_it(
        self, mock_mass: MagicMock
    ) -> None:
        """
        Speakers that all prefer another protocol are regrouped onto that protocol.

        The leader plays through its preferred protocol, so its members can follow it there.
        """
        controller, players = await self._build_left_on_bridge(mock_mass, with_airplay=True)
        preferred = {"speaker_leader": "airplay_leader", "speaker_member": "airplay_member"}
        mock_mass.config.get_player_value = MagicMock(
            side_effect=lambda player_id, key, default=None: (
                preferred.get(player_id, default)
                if key == CONF_PREFERRED_OUTPUT_PROTOCOL
                else default
            )
        )
        media = PlayerMedia(uri="http://test/stream")

        await controller._handle_play_media("speaker_leader", media)

        assert players["bridge_leader"].group_members == ["bridge_leader"]
        assert players["airplay_leader"].group_members == ["airplay_leader", "airplay_member"]
        assert players["speaker_leader"].active_output_protocol == "airplay_leader"
        players["airplay_leader"].play_media.assert_awaited_once_with(media)  # type: ignore[attr-defined]
        players["bridge_leader"].play_media.assert_not_awaited()  # type: ignore[attr-defined]

    @pytest.mark.parametrize("playback_state", [PlaybackState.PLAYING, PlaybackState.PAUSED])
    async def test_running_playback_is_not_interrupted(
        self, mock_mass: MagicMock, playback_state: PlaybackState
    ) -> None:
        """A group that is playing (or paused) keeps playing through the bridge."""
        controller, players = await self._build_left_on_bridge(mock_mass)
        leader = players["speaker_leader"]
        # the bridge renders the group, so the leader reports its playback state
        players["bridge_leader"]._attr_playback_state = playback_state
        players["bridge_leader"].update_state(signal_event=False)
        leader.refresh_state(signal_event=False)
        assert leader.state.playback_state == playback_state
        media = PlayerMedia(uri="http://test/stream")

        await controller._handle_play_media("speaker_leader", media)

        assert players["bridge_leader"].group_members == ["bridge_leader", "bridge_member"]
        assert "speaker_member" not in leader.group_members
        players["bridge_leader"].play_media.assert_awaited_once_with(media)  # type: ignore[attr-defined]
        leader.play_media.assert_not_awaited()  # type: ignore[attr-defined]


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

    async def test_idle_native_members_migrate_without_stopping_the_parent(
        self, mock_mass: MagicMock
    ) -> None:
        """
        An idle parent hands its native members over too, with no session to stop.

        A native group outlives the session it was formed for, so the members are still
        attached to the parent when it switches protocol. Leaving them behind would keep
        them listed as grouped while nothing feeds them on the next play.
        """
        controller, players = self._build_session_bound_group(mock_mass, PlaybackState.IDLE)
        leader = players["speaker_leader"]
        bridge_leader = players["bridge_leader"]

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

        await controller._forward_protocol_set_members(
            parent_player=leader,
            parent_protocol_player=bridge_leader,
            protocol_members_to_add=["bridge_only"],
            protocol_members_to_remove=[],
        )

        # the native member joins the protocol group and is released from the parent,
        # but an idle parent has no session to stop and nothing to resume
        assert calls == [
            ("bridge_set_members", ["bridge_only", "bridge_member"]),
            ("leader_set_members", ["speaker_member"]),
        ]
        assert leader.active_output_protocol == "bridge_leader"
        controller.cmd_resume.assert_not_awaited()

    def _build_protocol_group(
        self, mock_mass: MagicMock
    ) -> tuple[PlayerController, dict[str, MockPlayer]]:
        """
        Build a leader playing over AirPlay with one AirPlay member and an idle Sendspin player.

        The leader and the member each have an AirPlay output and a Sendspin bridge.

        :param mock_mass: The mocked MusicAssistant instance.
        :return: The controller and its players by ID.
        """
        controller = PlayerController(mock_mass)
        sonos_provider = MockProvider("sonos", instance_id="sonos", mass=mock_mass)
        wiim_provider = MockProvider("wiim", instance_id="wiim", mass=mock_mass)
        airplay_provider = MockProvider("airplay", instance_id="airplay", mass=mock_mass)
        sendspin_provider = MockProvider("sendspin", instance_id="sendspin", mass=mock_mass)

        leader = MockPlayer(sonos_provider, "leader", "Living Room")
        leader._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        member = MockPlayer(wiim_provider, "member", "Kitchen")
        joiner = MockPlayer(sendspin_provider, "joiner", "Bedroom")
        joiner._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        players = [leader, member, joiner]
        for parent in (leader, member):
            protocol_players = (
                MockPlayer(
                    airplay_provider,
                    f"{parent.player_id}_airplay",
                    parent.display_name,
                    player_type=PlayerType.PROTOCOL,
                ),
                MockPlayer(
                    sendspin_provider,
                    f"{parent.player_id}_bridge",
                    parent.display_name,
                    player_type=PlayerType.PROTOCOL,
                ),
            )
            for protocol_player in protocol_players:
                protocol_player._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
                protocol_player.set_protocol_parent_id(parent.player_id)
            parent.set_linked_output_protocols(
                [
                    LinkedOutputProtocol(
                        output_protocol_id=f"{parent.player_id}_airplay",
                        protocol_domain="airplay",
                        priority=10,
                    ),
                    LinkedOutputProtocol(
                        output_protocol_id=f"{parent.player_id}_bridge",
                        protocol_domain="sendspin",
                        priority=40,
                    ),
                ]
            )
            players.extend(protocol_players)

        mock_mass.players = controller
        controller._players = {player.player_id: player for player in players}
        leader_airplay = controller._players["leader_airplay"]
        leader_airplay._attr_group_members = ["leader_airplay", "member_airplay"]
        leader_airplay._attr_playback_state = PlaybackState.PLAYING
        for player in players:
            player.update_state(signal_event=False)
        leader.set_active_output_protocol("leader_airplay")
        member.set_active_output_protocol("member_airplay")
        return controller, {player.player_id: player for player in players}

    async def test_resume_after_switch_plays_through_the_new_protocol(
        self, mock_mass: MagicMock
    ) -> None:
        """
        The resume after a protocol switch plays through the protocol that took over.

        The members of the stopped protocol group are re-added on the new protocol after the
        resume, so the stopped group must no longer count as grouped when the resume picks
        the output.
        """
        controller, players = self._build_protocol_group(mock_mass)
        leader = players["leader"]

        selected_on_resume: list[str] = []

        async def record_resume(player_id: str) -> None:
            for player in players.values():
                player.update_state(signal_event=False)
            selected_on_resume.append(
                controller._select_best_output_protocol(controller._players[player_id])[0].player_id
            )

        controller._handle_cmd_stop = AsyncMock()  # type: ignore[method-assign]
        controller.cmd_resume = AsyncMock(side_effect=record_resume)  # type: ignore[method-assign]
        controller._handle_set_members = AsyncMock()  # type: ignore[method-assign]

        await controller._forward_protocol_set_members(
            parent_player=leader,
            parent_protocol_player=controller._players["leader_bridge"],
            protocol_members_to_add=["joiner"],
            protocol_members_to_remove=[],
        )

        assert selected_on_resume == ["leader_bridge"]

    async def test_switch_that_would_strand_a_member_is_refused(self, mock_mass: MagicMock) -> None:
        """
        A player whose protocol a grouped member cannot follow is not added to the group.

        The re-added member would move the group back onto a protocol the new player cannot
        join, which hands the output back and forth without end.
        """
        controller, players = self._build_protocol_group(mock_mass)
        players["member_bridge"]._attr_available = False
        for player in players.values():
            player.update_state(signal_event=False)
        controller._handle_cmd_stop = AsyncMock()  # type: ignore[method-assign]
        controller.cmd_resume = AsyncMock()  # type: ignore[method-assign]
        controller._handle_set_members = AsyncMock()  # type: ignore[method-assign]

        with pytest.raises(PlayerCommandFailed) as err:
            await controller._forward_protocol_set_members(
                parent_player=players["leader"],
                parent_protocol_player=players["leader_bridge"],
                protocol_members_to_add=["joiner"],
                protocol_members_to_remove=[],
            )

        assert (
            err.value.translation_key,
            players["leader"].active_output_protocol,
            players["leader_bridge"].group_members,
            controller._handle_cmd_stop.await_count,
        ) == ("member_cannot_follow_protocol", "leader_airplay", [], 0)

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

    async def test_leaving_member_releases_its_protocol(self, mock_mass: MagicMock) -> None:
        """A member that leaves a protocol group reports playback started on the device again."""
        controller = PlayerController(mock_mass)
        sendspin_provider = MockProvider("sendspin", instance_id="sendspin", mass=mock_mass)
        sonos_provider = MockProvider("sonos", instance_id="sonos", mass=mock_mass)

        leader = MockPlayer(sendspin_provider, "leader", "LedFx")
        leader_sendspin = MockPlayer(
            sendspin_provider, "leader_sendspin", "LedFx", player_type=PlayerType.PROTOCOL
        )
        leader_sendspin._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        leader_sendspin.set_protocol_parent_id("leader")

        member = MockPlayer(sonos_provider, "member", "Living Room")
        member_bridge = MockPlayer(
            sendspin_provider,
            "member_bridge",
            "Living Room (Sendspin)",
            player_type=PlayerType.PROTOCOL,
        )
        member_bridge.set_protocol_parent_id("member")
        member.set_linked_output_protocols(
            [
                LinkedOutputProtocol(
                    output_protocol_id="member_bridge", protocol_domain="sendspin", priority=40
                )
            ]
        )

        mock_mass.players = controller
        controller._players = {
            player.player_id: player for player in (leader, leader_sendspin, member, member_bridge)
        }
        for player in controller._players.values():
            player.update_state(signal_event=False)

        await controller._forward_protocol_set_members(
            parent_player=leader,
            parent_protocol_player=leader_sendspin,
            protocol_members_to_add=["member_bridge"],
            protocol_members_to_remove=[],
        )
        joined_protocol = member.active_output_protocol

        await controller._forward_protocol_set_members(
            parent_player=leader,
            parent_protocol_player=leader_sendspin,
            protocol_members_to_add=[],
            protocol_members_to_remove=["member_bridge"],
        )
        assert (joined_protocol, member.active_output_protocol) == ("member_bridge", None)

        member._attr_playback_state = PlaybackState.PLAYING
        member._attr_active_source = "Amazon Music"
        member.update_state(signal_event=False)

        assert member.state.playback_state == PlaybackState.PLAYING
        assert member.state.active_source == "Amazon Music"


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
    """Tests for the translation of protocol player IDs in the published group members."""

    def test_final_group_members_translates_protocol_ids(self, mock_mass: MagicMock) -> None:
        """
        Test that the published group members translate protocol player IDs to visible IDs.

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
    """Tests for the published active group with translated protocol player IDs."""

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
    """Tests for the published synced_to when sync parent is a native protocol player."""

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


class _LockedMockPlayer(MockPlayer):
    """A player that keeps its group read-only (e.g. an externally-created mixed group)."""

    @property
    def grouping_locked(self) -> bool:
        """Suppress grouping in the exposed state."""
        return True


class TestGroupingLockedPolicy:
    """A grouping-locked player withholds SET_MEMBERS and group targets in its final state."""

    def test_locked_player_withholds_set_members(self, mock_mass: MagicMock) -> None:
        """A locked player drops SET_MEMBERS from its exposed features."""
        provider = MockProvider("wiim", instance_id="wiim_instance", mass=mock_mass)
        player = _LockedMockPlayer(provider, "shell_1", "Shell")
        player._attr_supported_features = {PlayerFeature.SET_MEMBERS, PlayerFeature.VOLUME_SET}
        player._cache.clear()
        player.update_state(signal_event=False)
        assert PlayerFeature.SET_MEMBERS not in player.state.supported_features
        assert PlayerFeature.VOLUME_SET in player.state.supported_features

    def test_locked_player_withholds_protocol_derived_set_members(
        self, mock_mass: MagicMock
    ) -> None:
        """Even a linked protocol player's SET_MEMBERS is suppressed while locked."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("wiim", instance_id="wiim_instance", mass=mock_mass)
        player = _LockedMockPlayer(provider, "shell_1", "Shell")
        player._attr_supported_features = set()  # no native grouping of its own
        protocol = MockPlayer(provider, "airplay_p", "P (AirPlay)", player_type=PlayerType.PROTOCOL)
        protocol._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        protocol._cache.clear()
        protocol.set_protocol_parent_id("shell_1")
        player.set_linked_output_protocols(
            [
                LinkedOutputProtocol(
                    output_protocol_id="airplay_p", protocol_domain="airplay", priority=10
                )
            ]
        )
        mock_mass.players = controller
        controller._players = {"shell_1": player, "airplay_p": protocol}
        protocol.refresh_state(signal_event=False)
        player.refresh_state(signal_event=False)
        assert PlayerFeature.SET_MEMBERS not in player.state.supported_features

    def test_locked_player_offers_no_group_targets(self, mock_mass: MagicMock) -> None:
        """A locked player exposes no group targets, even an otherwise groupable peer."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("wiim", instance_id="wiim_instance", mass=mock_mass)
        player = _LockedMockPlayer(provider, "shell_1", "Shell")
        player._attr_supported_features = {PlayerFeature.SET_MEMBERS}
        player._attr_can_group_with = {"peer_1"}
        peer = MockPlayer(provider, "peer_1", "Peer")
        mock_mass.players = controller
        controller._players = {"shell_1": player, "peer_1": peer}
        player._cache.clear()
        peer.refresh_state(signal_event=False)
        player.refresh_state(signal_event=False)
        assert player.state.can_group_with == set()

    def test_unlocked_player_keeps_grouping(self, mock_mass: MagicMock) -> None:
        """A normal (unlocked) player keeps SET_MEMBERS and its group targets."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("wiim", instance_id="wiim_instance", mass=mock_mass)
        player = MockPlayer(provider, "shell_2", "Shell2")
        player._attr_supported_features = {PlayerFeature.SET_MEMBERS}
        player._attr_can_group_with = {"peer_2"}
        peer = MockPlayer(provider, "peer_2", "Peer2")
        mock_mass.players = controller
        controller._players = {"shell_2": player, "peer_2": peer}
        player._cache.clear()
        peer.refresh_state(signal_event=False)
        player.refresh_state(signal_event=False)
        assert PlayerFeature.SET_MEMBERS in player.state.supported_features
        assert "peer_2" in player.state.can_group_with

    def test_locked_peer_excluded_from_group_targets(self, mock_mass: MagicMock) -> None:
        """An unlocked leader drops a locked peer but keeps an unlocked one as a target."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("wiim", instance_id="wiim_instance", mass=mock_mass)
        leader = MockPlayer(provider, "shell_1", "Shell")
        leader._attr_supported_features = {PlayerFeature.SET_MEMBERS}
        leader._attr_can_group_with = {"locked_peer", "open_peer"}
        locked_peer = _LockedMockPlayer(provider, "locked_peer", "Locked Peer")
        open_peer = MockPlayer(provider, "open_peer", "Open Peer")
        mock_mass.players = controller
        controller._players = {
            "shell_1": leader,
            "locked_peer": locked_peer,
            "open_peer": open_peer,
        }
        leader._cache.clear()
        for player in (locked_peer, open_peer, leader):
            player.refresh_state(signal_event=False)
        # the locked peer keeps its own group read-only, so it is not offered as a target,
        # while the ordinary peer still is
        assert "locked_peer" not in leader.state.can_group_with
        assert "open_peer" in leader.state.can_group_with

    def test_linked_protocol_cannot_reintroduce_locked_member(self, mock_mass: MagicMock) -> None:
        """A locked member offered by a linked protocol is still kept out of the targets."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("wiim", instance_id="wiim_instance", mass=mock_mass)
        leader = MockPlayer(provider, "shell_1", "Shell")
        leader._attr_supported_features = set()  # no native grouping of its own
        protocol = MockPlayer(provider, "airplay_p", "P (AirPlay)", player_type=PlayerType.PROTOCOL)
        protocol._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        protocol._attr_can_group_with = {"locked_peer", "open_peer"}
        protocol._cache.clear()
        protocol.set_protocol_parent_id("shell_1")
        leader.set_linked_output_protocols(
            [
                LinkedOutputProtocol(
                    output_protocol_id="airplay_p", protocol_domain="airplay", priority=10
                )
            ]
        )
        locked_peer = _LockedMockPlayer(provider, "locked_peer", "Locked Peer")
        open_peer = MockPlayer(provider, "open_peer", "Open Peer")
        mock_mass.players = controller
        controller._players = {
            "shell_1": leader,
            "airplay_p": protocol,
            "locked_peer": locked_peer,
            "open_peer": open_peer,
        }
        for player in (locked_peer, open_peer, protocol):
            player.refresh_state(signal_event=False)
        leader._cache.clear()
        leader.refresh_state(signal_event=False)
        # the linked protocol lists the locked peer, but the final filter still excludes it
        assert "locked_peer" not in leader.state.can_group_with
        assert "open_peer" in leader.state.can_group_with


class _NativeGroupingMockPlayer(MockPlayer):
    """A player that runs its own multiroom and prefers native grouping."""

    @property
    def prefer_native_grouping(self) -> bool:
        """Prefer native grouping over any linked protocol."""
        return True


class _RejectsNativeGroupingMockPlayer(MockPlayer):
    """A parent that never accepts another player as a native group peer."""

    def is_native_group_compatible(self, other: Player) -> bool:
        """Reject every peer as native-incompatible (e.g. a cross-backend device)."""
        return False


class TestPreferNativeGrouping:
    """A player that prefers native grouping uses it before its preferred output protocol."""

    def test_prefer_native_uses_native_before_child_preferred(self, mock_mass: MagicMock) -> None:
        """A prefer-native child groups natively even with an explicit protocol preference."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("wiim", instance_id="wiim_instance", mass=mock_mass)
        parent = MockPlayer(provider, "shell_parent", "Parent")
        child = _NativeGroupingMockPlayer(provider, "shell_child", "Child")
        # an explicit child output-protocol preference that would otherwise win
        mock_mass.config.get_player_value = MagicMock(return_value="airplay_child")
        mock_mass.players = controller
        controller._players = {"shell_parent": parent, "shell_child": child}
        native_members: list[str] = []
        protocol_members: list[str] = []
        controller._select_grouping_for_member(
            child, parent, None, None, True, protocol_members, native_members
        )
        assert native_members == ["shell_child"]
        assert protocol_members == []

    def test_prefer_native_falls_through_same_instance_not_native_peer(
        self, mock_mass: MagicMock
    ) -> None:
        """A same-instance pair the parent rejects as a native peer is not forced native."""
        controller = PlayerController(mock_mass)
        # official + generic LinkPlay share one wiim provider instance, but a cross-backend
        # peer is not native-group compatible, so the seam must not force native grouping.
        provider = MockProvider("wiim", instance_id="wiim_instance", mass=mock_mass)
        parent = _RejectsNativeGroupingMockPlayer(provider, "shell_parent", "Parent")
        child = _NativeGroupingMockPlayer(provider, "shell_child", "Child")
        mock_mass.config.get_player_value = MagicMock(return_value=None)
        mock_mass.players = controller
        controller._players = {"shell_parent": parent, "shell_child": child}
        native_members: list[str] = []
        protocol_members: list[str] = []
        controller._select_grouping_for_member(
            child, parent, None, None, True, protocol_members, native_members
        )
        assert native_members == []

    def test_prefer_native_falls_through_when_incompatible(self, mock_mass: MagicMock) -> None:
        """When native grouping is not possible (cross-backend), the seam does not force it."""
        controller = PlayerController(mock_mass)
        parent_provider = MockProvider("wiim", instance_id="wiim_instance", mass=mock_mass)
        child_provider = MockProvider("other", instance_id="other_instance", mass=mock_mass)
        parent = MockPlayer(parent_provider, "shell_parent", "Parent")
        parent._attr_can_group_with = set()  # does not accept the cross-backend child
        child = _NativeGroupingMockPlayer(child_provider, "other_child", "Child")
        mock_mass.config.get_player_value = MagicMock(return_value=None)
        mock_mass.players = controller
        controller._players = {"shell_parent": parent, "other_child": child}
        native_members: list[str] = []
        protocol_members: list[str] = []
        controller._select_grouping_for_member(
            child, parent, None, None, True, protocol_members, native_members
        )
        # the preferred-native seam did not force a cross-backend native group
        assert native_members == []

    def test_default_player_keeps_protocol_first_ordering(self, mock_mass: MagicMock) -> None:
        """An ordinary player (no native preference) is unaffected by the seam."""
        controller = PlayerController(mock_mass)
        provider = MockProvider("wiim", instance_id="wiim_instance", mass=mock_mass)
        parent = MockPlayer(provider, "shell_parent", "Parent")
        child = MockPlayer(provider, "plain_child", "Child")
        assert child.prefer_native_grouping is False
        mock_mass.config.get_player_value = MagicMock(return_value=None)
        mock_mass.players = controller
        controller._players = {"shell_parent": parent, "plain_child": child}
        native_members: list[str] = []
        protocol_members: list[str] = []
        controller._select_grouping_for_member(
            child, parent, None, None, True, protocol_members, native_members
        )
        # with no preferred/active/common protocol, the default player still reaches
        # native grouping via the normal priority (Priority 3), not the preferred seam
        assert native_members == ["plain_child"]


def _live_source(item_id: str, provider: str = "spotify_connect") -> AudioSource:
    """Build the AudioSource a live session publishes on a player."""
    return AudioSource(
        item_id=item_id,
        provider=provider,
        name=f"Live source ({item_id})",
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain=provider, provider_instance=provider)
        },
    )


class TestCanGroupWithExternalSource:
    """Grouping candidates while something other than the player's queue is playing."""

    @staticmethod
    def _build_rig(mock_mass: MagicMock) -> tuple[PlayerController, MockPlayer]:
        """
        Wire a player with one native peer and one reachable only over AirPlay.

        Returns the controller and the player whose grouping candidates are asserted on.
        """
        controller = PlayerController(mock_mass)
        sonos_provider = MockProvider("sonos", instance_id="sonos_instance", mass=mock_mass)
        airplay_provider = MockProvider("airplay", instance_id="airplay_instance", mass=mock_mass)

        sonos_player = MockPlayer(
            sonos_provider,
            "sonos_123",
            "Living Room",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:01"},
        )
        sonos_player._attr_supported_features.add(PlayerFeature.PLAY_MEDIA)
        sonos_player._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        sonos_player._attr_can_group_with = {"sonos_456"}
        sonos_player_b = MockPlayer(
            sonos_provider,
            "sonos_456",
            "Kitchen",
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:02"},
        )
        sonos_airplay = MockPlayer(
            airplay_provider,
            "airplay_sonos",
            "Living Room (AirPlay)",
            player_type=PlayerType.PROTOCOL,
            identifiers={IdentifierType.MAC_ADDRESS: "AA:BB:CC:DD:EE:01"},
        )
        sonos_airplay._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        sonos_airplay._attr_can_group_with = {"airplay_other"}
        sonos_airplay.set_protocol_parent_id("sonos_123")

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
        airplay_other.set_protocol_parent_id("wiim_789")

        for player, protocol_id in (
            (sonos_player, "airplay_sonos"),
            (wiim_player, "airplay_other"),
        ):
            player.set_linked_output_protocols(
                [
                    LinkedOutputProtocol(
                        output_protocol_id=protocol_id,
                        protocol_domain="airplay",
                        priority=10,
                    )
                ]
            )

        mock_mass.players = controller
        # nothing here is a registered queue, so the external-source check is reachable
        mock_mass.player_queues.get = MagicMock(return_value=None)
        controller._players = {
            "sonos_123": sonos_player,
            "sonos_456": sonos_player_b,
            "wiim_789": wiim_player,
            "airplay_sonos": sonos_airplay,
            "airplay_other": airplay_other,
        }
        for registered in controller._players.values():
            registered._cache.clear()
        sonos_airplay.refresh_state(signal_event=False)
        airplay_other.refresh_state(signal_event=False)
        sonos_player.refresh_state(signal_event=False)
        sonos_player_b.refresh_state(signal_event=False)
        wiim_player.refresh_state(signal_event=False)
        return controller, sonos_player

    def test_live_audio_source_keeps_protocol_candidates(self, mock_mass: MagicMock) -> None:
        """A source MA streams itself (e.g. Spotify Connect) can still be grouped."""
        controller, sonos_player = self._build_rig(mock_mass)
        controller._start_audio_source_session(
            "sonos_123", _live_source("sonos_123"), "spotify_connect"
        )
        sonos_player.refresh_state(signal_event=False)

        assert sonos_player.state.active_source == "spotify_connect://audio_source/sonos_123"
        # MA produces the audio, so both the native peer and the one reachable
        # only over AirPlay can be added to it
        assert sonos_player.state.can_group_with == {"sonos_456", "wiim_789"}

    def test_device_own_external_source_hides_protocol_candidates(
        self, mock_mass: MagicMock
    ) -> None:
        """The device can still group its own line-in natively, just not over a protocol."""
        controller, sonos_player = self._build_rig(mock_mass)
        # a live source elsewhere says nothing about what this player is playing
        controller._start_audio_source_session("wiim_789", _live_source("wiim_789"), "vban")
        sonos_player._attr_active_source = "tv"
        sonos_player._attr_playback_state = PlaybackState.PLAYING
        sonos_player.refresh_state(signal_event=False)

        assert sonos_player.state.active_source == "tv"
        # the device distributes its own input to its own kind, but MA cannot put
        # audio it never receives onto an AirPlay/Sendspin protocol player
        assert sonos_player.state.can_group_with == {"sonos_456"}


class TestExternalSourceTakeover:
    """Whether a source change while a protocol renders the audio counts as a takeover."""

    @staticmethod
    def _playing_over_airplay(
        mock_mass: MagicMock,
    ) -> tuple[PlayerController, MockPlayer]:
        """Wire a player rendering through its AirPlay protocol player."""
        controller, sonos_player = TestCanGroupWithExternalSource._build_rig(mock_mass)
        sonos_player._attr_playback_state = PlaybackState.PLAYING
        sonos_player.set_active_output_protocol("airplay_sonos")
        sonos_player.refresh_state(signal_event=False)
        return controller, sonos_player

    def test_live_audio_source_is_not_a_takeover(self, mock_mass: MagicMock) -> None:
        """MA putting a live source on the player must not tear down its protocol group."""
        controller, sonos_player = self._playing_over_airplay(mock_mass)
        controller._start_audio_source_session(
            "sonos_123", _live_source("sonos_123"), "spotify_connect"
        )
        sonos_player.refresh_state(signal_event=False)

        controller._check_external_source_takeover(sonos_player)

        assert sonos_player.active_output_protocol == "airplay_sonos"

    def test_device_own_external_source_stays_external(self, mock_mass: MagicMock) -> None:
        """A source MA does not produce is still classified as external."""
        # checked on the classifier directly: while an output protocol renders the audio,
        # the published active source overrules what the device reports, so a device-local
        # source cannot be driven through _check_external_source_takeover at all
        controller, sonos_player = TestCanGroupWithExternalSource._build_rig(mock_mass)

        assert not controller._is_ma_managed_source(sonos_player, "tv")
