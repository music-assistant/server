"""Tests for Linn/OpenHome Media player integration."""

import asyncio
import time
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from async_upnp_client.profiles.ohmedia import OhmDevice
from music_assistant_models.enums import (
    MediaType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
)
from music_assistant_models.errors import PlayerUnavailableError
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.openhome_media.player import (  # type: ignore[attr-defined]
    OpenHomePlayer,
    ProductSourceType,
    ProductState,
    ServiceId,
    TransportState,
    TransportStateAllowedValues,
    UpnpError,
    VolumeState,
)
from music_assistant.providers.openhome_media.provider import OpenHomePlayerProvider

# =============================================================================
# region Fixtures
# =============================================================================


@pytest.fixture
def mock_provider() -> OpenHomePlayerProvider:
    """Create a mock OpenHomePlayerProvider."""
    provider = AsyncMock(spec=OpenHomePlayerProvider)
    provider.logger = MagicMock()
    provider.logger.getChild.return_value = MagicMock()
    provider.mass = MagicMock()
    provider.mass.streams.resolve_stream_url = AsyncMock(return_value="http://test/stream.mp3")
    provider.mass.player_queues = MagicMock()
    provider.mass.player_queues.get_active_queue = MagicMock(return_value=None)
    return provider


@pytest.fixture
def mock_ohm_device() -> OhmDevice:
    """Create a mock OhmDevice."""
    return AsyncMock(spec=OhmDevice)


@pytest.fixture
def player(mock_provider: OpenHomePlayerProvider, mock_ohm_device: OhmDevice) -> OpenHomePlayer:
    """Create an OpenHomePlayer instance for testing."""
    return OpenHomePlayer(
        provider=mock_provider,
        player_id="uuid:12345678-abcd-efgh-ijkl-123456789abc",
        description_url="http://192.168.1.100/description.xml",
        device=mock_ohm_device,
    )


# endregion

# =============================================================================
# Initialization Tests
# =============================================================================


# mypy: disable-error-code="misc"
class TestInitialization:
    """Tests for player initialization."""

    def test_initial_attributes(self, player: OpenHomePlayer) -> None:
        """Verify initial attribute values after construction."""
        assert player.player_id == "uuid:12345678-abcd-efgh-ijkl-123456789abc"
        assert player.description_url == "http://192.168.1.100/description.xml"
        assert player.last_seen is None
        assert isinstance(player.lock, type(asyncio.Lock()))
        assert player.state_update_pending is False
        assert player.state_update_period_ms == 1000
        assert player._attr_type == PlayerType.PROTOCOL
        assert "Linn/OpenHome Media Player" in player._attr_name

    def test_device_not_connected_on_init(self, player: OpenHomePlayer) -> None:
        """Device should not be connected until setup() is called."""
        assert player.profile is not None  # Passed in constructor
        # But actual network connection should not happen yet


# =============================================================================
# Setup Tests
# =============================================================================


class TestSetup:
    """Test for player setup."""

    @pytest.mark.asyncio
    async def test_setup_success(
        self, player: OpenHomePlayer, mock_provider: OpenHomePlayerProvider
    ) -> None:
        # async def test_setup_success(self, player: OpenHomePlayer, mock_provider: OpenHomePlayerProvider) -> None:
        """Test successful player setup returns True."""
        with (
            patch.object(type(player), "_device_connect", AsyncMock()) as mock_connect,
            patch.object(player.mass.players, "register_or_update", AsyncMock()) as mock_register,
        ):
            result = await player.setup()
            mock_connect.assert_called_once()
            mock_register.assert_called_once_with(player)
            assert result is True


# =============================================================================
# Command Tests
# =============================================================================


