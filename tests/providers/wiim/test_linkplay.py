"""Tests for the generic LinkPlay backend of the WiiM provider."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from async_upnp_client.exceptions import UpnpConnectionError
from music_assistant_models.enums import PlaybackState, PlayerFeature
from music_assistant_models.errors import PlayerCommandFailed, UnsupportedFeaturedException
from pywiim import WiiMError, WiiMGroupCompatibilityError

from music_assistant.providers.wiim.constants import PLAYER_ID_PREFIX
from music_assistant.providers.wiim.helpers import (
    is_official_manufacturer,
    linkplay_slave_uuid_to_player_id,
    linkplay_slave_uuid_to_udn,
)
from music_assistant.providers.wiim.linkplay_player import (
    PYWIIM_STATE_TO_MA,
    LinkPlayPlayer,
)
from music_assistant.providers.wiim.provider import WiimProvider

# Verified Edifier MS50A identity used across the tests.
EDIFIER_HTTP_UUID = "FF97F002783E65056579F15F"
EDIFIER_UDN = "uuid:FF97F002-783E-6505-6579-F15FFF97F002"
EDIFIER_PLAYER_ID = f"{PLAYER_ID_PREFIX}{EDIFIER_UDN}"
# Typed as optional so assigning it in tests does not narrow the attribute to str
# (which would make the "released to None" assertions look unreachable to mypy).
_MA_STREAM_URI: str | None = "http://ma/stream.flac"


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
            EDIFIER_UDN,  # uuid:-prefixed, dashed 32-hex
            EDIFIER_UDN.removeprefix("uuid:"),  # dashed 32-hex
            EDIFIER_UDN.removeprefix("uuid:").replace("-", ""),  # plain 32-hex
            EDIFIER_UDN.lower(),  # lowercase prefixed/dashed
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


@pytest.fixture
def mock_provider() -> MagicMock:
    """Create a mock WiimProvider suitable for constructing players."""
    provider = MagicMock()
    provider.instance_id = "wiim_test"
    provider.domain = "wiim"
    provider.mass = MagicMock()
    provider.mass.players = MagicMock()
    provider.notify_server = MagicMock()
    config = MagicMock()
    config.name = None
    config.default_name = "Edifier MS50A"
    config.enabled = True
    config.player_type = None
    config.get_value = MagicMock(return_value=None)
    provider.mass.config.get_base_player_config.return_value = config
    return provider


@pytest.fixture
def mock_pywiim_player() -> MagicMock:
    """Create a mock pywiim Player with the properties the MA player reads."""
    player = MagicMock()
    player.name = "Edifier MS50A"
    player.model = "MS50A"
    player.firmware = "Linkplay.4.6.430230"
    player.host = "192.168.1.50"
    player.available = True
    player.volume_level = 0.5
    player.is_muted = False
    player.is_master = False
    player.is_slave = False
    player.play_state = "stop"
    player.source = "network"
    player.supports_seek = False
    player.media_position = None
    player.media_title = None
    player.media_artist = None
    player.media_album = None
    player.media_image_url = None
    player.media_duration = None
    player.client = MagicMock()
    player.client.host = "192.168.1.50"
    player.client.get_slaves_info = AsyncMock(return_value=[])
    player.refresh = AsyncMock()
    player.play_url = AsyncMock()
    player.resume = AsyncMock()
    player.pause = AsyncMock()
    player.stop = AsyncMock()
    player.seek = AsyncMock()
    player.set_volume = AsyncMock()
    player.set_mute = AsyncMock()
    player.join_group = AsyncMock()
    player.leave_group = AsyncMock()
    return player


@pytest.fixture
def mock_upnp_device() -> MagicMock:
    """Create a mock async-upnp-client UpnpDevice."""
    device = MagicMock()
    device.manufacturer = "Edifier Inc"
    device.model_name = "Edifier MS50A"
    device.udn = EDIFIER_UDN
    return device


def _make_player(
    provider: MagicMock,
    pywiim_player: MagicMock,
    upnp_device: MagicMock,
    pywiim_upnp: MagicMock | None = None,
) -> LinkPlayPlayer:
    player = LinkPlayPlayer(
        provider=provider,
        player_id=EDIFIER_PLAYER_ID,
        pywiim_player=pywiim_player,
        upnp_device=upnp_device,
        pywiim_upnp=pywiim_upnp or MagicMock(close=AsyncMock()),
        description_url="http://192.168.1.50:49152/description.xml",
        mac_address="AA:BB:CC:DD:EE:FF",
    )
    player.update_state = MagicMock()  # type: ignore[misc,method-assign]
    return player


class TestStateMapping:
    """pywiim state must map correctly onto MA player attributes."""

    def test_state_map_covers_all_values(self) -> None:
        """Every pywiim play_state has a MA mapping."""
        assert set(PYWIIM_STATE_TO_MA) == {"play", "pause", "stop", "idle", "buffering"}

    def test_push_state_maps_volume_and_ma_playback(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """MA-initiated playback reports volume, playing state and this player as source."""
        mock_pywiim_player.play_state = "play"
        mock_pywiim_player.volume_level = 0.42
        mock_pywiim_player.media_title = "Song"
        mock_pywiim_player.media_position = 12
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        player._ma_stream_uri = _MA_STREAM_URI  # active MA stream
        player._ma_stream_confirmed = True

        player._push_state()

        assert player._attr_playback_state == PlaybackState.PLAYING
        assert player._attr_volume_level == 42
        assert player._attr_active_source == EDIFIER_PLAYER_ID
        assert player._attr_current_media is not None
        assert player._attr_current_media.title == "Song"

    def test_external_playback_is_not_claimed_as_own_source(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """Playback MA did not start is reported without claiming it as our queue."""
        mock_pywiim_player.play_state = "play"
        mock_pywiim_player.source = "spotify"
        mock_pywiim_player.media_title = "External"
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)

        player._push_state()

        # not our MA queue, and the external source is surfaced so MA shows this media
        assert player._attr_active_source != EDIFIER_PLAYER_ID
        assert player._attr_active_source == "spotify"

    def test_unknown_mute_state_is_preserved(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A device that reports no mute status stays unknown rather than 'unmuted'."""
        mock_pywiim_player.is_muted = None
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)

        player._push_state()

        assert player._attr_volume_muted is None

    def test_zero_position_is_applied(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A reported position of 0 is a real position and is applied."""
        mock_pywiim_player.play_state = "play"
        mock_pywiim_player.media_position = 0
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        player._attr_elapsed_time = 99.0

        player._push_state()

        assert player._attr_elapsed_time == 0.0

    def test_push_state_idle_clears_media(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A stopped device clears its active source and media."""
        mock_pywiim_player.play_state = "stop"
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)

        player._push_state()

        assert player._attr_playback_state == PlaybackState.IDLE
        assert player._attr_active_source is None
        assert player._attr_current_media is None

    def test_unavailable_device_clears_state(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """An unavailable device reports no media and no source."""
        mock_pywiim_player.available = False
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)

        player._push_state()

        assert player._attr_available is False
        assert player._attr_current_media is None

    def test_follower_state_is_derived_from_leader(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A follower only reports its own volume and manages no members."""
        mock_pywiim_player.is_slave = True
        mock_pywiim_player.volume_level = 0.3
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        player._attr_group_members = [EDIFIER_PLAYER_ID, "wiim_uuid:other"]

        player._push_state()

        assert player._attr_volume_level == 30
        assert player._attr_group_members == []


class TestSourceTakeover:
    """MA ownership of playback must be released when another source takes over."""

    @pytest.mark.parametrize("source", ["spotify", "bluetooth", "airplay", "line_in"])
    def test_non_network_source_releases_ownership(
        self,
        mock_provider: MagicMock,
        mock_pywiim_player: MagicMock,
        mock_upnp_device: MagicMock,
        source: str,
    ) -> None:
        """A switch to any non-network source drops the MA stream marker even if stale."""
        mock_pywiim_player.play_state = "play"
        mock_pywiim_player.source = source
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        player._ma_stream_uri = _MA_STREAM_URI
        player._ma_stream_confirmed = True

        player._push_state()

        assert player._ma_stream_uri is None
        assert player._attr_active_source != EDIFIER_PLAYER_ID

    def test_network_takeover_via_live_uri_mismatch(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """In network mode, a different live UPnP URI after confirmation is a takeover."""
        mock_pywiim_player.play_state = "play"
        mock_pywiim_player.source = "network"
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        player._ma_stream_uri = _MA_STREAM_URI
        player._ma_stream_confirmed = True
        player._dmr_device = MagicMock(current_track_uri="http://other/app.mp3")

        player._push_state()

        assert player._ma_stream_uri is None

    def test_handover_lag_keeps_marker(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """Before confirmation, a stale (previous-track) live URI must not drop ownership."""
        mock_pywiim_player.play_state = "play"
        mock_pywiim_player.source = "network"
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        player._ma_stream_uri = _MA_STREAM_URI
        player._ma_stream_confirmed = False
        player._dmr_device = MagicMock(current_track_uri="http://previous/track.mp3")

        player._push_state()

        assert player._ma_stream_uri == _MA_STREAM_URI
        assert player._attr_active_source == EDIFIER_PLAYER_ID

    def test_confirmation_on_matching_live_uri(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A live URI equal to our stream confirms ownership."""
        mock_pywiim_player.play_state = "play"
        mock_pywiim_player.source = "network"
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        player._ma_stream_uri = _MA_STREAM_URI
        player._dmr_device = MagicMock(current_track_uri=_MA_STREAM_URI)

        player._push_state()

        assert player._ma_stream_confirmed is True
        assert player._ma_stream_uri == _MA_STREAM_URI

    def test_network_takeover_after_timeout_even_if_unconfirmed(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A persistent different live URI past the handover window is a takeover."""
        mock_pywiim_player.play_state = "play"
        mock_pywiim_player.source = "network"
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        player._ma_stream_uri = _MA_STREAM_URI
        player._ma_stream_confirmed = False  # our stream was never seen
        player._ma_stream_since = time.time() - 120  # past the handover window
        player._dmr_device = MagicMock(current_track_uri="http://other/app.mp3")

        player._push_state()

        assert player._ma_stream_uri is None
        assert player._attr_active_source != EDIFIER_PLAYER_ID

    def test_transient_none_source_keeps_marker(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A transient handover report (source None) does not drop ownership."""
        mock_pywiim_player.play_state = "buffering"
        mock_pywiim_player.source = None
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        player._ma_stream_uri = _MA_STREAM_URI

        player._push_state()

        assert player._ma_stream_uri == _MA_STREAM_URI

    def test_transient_idle_during_handover_keeps_optimistic_media(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A transient idle mid-handover must not wipe the media play_media published."""
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        player._ma_stream_uri = _MA_STREAM_URI
        player._ma_stream_confirmed = False
        player._ma_stream_since = time.time()  # within the handover window
        player._attr_active_source = EDIFIER_PLAYER_ID
        player.set_current_media(uri=_MA_STREAM_URI or "", title="MA Track", clear_all=True)
        mock_pywiim_player.play_state = "stop"  # transient idle during handover

        player._push_state()

        assert player._ma_stream_uri == _MA_STREAM_URI
        assert player._attr_active_source == EDIFIER_PLAYER_ID
        assert player._attr_current_media is not None
        assert player._attr_current_media.title == "MA Track"

    @pytest.mark.parametrize("source", ["custompushurl", "http", "network"])
    def test_url_streaming_sources_keep_ma_ownership(
        self,
        mock_provider: MagicMock,
        mock_pywiim_player: MagicMock,
        mock_upnp_device: MagicMock,
        source: str,
    ) -> None:
        """play_url() reports custompushurl/http/network; these stay MA-owned."""
        mock_pywiim_player.play_state = "play"
        mock_pywiim_player.source = source
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        player._ma_stream_uri = _MA_STREAM_URI

        player._push_state()

        assert player._ma_stream_uri == _MA_STREAM_URI
        assert player._attr_active_source == EDIFIER_PLAYER_ID

    def test_idle_after_confirmed_playback_releases_ownership(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """When a confirmed MA stream ends (idle), ownership is released."""
        mock_pywiim_player.play_state = "stop"
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        player._ma_stream_uri = _MA_STREAM_URI
        player._ma_stream_confirmed = True

        player._push_state()

        assert player._ma_stream_uri is None

    def test_idle_during_handover_keeps_marker(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A transient idle before the stream starts must keep the optimistic marker."""
        mock_pywiim_player.play_state = "stop"
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        player._ma_stream_uri = _MA_STREAM_URI
        player._ma_stream_confirmed = False
        player._ma_stream_since = time.time()

        player._push_state()

        assert player._ma_stream_uri == _MA_STREAM_URI

    def test_idle_past_handover_window_releases_unconfirmed(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """Without eventing an unconfirmed marker is released once idle past the window."""
        mock_pywiim_player.play_state = "stop"
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        player._ma_stream_uri = _MA_STREAM_URI
        player._ma_stream_confirmed = False
        player._ma_stream_since = time.time() - 120  # well past the handover window

        player._push_state()

        assert player._ma_stream_uri is None


class TestExternalMediaIdentity:
    """External track changes must not leak metadata from the previous track."""

    def test_external_track_change_drops_stale_fields(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """When an external track omits fields, they are not carried over."""
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        mock_pywiim_player.play_state = "play"
        mock_pywiim_player.source = "spotify"

        # first external track: full metadata, distinct live URI
        player._dmr_device = MagicMock(current_track_uri="spotify://track/1")
        mock_pywiim_player.media_title = "First"
        mock_pywiim_player.media_album = "Album A"
        mock_pywiim_player.media_image_url = "http://art/a.jpg"
        player._push_state()
        first = player._attr_current_media
        assert first is not None
        assert first.album == "Album A"

        # second external track omits album/artwork -> must not survive
        player._dmr_device = MagicMock(current_track_uri="spotify://track/2")
        mock_pywiim_player.media_title = "Second"
        mock_pywiim_player.media_album = None
        mock_pywiim_player.media_image_url = None
        player._push_state()

        second = player._attr_current_media
        assert second is not None
        assert second.title == "Second"
        assert second.album is None
        assert second.image_url is None

    def test_external_track_change_on_constant_stream_uri(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A radio-style stream keeps one URI, so metadata changes must still refresh media."""
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        mock_pywiim_player.play_state = "play"
        mock_pywiim_player.source = "network"
        player._dmr_device = MagicMock(current_track_uri="http://radio/stream")

        mock_pywiim_player.media_title = "Now Playing A"
        mock_pywiim_player.media_album = "Album A"
        player._push_state()
        first = player._attr_current_media
        assert first is not None
        assert first.album == "Album A"

        # same stream URI, next song omits album -> must not carry over
        mock_pywiim_player.media_title = "Now Playing B"
        mock_pywiim_player.media_album = None
        player._push_state()
        second = player._attr_current_media
        assert second is not None
        assert second.title == "Now Playing B"
        assert second.album is None


class TestFeatures:
    """The generic player exposes a conservative, capability-backed feature set."""

    async def test_base_features(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """Direct playback, transport and volume are always available."""
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        player._connect_eventing = AsyncMock()  # type: ignore[method-assign]

        await player.setup()

        assert PlayerFeature.PLAY_MEDIA in player.supported_features
        assert PlayerFeature.PAUSE in player.supported_features
        assert PlayerFeature.VOLUME_SET in player.supported_features
        assert PlayerFeature.VOLUME_MUTE in player.supported_features
        assert PlayerFeature.SET_MEMBERS in player.supported_features
        assert PlayerFeature.SEEK not in player.supported_features

    async def test_seek_added_and_removed_dynamically(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """SEEK follows the pywiim supports_seek property across refreshes."""
        mock_pywiim_player.supports_seek = True
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        player._connect_eventing = AsyncMock()  # type: ignore[method-assign]

        await player.setup()
        assert PlayerFeature.SEEK in player.supported_features

        # a later source change drops seek support
        mock_pywiim_player.supports_seek = False
        player._push_state()
        assert PlayerFeature.SEEK not in player.supported_features


class TestCommandErrors:
    """Command failures must surface as typed MA errors, not silent successes."""

    @pytest.mark.parametrize(
        "command", ["play", "pause", "stop", "seek", "volume_set", "volume_mute"]
    )
    async def test_command_failure_raises(
        self,
        mock_provider: MagicMock,
        mock_pywiim_player: MagicMock,
        mock_upnp_device: MagicMock,
        command: str,
    ) -> None:
        """A pywiim error during a command raises PlayerCommandFailed."""
        method = {
            "play": "resume",
            "pause": "pause",
            "stop": "stop",
            "seek": "seek",
            "volume_set": "set_volume",
            "volume_mute": "set_mute",
        }[command]
        getattr(mock_pywiim_player, method).side_effect = WiiMError("boom")
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        invocations = {
            "play": lambda: player.play(),
            "pause": lambda: player.pause(),
            "stop": lambda: player.stop(),
            "seek": lambda: player.seek(10),
            "volume_set": lambda: player.volume_set(50),
            "volume_mute": lambda: player.volume_mute(True),
        }

        with pytest.raises(PlayerCommandFailed):
            await invocations[command]()


class TestGrouping:
    """Native grouping stays within the generic LinkPlay backend."""

    async def test_set_members_add_joins_same_backend(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """Adding a generic member joins it and is confirmed via the leader's slave list."""
        leader = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        leader._refresh_state = AsyncMock()  # type: ignore[method-assign]

        member_uuid = "A1B2C3D4E5F6A7B8C9D0E1F2"
        member_player_id = linkplay_slave_uuid_to_player_id(member_uuid)
        assert member_player_id is not None
        member_pywiim = MagicMock()
        member_pywiim.join_group = AsyncMock()
        member = MagicMock(spec=LinkPlayPlayer)
        member._pywiim = member_pywiim
        member.player_id = member_player_id
        mock_provider.mass.players.get_player.return_value = member
        mock_provider.players = [leader, member]
        # after the join the leader's slave list contains the member
        mock_pywiim_player.client.get_slaves_info = AsyncMock(return_value=[{"uuid": member_uuid}])

        await leader.set_members(player_ids_to_add=[member_player_id])

        member_pywiim.join_group.assert_awaited_once_with(mock_pywiim_player)

    async def test_set_members_join_that_does_not_take_effect_fails(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A join that pywiim reports OK but the leader's slave list ignores must raise."""
        leader = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        leader._refresh_state = AsyncMock()  # type: ignore[method-assign]
        member_pywiim = MagicMock()
        member_pywiim.join_group = AsyncMock()
        member = MagicMock(spec=LinkPlayPlayer)
        member._pywiim = member_pywiim
        member.player_id = "wiim_uuid:member"
        mock_provider.mass.players.get_player.return_value = member
        mock_provider.players = [leader, member]
        # the leader never lists the member -> the join did not take effect
        mock_pywiim_player.client.get_slaves_info = AsyncMock(return_value=[])

        with pytest.raises(PlayerCommandFailed):
            await leader.set_members(player_ids_to_add=["wiim_uuid:member"])

    async def test_set_members_remove_that_no_ops_fails(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """Removing a member the leader still lists (external-group no-op) must raise."""
        leader = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        leader._refresh_state = AsyncMock()  # type: ignore[method-assign]
        member_uuid = "A1B2C3D4E5F6A7B8C9D0E1F2"
        member_player_id = linkplay_slave_uuid_to_player_id(member_uuid)
        assert member_player_id is not None
        member_pywiim = MagicMock()
        member_pywiim.leave_group = AsyncMock()
        member = MagicMock(spec=LinkPlayPlayer)
        member._pywiim = member_pywiim
        member.player_id = member_player_id
        mock_provider.mass.players.get_player.return_value = member
        mock_provider.players = [leader, member]
        # the leader still lists the member -> the leave silently did nothing
        mock_pywiim_player.client.get_slaves_info = AsyncMock(return_value=[{"uuid": member_uuid}])

        with pytest.raises(PlayerCommandFailed):
            await leader.set_members(player_ids_to_remove=[member_player_id])

    async def test_set_members_rejects_cross_backend(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A non-LinkPlay member raises UnsupportedFeaturedException instead of casting."""
        leader = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        leader._refresh_state = AsyncMock()  # type: ignore[method-assign]
        mock_provider.mass.players.get_player.return_value = MagicMock()  # not a LinkPlayPlayer

        with pytest.raises(UnsupportedFeaturedException):
            await leader.set_members(player_ids_to_add=["wiim_uuid:official"])

    async def test_set_members_incompatible_group_fails(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """An incompatible same-backend group must raise, not silently return."""
        leader = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        leader._refresh_state = AsyncMock()  # type: ignore[method-assign]
        member_pywiim = MagicMock()
        member_pywiim.join_group = AsyncMock(side_effect=WiiMGroupCompatibilityError("2.0", "4.2"))
        member = MagicMock(spec=LinkPlayPlayer)
        member._pywiim = member_pywiim
        mock_provider.mass.players.get_player.return_value = member

        with pytest.raises(PlayerCommandFailed):
            await leader.set_members(player_ids_to_add=["wiim_uuid:member"])

    def test_can_group_with_same_backend_only(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """can_group_with lists only other available generic LinkPlay players."""
        leader = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)

        peer = MagicMock(spec=LinkPlayPlayer)
        peer.player_id = "wiim_uuid:peer"
        peer.available = True
        official = MagicMock()  # not a LinkPlayPlayer
        official.player_id = "wiim_uuid:official"
        official.available = True
        mock_provider.players = [leader, peer, official]

        assert leader.can_group_with == {"wiim_uuid:peer"}


class TestExternalTopology:
    """Externally-created groups are represented read-only, leader first."""

    async def test_master_lists_resolved_members_leader_first(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A master's group_members starts with the leader and skips unknown members."""
        mock_pywiim_player.is_master = True
        known_uuid = "A1B2C3D4E5F6A7B8C9D0E1F2"
        known_player_id = linkplay_slave_uuid_to_player_id(known_uuid)
        mock_pywiim_player.client.get_slaves_info = AsyncMock(
            return_value=[
                {"uuid": known_uuid, "ip": "192.168.1.51", "name": "Kitchen"},
                {"uuid": "FFFFFFFFFFFFFFFFFFFFFFFF", "ip": "192.168.1.52", "name": "Unknown"},
            ]
        )
        leader = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)

        known_member = MagicMock()
        known_member.player_id = known_player_id
        mock_provider.players = [leader, known_member]

        await leader._update_group_members()

        assert leader._attr_group_members == [EDIFIER_PLAYER_ID, known_player_id]

    async def test_master_resolves_full_udn_slave(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A slave that reports a full 32-hex UDN is resolved to its registered player."""
        mock_pywiim_player.is_master = True
        member_udn = "uuid:A1B2C3D4-E5F6-A7B8-C9D0-E1F2A1B2C3D4"
        member_player_id = f"{PLAYER_ID_PREFIX}{member_udn}"
        mock_pywiim_player.client.get_slaves_info = AsyncMock(
            return_value=[{"uuid": member_udn, "ip": "192.168.1.53", "name": "Study"}]
        )
        leader = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        member = MagicMock()
        member.player_id = member_player_id
        mock_provider.players = [leader, member]

        await leader._update_group_members()

        assert leader._attr_group_members == [EDIFIER_PLAYER_ID, member_player_id]

    async def test_solo_has_no_members(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A solo player manages no members."""
        mock_pywiim_player.is_master = False
        leader = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)

        await leader._update_group_members()

        assert leader._attr_group_members == []


class TestEventing:
    """UPnP events trigger a state refresh."""

    def test_upnp_event_schedules_deduplicated_refresh(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A UPnP event schedules a refresh with a stable task id so bursts coalesce."""
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)

        player._handle_upnp_event(MagicMock(), [])

        mock_provider.mass.create_task.assert_called_once()
        _, kwargs = mock_provider.mass.create_task.call_args
        assert kwargs.get("task_id") == f"linkplay_refresh_{EDIFIER_PLAYER_ID}"


class TestSessionOwnership:
    """The shared aiohttp session must never be closed by the generic player."""

    async def test_on_unload_closes_upnp_client_not_shared_session(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """Unloading tears down eventing and the injected UPnP client, never the session."""
        pywiim_upnp = MagicMock(close=AsyncMock())
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device, pywiim_upnp)
        player._disconnect_eventing = AsyncMock()  # type: ignore[method-assign]

        await player.on_unload()

        player._disconnect_eventing.assert_awaited_once()
        pywiim_upnp.close.assert_awaited_once()
        # pywiim's WiiMClient.close would close the shared session, so it must not be called.
        mock_pywiim_player.client.close.assert_not_called()


class TestAddressChange:
    """A moved device rebuilds its backend atomically and safely."""

    async def test_successful_rebuild_swaps_and_closes_old(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A validated replacement is adopted and the old UPnP client is closed."""
        old_upnp = MagicMock(close=AsyncMock())
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device, old_upnp)
        player._disconnect_eventing = AsyncMock()  # type: ignore[method-assign]
        player._connect_eventing = AsyncMock()  # type: ignore[method-assign]
        player._refresh_state = AsyncMock()  # type: ignore[method-assign]

        new_pywiim = MagicMock()
        new_pywiim.refresh = AsyncMock()
        new_upnp = MagicMock(close=AsyncMock())
        new_device = MagicMock()
        with (
            patch("music_assistant.providers.wiim.linkplay_player.WiiMClient"),
            patch(
                "music_assistant.providers.wiim.linkplay_player.UpnpClient.create",
                new=AsyncMock(return_value=new_upnp),
            ),
            patch(
                "music_assistant.providers.wiim.linkplay_player.PywiimPlayer",
                return_value=new_pywiim,
            ),
        ):
            await player.async_handle_address_change(
                "192.168.1.99", new_device, "http://192.168.1.99:49152/description.xml"
            )

        assert player._pywiim is new_pywiim
        assert player._pywiim_upnp is new_upnp
        old_upnp.close.assert_awaited_once()
        player._connect_eventing.assert_awaited_once()
        player._refresh_state.assert_awaited_once()

    async def test_failed_rebuild_preserves_old_backend(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """If the replacement cannot be reached, the existing backend is kept intact."""
        old_upnp = MagicMock(close=AsyncMock())
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device, old_upnp)
        player._disconnect_eventing = AsyncMock()  # type: ignore[method-assign]
        player._connect_eventing = AsyncMock()  # type: ignore[method-assign]

        new_pywiim = MagicMock()
        new_pywiim.refresh = AsyncMock(side_effect=WiiMError("unreachable"))
        new_upnp = MagicMock(close=AsyncMock())
        with (
            patch("music_assistant.providers.wiim.linkplay_player.WiiMClient"),
            patch(
                "music_assistant.providers.wiim.linkplay_player.UpnpClient.create",
                new=AsyncMock(return_value=new_upnp),
            ),
            patch(
                "music_assistant.providers.wiim.linkplay_player.PywiimPlayer",
                return_value=new_pywiim,
            ),
        ):
            await player.async_handle_address_change(
                "192.168.1.99", MagicMock(), "http://192.168.1.99:49152/description.xml"
            )

        # old backend untouched, new (failed) UPnP client cleaned up
        assert player._pywiim is mock_pywiim_player
        assert player._pywiim_upnp is old_upnp
        old_upnp.close.assert_not_called()
        new_upnp.close.assert_awaited_once()
        player._connect_eventing.assert_not_called()


class TestProviderRouting:
    """Classification must pick the right backend and never fall back on transient errors."""

    def _make_provider(self) -> Any:
        provider: Any = WiimProvider.__new__(WiimProvider)
        provider.upnp_factory = MagicMock()
        provider.mass = MagicMock()
        provider.mass.players.get_player.return_value = None
        provider.logger = MagicMock()
        provider.try_add_player = AsyncMock()
        provider.try_add_linkplay_player = AsyncMock()
        return provider

    async def test_official_manufacturer_routes_to_official_backend(self) -> None:
        """A WiiM/Audio Pro device is handed to the official SDK path."""
        provider = self._make_provider()
        upnp_device = MagicMock()
        upnp_device.manufacturer = "Linkplay"
        upnp_device.udn = "uuid:wiim-1"
        provider.upnp_factory.async_create_device = AsyncMock(return_value=upnp_device)

        await provider._discover_device("192.168.1.10", "WiiM", ("http://x/description.xml",))

        provider.try_add_player.assert_awaited_once()
        provider.try_add_linkplay_player.assert_not_awaited()

    async def test_generic_manufacturer_routes_to_generic_backend(self) -> None:
        """An Edifier device is handed to the generic pywiim path."""
        provider = self._make_provider()
        upnp_device = MagicMock()
        upnp_device.manufacturer = "Edifier Inc"
        upnp_device.udn = EDIFIER_UDN
        provider.upnp_factory.async_create_device = AsyncMock(return_value=upnp_device)

        await provider._discover_device("192.168.1.50", "Edifier", ("http://x/description.xml",))

        provider.try_add_linkplay_player.assert_awaited_once()
        provider.try_add_player.assert_not_awaited()

    async def test_transient_probe_failure_selects_no_backend(self) -> None:
        """A device that fails to answer the UPnP probe is left undecided."""
        provider = self._make_provider()
        provider.upnp_factory.async_create_device = AsyncMock(side_effect=UpnpConnectionError())

        await provider._discover_device(
            "192.168.1.50", "Edifier", ("http://a/description.xml", "http://b/description.xml")
        )

        provider.try_add_player.assert_not_awaited()
        provider.try_add_linkplay_player.assert_not_awaited()

    async def test_existing_linkplay_player_address_is_reconciled(self) -> None:
        """A moved, already-registered generic player is rebuilt instead of skipped."""
        provider = self._make_provider()
        existing = MagicMock(spec=LinkPlayPlayer)
        existing.player_id = EDIFIER_PLAYER_ID
        existing.device_info.ip_address = "192.168.1.40"
        existing.async_handle_address_change = AsyncMock()
        provider.mass.players.get_player.return_value = existing
        upnp_device = MagicMock()
        upnp_device.manufacturer = "Edifier Inc"
        upnp_device.udn = EDIFIER_UDN
        provider.upnp_factory.async_create_device = AsyncMock(return_value=upnp_device)

        await provider._discover_device(
            "192.168.1.99", "Edifier", ("http://192.168.1.99:49152/description.xml",)
        )

        existing.async_handle_address_change.assert_awaited_once()
        provider.try_add_linkplay_player.assert_not_awaited()

    async def test_address_reconcile_rejects_udn_mismatch(self) -> None:
        """A stale address that now hosts a different device must not rebuild the player."""
        provider = self._make_provider()
        existing = MagicMock(spec=LinkPlayPlayer)
        existing.player_id = EDIFIER_PLAYER_ID
        existing.device_info.ip_address = "192.168.1.40"
        existing.async_handle_address_change = AsyncMock()

        other_device = MagicMock()  # a different speaker inherited the address
        other_device.udn = "uuid:SOME-OTHER-DEVICE"
        await provider._reconcile_player_address(
            existing, "192.168.1.99", other_device, "http://192.168.1.99:49152/description.xml"
        )

        existing.async_handle_address_change.assert_not_called()

    def test_candidate_locations_include_port_59152_deduped(self) -> None:
        """Manual/advertised locations include 59152 and 49152, advertised first, no dups."""
        provider: Any = WiimProvider.__new__(WiimProvider)
        # manual (no advertised port)
        manual = provider._candidate_locations("1.2.3.4")
        assert "http://1.2.3.4:59152/description.xml" in manual
        assert "http://1.2.3.4:49152/description.xml" in manual
        assert "http://1.2.3.4/description.xml" in manual
        # advertised port comes first and is not duplicated
        adv = provider._candidate_locations("1.2.3.4", 49152)
        assert adv[0] == "http://1.2.3.4:49152/description.xml"
        assert len(adv) == len(set(adv))


class TestGenericPlayerConstruction:
    """try_add_linkplay_player must inject a UPnP client and never fall back."""

    def _make_provider(self) -> Any:
        provider: Any = WiimProvider.__new__(WiimProvider)
        provider.mass = MagicMock()
        provider.mass.http_session = MagicMock()
        provider.logger = MagicMock()
        return provider

    async def test_client_uses_shared_session_and_injects_upnp(self) -> None:
        """The pywiim client and injected UPnP client both borrow MA's shared session."""
        provider = self._make_provider()
        upnp_client = MagicMock(close=AsyncMock())
        with (
            patch("music_assistant.providers.wiim.provider.WiiMClient") as client_cls,
            patch(
                "music_assistant.providers.wiim.provider.UpnpClient.create",
                new=AsyncMock(return_value=upnp_client),
            ) as upnp_create,
            patch("music_assistant.providers.wiim.provider.PywiimPlayer") as pywiim_cls,
            patch("music_assistant.providers.wiim.provider.LinkPlayPlayer") as player_cls,
        ):
            pywiim_cls.return_value.refresh = AsyncMock()
            player_cls.return_value.setup = AsyncMock()
            provider.mass.players.register_or_update = AsyncMock()
            provider.mass.players.get_player.return_value = player_cls.return_value  # registered

            await provider.try_add_linkplay_player(
                EDIFIER_PLAYER_ID,
                "192.168.1.50",
                MagicMock(),
                "http://192.168.1.50:49152/description.xml",
            )

            client_cls.assert_called_once_with("192.168.1.50", session=provider.mass.http_session)
            upnp_create.assert_awaited_once_with(
                "192.168.1.50",
                "http://192.168.1.50:49152/description.xml",
                session=provider.mass.http_session,
            )
            # the UPnP client is injected into the pywiim Player (suppresses auto-background one)
            _, kwargs = pywiim_cls.call_args
            assert kwargs.get("upnp_client") is upnp_client
            client_cls.return_value.close.assert_not_called()

    async def test_refresh_failure_prevents_registration(self) -> None:
        """A device that does not speak LinkPlay is not registered (no official fallback)."""
        provider = self._make_provider()
        upnp_client = MagicMock(close=AsyncMock())
        with (
            patch("music_assistant.providers.wiim.provider.WiiMClient"),
            patch(
                "music_assistant.providers.wiim.provider.UpnpClient.create",
                new=AsyncMock(return_value=upnp_client),
            ),
            patch("music_assistant.providers.wiim.provider.PywiimPlayer") as pywiim_cls,
            patch("music_assistant.providers.wiim.provider.LinkPlayPlayer") as player_cls,
        ):
            pywiim_cls.return_value.refresh = AsyncMock(side_effect=WiiMError("nope"))
            provider.mass.players.register_or_update = AsyncMock()

            await provider.try_add_linkplay_player(
                EDIFIER_PLAYER_ID,
                "192.168.1.50",
                MagicMock(),
                "http://192.168.1.50:49152/description.xml",
            )

            player_cls.assert_not_called()
            provider.mass.players.register_or_update.assert_not_awaited()
            # the injected UPnP client is cleaned up on the discovery miss
            upnp_client.close.assert_awaited_once()

    async def test_unregistered_player_is_cleaned_up(self) -> None:
        """If register_or_update skips (disabled/teardown), owned resources are released."""
        provider = self._make_provider()
        upnp_client = MagicMock(close=AsyncMock())
        with (
            patch("music_assistant.providers.wiim.provider.WiiMClient"),
            patch(
                "music_assistant.providers.wiim.provider.UpnpClient.create",
                new=AsyncMock(return_value=upnp_client),
            ),
            patch("music_assistant.providers.wiim.provider.PywiimPlayer") as pywiim_cls,
            patch("music_assistant.providers.wiim.provider.LinkPlayPlayer") as player_cls,
        ):
            pywiim_cls.return_value.refresh = AsyncMock()
            player_cls.return_value.setup = AsyncMock()
            player_cls.return_value.cleanup_resources = AsyncMock()
            provider.mass.players.register_or_update = AsyncMock()
            provider.mass.players.get_player.return_value = None  # not actually registered

            await provider.try_add_linkplay_player(
                EDIFIER_PLAYER_ID,
                "192.168.1.50",
                MagicMock(),
                "http://192.168.1.50:49152/description.xml",
            )

            player_cls.return_value.cleanup_resources.assert_awaited_once()


class TestCommandStatePreservation:
    """A failed command must not destroy the state of a still-playing stream."""

    async def test_play_media_failure_keeps_current_stream(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """When the replacement stream is rejected, the old ownership/media survive."""
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        player.mass.streams.resolve_stream_url = AsyncMock(return_value="http://new/stream")  # type: ignore[method-assign]
        player._ma_stream_uri = "http://old/stream"
        player._attr_active_source = EDIFIER_PLAYER_ID
        player.set_current_media(uri="http://old/stream", title="Old", clear_all=True)
        mock_pywiim_player.play_url = AsyncMock(side_effect=WiiMError("rejected"))

        with pytest.raises(PlayerCommandFailed):
            await player.play_media(MagicMock())

        assert player._ma_stream_uri == "http://old/stream"
        assert player._attr_active_source == EDIFIER_PLAYER_ID
        assert player._attr_current_media is not None
        assert player._attr_current_media.title == "Old"

    async def test_stop_failure_keeps_current_stream(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """When stop is rejected, ownership/media are left intact for the next refresh."""
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        player._ma_stream_uri = "http://old/stream"
        player._attr_active_source = EDIFIER_PLAYER_ID
        player.set_current_media(uri="http://old/stream", title="Old", clear_all=True)
        mock_pywiim_player.stop = AsyncMock(side_effect=WiiMError("rejected"))

        with pytest.raises(PlayerCommandFailed):
            await player.stop()

        assert player._ma_stream_uri == "http://old/stream"
        assert player._attr_active_source == EDIFIER_PLAYER_ID
        assert player._attr_current_media is not None


class TestProviderLifecycle:
    """Provider teardown must be robust to a partially-initialised state."""

    async def test_unload_without_notify_server_does_not_raise(self) -> None:
        """A teardown after a failed init must not mask the error with AttributeError."""
        provider: Any = WiimProvider.__new__(WiimProvider)
        # notify_server was never assigned (handle_async_init failed partway)
        await provider.unload()  # must not raise

    async def test_unload_unregisters_notify_server(self) -> None:
        """A normal teardown unregisters the NOTIFY route."""
        provider: Any = WiimProvider.__new__(WiimProvider)
        provider.notify_server = MagicMock()
        await provider.unload()
        provider.notify_server.unregister.assert_called_once()


class TestPollingFallback:
    """Without event subscriptions the DMR transport URI must still be polled."""

    async def test_refresh_polls_dmr_when_eventing_inactive(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """When eventing failed, _refresh_state polls the DMR so takeover is detectable."""
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        player.update_state = MagicMock()  # type: ignore[misc,method-assign]
        dmr = MagicMock()
        dmr.async_update = AsyncMock()
        dmr.current_track_uri = "http://other/app.mp3"
        player._dmr_device = dmr
        player._eventing_active = False
        # a long-running unconfirmed MA stream past the handover window
        mock_pywiim_player.play_state = "play"
        mock_pywiim_player.source = "network"
        player._ma_stream_uri = _MA_STREAM_URI
        player._ma_stream_since = time.time() - 120

        await player._refresh_state()

        dmr.async_update.assert_awaited_once()
        assert player._ma_stream_uri is None  # takeover detected via polled live URI

    async def test_refresh_does_not_poll_dmr_when_eventing_active(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """When events are live, the DMR is driven by events, not polled."""
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        player.update_state = MagicMock()  # type: ignore[misc,method-assign]
        dmr = MagicMock()
        dmr.async_update = AsyncMock()
        dmr.current_track_uri = None
        player._dmr_device = dmr
        player._eventing_active = True

        await player._refresh_state()

        dmr.async_update.assert_not_called()


class TestRefreshFailure:
    """A failed poll must mark the device offline and drop stale media/source."""

    async def test_refresh_failure_clears_media_and_source(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """An offline speaker stops publishing its previous media and active source."""
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        player.update_state = MagicMock()  # type: ignore[misc,method-assign]
        player._attr_active_source = EDIFIER_PLAYER_ID
        player.set_current_media(uri="http://old/stream", title="Old", clear_all=True)
        mock_pywiim_player.refresh = AsyncMock(side_effect=WiiMError("offline"))

        await player._refresh_state()

        assert player._attr_available is False
        assert player._attr_current_media is None
        assert player._attr_active_source is None


class TestOptimisticCommandState:
    """Transport/volume commands must publish their result immediately."""

    async def test_play_publishes_playing_and_fast_poll(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A successful resume reports PLAYING at once and switches to fast polling."""
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        player._attr_poll_interval = 30

        await player.play()

        assert player._attr_playback_state == PlaybackState.PLAYING
        assert player._attr_poll_interval == 5

    async def test_pause_publishes_paused(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A successful pause reports PAUSED at once."""
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)

        await player.pause()

        assert player._attr_playback_state == PlaybackState.PAUSED

    async def test_play_media_publishes_playing(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """play_media optimistically reports PLAYING for fire-and-forget play_url."""
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        player.mass.streams.resolve_stream_url = AsyncMock(return_value="http://ma/s")  # type: ignore[method-assign]

        await player.play_media(MagicMock())

        assert player._attr_playback_state == PlaybackState.PLAYING
        assert player._attr_active_source == EDIFIER_PLAYER_ID

    async def test_volume_set_publishes_level(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A successful volume change is reflected immediately."""
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)

        await player.volume_set(37)

        assert player._attr_volume_level == 37


class TestNowPlayingIdentity:
    """Position and media identity must track URI vs metadata changes correctly."""

    def test_external_track_change_resets_position_without_fresh_position(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A new external track (new URI) with no reported position resets the anchor."""
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        mock_pywiim_player.play_state = "play"
        mock_pywiim_player.source = "spotify"

        player._dmr_device = MagicMock(current_track_uri="spotify://1")
        mock_pywiim_player.media_position = 40
        mock_pywiim_player.media_title = "A"
        player._push_state()
        assert player._attr_elapsed_time == 40.0

        player._dmr_device = MagicMock(current_track_uri="spotify://2")
        mock_pywiim_player.media_position = None
        mock_pywiim_player.media_title = "B"
        player._push_state()
        assert player._attr_elapsed_time == 0.0

    def test_radio_metadata_change_keeps_continuous_position(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """A now-playing change on the same stream URI must not reset the position."""
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        mock_pywiim_player.play_state = "play"
        mock_pywiim_player.source = "network"
        player._dmr_device = MagicMock(current_track_uri="http://radio/stream")

        mock_pywiim_player.media_position = 100
        mock_pywiim_player.media_title = "Song A"
        player._push_state()
        assert player._attr_elapsed_time == 100.0

        mock_pywiim_player.media_position = None
        mock_pywiim_player.media_title = "Song B"
        player._push_state()
        assert player._attr_elapsed_time == 100.0  # continuous stream, no reset

    def test_ma_radio_metadata_change_clears_stale_fields(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """MA radio keeps one URI, so a now-playing change must still refresh media."""
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        mock_pywiim_player.play_state = "play"
        mock_pywiim_player.source = "custompushurl"
        player._ma_stream_uri = _MA_STREAM_URI
        player._ma_stream_confirmed = True

        mock_pywiim_player.media_title = "Song A"
        mock_pywiim_player.media_album = "Album A"
        player._push_state()
        first = player._attr_current_media
        assert first is not None
        assert first.album == "Album A"

        mock_pywiim_player.media_title = "Song B"
        mock_pywiim_player.media_album = None
        player._push_state()
        second = player._attr_current_media
        assert second is not None
        assert second.title == "Song B"
        assert second.album is None


class TestPywiimFinders:
    """The provider exposes group-linking finders for pywiim (survives reload)."""

    def test_player_finder_matches_by_host(self) -> None:
        """player_finder returns the pywiim Player registered for a given host."""
        provider: Any = WiimProvider.__new__(WiimProvider)
        lp = MagicMock(spec=LinkPlayPlayer)
        lp.pywiim_player = MagicMock(host="192.168.1.50")
        other = MagicMock()  # not a LinkPlayPlayer
        with patch.object(
            WiimProvider, "players", new_callable=PropertyMock, return_value=[lp, other]
        ):
            assert provider.pywiim_player_finder("192.168.1.50") is lp.pywiim_player
            assert provider.pywiim_player_finder("10.0.0.1") is None
            assert provider.pywiim_all_players_finder() == [lp.pywiim_player]


class TestMaSourceIdentity:
    """MA current_media must keep the source id play_media supplied."""

    def test_ma_media_preserves_source_id_on_metadata_refresh(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """The queue/plugin source id survives when device metadata rebuilds the media."""
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        player._ma_stream_uri = _MA_STREAM_URI
        player._ma_stream_confirmed = True
        player._ma_source_id = "my_plugin_source"
        mock_pywiim_player.play_state = "play"
        mock_pywiim_player.source = "custompushurl"
        mock_pywiim_player.media_title = "Song"

        player._push_state()

        assert player._attr_current_media is not None
        assert player._attr_current_media.source_id == "my_plugin_source"

    def test_unconfirmed_handover_keeps_optimistic_media(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """Before confirmation, the device's stale metadata must not replace the MA media."""
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        player._ma_stream_uri = _MA_STREAM_URI
        player._ma_stream_confirmed = False
        player._ma_stream_since = time.time()  # within the handover window
        player.set_current_media(uri=_MA_STREAM_URI or "", title="New MA Track", clear_all=True)
        mock_pywiim_player.play_state = "play"
        mock_pywiim_player.source = "custompushurl"
        # the device still exposes the previous track's cached metadata
        mock_pywiim_player.media_title = "Old Track"
        mock_pywiim_player.media_artist = "Old Artist"

        player._push_state()

        assert player._attr_active_source == EDIFIER_PLAYER_ID
        assert player._attr_current_media is not None
        assert player._attr_current_media.title == "New MA Track"

    def test_window_expired_publishes_device_metadata_without_eventing(
        self, mock_provider: MagicMock, mock_pywiim_player: MagicMock, mock_upnp_device: MagicMock
    ) -> None:
        """Without eventing, device metadata is trusted once the handover window elapses."""
        player = _make_player(mock_provider, mock_pywiim_player, mock_upnp_device)
        player._ma_stream_uri = _MA_STREAM_URI
        player._ma_stream_confirmed = False
        player._ma_stream_since = time.time() - 120  # past the handover window
        mock_pywiim_player.play_state = "play"
        mock_pywiim_player.source = "custompushurl"
        mock_pywiim_player.media_title = "Live Song"

        player._push_state()

        assert player._attr_current_media is not None
        assert player._attr_current_media.title == "Live Song"
