"""Tests for the generic LinkPlay grouping/identity shell of the WiiM provider."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import IdentifierType, PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import PlayerCommandFailed, UnsupportedFeaturedException
from pywiim import WiiMError

from music_assistant.controllers.players.protocol_linking import ProtocolLinkingMixin
from music_assistant.models.player import LinkedOutputProtocol
from music_assistant.providers.wiim.constants import PLAYER_ID_PREFIX
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
    provider.mass = MagicMock()
    provider.mass.players = MagicMock()
    config = MagicMock()
    config.name = None
    config.default_name = "Edifier MS50A"
    config.enabled = True
    config.player_type = None
    config.get_value = MagicMock(return_value=None)
    provider.mass.config.get_base_player_config.return_value = config
    return provider


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

    def test_can_group_with_requires_health(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A shell with an unreachable API advertises no grouping peers."""
        player = _make_shell(mock_provider, mock_client, mock_upnp_device)
        peer = _make_shell(mock_provider, mock_client, mock_upnp_device, PEER_PLAYER_ID)
        peer._linkplay_available = True
        mock_provider.players = [player, peer]
        player._linkplay_available = False
        assert player.can_group_with == set()
        player._linkplay_available = True
        assert player.can_group_with == {PEER_PLAYER_ID}

    def test_can_group_with_skips_unhealthy_peer(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """An unreachable peer is not offered as a grouping target."""
        player = _make_shell(mock_provider, mock_client, mock_upnp_device)
        player._linkplay_available = True
        peer = _make_shell(mock_provider, mock_client, mock_upnp_device, PEER_PLAYER_ID)
        peer._linkplay_available = False
        mock_provider.players = [player, peer]
        assert player.can_group_with == set()

    def test_native_group_compat_only_generic_peers(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """The native-compat seam only accepts reachable generic peers, never other backends."""
        player = _make_shell(mock_provider, mock_client, mock_upnp_device)
        player._linkplay_available = True
        peer = _make_shell(mock_provider, mock_client, mock_upnp_device, PEER_PLAYER_ID)
        peer._linkplay_available = True
        mock_provider.players = [player, peer]
        assert player.is_native_group_compatible(peer) is True
        # a same-instance player of another backend (e.g. official WiiM) is not native-compatible,
        # so the grouping layer never routes such a pair onto a native group.
        official = MagicMock(player_id="wiim_official")
        assert player.is_native_group_compatible(official) is False


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
    """Same-backend grouping uses the low-level WiiMClient GroupAPI only."""

    async def test_join_uses_low_level_client(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """Adding a member joins it to this leader via join_slave with the leader device info."""
        leader = _make_shell(mock_provider, mock_client, mock_upnp_device)
        leader._linkplay_available = True
        member_client = MagicMock(join_slave=AsyncMock(), leave_group=AsyncMock())
        member = _make_shell(mock_provider, member_client, mock_upnp_device, PEER_PLAYER_ID)
        member._linkplay_available = True
        mock_provider.mass.players.get_player.return_value = member
        # after joining, the leader lists the member as a slave
        mock_client.get_slaves_info = AsyncMock(return_value=_slaves([PEER_HTTP_UUID]))
        mock_provider.players = [leader, member]
        await leader.set_members(player_ids_to_add=[PEER_PLAYER_ID])
        member_client.join_slave.assert_awaited_once()
        args, kwargs = member_client.join_slave.call_args
        assert args[0] == "192.168.1.50"  # leader ip
        assert kwargs["master_device_info"] is leader.cached_device_info

    async def test_leave_uses_low_level_client(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """Removing a member calls leave_group and verifies it left the leader's group."""
        leader = _make_shell(mock_provider, mock_client, mock_upnp_device)
        leader._linkplay_available = True
        member_client = MagicMock(join_slave=AsyncMock(), leave_group=AsyncMock())
        member = _make_shell(mock_provider, member_client, mock_upnp_device, PEER_PLAYER_ID)
        member._linkplay_available = True
        mock_provider.mass.players.get_player.return_value = member
        mock_client.get_slaves_info = AsyncMock(return_value=_slaves([]))
        mock_provider.players = [leader, member]
        await leader.set_members(player_ids_to_remove=[PEER_PLAYER_ID])
        member_client.leave_group.assert_awaited_once()

    async def test_cross_backend_member_rejected(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A non-generic member raises a typed unsupported error (B1)."""
        leader = _make_shell(mock_provider, mock_client, mock_upnp_device)
        leader._linkplay_available = True
        mock_provider.mass.players.get_player.return_value = MagicMock()  # not a LinkPlayPlayer
        with pytest.raises(UnsupportedFeaturedException):
            await leader.set_members(player_ids_to_add=["airplay_other"])

    async def test_unreachable_leader_fails(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """Grouping fails cleanly when the leader's LinkPlay API is unreachable."""
        leader = _make_shell(mock_provider, mock_client, mock_upnp_device)
        leader._linkplay_available = False
        with pytest.raises(PlayerCommandFailed):
            await leader.set_members(player_ids_to_add=[PEER_PLAYER_ID])

    async def test_group_error_becomes_command_failed(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A WiiMError from the device is surfaced as PlayerCommandFailed."""
        leader = _make_shell(mock_provider, mock_client, mock_upnp_device)
        leader._linkplay_available = True
        member_client = MagicMock(join_slave=AsyncMock(side_effect=WiiMError("boom")))
        member = _make_shell(mock_provider, member_client, mock_upnp_device, PEER_PLAYER_ID)
        member._linkplay_available = True
        mock_provider.mass.players.get_player.return_value = member
        with pytest.raises(PlayerCommandFailed):
            await leader.set_members(player_ids_to_add=[PEER_PLAYER_ID])

    async def test_incompatible_generation_rejected(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A peer on an incompatible/legacy multiroom generation is rejected before joining."""
        leader = _make_shell(mock_provider, mock_client, mock_upnp_device)
        leader._linkplay_available = True
        member_client = MagicMock(join_slave=AsyncMock())
        member = _make_shell(mock_provider, member_client, mock_upnp_device, PEER_PLAYER_ID)
        member._linkplay_available = True
        # a legacy Wi-Fi-Direct / older-generation device that we do not group
        member._cached_device_info = _device_info("2.0", legacy=True)
        mock_provider.mass.players.get_player.return_value = member
        with pytest.raises(UnsupportedFeaturedException):
            await leader.set_members(player_ids_to_add=[PEER_PLAYER_ID])
        member_client.join_slave.assert_not_awaited()

    async def test_unverified_join_raises(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """If the member never appears in the leader's slave list, the command fails."""
        leader = _make_shell(mock_provider, mock_client, mock_upnp_device)
        leader._linkplay_available = True
        member_client = MagicMock(join_slave=AsyncMock(), leave_group=AsyncMock())
        member = _make_shell(mock_provider, member_client, mock_upnp_device, PEER_PLAYER_ID)
        member._linkplay_available = True
        mock_provider.mass.players.get_player.return_value = member
        # leader keeps reporting no slaves -> join not verified
        mock_client.get_slaves_info = AsyncMock(return_value=_slaves([]))
        mock_provider.players = [leader, member]
        with (
            patch("music_assistant.providers.wiim.linkplay_player.asyncio.sleep", AsyncMock()),
            pytest.raises(PlayerCommandFailed),
        ):
            await leader.set_members(player_ids_to_add=[PEER_PLAYER_ID])

    async def test_join_verification_retries_until_settled(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A join that the device reports only after settling still succeeds."""
        leader = _make_shell(mock_provider, mock_client, mock_upnp_device)
        leader._linkplay_available = True
        member_client = MagicMock(join_slave=AsyncMock(), leave_group=AsyncMock())
        member = _make_shell(mock_provider, member_client, mock_upnp_device, PEER_PLAYER_ID)
        member._linkplay_available = True
        mock_provider.mass.players.get_player.return_value = member
        # first read still shows no slaves (group forming), second read shows the member
        mock_client.get_slaves_info = AsyncMock(
            side_effect=[_slaves([]), _slaves([PEER_HTTP_UUID])]
        )
        mock_provider.players = [leader, member]
        with patch("music_assistant.providers.wiim.linkplay_player.asyncio.sleep", AsyncMock()):
            await leader.set_members(player_ids_to_add=[PEER_PLAYER_ID])
        assert mock_client.get_slaves_info.await_count == 2

    async def test_grouping_available_during_external_source(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """Native grouping stays available while a linked protocol plays an external source."""
        player = _make_shell(mock_provider, mock_client, mock_upnp_device)
        player._linkplay_available = True
        # a linked DLNA protocol player is casting an external source
        player.set_linked_output_protocols([LinkedOutputProtocol("dlna_x", "dlna", priority=50)])
        ext_player = MagicMock(
            available=True,
            active_source="spotify",
            playback_state=PlaybackState.PLAYING,
            supported_features={PlayerFeature.PAUSE, PlayerFeature.SEEK},
        )
        ext_player.provider.domain = "dlna"
        mock_provider.mass.players.get_player.return_value = ext_player
        assert PlayerFeature.SET_MEMBERS in player.supported_features

    async def test_invalid_target_aborts_before_any_join(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A batch containing a cross-backend target performs no join at all (transactional)."""
        leader = _make_shell(mock_provider, mock_client, mock_upnp_device)
        leader._linkplay_available = True
        valid_client = MagicMock(join_slave=AsyncMock(), leave_group=AsyncMock())
        valid = _make_shell(mock_provider, valid_client, mock_upnp_device, PEER_PLAYER_ID)
        valid._linkplay_available = True
        players = {PEER_PLAYER_ID: valid, "official_x": MagicMock()}  # official = not a shell
        mock_provider.mass.players.get_player.side_effect = lambda pid, *_a, **_k: players.get(pid)
        with pytest.raises(UnsupportedFeaturedException):
            await leader.set_members(player_ids_to_add=[PEER_PLAYER_ID, "official_x"])
        valid_client.join_slave.assert_not_awaited()

    async def test_incompatible_target_aborts_before_any_join(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A later incompatible target aborts the batch before the earlier valid join runs."""
        leader = _make_shell(mock_provider, mock_client, mock_upnp_device)
        leader._linkplay_available = True
        good_client = MagicMock(join_slave=AsyncMock())
        good = _make_shell(mock_provider, good_client, mock_upnp_device, PEER_PLAYER_ID)
        good._linkplay_available = True
        bad_client = MagicMock(join_slave=AsyncMock())
        bad = _make_shell(mock_provider, bad_client, mock_upnp_device, "wiim_uuid:third")
        bad._linkplay_available = True
        bad._cached_device_info = _device_info("2.0", legacy=True)  # incompatible
        players = {PEER_PLAYER_ID: good, "wiim_uuid:third": bad}
        mock_provider.mass.players.get_player.side_effect = lambda pid, *_a, **_k: players.get(pid)
        with pytest.raises(UnsupportedFeaturedException):
            await leader.set_members(player_ids_to_add=[PEER_PLAYER_ID, "wiim_uuid:third"])
        good_client.join_slave.assert_not_awaited()

    async def test_duplicate_ids_rejected(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A duplicate member id in the request is rejected up front."""
        leader = _make_shell(mock_provider, mock_client, mock_upnp_device)
        leader._linkplay_available = True
        with pytest.raises(PlayerCommandFailed):
            await leader.set_members(player_ids_to_add=[PEER_PLAYER_ID, PEER_PLAYER_ID])

    async def test_conflicting_add_and_remove_rejected(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """Adding and removing the same member in one request is rejected up front."""
        leader = _make_shell(mock_provider, mock_client, mock_upnp_device)
        leader._linkplay_available = True
        with pytest.raises(PlayerCommandFailed):
            await leader.set_members(
                player_ids_to_add=[PEER_PLAYER_ID], player_ids_to_remove=[PEER_PLAYER_ID]
            )

    async def test_valid_batch_applies_all_members(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A fully valid multi-member batch joins every member."""
        leader = _make_shell(mock_provider, mock_client, mock_upnp_device)
        leader._linkplay_available = True
        leader._verify_group_change = AsyncMock()  # type: ignore[method-assign]
        first_client = MagicMock(join_slave=AsyncMock())
        first = _make_shell(mock_provider, first_client, mock_upnp_device, PEER_PLAYER_ID)
        first._linkplay_available = True
        second_client = MagicMock(join_slave=AsyncMock())
        second = _make_shell(mock_provider, second_client, mock_upnp_device, "wiim_uuid:third")
        second._linkplay_available = True
        players = {PEER_PLAYER_ID: first, "wiim_uuid:third": second}
        mock_provider.mass.players.get_player.side_effect = lambda pid, *_a, **_k: players.get(pid)
        await leader.set_members(player_ids_to_add=[PEER_PLAYER_ID, "wiim_uuid:third"])
        first_client.join_slave.assert_awaited_once()
        second_client.join_slave.assert_awaited_once()


class TestTopology:
    """Native group topology is mapped to MA group members, leader first."""

    async def test_master_lists_leader_first(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A master with a known slave reports [leader, follower]."""
        leader = _make_shell(mock_provider, mock_client, mock_upnp_device)
        peer = _make_shell(mock_provider, mock_client, mock_upnp_device, PEER_PLAYER_ID)
        mock_client.get_slaves_info = AsyncMock(return_value=_slaves([PEER_HTTP_UUID]))
        mock_provider.players = [leader, peer]
        await leader._update_group_members()
        assert leader._attr_group_members == [EDIFIER_PLAYER_ID, PEER_PLAYER_ID]

    async def test_solo_has_no_members(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A solo device manages no members."""
        leader = _make_shell(mock_provider, mock_client, mock_upnp_device)
        mock_client.get_slaves_info = AsyncMock(return_value=_slaves([]))
        await leader._update_group_members()
        assert leader._attr_group_members == []

    async def test_follower_has_no_members(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A follower derives its relationship from the leader, managing no members itself."""
        follower = _make_shell(mock_provider, mock_client, mock_upnp_device)
        mock_client.get_slaves_info = AsyncMock(return_value=_slaves([]))
        await follower._update_group_members()
        assert follower._attr_group_members == []

    async def test_unknown_slave_ignored(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A master whose only slave is not a registered player reports no members."""
        leader = _make_shell(mock_provider, mock_client, mock_upnp_device)
        mock_client.get_slaves_info = AsyncMock(return_value=_slaves(["001122334455667788990011"]))
        mock_provider.players = [leader]
        await leader._update_group_members()
        assert leader._attr_group_members == []


class TestMixedGroupReadOnly:
    """An externally-created mixed group is read-only from the generic side (until layer 2)."""

    def _mixed_leader(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> LinkPlayPlayer:
        leader = _make_shell(mock_provider, mock_client, mock_upnp_device)
        leader._linkplay_available = True
        # an official WiiM member (not a LinkPlayPlayer) shares the hardware group
        official = MagicMock()
        official.player_id = PEER_PLAYER_ID
        mock_provider.players = [leader, official]
        mock_provider.mass.players.get_player.side_effect = lambda pid, *_a, **_k: (
            official if pid == PEER_PLAYER_ID else None
        )
        mock_client.get_slaves_info = AsyncMock(return_value=_slaves([PEER_HTTP_UUID]))
        return leader

    async def test_mixed_group_detected_and_represented(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A group containing an official member is flagged mixed but still shown read-only."""
        leader = self._mixed_leader(mock_provider, mock_client, mock_upnp_device)
        await leader._update_group_members()
        assert leader._in_mixed_group is True
        assert leader._attr_group_members == [EDIFIER_PLAYER_ID, PEER_PLAYER_ID]

    async def test_mixed_group_withdraws_grouping(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """Grouping feature and targets are withdrawn while in a mixed group."""
        leader = self._mixed_leader(mock_provider, mock_client, mock_upnp_device)
        await leader._update_group_members()
        assert leader.grouping_locked is True
        assert PlayerFeature.SET_MEMBERS not in leader.supported_features
        assert leader.can_group_with == set()

    async def test_mixed_group_rejects_set_members(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """Mutating a mixed group is rejected rather than partially applied."""
        leader = self._mixed_leader(mock_provider, mock_client, mock_upnp_device)
        await leader._update_group_members()
        with pytest.raises(PlayerCommandFailed):
            await leader.set_members(player_ids_to_add=[EDIFIER_PLAYER_ID])

    def test_generic_follower_of_official_leader_is_mixed(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A generic device an official leader lists as a follower is read-only too."""
        follower = _make_shell(mock_provider, mock_client, mock_upnp_device)
        follower._linkplay_available = True
        follower._attr_group_members = []  # a follower manages nothing itself
        # an official leader (other backend) currently lists this generic device
        official_leader = MagicMock(
            player_id="wiim_uuid:official-leader", linkplay_backend="official"
        )
        official_leader._attr_group_members = [official_leader.player_id, EDIFIER_PLAYER_ID]
        mock_provider.players = [follower, official_leader]
        assert PlayerFeature.SET_MEMBERS not in follower.supported_features
        assert follower.can_group_with == set()


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

    async def test_can_group_with_excludes_incompatible_peer(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A reachable but legacy/incompatible peer is not offered as a grouping target."""
        player = _make_shell(mock_provider, mock_client, mock_upnp_device)
        player._linkplay_available = True
        peer = _make_shell(mock_provider, mock_client, mock_upnp_device, PEER_PLAYER_ID)
        peer._linkplay_available = True
        peer._cached_device_info = _device_info("2.0", legacy=True)
        mock_provider.players = [player, peer]
        assert player.can_group_with == set()


class TestRefreshResilience:
    """A poll must survive a transient topology blip and detect an unreachable API."""

    async def test_transient_topology_read_keeps_last_members(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A failed slave-list read must not clear a real group; last members are kept."""
        leader = _make_shell(mock_provider, mock_client, mock_upnp_device)
        peer = _make_shell(mock_provider, mock_client, mock_upnp_device, PEER_PLAYER_ID)
        mock_provider.players = [leader, peer]
        mock_client.get_slaves_info = AsyncMock(return_value=_slaves([PEER_HTTP_UUID]))
        await leader._refresh_linkplay()
        assert leader._attr_group_members == [EDIFIER_PLAYER_ID, PEER_PLAYER_ID]
        # a topology read blip must not drop the group, and the device stays available
        mock_client.get_slaves_info = AsyncMock(side_effect=WiiMError("blip"))
        await leader._refresh_linkplay()
        assert leader._attr_group_members == [EDIFIER_PLAYER_ID, PEER_PLAYER_ID]
        assert leader._linkplay_available is True

    async def test_unreachable_api_marks_unhealthy_but_keeps_topology(
        self, mock_provider: MagicMock, mock_client: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """When unreachable the shell goes unhealthy but keeps its last known group."""
        leader = _make_shell(mock_provider, mock_client, mock_upnp_device)
        leader._linkplay_available = True
        leader._attr_group_members = [EDIFIER_PLAYER_ID, PEER_PLAYER_ID]
        mock_client.get_device_info_model = AsyncMock(side_effect=WiiMError("down"))
        await leader._refresh_linkplay()
        assert leader._linkplay_available is False
        # last known topology is kept so the reachable side stays locked read-only
        assert leader._attr_group_members == [EDIFIER_PLAYER_ID, PEER_PLAYER_ID]

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

    def test_explicit_auto_falls_back_to_priority(self) -> None:
        """An explicit "auto" preference skips the default domain and uses plain priority."""
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
        assert target is players["ap_x"]