class TestPowerCommand:
    """Tests for POWER command."""

    @pytest.mark.asyncio
    async def test_power_on(self, player: OpenHomePlayer, mock_ohm_device: OhmDevice) -> None:
        """Test turning player ON."""
        player.set_available(True)
        await player.power(powered=True)

        cast("AsyncMock", mock_ohm_device.async_product_set_standby).assert_called_once_with(
            False
        )  # standby=False = on

    @pytest.mark.asyncio
    async def test_power_off(self, player: OpenHomePlayer, mock_ohm_device: OhmDevice) -> None:
        """Test turning player OFF."""
        player.set_available(True)
        await player.power(powered=False)

        cast("AsyncMock", mock_ohm_device.async_product_set_standby).assert_called_once_with(
            True
        )  # standby=True = off

    @pytest.mark.asyncio
    async def test_power_error_handling(
        self, player: OpenHomePlayer, mock_ohm_device: OhmDevice
    ) -> None:
        """Test power command handles UPnP errors gracefully."""
        player.set_available(True)
        cast("AsyncMock", mock_ohm_device.async_product_set_standby).side_effect = UpnpError(
            "Connection failed"
        )

        await player.power(powered=True)

        # Should return None on error (decorator behaviour)
        assert player._attr_needs_poll is True


class TestVolumeCommands:
    """Tests for VOLUME commands."""

    @pytest.mark.asyncio
    async def test_volume_set(self, player: OpenHomePlayer, mock_ohm_device: OhmDevice) -> None:
        """Test setting volume level."""
        player.set_available(True)
        await player.volume_set(volume_level=75)

        cast("AsyncMock", mock_ohm_device.async_volume_set).assert_called_once_with(75)

    @pytest.mark.asyncio
    async def test_volume_mute(self, player: OpenHomePlayer, mock_ohm_device: OhmDevice) -> None:
        """Test muting/unmuting."""
        player.set_available(True)

        await player.volume_mute(muted=True)
        cast("AsyncMock", mock_ohm_device.async_volume_set_mute).assert_called_once_with(True)

        await player.volume_mute(muted=False)
        cast("AsyncMock", mock_ohm_device.async_volume_set_mute).assert_called_with(False)


