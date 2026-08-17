"""Tests for the generic LinkPlay grouping/identity shell of the WiiM provider."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import (
    IdentifierType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
)
from pywiim import WiiMError

from music_assistant.controllers.players.protocol_linking import ProtocolLinkingMixin
from music_assistant.models.player import LinkedOutputProtocol
from music_assistant.providers.wiim.constants import PLAYER_ID_PREFIX
from music_assistant.providers.wiim.grouping import NativeGroupRole
from music_assistant.providers.wiim.helpers import (
    is_official_manufacturer,
    linkplay_group_compatible,
    linkplay_slave_uuid_to_player_id,
    linkplay_slave_uuid_to_udn,
)
from music_assistant.providers.wiim.linkplay_player import LinkPlayPlayer
from music_assistant.providers.wiim.provider import WiimProvider

if TYPE_CHECKING:
    from pywiim.models import DeviceInfo as PywiimDeviceInfo

# Verified Edifier MS50A identity used across the tests.
EDIFIER_HTTP_UUID = "FF97F002783E65056579F15F"
EDIFIER_UDN = "uuid:FF97F002-783E-6505-6579-F15FFF97F002"
EDIFIER_PLAYER_ID = f"{PLAYER_ID_PREFIX}{EDIFIER_UDN}"

# A second generic device, used for same-backend grouping tests.
PEER_HTTP_UUID = "AA11BB22CC33DD44EE55FF66"
PEER_UDN = "uuid:AA11BB22-CC33-DD44-EE55-FF66AA11BB22"
PEER_PLAYER_ID = f"{PLAYER_ID_PREFIX}{PEER_UDN}"


class TestIdentityNormalization:
    """The 24-char HTTP UUID must map deterministically to the UPnP UDN/player id."""

    def test_http_uuid_to_udn_matches_hardware(self) -> None:
        """The Edifier HTTP UUID resolves to its verified UPnP UDN."""
        assert linkplay_slave_uuid_to_udn(EDIFIER_HTTP_UUID) == EDIFIER_UDN

    def test_http_uuid_to_player_id(self) -> None:
        """The player id is the prefixed UDN."""
        assert linkplay_slave_uuid_to_player_id(EDIFIER_HTTP_UUID) == EDIFIER_PLAYER_ID

    def test_lowercase_input_normalizes_to_canonical_udn(self) -> None:
        """Case does not matter; the canonical UDN is uppercase."""
        assert linkplay_slave_uuid_to_udn(EDIFIER_HTTP_UUID.lower()) == EDIFIER_UDN

    @pytest.mark.parametrize(
        "value",
        [
            EDIFIER_UDN,
            EDIFIER_UDN.removeprefix("uuid:"),
            EDIFIER_UDN.removeprefix("uuid:").replace("-", ""),
            EDIFIER_UDN.lower(),
        ],
    )
    def test_full_udn_forms_normalize_to_canonical(self, value: str) -> None:
        """Slaves that report a full 32-hex UDN (any form) resolve to the same UDN."""
        assert linkplay_slave_uuid_to_udn(value) == EDIFIER_UDN
        assert linkplay_slave_uuid_to_player_id(value) == EDIFIER_PLAYER_ID

    @pytest.mark.parametrize(
        "value", ["", "tooshort", "ZZ97F002783E65056579F15F", "1" * 30, "1" * 40]
    )
    def test_invalid_uuid_returns_none(self, value: str) -> None:
        """Input that is neither a 24-hex HTTP UUID nor a 32-hex UDN is rejected."""
        assert linkplay_slave_uuid_to_udn(value) is None
        assert linkplay_slave_uuid_to_player_id(value) is None


class TestManufacturerClassification:
    """Only WiiM/Audio Pro manufacturers select the official backend."""

    @pytest.mark.parametrize("manufacturer", ["Linkplay", "linkplay technology", "Audio Pro AB"])
    def test_official_manufacturers(self, manufacturer: str) -> None:
        """Official manufacturers are recognised (mirrors the official SDK)."""
        assert is_official_manufacturer(manufacturer) is True

    @pytest.mark.parametrize("manufacturer", ["Edifier Inc", "", None, "Arylic", "WiiM"])
    def test_generic_manufacturers(self, manufacturer: str | None) -> None:
        """Everything else is treated as generic LinkPlay."""
        assert is_official_manufacturer(manufacturer) is False


def _slaves(uuids: list[str]) -> list[dict[str, str]]:
    """Build a pywiim get_slaves_info()-style slave list."""
    return [{"uuid": uuid, "ip": f"10.0.0.{index + 2}"} for index, uuid in enumerate(uuids)]


def _device_info(wmrm_version: str = "4.2", *, legacy: bool = False) -> PywiimDeviceInfo:
    """Build a device-info stand-in; legacy=True marks it as a Wi-Fi-Direct device."""
    return cast(
        "PywiimDeviceInfo",
        SimpleNamespace(wmrm_version=wmrm_version, needs_wifi_direct_multiroom=legacy),
    )


@pytest.fixture
def mock_provider() -> MagicMock:
    """Create a mock WiimProvider suitable for constructing players."""
    provider = MagicMock()
    provider.instance_id = "wiim_test"
    provider.domain = "wiim"
    provider.players = []
    provider.native_groups = _mock_native_groups()
    provider.mass = MagicMock()
    provider.mass.players = MagicMock()
    config = MagicMock()
    config.name = None
    config.default_name = "Edifier MS50A"
    config.enabled = True
    config.player_type = None
    config.get_value = MagicMock(return_value=None)
    provider.mass.config.get_base_player_config.return_value = config
    # used by the core final-state calculation to resolve power/volume controls
    provider.mass.config.get_raw_player_config_value = MagicMock(
        side_effect=lambda _player_id, _key, default=None: default
    )
    return provider


def _mock_native_groups() -> MagicMock:
    """Create a coordinator mock that reports a standalone topology by default."""
    groups = MagicMock()
    groups.role_of.return_value = NativeGroupRole.STANDALONE
    groups.members_of.return_value = []
    groups.can_group_with.return_value = set()
    groups.refresh_leader = AsyncMock()
    groups.reconcile = AsyncMock()
    groups.set_members = AsyncMock()
    groups.schedule_reconcile = MagicMock()
    groups.unregister = MagicMock()
    groups.is_unknown_leader_follower = MagicMock(return_value=False)
    groups.set_self_role = MagicMock(return_value=False)
    return groups


@pytest.fixture
def mock_client() -> MagicMock:
    """Create a mock low-level WiiMClient with the GroupAPI methods the shell uses."""
    client = MagicMock()
    client.host = "192.168.1.50"
    client.get_device_info_model = AsyncMock(
        return_value=SimpleNamespace(
            uuid=EDIFIER_HTTP_UUID, wmrm_version="4.2", firmware="Linkplay.4.6.430230"
        )
    )
    client.get_slaves_info = AsyncMock(return_value=_slaves([]))
    client.get_device_group_info = AsyncMock(return_value=SimpleNamespace(role="solo"))
    client.capabilities = {}
    client.join_slave = AsyncMock()
    client.leave_group = AsyncMock()
    return client


@pytest.fixture
def mock_upnp_device() -> MagicMock:
    """Create a mock async-upnp-client UpnpDevice."""
    device = MagicMock()
    device.manufacturer = "Edifier Inc"
    device.model_name = "Edifier MS50A"
    device.friendly_name = "Edifier MS50A"
    device.udn = EDIFIER_UDN
    return device


def _make_shell(
    provider: MagicMock,
    client: MagicMock,
    upnp_device: MagicMock,
    player_id: str = EDIFIER_PLAYER_ID,
) -> LinkPlayPlayer:
    player = LinkPlayPlayer(
        provider=provider,
        player_id=player_id,
        client=client,
        upnp_device=upnp_device,
        description_url="http://192.168.1.50:49152/description.xml",
        mac_address="AA:BB:CC:DD:EE:FF",
    )
    player.update_state = MagicMock()  # type: ignore[misc,method-assign]
    # a modern, router-based device so compatibility checks pass by default
    player._cached_device_info = _device_info("4.2")
    return player


class TestShellConstruction:
    """The shell exposes device identity and a grouping-only native capability."""

    def test_is_a_native_player(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """The shell is a native PlayerType.PLAYER so the controller can link protocols."""
        player = _make_shell(mock_provider, mock_client, mock_upnp_device)
        assert player._attr_type == PlayerType.PLAYER

    def test_prefers_dlna_default_output(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """The shell steers automatic output selection at its linked DLNA protocol."""
        player = _make_shell(mock_provider, mock_client, mock_upnp_device)
        assert player.default_output_protocol_domain == "dlna"

    def test_device_info_identifiers(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """UUID/IP/MAC identifiers are set so DLNA/AirPlay children link by identity."""
        player = _make_shell(mock_provider, mock_client, mock_upnp_device)
        identifiers = player.device_info.identifiers
        assert identifiers[IdentifierType.UUID] == EDIFIER_UDN.removeprefix("uuid:")
        assert identifiers[IdentifierType.IP_ADDRESS] == "192.168.1.50"
        assert identifiers[IdentifierType.MAC_ADDRESS] == "AA:BB:CC:DD:EE:FF"

    def test_has_no_native_playback(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """The shell must not implement native playback: no play_media, no PLAY_MEDIA."""
        player = _make_shell(mock_provider, mock_client, mock_upnp_device)
        # play_media is only defined on the base Player as the default reject stub, never
        # overridden here, so the shell exposes no native PLAY_MEDIA feature.
        assert "play_media" not in LinkPlayPlayer.__dict__
        assert PlayerFeature.PLAY_MEDIA not in player._attr_supported_features
        # no eventing/state-machine internals leaked from the old backend
        for leaked in ("_push_state", "_connect_eventing", "_refresh_state", "_dmr_device"):
            assert not hasattr(player, leaked)


class TestHealthGating:
    """Native grouping is only offered while the LinkPlay HTTP API is reachable."""

    def test_set_members_feature_requires_health(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """SET_MEMBERS is withdrawn when the LinkPlay API is unreachable."""
        player = _make_shell(mock_provider, mock_client, mock_upnp_device)
        player._linkplay_available = True
        assert PlayerFeature.SET_MEMBERS in player.supported_features
        player._linkplay_available = False
        assert PlayerFeature.SET_MEMBERS not in player.supported_features

    def test_prefers_native_grouping_when_healthy(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A reachable shell prefers native LinkPlay grouping over a linked protocol."""
        player = _make_shell(mock_provider, mock_client, mock_upnp_device)
        player._linkplay_available = True
        assert player.prefer_native_grouping is True
        player._linkplay_available = False
        assert player.prefer_native_grouping is False

    def test_can_group_with_delegates_to_coordinator(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """can_group_with returns exactly the coordinator's candidate set for this player."""
        player = _make_shell(mock_provider, mock_client, mock_upnp_device)
        mock_provider.native_groups.can_group_with.return_value = {PEER_PLAYER_ID}
        assert player.can_group_with == {PEER_PLAYER_ID}
        mock_provider.native_groups.can_group_with.assert_called_once_with(player)

    def test_native_available_reflects_health(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """native_available (used by the coordinator) tracks the LinkPlay API reachability."""
        healthy = _make_shell(mock_provider, mock_client, mock_upnp_device)
        healthy._linkplay_available = True
        assert healthy.native_available is True
        unhealthy = _make_shell(mock_provider, mock_client, mock_upnp_device)
        unhealthy._linkplay_available = False
        assert unhealthy.native_available is False

    def test_native_group_compat_follows_can_group_with(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A peer is native-compatible exactly when the coordinator offers it as a candidate."""
        player = _make_shell(mock_provider, mock_client, mock_upnp_device)
        peer = MagicMock(player_id=PEER_PLAYER_ID)
        mock_provider.native_groups.can_group_with.return_value = {PEER_PLAYER_ID}
        assert player.is_native_group_compatible(peer) is True
        other = MagicMock(player_id="wiim_uuid:not-a-candidate")
        assert player.is_native_group_compatible(other) is False


class TestPlaybackAvailability:
    """Playback availability derives from linked protocols, not the LinkPlay API (B2)."""

    def test_available_from_linked_protocol_even_when_api_unhealthy(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A reachable linked DLNA keeps the shell available even if the LinkPlay API is down."""
        player = _make_shell(mock_provider, mock_client, mock_upnp_device)
        player._linkplay_available = False
        player.set_linked_output_protocols([LinkedOutputProtocol("dlna_x", "dlna", priority=50)])
        protocol_player = MagicMock(available_for_playback=True)
        mock_provider.mass.players.get_player.return_value = protocol_player
        assert player.available is True

    def test_unavailable_without_linked_protocols(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """With no linked protocol players there is nothing to play through."""
        player = _make_shell(mock_provider, mock_client, mock_upnp_device)
        assert player.available is False

    def test_backing_ids_derive_from_linked_protocols(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """The backing protocol ids come from the linked output protocols."""
        player = _make_shell(mock_provider, mock_client, mock_upnp_device)
        player.set_linked_output_protocols(
            [
                LinkedOutputProtocol("dlna_x", "dlna", priority=50),
                LinkedOutputProtocol("ap_x", "airplay", priority=10),
            ]
        )
        assert player._backing_protocol_player_ids() == ["dlna_x", "ap_x"]


class TestGrouping:
    """Grouping is delegated to the shared native coordinator across both backends."""

    async def test_set_members_delegates_to_coordinator(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """set_members forwards the add/remove batch to the coordinator unchanged."""
        player = _make_shell(mock_provider, mock_client, mock_upnp_device)
        await player.set_members(
            player_ids_to_add=[PEER_PLAYER_ID], player_ids_to_remove=["wiim_uuid:gone"]
        )
        mock_provider.native_groups.set_members.assert_awaited_once_with(
            player, [PEER_PLAYER_ID], ["wiim_uuid:gone"]
        )

    def test_api_unreachable_gates_only_native_grouping(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """An unreachable LinkPlay API withdraws only native grouping, not the broad lock."""
        player = _make_shell(mock_provider, mock_client, mock_upnp_device)
        player._linkplay_available = False
        # the broad lock stays off, so core may still group this device via a linked protocol
        assert player.grouping_locked is False
        # but native grouping is gated: the raw SET_MEMBERS is withdrawn ...
        assert PlayerFeature.SET_MEMBERS not in player.supported_features
        # ... and the coordinator offers no native peers for an unreachable device
        assert player.can_group_with == set()

    def test_unknown_leader_follower_locks_grouping(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A reachable shell that follows an undiscovered group withdraws ALL grouping."""
        player = _make_shell(mock_provider, mock_client, mock_upnp_device)
        player._linkplay_available = True
        mock_provider.native_groups.is_unknown_leader_follower.return_value = True
        # the broad lock holds, so even a linked-protocol group is withdrawn in the final state
        assert player.grouping_locked is True

    def test_grouping_rebuild_lock_serializes_with_address_change(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """The lock the coordinator holds during a command is the shell's address-rebuild lock."""
        player = _make_shell(mock_provider, mock_client, mock_upnp_device)
        assert player.grouping_rebuild_lock is player._rebuild_lock

    def test_native_follower_suppresses_playback(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A native follower reports idle and no media instead of its delegated state."""
        player = _make_shell(mock_provider, mock_client, mock_upnp_device)
        mock_provider.native_groups.role_of.return_value = NativeGroupRole.FOLLOWER
        assert player.playback_state == PlaybackState.IDLE
        assert player.current_media is None
        assert player.active_source is None


class TestTopology:
    """The coordinator publishes resolved membership onto the shell."""

    def test_on_native_group_update_publishes_leader_members(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A leader publishes exactly the members the coordinator resolved for it."""
        player = _make_shell(mock_provider, mock_client, mock_upnp_device)
        mock_provider.native_groups.members_of.return_value = [EDIFIER_PLAYER_ID, PEER_PLAYER_ID]
        player.on_native_group_update()
        assert player._attr_group_members == [EDIFIER_PLAYER_ID, PEER_PLAYER_ID]

    def test_on_native_group_update_clears_members_for_follower(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A standalone/follower shell publishes no members of its own."""
        player = _make_shell(mock_provider, mock_client, mock_upnp_device)
        player._attr_group_members = ["stale"]
        mock_provider.native_groups.members_of.return_value = []
        player.on_native_group_update()
        assert player._attr_group_members == []

    async def test_poll_pushes_topology_to_coordinator(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A poll refreshes reachability and forces a coordinator topology read."""
        player = _make_shell(mock_provider, mock_client, mock_upnp_device)
        await player.poll()
        mock_client.get_device_info_model.assert_awaited()
        mock_provider.native_groups.refresh_leader.assert_awaited_with(player, force=True)

    def test_becoming_follower_clears_active_output_protocol(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A shell that was playing through DLNA drops that output when it becomes a follower."""
        player = _make_shell(mock_provider, mock_client, mock_upnp_device)
        player.set_active_output_protocol("dlna_x")
        mock_provider.native_groups.role_of.return_value = NativeGroupRole.FOLLOWER

        player.on_native_group_update()

        assert player.active_output_protocol is None
        assert player.playback_state == PlaybackState.IDLE
        assert player.current_media is None

    def test_leaving_follower_does_not_resurrect_protocol(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """The dropped output stays cleared after leaving; normal playback reselects it."""
        player = _make_shell(mock_provider, mock_client, mock_upnp_device)
        player.set_active_output_protocol("dlna_x")
        mock_provider.native_groups.role_of.return_value = NativeGroupRole.FOLLOWER
        player.on_native_group_update()

        mock_provider.native_groups.role_of.return_value = NativeGroupRole.STANDALONE
        player.on_native_group_update()

        assert player.active_output_protocol is None

    def test_standalone_update_keeps_active_output_protocol(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A non-follower topology update never touches the active output (no churn)."""
        player = _make_shell(mock_provider, mock_client, mock_upnp_device)
        player.set_active_output_protocol("dlna_x")

        player.on_native_group_update()  # role stays standalone

        assert player.active_output_protocol == "dlna_x"


class TestGroupCompatibility:
    """Only compatible, modern router-based generic LinkPlay devices may be grouped."""

    def test_same_major_generation_compatible(self) -> None:
        """4.2 and 4.3 (same WMRM major, router-based) can be grouped."""
        first = _device_info("4.2")
        second = _device_info("4.3")
        assert linkplay_group_compatible(first, second) is True

    def test_wifi_direct_rejected(self) -> None:
        """A legacy Wi-Fi-Direct device is never grouped."""
        first = _device_info("4.2")
        second = _device_info("2.0", legacy=True)
        assert linkplay_group_compatible(first, second) is False

    def test_different_major_generation_rejected(self) -> None:
        """Different WMRM major generations are not grouped."""
        first = _device_info("4.2")
        second = _device_info("3.0")
        assert linkplay_group_compatible(first, second) is False

    def test_unknown_device_info_rejected(self) -> None:
        """An unknown (missing) device info is treated as incompatible."""
        known = _device_info("4.2")
        assert linkplay_group_compatible(None, known) is False

    def test_unknown_generation_rejected(self) -> None:
        """A device whose WMRM generation cannot be determined is not grouped."""
        known = _device_info("4.2")
        unknown = cast(
            "PywiimDeviceInfo",
            SimpleNamespace(wmrm_version=None, needs_wifi_direct_multiroom=False),
        )
        assert linkplay_group_compatible(known, unknown) is False


class TestRefreshResilience:
    """A poll refreshes reachability and pushes topology without blocking on a blip."""

    async def test_reachable_refresh_marks_healthy(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A successful device-info read keeps the shell reachable and caches the info."""
        player = _make_shell(mock_provider, mock_client, mock_upnp_device)
        player._linkplay_available = False
        await player.poll()
        assert player._linkplay_available is True
        assert player._cached_device_info is not None

    async def test_unreachable_api_marks_unhealthy(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A failed device-info read marks the shell unhealthy, withdrawing NATIVE grouping."""
        player = _make_shell(mock_provider, mock_client, mock_upnp_device)
        player._linkplay_available = True
        mock_client.get_device_info_model = AsyncMock(side_effect=WiiMError("down"))
        await player.poll()
        assert player._linkplay_available is False
        # only native grouping is gated; the broad lock stays off so a linked protocol can group
        assert PlayerFeature.SET_MEMBERS not in player.supported_features
        assert player.grouping_locked is False

    async def test_setup_without_primed_info_does_full_refresh(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A shell built without a primed device info probes the API during setup."""
        player = LinkPlayPlayer(
            provider=mock_provider,
            player_id=EDIFIER_PLAYER_ID,
            client=mock_client,
            upnp_device=mock_upnp_device,
            description_url="http://192.168.1.50:49152/description.xml",
        )
        player.update_state = MagicMock()  # type: ignore[misc,method-assign]
        mock_provider.players = [player]
        assert player._linkplay_available is False
        await player.setup()
        mock_client.get_device_info_model.assert_awaited_once()
        assert player._linkplay_available is True


class TestAddressChange:
    """A moved device rebuilds its low-level client without touching the MA player."""

    async def test_successful_rebuild_swaps_client(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A reachable new address swaps in a fresh client and updates the IP identifier."""
        player = _make_shell(mock_provider, mock_client, mock_upnp_device)
        new_client = MagicMock(host="192.168.1.99")
        new_client.get_device_info_model = AsyncMock(return_value=SimpleNamespace(uuid=""))
        new_client.get_slaves_info = AsyncMock(return_value=_slaves([]))
        mock_provider.players = [player]
        with patch(
            "music_assistant.providers.wiim.linkplay_player.WiiMClient", return_value=new_client
        ):
            await player.async_handle_address_change(
                "192.168.1.99", mock_upnp_device, "http://192.168.1.99:49152/description.xml"
            )
        assert player._client is new_client
        assert player.device_info.identifiers[IdentifierType.IP_ADDRESS] == "192.168.1.99"

    async def test_failed_rebuild_keeps_old_client(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """If the new address is unreachable, the existing client is preserved."""
        player = _make_shell(mock_provider, mock_client, mock_upnp_device)
        new_client = MagicMock(host="192.168.1.99")
        new_client.get_device_info_model = AsyncMock(side_effect=WiiMError("unreachable"))
        with patch(
            "music_assistant.providers.wiim.linkplay_player.WiiMClient", return_value=new_client
        ):
            await player.async_handle_address_change(
                "192.168.1.99", mock_upnp_device, "http://192.168.1.99:49152/description.xml"
            )
        assert player._client is mock_client


class TestProviderRouting:
    """Discovery classifies a device and routes it to the correct backend."""

    async def test_generic_device_registers_shell(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A reachable generic LinkPlay device is registered as a shell."""
        mock_provider.players = []
        registered: list[Any] = []
        mock_provider.mass.players.register_or_update = AsyncMock(
            side_effect=lambda p: registered.append(p)
        )
        with patch("music_assistant.providers.wiim.provider.WiiMClient", return_value=mock_client):
            await WiimProvider.try_add_linkplay_player(
                mock_provider,
                EDIFIER_PLAYER_ID,
                "192.168.1.50",
                mock_upnp_device,
                "http://192.168.1.50:49152/description.xml",
                "AA:BB:CC:DD:EE:FF",
            )
        assert registered
        assert isinstance(registered[0], LinkPlayPlayer)
        # discovery does a single authoritative device-info probe that primes the shell:
        # setup must not repeat it, and the shell is registered already reachable.
        mock_client.get_device_info_model.assert_awaited_once()
        assert registered[0]._linkplay_available is True
        # the live topology is read only after the player is registered (a read taken during
        # setup, before registration, would be discarded by the coordinator).
        mock_provider.native_groups.refresh_leader.assert_awaited_with(registered[0], force=True)

    async def test_unreachable_device_not_registered(
        self, mock_provider: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A device that does not answer the LinkPlay API is not registered."""
        mock_provider.mass.players.register_or_update = AsyncMock()
        bad_client = MagicMock()
        bad_client.get_device_info_model = AsyncMock(side_effect=WiiMError("no api"))
        with patch("music_assistant.providers.wiim.provider.WiiMClient", return_value=bad_client):
            await WiimProvider.try_add_linkplay_player(
                mock_provider,
                EDIFIER_PLAYER_ID,
                "192.168.1.50",
                mock_upnp_device,
                "http://192.168.1.50:49152/description.xml",
            )
        mock_provider.mass.players.register_or_update.assert_not_called()


class TestDefaultProtocolSelection:
    """The default_output_protocol_domain steers automatic output selection."""

    def _controller(self, protocol_players: dict[str, Any], preferred: Any = None) -> MagicMock:
        controller = MagicMock()
        controller.get_player.side_effect = protocol_players.get
        controller.mass.config.get_raw_player_config_value.return_value = preferred
        controller._is_protocol_grouped.return_value = False
        controller.logger = MagicMock()
        return controller

    def _shell_player(self, links: list[LinkedOutputProtocol]) -> MagicMock:
        player = MagicMock()
        player.default_output_protocol_domain = "dlna"
        player.supported_features = set()  # no native PLAY_MEDIA
        player.linked_output_protocols = links
        player.get_linked_protocol.side_effect = lambda pid: next(
            (link for link in links if link.output_protocol_id == pid), None
        )
        return player

    def test_prefers_default_domain_when_available(self) -> None:
        """With DLNA available, the DLNA output is chosen over the higher-priority AirPlay."""
        links = [
            LinkedOutputProtocol("dlna_x", "dlna", priority=50),
            LinkedOutputProtocol("ap_x", "airplay", priority=10),
        ]
        players = {
            "dlna_x": MagicMock(available_for_playback=True),
            "ap_x": MagicMock(available_for_playback=True),
        }
        controller = self._controller(players)
        player = self._shell_player(links)
        target, _ = ProtocolLinkingMixin._select_best_output_protocol(controller, player)
        assert target is players["dlna_x"]

    def test_falls_back_to_priority_when_default_absent(self) -> None:
        """Without an available DLNA, selection falls back to priority (AirPlay)."""
        links = [
            LinkedOutputProtocol("dlna_x", "dlna", priority=50),
            LinkedOutputProtocol("ap_x", "airplay", priority=10),
        ]
        players = {
            "dlna_x": MagicMock(available_for_playback=False),
            "ap_x": MagicMock(available_for_playback=True),
        }
        controller = self._controller(players)
        player = self._shell_player(links)
        target, _ = ProtocolLinkingMixin._select_best_output_protocol(controller, player)
        assert target is players["ap_x"]

    def test_explicit_user_preference_wins(self) -> None:
        """An explicit stored user preference overrides the default domain."""
        links = [
            LinkedOutputProtocol("dlna_x", "dlna", priority=50),
            LinkedOutputProtocol("ap_x", "airplay", priority=10),
        ]
        players = {
            "dlna_x": MagicMock(available_for_playback=True),
            "ap_x": MagicMock(available_for_playback=True),
        }
        controller = self._controller(players, preferred="ap_x")
        player = self._shell_player(links)
        target, _ = ProtocolLinkingMixin._select_best_output_protocol(controller, player)
        assert target is players["ap_x"]

    def test_explicit_auto_resolves_to_default_domain(self) -> None:
        """Selecting Auto resolves to the default domain, just like an unset value."""
        links = [
            LinkedOutputProtocol("dlna_x", "dlna", priority=50),
            LinkedOutputProtocol("ap_x", "airplay", priority=10),
        ]
        players = {
            "dlna_x": MagicMock(available_for_playback=True),
            "ap_x": MagicMock(available_for_playback=True),
        }
        controller = self._controller(players, preferred="auto")
        player = self._shell_player(links)
        target, _ = ProtocolLinkingMixin._select_best_output_protocol(controller, player)
        assert target is players["dlna_x"]


class TestFinalGroupingState:
    """The FINAL state gates native grouping by API health but keeps linked-protocol grouping."""

    @staticmethod
    def _final_supported_features(player: LinkPlayPlayer) -> set[PlayerFeature]:
        return cast(
            "set[PlayerFeature]",
            player._Player__final_supported_features,  # type: ignore[attr-defined]
        )

    def _link_grouping_protocol(self, player: LinkPlayPlayer, mock_provider: MagicMock) -> None:
        """Link a DLNA protocol player that itself supports SET_MEMBERS."""
        player.set_linked_output_protocols([LinkedOutputProtocol("dlna_x", "dlna", priority=50)])
        protocol_player = MagicMock()
        protocol_player.available = True
        protocol_player.available_for_playback = True
        protocol_player.supported_features = {PlayerFeature.SET_MEMBERS, PlayerFeature.PAUSE}
        mock_provider.mass.players.get_player.return_value = protocol_player

    def test_protocol_grouping_survives_api_outage(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """With the LinkPlay API down, a linked protocol still supplies SET_MEMBERS to the final state."""
        player = _make_shell(mock_provider, mock_client, mock_upnp_device)
        player._linkplay_available = False
        self._link_grouping_protocol(player, mock_provider)

        # native raw is gated, but the broad lock is off, so the linked protocol re-adds it
        assert PlayerFeature.SET_MEMBERS not in player.supported_features
        assert player.grouping_locked is False
        assert PlayerFeature.SET_MEMBERS in self._final_supported_features(player)

    def test_unknown_leader_follower_withdraws_even_protocol_grouping(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """The broad lock withdraws grouping in the final state even a linked protocol would add."""
        player = _make_shell(mock_provider, mock_client, mock_upnp_device)
        player._linkplay_available = True
        mock_provider.native_groups.is_unknown_leader_follower.return_value = True
        self._link_grouping_protocol(player, mock_provider)

        assert player.grouping_locked is True
        assert PlayerFeature.SET_MEMBERS not in self._final_supported_features(player)

    def test_healthy_native_keeps_set_members(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A reachable, standalone shell keeps native SET_MEMBERS and no broad lock."""
        player = _make_shell(mock_provider, mock_client, mock_upnp_device)
        player._linkplay_available = True
        assert PlayerFeature.SET_MEMBERS in player.supported_features
        assert player.grouping_locked is False
