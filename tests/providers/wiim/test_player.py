"""Tests for WiiM player provider."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import PlaybackState, PlayerFeature
from music_assistant_models.errors import PlayerCommandFailed, UnsupportedFeaturedException
from wiim import PlayingStatus
from wiim.exceptions import (
    WiimDeviceException,
    WiimException,
    WiimInvalidDataException,
    WiimRequestException,
)

from music_assistant.models.player import PlayerMedia
from music_assistant.providers.wiim.constants import (
    BACKEND_GENERIC,
    BACKEND_OFFICIAL,
    PLAYER_ID_PREFIX,
    SOURCE_NETWORK,
)
from music_assistant.providers.wiim.player import SDK_TO_MA_STATE, WiimPlayer


@pytest.fixture
def mock_wiim_device() -> MagicMock:
    """Create a mock WiimDevice."""
    device = MagicMock()
    device.name = "Test WiiM Pro"
    device.udn = "uuid:test-wiim-001"
    device.available = True
    device.volume = 50
    device.is_muted = False
    device.playing_status = None
    device.play_mode = None
    device.current_media = None
    device.model_name = "WiiM Pro"
    device.manufacturer = "Linkplay"
    device.firmware_version = "4.8.1"
    device.ip_address = "192.168.1.100"
    device.supports_http_api = True
    device.supported_input_modes = ("Network", "Bluetooth", "Line In", "Optical In")
    device.async_play = AsyncMock()
    device.async_pause = AsyncMock()
    device.async_stop = AsyncMock()
    device.async_set_volume = AsyncMock()
    device.async_set_mute = AsyncMock()
    device.async_set_play_mode = AsyncMock()
    device.sync_device_duration_and_position = AsyncMock()
    device.async_update_http_status = AsyncMock()
    device.disconnect = AsyncMock()
    device.ensure_subscriptions = AsyncMock()
    device.general_event_callback = None
    device.rendering_control_event_callback = None
    device.av_transport_event_callback = None
    device.play_queue_event_callback = None
    return device


@pytest.fixture
def mock_controller() -> MagicMock:
    """Create a mock WiimController."""
    controller = MagicMock()
    snapshot = MagicMock()
    snapshot.role = "standalone"
    snapshot.leader_udn = "uuid:test-wiim-001"
    snapshot.member_udns = ("uuid:test-wiim-001",)
    controller.get_group_snapshot.return_value = snapshot
    controller.get_group_members.return_value = []
    controller.get_device.return_value = MagicMock()
    controller.async_join_group = AsyncMock()
    controller.async_ungroup_device = AsyncMock()
    return controller


@pytest.fixture
def mock_provider(mock_controller: MagicMock) -> MagicMock:
    """Create a mock WiimProvider."""
    provider = MagicMock()
    provider.wiim_controller = mock_controller
    provider.instance_id = "wiim_test"
    provider.domain = "wiim"
    provider.manifest = MagicMock()
    provider.manifest.domain = "wiim"
    provider.mass = MagicMock()
    provider.mass.players = MagicMock()

    config = MagicMock()
    config.name = None
    config.default_name = "Test WiiM Pro"
    config.enabled = True
    config.player_type = None
    config.get_value = MagicMock(return_value=None)
    provider.mass.config.get_base_player_config.return_value = config
    return provider


class TestSDKStateMapping:
    """Test SDK to MA state mapping."""

    def test_playing_maps_to_playing(self) -> None:
        """PLAYING should map to PlaybackState.PLAYING."""
        assert SDK_TO_MA_STATE[PlayingStatus.PLAYING] == PlaybackState.PLAYING

    def test_paused_maps_to_paused(self) -> None:
        """PAUSED should map to PlaybackState.PAUSED."""
        assert SDK_TO_MA_STATE[PlayingStatus.PAUSED] == PlaybackState.PAUSED

    def test_stopped_maps_to_idle(self) -> None:
        """STOPPED should map to PlaybackState.IDLE."""
        assert SDK_TO_MA_STATE[PlayingStatus.STOPPED] == PlaybackState.IDLE

    def test_loading_maps_to_playing(self) -> None:
        """LOADING should map to PlaybackState.PLAYING."""
        assert SDK_TO_MA_STATE[PlayingStatus.LOADING] == PlaybackState.PLAYING

    def test_all_sdk_states_mapped(self) -> None:
        """All non-UNKNOWN SDK states should have a mapping."""
        for status in PlayingStatus:
            if status != PlayingStatus.UNKNOWN:
                assert status in SDK_TO_MA_STATE, f"{status} not mapped"


class TestFalsePlayingFilter:
    """A uri-less PLAYING report in network mode must not become PLAYING state."""

    def _make_player(self, provider: MagicMock, device: MagicMock) -> WiimPlayer:
        player = WiimPlayer(provider=provider, player_id="uuid:test", device=device)
        player.update_state = MagicMock()  # type: ignore[misc,method-assign]
        return player

    def test_false_playing_ack_is_suppressed(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """
        The transient PLAYING ack without media loaded must keep the previous state.

        The device acks (group) transport commands with a short false PLAYING
        report before any track is loaded; propagating it causes a
        PLAYING->IDLE->PLAYING flicker downstream.
        """
        mock_wiim_device.play_mode = SOURCE_NETWORK
        mock_wiim_device.current_media = None
        mock_wiim_device.playing_status = PlayingStatus.PLAYING
        player = self._make_player(mock_provider, mock_wiim_device)
        player._attr_playback_state = PlaybackState.IDLE

        player._update_ma_state_from_sdk_cache()

        assert player._attr_playback_state == PlaybackState.IDLE

    def test_loading_without_uri_is_suppressed(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """LOADING maps to PLAYING and gets the same uri-less filter."""
        mock_wiim_device.play_mode = SOURCE_NETWORK
        mock_wiim_device.current_media = None
        mock_wiim_device.playing_status = PlayingStatus.LOADING
        player = self._make_player(mock_provider, mock_wiim_device)
        player._attr_playback_state = PlaybackState.IDLE

        player._update_ma_state_from_sdk_cache()

        assert player._attr_playback_state == PlaybackState.IDLE

    def test_playing_kept_when_uri_drops_mid_playback(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """The filter keeps the previous state; it never forces a playing player idle."""
        mock_wiim_device.play_mode = SOURCE_NETWORK
        mock_wiim_device.current_media = None
        mock_wiim_device.playing_status = PlayingStatus.PLAYING
        player = self._make_player(mock_provider, mock_wiim_device)
        player._attr_playback_state = PlaybackState.PLAYING

        player._update_ma_state_from_sdk_cache()

        assert player._attr_playback_state == PlaybackState.PLAYING

    def test_playing_accepted_once_uri_present(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """A PLAYING report with media loaded is a real start and passes through."""
        media = MagicMock()
        media.uri = "http://192.168.1.80:8097/single/abc/queue/item/uuid:test.flac"
        mock_wiim_device.play_mode = SOURCE_NETWORK
        mock_wiim_device.current_media = media
        mock_wiim_device.playing_status = PlayingStatus.PLAYING
        player = self._make_player(mock_provider, mock_wiim_device)
        player._attr_playback_state = PlaybackState.IDLE

        player._update_ma_state_from_sdk_cache()

        assert player._attr_playback_state == PlaybackState.PLAYING

    def test_external_input_playing_without_uri_accepted(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """External inputs legitimately play without a URI and must not be filtered."""
        mock_wiim_device.play_mode = "Line In"
        mock_wiim_device.current_media = None
        mock_wiim_device.playing_status = PlayingStatus.PLAYING
        player = self._make_player(mock_provider, mock_wiim_device)
        player._attr_playback_state = PlaybackState.IDLE

        player._update_ma_state_from_sdk_cache()

        assert player._attr_playback_state == PlaybackState.PLAYING

    def test_unknown_play_mode_trusts_device(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """Without a known play mode the device report is trusted (no suppression)."""
        mock_wiim_device.play_mode = None
        mock_wiim_device.current_media = None
        mock_wiim_device.playing_status = PlayingStatus.PLAYING
        player = self._make_player(mock_provider, mock_wiim_device)
        player._attr_playback_state = PlaybackState.IDLE

        player._update_ma_state_from_sdk_cache()

        assert player._attr_playback_state == PlaybackState.PLAYING

    def test_stopped_report_unaffected(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """The filter only guards PLAYING-mapped reports; STOPPED passes through."""
        mock_wiim_device.play_mode = SOURCE_NETWORK
        mock_wiim_device.current_media = None
        mock_wiim_device.playing_status = PlayingStatus.STOPPED
        player = self._make_player(mock_provider, mock_wiim_device)
        player._attr_playback_state = PlaybackState.PLAYING

        player._update_ma_state_from_sdk_cache()

        assert player._attr_playback_state == PlaybackState.IDLE


class TestSupportedFeatures:
    """Test that required features are declared."""

    def test_play_media_in_features(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """PLAY_MEDIA should be in supported features."""
        player = WiimPlayer(provider=mock_provider, player_id="uuid:test", device=mock_wiim_device)
        assert PlayerFeature.PLAY_MEDIA in player._attr_supported_features

    def test_volume_features(self, mock_provider: MagicMock, mock_wiim_device: MagicMock) -> None:
        """VOLUME_SET and VOLUME_MUTE should be in supported features."""
        player = WiimPlayer(provider=mock_provider, player_id="uuid:test", device=mock_wiim_device)
        assert PlayerFeature.VOLUME_SET in player._attr_supported_features
        assert PlayerFeature.VOLUME_MUTE in player._attr_supported_features

    def test_select_source_in_features(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """SELECT_SOURCE should be in supported features."""
        player = WiimPlayer(provider=mock_provider, player_id="uuid:test", device=mock_wiim_device)
        assert PlayerFeature.SELECT_SOURCE in player._attr_supported_features


class TestGroupMembers:
    """Test group member state handling."""

    def test_unmanaged_group_members_are_ignored(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """Only group members registered in Music Assistant should be reported."""
        managed_udn = "uuid:test-wiim-002"
        unmanaged_udn = "uuid:test-linkplay-001"
        leader_player_id = f"{PLAYER_ID_PREFIX}{mock_wiim_device.udn}"
        managed_player_id = f"{PLAYER_ID_PREFIX}{managed_udn}"
        snapshot = mock_provider.wiim_controller.get_group_snapshot.return_value
        snapshot.role = "leader"
        snapshot.member_udns = (mock_wiim_device.udn, managed_udn, unmanaged_udn)
        mock_provider.wiim_controller.get_group_members.side_effect = ValueError(
            f"Device {unmanaged_udn} is not managed by the controller"
        )
        mock_provider.mass.players.get_player.side_effect = {managed_player_id: MagicMock()}.get
        player = WiimPlayer(
            provider=mock_provider,
            player_id=leader_player_id,
            device=mock_wiim_device,
        )
        player.update_state = MagicMock()  # type: ignore[misc,method-assign]

        player._update_ma_state_from_sdk_cache()

        assert player._attr_group_members == [leader_player_id, managed_player_id]
        mock_provider.wiim_controller.get_group_members.assert_not_called()

    def test_only_unmanaged_group_members_reports_no_group(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """A group without any MA-managed followers should not be reported."""
        snapshot = mock_provider.wiim_controller.get_group_snapshot.return_value
        snapshot.role = "leader"
        snapshot.member_udns = (mock_wiim_device.udn, "uuid:test-linkplay-001")
        mock_provider.mass.players.get_player.return_value = None
        player = WiimPlayer(
            provider=mock_provider,
            player_id=f"{PLAYER_ID_PREFIX}{mock_wiim_device.udn}",
            device=mock_wiim_device,
        )
        player.update_state = MagicMock()  # type: ignore[misc,method-assign]

        player._update_ma_state_from_sdk_cache()

        assert player._attr_group_members == []


class TestMixedGroupReadOnly:
    """An externally-created mixed group is read-only from the official side (until layer 2)."""

    def _leader_with_member(
        self,
        mock_provider: MagicMock,
        mock_wiim_device: MagicMock,
        member_udn: str,
        member_backend: str,
    ) -> WiimPlayer:
        leader_player_id = f"{PLAYER_ID_PREFIX}{mock_wiim_device.udn}"
        member_player_id = f"{PLAYER_ID_PREFIX}{member_udn}"
        snapshot = mock_provider.wiim_controller.get_group_snapshot.return_value
        snapshot.role = "leader"
        snapshot.member_udns = (mock_wiim_device.udn, member_udn)
        member = MagicMock(linkplay_backend=member_backend)
        mock_provider.mass.players.get_player.side_effect = {member_player_id: member}.get
        player = WiimPlayer(
            provider=mock_provider, player_id=leader_player_id, device=mock_wiim_device
        )
        player.update_state = MagicMock()  # type: ignore[misc,method-assign]
        # the shared mixed-group predicate scans registered provider players
        mock_provider.players = [player]
        return player

    def test_generic_member_makes_group_mixed_and_read_only(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """A generic LinkPlay member makes the group mixed; grouping is withdrawn."""
        player = self._leader_with_member(
            mock_provider, mock_wiim_device, "uuid:test-linkplay-001", BACKEND_GENERIC
        )
        player._update_ma_state_from_sdk_cache()
        assert player._in_mixed_group is True
        # the mixed group is still represented read-only (leader first)
        assert player._attr_group_members == [
            f"{PLAYER_ID_PREFIX}{mock_wiim_device.udn}",
            f"{PLAYER_ID_PREFIX}uuid:test-linkplay-001",
        ]
        assert PlayerFeature.SET_MEMBERS not in player.supported_features
        assert player.can_group_with == set()

    def test_official_member_group_is_not_mixed(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """An all-official group is not mixed; grouping stays available."""
        player = self._leader_with_member(
            mock_provider, mock_wiim_device, "uuid:test-wiim-002", BACKEND_OFFICIAL
        )
        player._update_ma_state_from_sdk_cache()
        assert player._in_mixed_group is False
        assert PlayerFeature.SET_MEMBERS in player.supported_features

    def test_mixed_to_same_backend_transition_restores_grouping(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """Leaving a mixed group restores the grouping capability on the next update."""
        player = self._leader_with_member(
            mock_provider, mock_wiim_device, "uuid:test-linkplay-001", BACKEND_GENERIC
        )
        player._update_ma_state_from_sdk_cache()
        assert PlayerFeature.SET_MEMBERS not in player.supported_features
        # the device becomes standalone again
        snapshot = mock_provider.wiim_controller.get_group_snapshot.return_value
        snapshot.role = "standalone"
        snapshot.member_udns = (mock_wiim_device.udn,)
        player._update_ma_state_from_sdk_cache()
        assert PlayerFeature.SET_MEMBERS in player.supported_features

    async def test_set_members_rejected_while_mixed(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """A direct set_members on a mixed group is rejected, not partially applied."""
        player = self._leader_with_member(
            mock_provider, mock_wiim_device, "uuid:test-linkplay-001", BACKEND_GENERIC
        )
        player._update_ma_state_from_sdk_cache()  # populates a mixed group
        with pytest.raises(UnsupportedFeaturedException):
            await player.set_members(player_ids_to_add=["wiim_uuid:other"])

    def test_official_follower_of_generic_leader_is_mixed(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """An official device a generic leader lists as a follower is read-only too."""
        player = WiimPlayer(
            provider=mock_provider,
            player_id=f"{PLAYER_ID_PREFIX}{mock_wiim_device.udn}",
            device=mock_wiim_device,
        )
        player.update_state = MagicMock()  # type: ignore[misc,method-assign]
        player._attr_group_members = []  # a follower manages nothing itself
        generic_leader = MagicMock(
            player_id="wiim_uuid:generic-leader", linkplay_backend=BACKEND_GENERIC
        )
        generic_leader._attr_group_members = [generic_leader.player_id, player.player_id]
        mock_provider.players = [player, generic_leader]
        assert PlayerFeature.SET_MEMBERS not in player.supported_features
        assert player.can_group_with == set()

    def test_can_group_with_excludes_official_peer_in_mixed_group(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """An official peer locked in a generic-led mixed group is not offered as a target."""
        leader = WiimPlayer(
            provider=mock_provider,
            player_id=f"{PLAYER_ID_PREFIX}{mock_wiim_device.udn}",
            device=mock_wiim_device,
        )
        leader.update_state = MagicMock()  # type: ignore[misc,method-assign]
        leader._attr_available = True
        # an official peer that is a follower in a generic-led mixed group
        peer = WiimPlayer(
            provider=mock_provider,
            player_id="wiim_uuid:official-peer",
            device=mock_wiim_device,
        )
        peer.update_state = MagicMock()  # type: ignore[misc,method-assign]
        peer._attr_available = True
        peer._attr_group_members = []
        generic_leader = MagicMock(
            player_id="wiim_uuid:generic-leader", linkplay_backend=BACKEND_GENERIC
        )
        generic_leader._attr_group_members = [generic_leader.player_id, peer.player_id]
        mock_provider.players = [leader, peer, generic_leader]
        assert peer._in_mixed_group is True
        assert peer.player_id not in leader.can_group_with


class TestSourceList:
    """Test dynamic source list construction."""

    @pytest.mark.asyncio
    async def test_setup_adds_device_input_modes(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """setup() should add sources for device-supported input modes."""
        player = WiimPlayer(provider=mock_provider, player_id="uuid:test", device=mock_wiim_device)
        await player.setup()
        source_ids = [s.id for s in player._attr_source_list]
        assert "bluetooth" in source_ids
        assert "line_in" in source_ids
        assert "optical" in source_ids

    @pytest.mark.asyncio
    async def test_setup_adds_passive_sources(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """setup() should add passive sources (AirPlay, Spotify)."""
        player = WiimPlayer(provider=mock_provider, player_id="uuid:test", device=mock_wiim_device)
        await player.setup()
        source_ids = [s.id for s in player._attr_source_list]
        assert "airplay" in source_ids
        assert "spotify" in source_ids

    @pytest.mark.asyncio
    async def test_setup_skips_unknown_input_modes(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """setup() should skip input modes not in INPUT_MODE_SOURCES."""
        mock_wiim_device.supported_input_modes = ("Network", "FutureMode")
        player = WiimPlayer(provider=mock_provider, player_id="uuid:test", device=mock_wiim_device)
        await player.setup()
        source_ids = [s.id for s in player._attr_source_list]
        assert "futuremode" not in source_ids


class TestVolumeCommand:
    """Test the volume command reaches the device."""

    @pytest.mark.asyncio
    async def test_volume_set_delegates_to_device(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """Setting the volume should reach the device and land in the player state."""
        player = WiimPlayer(provider=mock_provider, player_id="uuid:test", device=mock_wiim_device)
        player.update_state = MagicMock()  # type: ignore[misc,method-assign]

        async def _apply_volume(volume_level: int) -> None:
            mock_wiim_device.volume = volume_level

        mock_wiim_device.async_set_volume = AsyncMock(side_effect=_apply_volume)
        await player.volume_set(42)

        mock_wiim_device.async_set_volume.assert_awaited_once_with(42)
        assert player._attr_volume_level == 42


class TestErrorHandling:
    """Test that command errors mark device unavailable."""

    @pytest.mark.asyncio
    async def test_play_error_refreshes_state(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """Play command error should refresh state without marking unavailable."""
        mock_wiim_device.async_play.side_effect = WiimRequestException("timeout")
        player = WiimPlayer(provider=mock_provider, player_id="uuid:test", device=mock_wiim_device)
        player.update_state = MagicMock()  # type: ignore[misc,method-assign]
        await player.play()
        assert player._attr_available is True
        player.update_state.assert_called()

    @pytest.mark.asyncio
    async def test_volume_set_error_refreshes_state(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """Volume set error should refresh state without marking unavailable."""
        mock_wiim_device.async_set_volume.side_effect = WiimDeviceException("disconnected")
        player = WiimPlayer(provider=mock_provider, player_id="uuid:test", device=mock_wiim_device)
        player.update_state = MagicMock()  # type: ignore[misc,method-assign]
        await player.volume_set(50)
        assert player._attr_available is True
        player.update_state.assert_called()

    @pytest.mark.asyncio
    async def test_volume_set_survives_invalid_data_from_device(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """A speaker answering a volume command with something other than OK must not throw."""
        mock_wiim_device.async_set_volume = AsyncMock(
            side_effect=WiimInvalidDataException("did not return 'OK'")
        )
        player = WiimPlayer(provider=mock_provider, player_id="uuid:test", device=mock_wiim_device)
        player.update_state = MagicMock()  # type: ignore[misc,method-assign]
        await player.volume_set(42)
        assert player._attr_available is True

    @pytest.mark.asyncio
    async def test_select_source_survives_invalid_data_from_device(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """A speaker rejecting a source change must not throw out of the command."""
        mock_wiim_device.async_set_play_mode = AsyncMock(
            side_effect=WiimInvalidDataException("did not return 'OK'")
        )
        player = WiimPlayer(provider=mock_provider, player_id="uuid:test", device=mock_wiim_device)
        player.update_state = MagicMock()  # type: ignore[misc,method-assign]
        await player.select_source("bluetooth")
        assert player._attr_available is True

    @pytest.mark.asyncio
    async def test_stop_error_refreshes_state(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """Stop command error should refresh state without marking unavailable."""
        mock_wiim_device.async_stop.side_effect = WiimRequestException("connection lost")
        player = WiimPlayer(provider=mock_provider, player_id="uuid:test", device=mock_wiim_device)
        player.update_state = MagicMock()  # type: ignore[misc,method-assign]
        await player.stop()
        assert player._attr_available is True
        player.update_state.assert_called()

    @pytest.mark.asyncio
    async def test_pause_error_refreshes_state(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """Pause command error should refresh state without marking unavailable."""
        mock_wiim_device.async_pause.side_effect = WiimDeviceException("timeout")
        player = WiimPlayer(provider=mock_provider, player_id="uuid:test", device=mock_wiim_device)
        player.update_state = MagicMock()  # type: ignore[misc,method-assign]
        await player.pause()
        assert player._attr_available is True
        player.update_state.assert_called()


class TestStalePositionOnNewStream:
    """A new stream handed to the device must not inherit the previous position."""

    def _make_player(self, provider: MagicMock, device: MagicMock) -> WiimPlayer:
        player = WiimPlayer(provider=provider, player_id="uuid:test", device=device)
        player.update_state = MagicMock()  # type: ignore[misc,method-assign]
        return player

    @pytest.mark.asyncio
    async def test_play_media_resets_elapsed_time(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """play_media() must clear the stale elapsed_time anchor from prior content."""
        stream_url = "http://192.168.1.80:8097/single/abc/queue/item/uuid:test.flac"
        mock_provider.mass.streams.resolve_stream_url = AsyncMock(return_value=stream_url)
        player = self._make_player(mock_provider, mock_wiim_device)
        player._attr_elapsed_time = 273
        player._attr_elapsed_time_last_updated = 1000.0

        await player.play_media(PlayerMedia(uri="library://track/1", title="Some Track"))

        assert player._attr_elapsed_time == 0
        assert player._attr_elapsed_time_last_updated is not None
        assert player._attr_elapsed_time_last_updated > 1000.0

    @pytest.mark.asyncio
    async def test_play_media_then_sync_position_end_to_end(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """
        The device still reports the previous AirPlay content after play_media.

        Its metadata makes MA rebuild _attr_current_media from the device's own
        uri, so the position guard must not key off _attr_current_media.
        """
        stream_url = "http://192.168.1.80:8097/single/abc/queue/item/uuid:test.flac"
        mock_provider.mass.streams.resolve_stream_url = AsyncMock(return_value=stream_url)

        device_media = MagicMock()
        device_media.uri = "wiimu_airplay"
        device_media.title = "Previous Song"
        device_media.artist = "Previous Artist"
        device_media.album = "Previous Album"
        device_media.position = 273
        mock_wiim_device.play_mode = SOURCE_NETWORK
        mock_wiim_device.current_media = device_media
        mock_wiim_device.playing_status = PlayingStatus.PLAYING

        player = self._make_player(mock_provider, mock_wiim_device)
        player._attr_elapsed_time = 273

        await player.play_media(PlayerMedia(uri="library://track/1", title="New Track"))
        assert player._attr_elapsed_time == 0

        await player._sync_position()

        assert player._attr_elapsed_time == 0

    @pytest.mark.asyncio
    async def test_failed_play_media_releases_the_guard(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """A device that never took our stream must not stay guarded against its own position."""
        stream_url = "http://192.168.1.80:8097/single/abc/queue/item/uuid:test.flac"
        mock_provider.mass.streams.resolve_stream_url = AsyncMock(return_value=stream_url)
        mock_wiim_device.async_play = AsyncMock(side_effect=WiimDeviceException("boom"))
        player = self._make_player(mock_provider, mock_wiim_device)

        await player.play_media(PlayerMedia(uri="library://track/1", title="New Track"))

        device_media = MagicMock()
        device_media.uri = "wiimu_airplay"
        device_media.position = 42
        mock_wiim_device.current_media = device_media

        await player._sync_position()

        assert player._attr_elapsed_time == 42

    @pytest.mark.asyncio
    async def test_sync_position_ignores_position_reported_without_uri(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """The device clears its uri mid-handover while still reporting the old position."""
        stream_url = "http://192.168.1.80:8097/single/abc/queue/item/uuid:test.flac"
        mock_provider.mass.streams.resolve_stream_url = AsyncMock(return_value=stream_url)

        device_media = MagicMock()
        device_media.uri = None
        device_media.title = None
        device_media.artist = None
        device_media.album = None
        device_media.position = 273
        mock_wiim_device.play_mode = SOURCE_NETWORK
        mock_wiim_device.current_media = device_media

        player = self._make_player(mock_provider, mock_wiim_device)

        await player.play_media(PlayerMedia(uri="library://track/1", title="New Track"))
        await player._sync_position()

        assert player._attr_elapsed_time == 0

    @pytest.mark.asyncio
    async def test_sync_position_ignores_foreign_uri(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """The device's position is rejected while it hasn't loaded MA's stream uri."""
        stream_uri = "http://192.168.1.80:8097/single/abc/queue/item/uuid:test.flac"
        player = self._make_player(mock_provider, mock_wiim_device)
        player._ma_stream_uri = stream_uri
        player._attr_elapsed_time = 0
        player._attr_elapsed_time_last_updated = 1000.0

        device_media = MagicMock()
        device_media.uri = "wiimu_airplay"
        device_media.position = 273
        mock_wiim_device.current_media = device_media

        await player._sync_position()

        assert player._attr_elapsed_time == 0
        assert player._attr_elapsed_time_last_updated == 1000.0

    @pytest.mark.asyncio
    async def test_sync_position_accepts_matching_uri_and_clears_guard(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """Once the device reports MA's own uri, its position is trusted and the guard lifts."""
        stream_uri = "http://192.168.1.80:8097/single/abc/queue/item/uuid:test.flac"
        mock_provider.mass.streams.resolve_stream_url = AsyncMock(return_value=stream_uri)
        player = self._make_player(mock_provider, mock_wiim_device)

        device_media = MagicMock()
        device_media.uri = stream_uri
        device_media.position = 12
        mock_wiim_device.current_media = device_media

        await player.play_media(PlayerMedia(uri="library://track/1", title="New Track"))
        player._attr_elapsed_time_last_updated = 1000.0

        await player._sync_position()

        assert player._attr_elapsed_time == 12
        assert player._attr_elapsed_time_last_updated > 1000.0
        assert player._ma_stream_uri is None

        # A later switch to an external source must not be blocked by a stale guard.
        device_media.uri = "wiimu_airplay"
        device_media.position = 55
        await player._sync_position()

        assert player._attr_elapsed_time == 55

    @pytest.mark.asyncio
    async def test_sync_position_accepts_device_when_ma_not_driving_playback(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """With no stream handed over by MA (external source), the device's position is authoritative."""
        player = self._make_player(mock_provider, mock_wiim_device)
        player._ma_stream_uri = None
        player._attr_elapsed_time = 0
        player._attr_elapsed_time_last_updated = 1000.0

        device_media = MagicMock()
        device_media.uri = "wiimu_airplay"
        device_media.position = 42
        mock_wiim_device.current_media = device_media

        await player._sync_position()

        assert player._attr_elapsed_time == 42
        assert player._attr_elapsed_time_last_updated > 1000.0


class TestPollRefreshesTransportState:
    """Polling must correct state the device stopped pushing events for."""

    def _make_player(self, provider: MagicMock, device: MagicMock) -> WiimPlayer:
        player = WiimPlayer(provider=provider, player_id="uuid:test", device=device)
        player.update_state = MagicMock()  # type: ignore[misc,method-assign]
        return player

    @pytest.mark.asyncio
    async def test_poll_fetches_device_status(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """A poll must ask the device for its transport state, not just its position."""
        player = self._make_player(mock_provider, mock_wiim_device)

        await player.poll()

        mock_wiim_device.async_update_http_status.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_poll_applies_stop_reported_after_missed_events(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """A device that went to stop while events were lost must no longer read as paused."""
        player = self._make_player(mock_provider, mock_wiim_device)
        player._attr_playback_state = PlaybackState.PAUSED
        mock_wiim_device.play_mode = SOURCE_NETWORK

        async def _report_stopped() -> None:
            mock_wiim_device.playing_status = PlayingStatus.STOPPED

        mock_wiim_device.async_update_http_status = AsyncMock(side_effect=_report_stopped)

        await player.poll()

        assert player._attr_playback_state == PlaybackState.IDLE

    @pytest.mark.asyncio
    async def test_poll_survives_invalid_data_from_device(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """A 'Failed' or unparsable status response must not escape the poll."""
        mock_wiim_device.async_update_http_status = AsyncMock(
            side_effect=WiimInvalidDataException("Command getStatusEx returned 'Failed'")
        )
        player = self._make_player(mock_provider, mock_wiim_device)

        await player.poll()

        mock_wiim_device.sync_device_duration_and_position.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_poll_skips_status_of_unavailable_device(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """A device the SDK already gave up on must not be queried again by the poll."""
        mock_wiim_device.available = False
        player = self._make_player(mock_provider, mock_wiim_device)

        await player.poll()

        mock_wiim_device.async_update_http_status.assert_not_awaited()


class TestSetMembersTypeSafety:
    """Official grouping must reject cross-backend members instead of crashing."""

    async def test_add_generic_member_raises_unsupported(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """Adding a non-WiimPlayer member raises a typed error rather than AttributeError."""
        leader = WiimPlayer(
            provider=mock_provider,
            player_id=f"{PLAYER_ID_PREFIX}{mock_wiim_device.udn}",
            device=mock_wiim_device,
        )
        # a registered generic LinkPlay player (no .device attribute)
        mock_provider.mass.players.get_player.return_value = MagicMock(spec=[])

        with pytest.raises(UnsupportedFeaturedException):
            await leader.set_members(player_ids_to_add=["wiim_uuid:generic"])

    async def test_add_same_backend_member_joins(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """Adding another official WiiM player joins it via the controller."""
        leader = WiimPlayer(
            provider=mock_provider,
            player_id=f"{PLAYER_ID_PREFIX}{mock_wiim_device.udn}",
            device=mock_wiim_device,
        )
        follower = MagicMock(spec=WiimPlayer)
        follower.player_id = "wiim_uuid:follower"
        follower.device = MagicMock()
        follower.device.udn = "uuid:follower"
        follower._in_mixed_group = False
        mock_provider.mass.players.get_player.return_value = follower

        await leader.set_members(player_ids_to_add=["wiim_uuid:follower"])

        mock_provider.wiim_controller.async_join_group.assert_awaited_once_with(
            mock_wiim_device.udn, ["uuid:follower"]
        )

    def _leader(self, mock_provider: MagicMock, mock_wiim_device: MagicMock) -> WiimPlayer:
        return WiimPlayer(
            provider=mock_provider,
            player_id=f"{PLAYER_ID_PREFIX}{mock_wiim_device.udn}",
            device=mock_wiim_device,
        )

    def _follower(self, player_id: str, udn: str, *, mixed: bool = False) -> MagicMock:
        follower = MagicMock(spec=WiimPlayer)
        follower.player_id = player_id
        follower.device = MagicMock(udn=udn)
        follower._in_mixed_group = mixed
        return follower

    async def test_add_remove_conflict_rejected(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """Adding and removing the same member in one request is rejected before any call."""
        leader = self._leader(mock_provider, mock_wiim_device)
        with pytest.raises(PlayerCommandFailed):
            await leader.set_members(
                player_ids_to_add=["wiim_uuid:x"], player_ids_to_remove=["wiim_uuid:x"]
            )
        mock_provider.wiim_controller.async_join_group.assert_not_awaited()
        mock_provider.wiim_controller.async_ungroup_device.assert_not_awaited()

    async def test_remove_current_member_ungroups(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """Removing a current member ungroups it via the controller."""
        leader = self._leader(mock_provider, mock_wiim_device)
        follower = self._follower("wiim_uuid:follower", "uuid:follower")
        mock_provider.mass.players.get_player.return_value = follower
        snapshot = mock_provider.wiim_controller.get_group_snapshot.return_value
        snapshot.member_udns = (mock_wiim_device.udn, "uuid:follower")
        await leader.set_members(player_ids_to_remove=["wiim_uuid:follower"])
        mock_provider.wiim_controller.async_ungroup_device.assert_awaited_once_with("uuid:follower")

    async def test_valid_batch_removes_all(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """A fully valid multi-member removal ungroups every member."""
        leader = self._leader(mock_provider, mock_wiim_device)
        members = {
            "wiim_uuid:a": self._follower("wiim_uuid:a", "uuid:a"),
            "wiim_uuid:b": self._follower("wiim_uuid:b", "uuid:b"),
        }
        mock_provider.mass.players.get_player.side_effect = lambda pid, *_a, **_k: members.get(pid)
        snapshot = mock_provider.wiim_controller.get_group_snapshot.return_value
        snapshot.member_udns = (mock_wiim_device.udn, "uuid:a", "uuid:b")
        await leader.set_members(player_ids_to_remove=["wiim_uuid:a", "wiim_uuid:b"])
        assert mock_provider.wiim_controller.async_ungroup_device.await_count == 2

    async def test_remove_moved_member_untouched(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """A removal target the leader no longer owns is rejected without any ungroup."""
        leader = self._leader(mock_provider, mock_wiim_device)
        follower = self._follower("wiim_uuid:follower", "uuid:follower")
        mock_provider.mass.players.get_player.return_value = follower
        # the leader's freshest snapshot no longer lists the follower (it moved elsewhere)
        snapshot = mock_provider.wiim_controller.get_group_snapshot.return_value
        snapshot.member_udns = (mock_wiim_device.udn,)
        with pytest.raises(PlayerCommandFailed):
            await leader.set_members(player_ids_to_remove=["wiim_uuid:follower"])
        mock_provider.wiim_controller.async_ungroup_device.assert_not_awaited()

    async def test_remove_member_in_mixed_group_rejected(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """A member that has moved into a read-only mixed group is not ungrouped."""
        leader = self._leader(mock_provider, mock_wiim_device)
        follower = self._follower("wiim_uuid:follower", "uuid:follower", mixed=True)
        mock_provider.mass.players.get_player.return_value = follower
        snapshot = mock_provider.wiim_controller.get_group_snapshot.return_value
        snapshot.member_udns = (mock_wiim_device.udn, "uuid:follower")
        with pytest.raises(PlayerCommandFailed):
            await leader.set_members(player_ids_to_remove=["wiim_uuid:follower"])
        mock_provider.wiim_controller.async_ungroup_device.assert_not_awaited()

    async def test_later_invalid_removal_aborts_batch(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """A later invalid removal target aborts the batch before any ungroup runs."""
        leader = self._leader(mock_provider, mock_wiim_device)
        members = {
            "wiim_uuid:good": self._follower("wiim_uuid:good", "uuid:good"),
            "wiim_uuid:bad": self._follower("wiim_uuid:bad", "uuid:bad"),
        }
        mock_provider.mass.players.get_player.side_effect = lambda pid, *_a, **_k: members.get(pid)
        # only 'good' is a current member; 'bad' has moved away
        snapshot = mock_provider.wiim_controller.get_group_snapshot.return_value
        snapshot.member_udns = (mock_wiim_device.udn, "uuid:good")
        with pytest.raises(PlayerCommandFailed):
            await leader.set_members(player_ids_to_remove=["wiim_uuid:good", "wiim_uuid:bad"])
        mock_provider.wiim_controller.async_ungroup_device.assert_not_awaited()

    async def test_join_sdk_error_propagates_typed(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """A controller error while joining surfaces as a typed failure, not a false success."""
        leader = self._leader(mock_provider, mock_wiim_device)
        mock_provider.mass.players.get_player.return_value = self._follower(
            "wiim_uuid:follower", "uuid:follower"
        )
        cause = WiimException("join boom")
        mock_provider.wiim_controller.async_join_group.side_effect = cause
        with pytest.raises(PlayerCommandFailed) as excinfo:
            await leader.set_members(player_ids_to_add=["wiim_uuid:follower"])
        assert excinfo.value.__cause__ is cause

    async def test_remove_sdk_error_propagates_typed(
        self, mock_provider: MagicMock, mock_wiim_device: MagicMock
    ) -> None:
        """A controller error while ungrouping surfaces as a typed failure with its cause."""
        leader = self._leader(mock_provider, mock_wiim_device)
        mock_provider.mass.players.get_player.return_value = self._follower(
            "wiim_uuid:follower", "uuid:follower"
        )
        snapshot = mock_provider.wiim_controller.get_group_snapshot.return_value
        snapshot.member_udns = (mock_wiim_device.udn, "uuid:follower")
        cause = WiimException("ungroup boom")
        mock_provider.wiim_controller.async_ungroup_device.side_effect = cause
        with pytest.raises(PlayerCommandFailed) as excinfo:
            await leader.set_members(player_ids_to_remove=["wiim_uuid:follower"])
        assert excinfo.value.__cause__ is cause
