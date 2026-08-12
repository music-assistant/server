"""Tests for WiiM player provider."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import PlaybackState, PlayerFeature
from wiim import PlayingStatus
from wiim.exceptions import WiimDeviceException, WiimRequestException

from music_assistant.models.player import PlayerMedia
from music_assistant.providers.wiim.constants import SOURCE_NETWORK
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

    @pytest.mark.skip(
        reason="volume_set inlines the HTTP fix from wiim PR#18 and no longer calls "
        "async_set_volume; re-enable when wiim-sdk 0.1.5+ has been released"
    )
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