class TestPlaybackCommands:
    """Tests for PLAYBACK commands."""

    @pytest.mark.asyncio
    async def test_play(self, player: OpenHomePlayer, mock_ohm_device: OhmDevice) -> None:
        """Test play command."""
        player.set_available(True)
        await player.play()
        cast("AsyncMock", mock_ohm_device.async_play).assert_called_once()

    @pytest.mark.asyncio
    async def test_play_error(self, player: OpenHomePlayer, mock_ohm_device: OhmDevice) -> None:
        """Test play command error handling."""
        player.set_available(True)
        cast("AsyncMock", mock_ohm_device.async_play).side_effect = UpnpError("Not ready")
        await player.play()
        # Should log warning but not raise

    @pytest.mark.asyncio
    async def test_stop(self, player: OpenHomePlayer, mock_ohm_device: OhmDevice) -> None:
        """Test stop command."""
        player.set_available(True)
        await player.stop()
        cast("AsyncMock", mock_ohm_device.async_stop).assert_called_once()

    @pytest.mark.asyncio
    async def test_pause_when_capable(
        self, player: OpenHomePlayer, mock_ohm_device: OhmDevice
    ) -> None:
        """Test pause when device supports pause."""
        player.set_available(True)
        # mock_ohm_device.get_state_variable_value() = True

        await player.pause()
        cast("AsyncMock", mock_ohm_device.async_pause).assert_called_once()
        cast("AsyncMock", mock_ohm_device.async_stop).assert_not_called()

    @pytest.mark.asyncio
    async def test_pause_when_incapable_fallbacks_to_stop(
        self, player: OpenHomePlayer, mock_ohm_device: OhmDevice
    ) -> None:
        """Test pause falls back to stop when pause not supported."""
        player.set_available(True)
        cast("AsyncMock", mock_ohm_device.get_state_variable_value).return_value = False

        await player.pause()
        cast("AsyncMock", mock_ohm_device.async_pause).assert_not_called()
        cast("AsyncMock", mock_ohm_device.async_stop).assert_called_once()

    @pytest.mark.asyncio
    async def test_next_track(self, player: OpenHomePlayer, mock_ohm_device: OhmDevice) -> None:
        """Test next track command."""
        player.set_available(True)
        await player.next_track()
        cast("AsyncMock", mock_ohm_device.async_playlist_next).assert_called_once()

    @pytest.mark.asyncio
    async def test_previous_track(self, player: OpenHomePlayer, mock_ohm_device: OhmDevice) -> None:
        """Test previous track command."""
        player.set_available(True)
        player.set_available(True)
        await player.previous_track()
        cast("AsyncMock", mock_ohm_device.async_playlist_previous).assert_called_once()

    @pytest.mark.asyncio
    async def test_seek_with_transport_support(
        self, player: OpenHomePlayer, mock_ohm_device: OhmDevice
    ) -> None:
        """Test seek when transport seek is available."""
        player.set_available(True)
        mock_ohm_device.has_transport_seek_second_absolute = True

        await player.seek(position=120)
        cast("AsyncMock", mock_ohm_device.async_transport_seek_second_absolute).assert_called_once()

    @pytest.mark.asyncio
    async def test_seek_without_transport_support_falls_back(
        self, player: OpenHomePlayer, mock_ohm_device: OhmDevice
    ) -> None:
        """Test seek falls back to appropriate method when transport seek not available."""
        player.set_available(True)
        mock_ohm_device.has_transport_seek_second_absolute = False

        player._attr_active_source = ProductSourceType.PLAYLIST
        await player.seek(position=60)
        cast(
            "AsyncMock", mock_ohm_device.async_playlist_seek_second_absolute
        ).assert_called_once_with(60)

        player._attr_active_source = ProductSourceType.RADIO
        await player.seek(position=40)
        cast("AsyncMock", mock_ohm_device.async_radio_seek_second_absolute).assert_called_once_with(
            40
        )


class TestPlayMediaCommand:
    """Tests for PLAY_MEDIA command."""

    @pytest.mark.asyncio
    async def test_play_media_basic(
        self, player: OpenHomePlayer, mock_ohm_device: OhmDevice
    ) -> None:
        """Test that play_media sends stop before starting new media."""
        player.set_available(True)
        media = PlayerMedia(
            uri="http://music/stream",
            media_type=MediaType.TRACK,
            title="Test Track",
        )

        await player.play_media(media)

        cast("AsyncMock", mock_ohm_device.async_stop).assert_called()
        # cast(AsyncMock, player.set_current_media).assert_called_once()

    @pytest.mark.asyncio
    async def test_play_media_radio_source(
        self, player: OpenHomePlayer, mock_ohm_device: OhmDevice
    ) -> None:
        """Test play_media with radio source."""
        with patch.object(
            mock_ohm_device, "has_source_type", return_value=True
        ):  # test is for Radio source type
            media = PlayerMedia(
                uri="http://music/stream",
                media_type=MediaType.TRACK,
                title="Test Track",
            )

            await player.play_media(media)

            cast(
                "AsyncMock", mock_ohm_device.async_product_set_source_index
            ).assert_called_once_with(0)
            cast("AsyncMock", mock_ohm_device.async_radio_set_channel).assert_called()
            cast("AsyncMock", mock_ohm_device.async_radio_play).assert_called()

    @pytest.mark.asyncio
    async def test_play_media_playlist_source(
        self, player: OpenHomePlayer, mock_ohm_device: OhmDevice
    ) -> None:
        """Test play_media with playlist source (non-radio)."""
        with patch.object(
            mock_ohm_device, "has_source_type", return_value=False
        ):  # test is for Radio source type
            media = PlayerMedia(
                uri="http://music/stream",
                media_type=MediaType.TRACK,
                title="Test Track",
            )

            await player.play_media(media)

            cast("AsyncMock", mock_ohm_device.async_playlist_last_id).assert_called_once()
            cast("AsyncMock", mock_ohm_device.async_playlist_insert).assert_called_once()
            cast("AsyncMock", mock_ohm_device.async_playlist_seek_id).assert_called_once()


# =============================================================================
# Event Handling Tests
# =============================================================================


class TestEventHandling:
    """Tests for event handler (_handle_event)."""

    def test_handle_event_no_state_variables(self, player: OpenHomePlayer) -> None:
        """Test event with no state variables triggers poll mode."""
        service = MagicMock(service_id=ServiceId.VOLUME)
        player._handle_event(service, [])
        assert player._attr_needs_poll is True

    def test_handle_event_volume_change(self, player: OpenHomePlayer) -> None:
        """Test handling volume change events."""
        service = MagicMock(service_id=ServiceId.VOLUME.value)
        state_var = MagicMock(value=65)
        state_var.name = (
            VolumeState.VOLUME
        )  # set after creation since name is a reserved MagicMock parameter
        player.state_update_pending = True  #  no scheduled task creation
        player._handle_event(service, [state_var])
        assert player._attr_volume_level == 65

    def test_handle_event_mute_change(self, player: OpenHomePlayer) -> None:
        """Test handling mute change events."""
        service = MagicMock(service_id=ServiceId.VOLUME)
        state_var = MagicMock(value=True)
        state_var.name = VolumeState.MUTE
        player.state_update_pending = True
        player._handle_event(service, [state_var])
        assert player._attr_volume_muted is True

    def test_handle_event_playing_state(self, player: OpenHomePlayer) -> None:
        """Test handling PLAYING transport state."""
        service = MagicMock(service_id=ServiceId.TRANSPORT)
        state_var = MagicMock(value=TransportStateAllowedValues.PLAYING)
        state_var.name = TransportState.TRANSPORT_STATE
        player.state_update_pending = True

        player._handle_event(service, [state_var])

        assert player._attr_playback_state == PlaybackState.PLAYING

    def test_handle_event_paused_state(self, player: OpenHomePlayer) -> None:
        """Test handling PAUSED transport state."""
        service = MagicMock(service_id=ServiceId.PLAYLIST)
        state_var = MagicMock(value=TransportStateAllowedValues.PAUSED)
        state_var.name = TransportState.TRANSPORT_STATE
        player.state_update_pending = True

        player._handle_event(service, [state_var])

        assert player._attr_playback_state == PlaybackState.PAUSED

    def test_handle_event_source_xml_update(
        self, player: OpenHomePlayer, mock_ohm_device: OhmDevice
    ) -> None:
        """Test handling SOURCE_XML change updates only visible sources."""
        player._attr_source_list = []  # initialise as empty
        service = MagicMock(service_id=ServiceId.PRODUCT)
        xml_value = """<Sources>
            <Source><Visible>true</Visible><Name>Playlist</Name><Type>Playlist</Type><SystemName>Playlist</SystemName></Source>
            <Source><Visible>true</Visible><Name>Radio</Name><Type>Radio</Type><SystemName>Radio</SystemName></Source>
            <Source><Visible>false</Visible><Name>CD</Name><Type>Digital</Type><SystemName>TOSLINK1</SystemName></Source>
        </Sources>"""
        state_var = MagicMock(value=xml_value)
        state_var.name = ProductState.SOURCE_XML
        player.state_update_pending = True
        player._handle_event(service, [state_var])

        assert [source.name for source in player._attr_source_list] == ["Playlist", "Radio"]

    def test_handle_event_schedules_deferred_update(self, player: OpenHomePlayer) -> None:
        """Test that state changes schedule deferred update."""
        service = MagicMock(service_id=ServiceId.VOLUME)
        state_var = MagicMock(value=50)
        state_var.name = VolumeState.VOLUME
        player.state_update_pending = False
        with patch.object(player.mass, "create_task") as mock_create_task:
            player._handle_event(service, [state_var])
            mock_create_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_event_doesnt_schedule_duplicate(self, player: OpenHomePlayer) -> None:
        """Test that handle event doesn't schedule new task if already pending."""
        service = MagicMock(service_id=ServiceId.VOLUME)
        state_var = MagicMock(value=42)
        state_var.name = VolumeState.VOLUME
        player.state_update_pending = True
        with patch.object(player.mass, "create_task") as mock_create_task:
            player._handle_event(service, [state_var])
            mock_create_task.assert_not_called()
            assert player.state_update_pending


# =============================================================================
# Feature Detection Tests
# =============================================================================


class TestFeatureDetection:
    """Tests for player feature detection (_set_player_features)."""

    def test_basic_features_always_present(
        self, player: OpenHomePlayer, mock_ohm_device: OhmDevice
    ) -> None:
        """Test that core features are always added."""
        player._set_player_features()

        assert PlayerFeature.PLAY_MEDIA in player._attr_supported_features
        assert PlayerFeature.PAUSE in player._attr_supported_features
        assert PlayerFeature.NEXT_PREVIOUS in player._attr_supported_features

    def test_power_feature_when_standby_supported(
        self, player: OpenHomePlayer, mock_ohm_device: OhmDevice
    ) -> None:
        """Test POWER feature when device supports standby."""
        mock_ohm_device.has_product_standby = True
        player._set_player_features()

        assert PlayerFeature.POWER in player._attr_supported_features

    def test_power_feature_absent_when_standby_unsupported(
        self, player: OpenHomePlayer, mock_ohm_device: OhmDevice
    ) -> None:
        """Test POWER feature absent when device doesn't support standby."""
        mock_ohm_device.has_product_standby = False
        player._set_player_features()

        assert PlayerFeature.POWER not in player._attr_supported_features

    def test_seek_feature_when_transport_seek_supported(
        self, player: OpenHomePlayer, mock_ohm_device: OhmDevice
    ) -> None:
        """Test SEEK feature when transport seek is available."""
        mock_ohm_device.has_transport_seek_second_absolute = True
        player._set_player_features()

        assert PlayerFeature.SEEK in player._attr_supported_features

    def test_select_source_feature_when_supported(
        self, player: OpenHomePlayer, mock_ohm_device: OhmDevice
    ) -> None:
        """Test SELECT_SOURCE feature when source index can be set."""
        mock_ohm_device.has_product_set_source_index = True
        player._set_player_features()

        assert PlayerFeature.SELECT_SOURCE in player._attr_supported_features


# =============================================================================
# Polling Tests
# =============================================================================


class TestPolling:
    """Tests for polling mechanisms."""

    @pytest.mark.asyncio
    async def test_poll_updates_when_subscription_active(
        self, player: OpenHomePlayer, mock_ohm_device: OhmDevice
    ) -> None:
        """Test poll skips update when subscribed."""
        mock_ohm_device.is_subscribed = True
        player._attr_needs_poll = True
        await player.poll()
        cast("AsyncMock", mock_ohm_device.async_update_state_variables).assert_not_called()
        assert player._attr_needs_poll is False

    @pytest.mark.asyncio
    async def test_poll_forces_update_when_needs_poll(
        self, player: OpenHomePlayer, mock_ohm_device: OhmDevice
    ) -> None:
        """Test poll forces update when needs_poll flag is set."""
        mock_ohm_device.is_subscribed = False
        player._attr_needs_poll = True

        await player.poll()

        cast("AsyncMock", mock_ohm_device.async_update_state_variables).assert_called_once_with(
            do_ping=True
        )

    # use fixed timestamp for mock_last_seen, just sometime in the past
    @pytest.mark.parametrize("mock_last_seen", [1234567890.0, None])
    @pytest.mark.asyncio
    async def test_poll_raises_on_device_unavailable(
        self, player: OpenHomePlayer, mock_ohm_device: OhmDevice, mock_last_seen: float | None
    ) -> None:
        """Test poll raises PlayerUnavailableError when device unavailable."""
        mock_ohm_device.is_subscribed = False
        cast("AsyncMock", mock_ohm_device.async_update_state_variables).side_effect = UpnpError(
            "Timeout"
        )
        player.last_seen = mock_last_seen
        with pytest.raises(PlayerUnavailableError):
            await player.poll()


# =============================================================================
# Availability Tests
# =============================================================================


class TestAvailability:
    """Tests for availability tracking."""

    @pytest.mark.parametrize("mock_available", [True, False])
    def test_set_available_flag(self, player: OpenHomePlayer, mock_available: bool) -> None:
        """Test setting availability flag."""
        player.set_available(mock_available)
        assert player._attr_available is mock_available

    def test_poll_interval_based_on_state(self, player: OpenHomePlayer) -> None:
        """Test poll interval varies by playback state."""
        player._attr_playback_state = PlaybackState.PLAYING
        assert player.poll_interval == 5

        player._attr_playback_state = PlaybackState.PAUSED
        assert player.poll_interval == 30

    def test_poll_interval_idle(self, player: OpenHomePlayer) -> None:
        """Test poll interval for idle state."""
        player._attr_playback_state = PlaybackState.IDLE
        assert player.poll_interval == 30


# =============================================================================
# Device Management Tests
# =============================================================================


class TestDeviceManagement:
    """Tests for device connect/disconnect."""

    @pytest.mark.asyncio
    async def test_device_connect_already_connected(
        self, player: OpenHomePlayer, mock_ohm_device: OhmDevice
    ) -> None:
        """Test connect skips when already connected."""
        player.profile = mock_ohm_device

        await player._device_connect()

        # Should return early, not attempt another connection
        cast("AsyncMock", mock_ohm_device.async_subscribe_services).assert_not_called()

    @pytest.mark.asyncio
    async def test_device_disconnect_clears_profile(
        self, player: OpenHomePlayer, mock_ohm_device: OhmDevice
    ) -> None:
        """Test disconnect clears profile reference."""
        player.profile = mock_ohm_device
        player.set_available(True)

        await player._device_disconnect()
        cast("AsyncMock", mock_ohm_device.async_unsubscribe_services).assert_called_once()
        assert player.profile is None

    @pytest.mark.asyncio
    async def test_device_disconnect_handles_not_connected(self, player: OpenHomePlayer) -> None:
        """Test disconnect when not connected doesn't error."""
        player.profile = None

        # Should not raise
        await player._device_disconnect()


# =============================================================================
# Error Recovery Tests
# =============================================================================


class TestErrorRecovery:
    """Tests for error handling and recovery."""

    async def test_decorator_catches_upnp_errors(
        self, player: OpenHomePlayer, mock_ohm_device: OhmDevice
    ) -> None:
        """Test that @catch_request_errors catches UpnpError."""
        player.set_available(True)

        with (
            patch.object(player, "power", wraps=player.power) as wrapped_power,
            patch.object(
                mock_ohm_device, "async_product_set_standby", side_effect=UpnpError("Network error")
            ),
        ):
            result = await wrapped_power(False)
            # catch request error decorator should catch and return None
            assert result is None


# =============================================================================
# Unload Tests
# =============================================================================


class TestUnload:
    """Tests for player unload."""

    @pytest.mark.asyncio
    async def test_on_unload_disconnects(
        self, player: OpenHomePlayer, mock_ohm_device: OhmDevice
    ) -> None:
        """Test unload properly disconnects device."""
        player.profile = mock_ohm_device

        await player.on_unload()

        cast("AsyncMock", mock_ohm_device.async_unsubscribe_services).assert_called_once()
        assert player.profile is None
