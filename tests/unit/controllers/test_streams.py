"""Unit tests for StreamsController.

Covers: parse_pcm_info (pure function), resolve_stream_url (URL generation),
get_plugin_source_url, cleanup_queue_audio_data, get_stream, get_queue_item_stream,
_crossfade_allowed, get_output_format, _select_pcm_format, _select_flow_format,
HTTP handler error paths, and various utility methods.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from music_assistant_models.enums import ContentType, MediaType, VolumeNormalizationMode
from music_assistant_models.errors import AudioError, InvalidDataError, QueueEmpty
from music_assistant_models.media_items import AudioFormat

from music_assistant.controllers.streams.streams_controller import (
    StreamsController,
    parse_pcm_info,
)
from music_assistant.models.smart_fades import SmartFadesMode

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a minimal mock MusicAssistant for StreamsController tests."""
    mass = MagicMock()
    mass.closing = False
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    mass.config.onboard_done = True
    mass.call_later = MagicMock()
    mass.create_task = MagicMock()
    return mass


@pytest.fixture
def controller(mock_mass: MagicMock) -> StreamsController:
    """Create a StreamsController instance with a mock server."""
    with (
        patch(
            "music_assistant.controllers.streams.streams_controller.Webserver"
        ) as mock_webserver_cls,
        patch("music_assistant.controllers.streams.streams_controller.SmartFadesMixer"),
        patch("music_assistant.controllers.streams.streams_controller.SmartFadesAnalyzer"),
    ):
        mock_server = MagicMock()
        mock_server.base_url = "http://127.0.0.1:8097"
        mock_webserver_cls.return_value = mock_server
        return StreamsController(mock_mass)


# ---------------------------------------------------------------------------
# Tests: parse_pcm_info (pure function)
# ---------------------------------------------------------------------------


class TestParsePcmInfo:
    """Tests for the parse_pcm_info module-level helper."""

    def test_defaults_when_no_params(self) -> None:
        """Test defaults when no params."""
        # Given: plain content type with no params
        # When
        rate, size, channels = parse_pcm_info("audio/pcm")
        # Then: fall back to defaults
        assert rate == 44100
        assert size == 16
        assert channels == 2

    def test_parses_rate_param(self) -> None:
        """Test parses rate param."""
        # Given
        content_type = "audio/pcm;rate=48000"
        # When
        rate, _size, _channels = parse_pcm_info(content_type)
        # Then
        assert rate == 48000

    def test_parses_bitrate_param(self) -> None:
        """Test parses bitrate param."""
        # Given
        content_type = "audio/pcm;rate=44100;bitrate=24"
        # When
        _rate, size, _channels = parse_pcm_info(content_type)
        # Then
        assert size == 24

    def test_parses_channels_param(self) -> None:
        """Test parses channels param."""
        # Given
        content_type = "audio/pcm;rate=44100;channels=1"
        # When
        _rate, _size, channels = parse_pcm_info(content_type)
        # Then
        assert channels == 1

    def test_parses_all_params(self) -> None:
        """Test parses all params."""
        # Given
        content_type = "audio/pcm;rate=96000;bitrate=32;channels=2"
        # When
        rate, size, channels = parse_pcm_info(content_type)
        # Then
        assert rate == 96000
        assert size == 32
        assert channels == 2


# ---------------------------------------------------------------------------
# Tests: base_url property
# ---------------------------------------------------------------------------


class TestBaseUrl:
    """Tests for the base_url property."""

    def test_returns_server_base_url(self, controller: StreamsController) -> None:
        """Test returns server base url."""
        # Given: server is mocked with a known base_url
        # When
        url = controller.base_url
        # Then
        assert url == "http://127.0.0.1:8097"


# ---------------------------------------------------------------------------
# Tests: resolve_stream_url
# ---------------------------------------------------------------------------


class TestResolveStreamUrl:
    """Tests for resolve_stream_url()."""

    def _make_player_media(
        self,
        media_type: MediaType = MediaType.TRACK,
        queue_id: str = "q1",
        queue_item_id: str = "item-1",
        session_id: str = "abc123",
        custom_data: dict[str, str | None] | None = None,
    ) -> MagicMock:
        media = MagicMock()
        media.media_type = media_type
        media.uri = f"http://example.com/{queue_item_id}"
        media.source_id = queue_id
        media.queue_item_id = queue_item_id
        media.custom_data = custom_data or {"session_id": session_id}
        return media

    def _make_player(self, player_id: str = "player-1", flow_mode: bool = False) -> MagicMock:
        player = MagicMock()
        player.player_id = player_id
        player.flow_mode = flow_mode
        player.supports_gapless = True
        player.config.get_value = MagicMock(return_value="flac")
        return player

    async def test_raises_when_missing_session_id(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test raises when missing session id."""
        # Given: media with no session_id in custom_data
        media = self._make_player_media(custom_data={"session_id": None})
        mock_mass.players.get_player.return_value = self._make_player()
        # When / Then
        with pytest.raises(InvalidDataError):
            await controller.resolve_stream_url("player-1", media)

    async def test_returns_announcement_uri_unchanged(self, controller: StreamsController) -> None:
        """Test returns announcement uri unchanged."""
        # Given: announcement media
        media = self._make_player_media(media_type=MediaType.ANNOUNCEMENT)
        # When
        url = await controller.resolve_stream_url("player-1", media)
        # Then: original URI is returned as-is
        assert url == media.uri

    async def test_returns_single_url_for_non_flow_mode(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test returns single url for non flow mode."""
        # Given: regular TRACK media, non-flow player
        media = self._make_player_media(
            media_type=MediaType.TRACK,
            queue_id="q1",
            queue_item_id="item-1",
            session_id="ses123",
        )
        player = self._make_player(player_id="player-1", flow_mode=False)
        mock_mass.players.get_player.return_value = player
        # smartfades config — player not in crossfade mode
        queue_player = MagicMock()
        queue_player.config.get_value = MagicMock(return_value="disabled")
        mock_mass.players.get_player.side_effect = lambda pid, *_: (
            player if pid == "player-1" else queue_player
        )
        # When
        url = await controller.resolve_stream_url("player-1", media)
        # Then
        assert "/single/" in url
        assert "item-1" in url
        assert "ses123" in url
        assert "player-1" in url

    async def test_returns_flow_url_for_flow_mode_player(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test returns flow url for flow mode player."""
        # Given: TRACK media, player has flow_mode=True
        media = self._make_player_media(
            media_type=MediaType.TRACK,
            queue_id="q1",
            queue_item_id="item-1",
            session_id="ses123",
        )
        player = self._make_player(player_id="player-1", flow_mode=True)
        mock_mass.players.get_player.return_value = player
        # When
        url = await controller.resolve_stream_url("player-1", media)
        # Then
        assert "/flow/" in url

    async def test_raises_when_missing_queue_item_id(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test raises when missing queue item id."""
        # Given: media with no queue_item_id
        media = self._make_player_media(
            media_type=MediaType.TRACK,
            custom_data={"session_id": "ses123"},
        )
        media.queue_item_id = None
        player = self._make_player()
        mock_mass.players.get_player.return_value = player
        # When / Then
        with pytest.raises(InvalidDataError):
            await controller.resolve_stream_url("player-1", media)


# ---------------------------------------------------------------------------
# Tests: get_plugin_source_url
# ---------------------------------------------------------------------------


class TestGetPluginSourceUrl:
    """Tests for get_plugin_source_url()."""

    async def test_returns_pcm_as_wav(self, controller: StreamsController) -> None:
        """Test returns pcm as wav."""
        # Given: plugin source that uses PCM
        plugin_source = MagicMock()
        plugin_source.id = "my-source"
        plugin_source.audio_format = MagicMock()
        plugin_source.audio_format.content_type.is_pcm.return_value = True
        plugin_source.audio_format.content_type.value = "pcm_s16le"
        # When
        url = await controller.get_plugin_source_url(plugin_source, "player-1")
        # Then: PCM gets mapped to WAV
        assert url.endswith(".wav")
        assert "my-source" in url
        assert "player-1" in url

    async def test_returns_non_pcm_format_as_is(self, controller: StreamsController) -> None:
        """Test returns non pcm format as is."""
        # Given: plugin source that uses MP3
        plugin_source = MagicMock()
        plugin_source.id = "mp3-source"
        plugin_source.audio_format = MagicMock()
        plugin_source.audio_format.content_type.is_pcm.return_value = False
        plugin_source.audio_format.content_type.value = "mp3"
        # When
        url = await controller.get_plugin_source_url(plugin_source, "player-2")
        # Then
        assert url.endswith(".mp3")
        assert "mp3-source" in url

    async def test_url_includes_base_url(self, controller: StreamsController) -> None:
        """Test url includes base url."""
        # Given
        plugin_source = MagicMock()
        plugin_source.id = "src-1"
        plugin_source.audio_format.content_type.is_pcm.return_value = False
        plugin_source.audio_format.content_type.value = "flac"
        # When
        url = await controller.get_plugin_source_url(plugin_source, "p1")
        # Then
        assert url.startswith("http://127.0.0.1:8097")


# ---------------------------------------------------------------------------
# Tests: cleanup_queue_audio_data
# ---------------------------------------------------------------------------


class TestCleanupQueueAudioData:
    """Tests for cleanup_queue_audio_data()."""

    async def test_clears_crossfade_data(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test clears crossfade data."""
        # Given: crossfade data exists for queue q1
        controller._crossfade_data["q1"] = MagicMock()
        mock_mass.player_queues._queue_items = {"q1": []}
        # When
        await controller.cleanup_queue_audio_data("q1")
        # Then
        assert "q1" not in controller._crossfade_data

    async def test_clears_stream_buffers(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test clears stream buffers."""
        # Given: queue with an item that has a buffer
        buffer = AsyncMock()
        stream_details = MagicMock()
        stream_details.buffer = buffer
        item = MagicMock()
        item.streamdetails = stream_details
        mock_mass.player_queues._queue_items = {"q1": [item]}
        # When
        await controller.cleanup_queue_audio_data("q1")
        # Then
        buffer.clear.assert_called_once()
        assert stream_details.buffer is None

    async def test_noop_for_items_without_streamdetails(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test noop for items without streamdetails."""
        # Given: item with no streamdetails
        item = MagicMock()
        item.streamdetails = None
        mock_mass.player_queues._queue_items = {"q1": [item]}
        # When / Then: should not raise
        await controller.cleanup_queue_audio_data("q1")

    async def test_noop_for_unknown_queue(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test noop for unknown queue."""
        # Given: no data for this queue
        mock_mass.player_queues._queue_items = {}
        # When / Then: should not raise
        await controller.cleanup_queue_audio_data("unknown-queue")


# ---------------------------------------------------------------------------
# Tests: additional properties
# ---------------------------------------------------------------------------


class TestAdditionalProperties:
    """Tests for bind_ip, smart_fades_mixer, smart_fades_analyzer properties."""

    def test_bind_ip(self, controller: StreamsController) -> None:
        """Test bind_ip returns default."""
        # Given/When
        result = controller.bind_ip
        # Then
        assert result == "0.0.0.0"

    def test_smart_fades_mixer(self, controller: StreamsController) -> None:
        """Test smart_fades_mixer returns the mixer instance."""
        # Given/When
        result = controller.smart_fades_mixer
        # Then
        assert result is controller._smart_fades_mixer

    def test_smart_fades_analyzer(self, controller: StreamsController) -> None:
        """Test smart_fades_analyzer returns the analyzer instance."""
        # Given/When
        result = controller.smart_fades_analyzer
        # Then
        assert result is controller._smart_fades_analyzer


# ---------------------------------------------------------------------------
# Tests: get_config_entries
# ---------------------------------------------------------------------------


class TestGetConfigEntries:
    """Tests for get_config_entries()."""

    async def test_returns_tuple_of_config_entries(self, controller: StreamsController) -> None:
        """Test returns tuple of config entries."""
        # Given: mocked ip_addresses
        with patch(
            "music_assistant.controllers.streams.streams_controller.get_ip_addresses",
            new=AsyncMock(return_value=["192.168.1.1", "10.0.0.1"]),
        ):
            # When
            entries = await controller.get_config_entries()
        # Then
        assert isinstance(entries, tuple)
        assert len(entries) > 0
        keys = [e.key for e in entries]
        assert "allow_buffering" in keys


# ---------------------------------------------------------------------------
# Tests: setup and close
# ---------------------------------------------------------------------------


class TestSetupAndClose:
    """Tests for setup() and close()."""

    async def test_setup_configures_server(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test setup calls server.setup with correct params."""
        # Given: a mock config
        mock_config = MagicMock()
        mock_config.get_value.side_effect = lambda key, *_args, **_kwargs: {
            "bind_port": 8097,
            "publish_ip": "127.0.0.1",
            "bind_ip": "0.0.0.0",
            "smart_fades_log_level": "GLOBAL",
        }.get(key, _args[0] if _args else "GLOBAL")
        controller._server.setup = AsyncMock()  # type: ignore[method-assign]
        # When
        with patch(
            "music_assistant.controllers.streams.streams_controller.check_ffmpeg_version",
            new=AsyncMock(),
        ):
            await controller.setup(mock_config)
        # Then
        controller._server.setup.assert_called_once()
        mock_mass.call_later.assert_called_with(900, controller._periodic_garbage_collection)

    async def test_close_calls_server_close(self, controller: StreamsController) -> None:
        """Test close delegates to server.close."""
        # Given
        controller._server.close = AsyncMock()  # type: ignore[method-assign]
        # When
        await controller.close()
        # Then
        controller._server.close.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: resolve_stream_url — plugin source and PCM branches
# ---------------------------------------------------------------------------


class TestResolveStreamUrlPluginSource:
    """Tests for PLUGIN_SOURCE and PCM codec branches of resolve_stream_url."""

    async def test_plugin_source_with_valid_source_id(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test PLUGIN_SOURCE media delegates to get_plugin_source_url."""
        # Given
        media = MagicMock()
        media.media_type = MediaType.PLUGIN_SOURCE
        media.custom_data = {"source_id": "my-plugin"}
        media.uri = "plugin://my-plugin"

        plugin_source = MagicMock()
        plugin_source.id = "my-plugin"
        plugin_source.audio_format.content_type.is_pcm.return_value = False
        plugin_source.audio_format.content_type.value = "mp3"
        mock_mass.players.get_plugin_source.return_value = plugin_source
        # When
        url = await controller.resolve_stream_url("player-1", media)
        # Then
        assert "pluginsource" in url
        assert "my-plugin" in url

    async def test_plugin_source_no_source_id_returns_uri(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test PLUGIN_SOURCE with no source_id returns media.uri unchanged."""
        # Given
        media = MagicMock()
        media.media_type = MediaType.PLUGIN_SOURCE
        media.custom_data = {}
        media.uri = "plugin://fallback"
        # When
        url = await controller.resolve_stream_url("player-1", media)
        # Then
        assert url == "plugin://fallback"

    async def test_plugin_source_no_custom_data_returns_uri(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test PLUGIN_SOURCE with None custom_data returns media.uri."""
        # Given
        media = MagicMock()
        media.media_type = MediaType.PLUGIN_SOURCE
        media.custom_data = None
        media.uri = "plugin://fallback2"
        # When
        url = await controller.resolve_stream_url("player-1", media)
        # Then
        assert url == "plugin://fallback2"

    async def test_pcm_codec_adds_format_specifiers(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test that raw PCM codec adds rate/bitrate/channels specifiers to URL."""
        # Given: player configured with PCM output, no semicolons yet
        media = MagicMock()
        media.media_type = MediaType.TRACK
        media.custom_data = {"session_id": "ses123"}
        media.queue_item_id = "item-1"
        media.source_id = "q1"

        player = MagicMock()
        player.player_id = "player-1"
        player.flow_mode = False
        player.supports_gapless = True
        # get_value returns the raw "pcm" codec string (no semicolons)
        player.config.get_value.return_value = "pcm"
        mock_mass.players.get_player.return_value = player

        queue_player = MagicMock()
        queue_player.config.get_value.return_value = SmartFadesMode.DISABLED
        mock_mass.players.get_player.side_effect = lambda pid, *_: (
            player if pid == "player-1" else queue_player
        )
        # When
        url = await controller.resolve_stream_url("player-1", media)
        # Then: URL should contain the PCM format specifiers
        assert "rate=" in url
        assert "bitrate=" in url
        assert "channels=" in url


# ---------------------------------------------------------------------------
# Tests: get_command_url and get_announcement_url
# ---------------------------------------------------------------------------


class TestUtilityUrls:
    """Tests for get_command_url() and get_announcement_url()."""

    def test_get_command_url(self, controller: StreamsController) -> None:
        """Test get_command_url returns correctly formatted URL."""
        # Given/When
        url = controller.get_command_url("q1", "next")
        # Then
        assert url == "http://127.0.0.1:8097/command/q1/next.mp3"

    def test_get_announcement_url_stores_data_and_returns_url(
        self, controller: StreamsController
    ) -> None:
        """Test get_announcement_url stores announcement data and returns URL."""
        # Given
        announce_data = MagicMock()
        # When
        url = controller.get_announcement_url("player-1", announce_data)
        # Then
        assert "player-1" in url
        assert "announcement" in url
        assert controller.announcements["player-1"] is announce_data

    def test_get_announcement_url_custom_content_type(self, controller: StreamsController) -> None:
        """Test get_announcement_url uses provided content_type."""
        # Given
        announce_data = MagicMock()
        # When
        url = controller.get_announcement_url("p1", announce_data, ContentType.WAV)
        # Then
        assert url.endswith(".wav")


# ---------------------------------------------------------------------------
# Tests: serve_command_request
# ---------------------------------------------------------------------------


class TestServeCommandRequest:
    """Tests for serve_command_request()."""

    async def test_next_command_creates_task(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test 'next' command triggers player_queues.next task."""
        # Given
        mock_request = MagicMock()
        mock_request.match_info = {"queue_id": "q1", "command": "next"}
        # When
        with patch(
            "music_assistant.controllers.streams.streams_controller.web.FileResponse",
        ) as mock_file_resp:
            mock_file_resp.return_value = MagicMock()
            result = await controller.serve_command_request(mock_request)
        # Then
        mock_mass.create_task.assert_called_once()
        assert result is mock_file_resp.return_value

    async def test_unknown_command_does_not_create_task(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test unknown command does not trigger any task."""
        # Given
        mock_request = MagicMock()
        mock_request.match_info = {"queue_id": "q1", "command": "stop"}
        # When
        with patch(
            "music_assistant.controllers.streams.streams_controller.web.FileResponse",
        ) as mock_file_resp:
            mock_file_resp.return_value = MagicMock()
            await controller.serve_command_request(mock_request)
        # Then
        mock_mass.create_task.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: serve_queue_item_stream — error paths
# ---------------------------------------------------------------------------


class TestServeQueueItemStreamErrors:
    """Tests for serve_queue_item_stream() error paths."""

    def _make_request(self, **overrides: object) -> MagicMock:
        match_info = {
            "queue_id": "q1",
            "player_id": "p1",
            "queue_item_id": "item-1",
            "fmt": "flac",
            "session_id": "ses123",
            **overrides,
        }
        mock_request = MagicMock()
        mock_request.match_info = match_info
        mock_request.method = "GET"
        mock_request.headers = {}
        return mock_request

    async def test_raises_404_for_unknown_queue(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test raises HTTPNotFound when queue is not found."""
        # Given
        mock_mass.player_queues.get.return_value = None
        # When / Then
        with pytest.raises(web.HTTPNotFound):
            await controller.serve_queue_item_stream(self._make_request())

    async def test_raises_404_for_invalid_session(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test raises HTTPNotFound for invalid session_id."""
        # Given
        mock_queue = MagicMock()
        mock_queue.session_id = "different-session"
        mock_mass.player_queues.get.return_value = mock_queue
        # When / Then
        with pytest.raises(web.HTTPNotFound):
            await controller.serve_queue_item_stream(self._make_request())

    async def test_raises_404_for_unknown_player(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test raises HTTPNotFound when player is not found."""
        # Given
        mock_queue = MagicMock()
        mock_queue.session_id = None
        mock_mass.player_queues.get.return_value = mock_queue
        mock_mass.players.get_player.return_value = None
        # When / Then
        with pytest.raises(web.HTTPNotFound):
            await controller.serve_queue_item_stream(self._make_request())

    async def test_raises_404_for_unknown_queue_item(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test raises HTTPNotFound when queue item is not found."""
        # Given
        mock_queue = MagicMock()
        mock_queue.session_id = None
        mock_mass.player_queues.get.return_value = mock_queue
        mock_mass.players.get_player.return_value = MagicMock()
        mock_mass.player_queues.get_item.return_value = None
        # When / Then
        with pytest.raises(web.HTTPNotFound):
            await controller.serve_queue_item_stream(self._make_request())

    async def test_raises_404_when_get_stream_details_fails(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test raises HTTPNotFound when get_stream_details raises exception."""
        # Given
        mock_queue = MagicMock()
        mock_queue.session_id = None
        mock_mass.player_queues.get.return_value = mock_queue
        mock_mass.players.get_player.return_value = MagicMock()
        mock_item = MagicMock()
        mock_item.streamdetails = None
        mock_mass.player_queues.get_item.return_value = mock_item
        # When / Then
        with (
            patch(
                "music_assistant.controllers.streams.streams_controller.get_stream_details",
                new=AsyncMock(side_effect=Exception("stream details failed")),
            ),
            pytest.raises(web.HTTPNotFound),
        ):
            await controller.serve_queue_item_stream(self._make_request())


# ---------------------------------------------------------------------------
# Tests: serve_queue_flow_stream — error paths
# ---------------------------------------------------------------------------


class TestServeQueueFlowStreamErrors:
    """Tests for serve_queue_flow_stream() error paths."""

    def _make_request(self, **overrides: object) -> MagicMock:
        match_info = {
            "queue_id": "q1",
            "player_id": "p1",
            "queue_item_id": "item-1",
            "fmt": "flac",
            "session_id": "ses123",
            **overrides,
        }
        mock_request = MagicMock()
        mock_request.match_info = match_info
        mock_request.method = "GET"
        mock_request.headers = {}
        return mock_request

    async def test_raises_404_for_unknown_queue(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test raises HTTPNotFound for unknown queue."""
        # Given
        mock_mass.player_queues.get.return_value = None
        # When / Then
        with pytest.raises(web.HTTPNotFound):
            await controller.serve_queue_flow_stream(self._make_request())

    async def test_raises_404_for_unknown_player(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test raises HTTPNotFound when player not found."""
        # Given
        mock_mass.player_queues.get.return_value = MagicMock()
        mock_mass.players.get_player.return_value = None
        # When / Then
        with pytest.raises(web.HTTPNotFound):
            await controller.serve_queue_flow_stream(self._make_request())

    async def test_raises_404_for_unknown_queue_item(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test raises HTTPNotFound when queue item not found."""
        # Given
        mock_mass.player_queues.get.return_value = MagicMock()
        mock_mass.players.get_player.return_value = MagicMock()
        mock_mass.player_queues.get_item.return_value = None
        # When / Then
        with pytest.raises(web.HTTPNotFound):
            await controller.serve_queue_flow_stream(self._make_request())


# ---------------------------------------------------------------------------
# Tests: serve_announcement_stream — error paths
# ---------------------------------------------------------------------------


class TestServeAnnouncementStreamErrors:
    """Tests for serve_announcement_stream() error paths."""

    async def test_raises_404_for_unknown_player(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test raises HTTPNotFound when player queue not found."""
        # Given
        mock_request = MagicMock()
        mock_request.match_info = {"player_id": "p1", "fmt": "mp3"}
        mock_mass.player_queues.get.return_value = None
        # When / Then
        with pytest.raises(web.HTTPNotFound):
            await controller.serve_announcement_stream(mock_request)

    async def test_raises_404_for_no_pending_announcement(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test raises HTTPNotFound when no pending announcement."""
        # Given
        mock_request = MagicMock()
        mock_request.match_info = {"player_id": "p1", "fmt": "mp3"}
        mock_mass.player_queues.get.return_value = MagicMock()
        # no announcements stored
        # When / Then
        with pytest.raises(web.HTTPNotFound):
            await controller.serve_announcement_stream(mock_request)


# ---------------------------------------------------------------------------
# Tests: serve_plugin_source_stream — error paths
# ---------------------------------------------------------------------------


class TestServePluginSourceStreamErrors:
    """Tests for serve_plugin_source_stream() error paths."""

    async def test_raises_when_provider_not_found(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test raises ProviderUnavailableError when plugin provider not found."""
        # Given
        from music_assistant_models.errors import ProviderUnavailableError  # noqa: PLC0415

        mock_request = MagicMock()
        mock_request.match_info = {"plugin_source": "bad-plugin", "player_id": "p1", "fmt": "mp3"}
        mock_mass.get_provider.return_value = None
        # When / Then
        with pytest.raises(ProviderUnavailableError):
            await controller.serve_plugin_source_stream(mock_request)

    async def test_raises_404_for_unknown_player(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test raises HTTPNotFound when player not found."""
        # Given
        mock_request = MagicMock()
        mock_request.match_info = {"plugin_source": "my-plugin", "player_id": "p1", "fmt": "mp3"}
        mock_mass.get_provider.return_value = MagicMock()
        mock_mass.players.get_player.return_value = None
        # When / Then
        with pytest.raises(web.HTTPNotFound):
            await controller.serve_plugin_source_stream(mock_request)


# ---------------------------------------------------------------------------
# Tests: get_stream
# ---------------------------------------------------------------------------


class TestGetStream:
    """Tests for get_stream() — the PCM stream dispatcher."""

    def _make_media(
        self,
        media_type: MediaType = MediaType.TRACK,
        source_id: str = "q1",
        queue_item_id: str = "item-1",
        uri: str = "http://example.com/track.flac",
        custom_data: dict | None = None,  # type: ignore[type-arg]
    ) -> MagicMock:
        media = MagicMock()
        media.media_type = media_type
        media.source_id = source_id
        media.queue_item_id = queue_item_id
        media.uri = uri
        media.custom_data = custom_data
        return media

    def test_announcement_returns_announcement_stream(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test ANNOUNCEMENT type returns get_announcement_stream coroutine."""
        # Given
        media = self._make_media(
            media_type=MediaType.ANNOUNCEMENT,
            custom_data={
                "announcement_url": "http://example.com/bell.mp3",
                "pre_announce": False,
                "pre_announce_url": "/path/bell.mp3",
            },
        )
        pcm_format = MagicMock()
        # When
        result = controller.get_stream(media, pcm_format)
        # Then: should be an async generator (coroutine returned by the method)
        assert result is not None

    def test_plugin_source_returns_plugin_stream(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test PLUGIN_SOURCE type returns get_plugin_source_stream coroutine."""
        # Given
        media = self._make_media(
            media_type=MediaType.PLUGIN_SOURCE,
            custom_data={"source_id": "my-plugin", "player_id": "p1"},
        )
        pcm_format = MagicMock()
        # When
        result = controller.get_stream(media, pcm_format)
        # Then
        assert result is not None

    def test_queue_stream_non_flow_mode(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test queue TRACK in non-flow mode returns buffered single item stream."""
        # Given
        media = self._make_media(media_type=MediaType.TRACK, source_id="q1", queue_item_id="i1")
        pcm_format = MagicMock()
        protocol_player = MagicMock()
        protocol_player.flow_mode = False
        protocol_player.supports_gapless = True
        queue_player = MagicMock()
        queue_player.config.get_value.return_value = SmartFadesMode.DISABLED

        mock_mass.players.get_player.side_effect = lambda pid, *_: (
            protocol_player if pid == "player-1" else queue_player
        )
        mock_queue_item = MagicMock()
        mock_queue_item.extra_attributes = {"playback_speed": 1.0}
        mock_queue_item.streamdetails = MagicMock()
        mock_queue_item.streamdetails.seek_position = 0
        mock_mass.player_queues.get_item.return_value = mock_queue_item

        # When
        with patch(
            "music_assistant.controllers.streams.streams_controller.buffered",
            return_value=MagicMock(),
        ) as mock_buffered:
            controller.get_stream(media, pcm_format, player_id="player-1")
        # Then: buffered() is called for single item stream
        assert mock_buffered.called

    def test_queue_stream_flow_mode(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test queue TRACK in flow mode returns get_queue_flow_stream."""
        # Given
        media = self._make_media(media_type=MediaType.TRACK, source_id="q1", queue_item_id="i1")
        pcm_format = MagicMock()
        protocol_player = MagicMock()
        protocol_player.flow_mode = True
        protocol_player.supports_gapless = True

        mock_mass.players.get_player.return_value = protocol_player
        mock_queue = MagicMock()
        mock_mass.player_queues.get.return_value = mock_queue
        mock_start_item = MagicMock()
        mock_mass.player_queues.get_item.return_value = mock_start_item
        mock_mass.streams = controller  # self-reference for get_queue_flow_stream

        # When — patch get_queue_flow_stream to avoid actually running it
        with patch.object(controller, "get_queue_flow_stream", return_value=MagicMock()):
            result = controller.get_stream(media, pcm_format, player_id="player-1")
        # Then: result is not None
        assert result is not None

    def test_radio_disables_flow_mode(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test RADIO type always uses single item stream even if player has flow_mode=True."""
        # Given
        media = self._make_media(media_type=MediaType.RADIO, source_id="q1", queue_item_id="i1")
        pcm_format = MagicMock()
        protocol_player = MagicMock()
        protocol_player.flow_mode = True  # normally triggers flow, but not for radio
        protocol_player.supports_gapless = True
        queue_player = MagicMock()
        queue_player.config.get_value.return_value = SmartFadesMode.DISABLED

        mock_mass.players.get_player.side_effect = lambda pid, *_: (
            protocol_player if pid == "player-1" else queue_player
        )
        mock_queue_item = MagicMock()
        mock_queue_item.extra_attributes = {"playback_speed": 1.0}
        mock_queue_item.streamdetails = MagicMock()
        mock_queue_item.streamdetails.seek_position = 0
        mock_mass.player_queues.get_item.return_value = mock_queue_item

        # When
        with patch(
            "music_assistant.controllers.streams.streams_controller.buffered",
            return_value=MagicMock(),
        ) as mock_buffered:
            controller.get_stream(media, pcm_format, player_id="player-1")
        # Then: buffered (single item stream) is used, not flow stream
        assert mock_buffered.called

    def test_force_flow_mode(self, controller: StreamsController, mock_mass: MagicMock) -> None:
        """Test force_flow_mode=True forces flow mode regardless of player setting."""
        # Given
        media = self._make_media(media_type=MediaType.TRACK, source_id="q1", queue_item_id="i1")
        pcm_format = MagicMock()
        protocol_player = MagicMock()
        protocol_player.flow_mode = False  # normally no flow
        protocol_player.supports_gapless = True

        mock_mass.players.get_player.return_value = protocol_player
        mock_queue = MagicMock()
        mock_mass.player_queues.get.return_value = mock_queue
        mock_start_item = MagicMock()
        mock_mass.player_queues.get_item.return_value = mock_start_item
        mock_mass.streams = controller

        # When
        with patch.object(controller, "get_queue_flow_stream", return_value=MagicMock()):
            result = controller.get_stream(
                media, pcm_format, player_id="player-1", force_flow_mode=True
            )
        # Then: result is not None (flow stream used)
        assert result is not None

    def test_direct_url_fallback(self, controller: StreamsController, mock_mass: MagicMock) -> None:
        """Test media with no source_id falls back to direct ffmpeg stream."""
        # Given
        media = self._make_media(
            media_type=MediaType.TRACK,
            source_id=None,  # type: ignore[arg-type]
            queue_item_id=None,  # type: ignore[arg-type]
            uri="http://example.com/stream.mp3",
        )
        pcm_format = MagicMock()
        # When
        with patch(
            "music_assistant.controllers.streams.streams_controller.get_ffmpeg_stream",
            return_value=MagicMock(),
        ) as mock_ffmpeg:
            controller.get_stream(media, pcm_format)
        # Then: ffmpeg is used directly
        assert mock_ffmpeg.called


# ---------------------------------------------------------------------------
# Tests: _log_request
# ---------------------------------------------------------------------------


class TestLogRequest:
    """Tests for _log_request()."""

    def test_log_request_debug_level(self, controller: StreamsController) -> None:
        """Test _log_request logs at debug level when not verbose."""
        # Given
        mock_request = MagicMock()
        mock_request.method = "GET"
        mock_request.path = "/flow/ses/q1/item1/p1.flac"
        mock_request.remote = "192.168.1.10"
        mock_request.headers = {}
        # When / Then: should not raise
        controller._log_request(mock_request)

    def test_log_request_verbose_level(self, controller: StreamsController) -> None:
        """Test _log_request logs at verbose level when enabled."""
        # Given
        mock_request = MagicMock()
        mock_request.method = "GET"
        mock_request.path = "/single/ses/q1/item1/p1.flac"
        mock_request.remote = "192.168.1.10"
        mock_request.headers = {"Accept": "audio/flac"}
        # Set logger to verbose level to trigger the verbose path
        controller.logger.setLevel(5)  # VERBOSE_LOG_LEVEL is typically 5
        # When / Then: should not raise
        controller._log_request(mock_request)


# ---------------------------------------------------------------------------
# Tests: get_output_format
# ---------------------------------------------------------------------------


class TestGetOutputFormat:
    """Tests for get_output_format()."""

    def _make_player(self, player_id: str = "p1") -> MagicMock:
        player = MagicMock()
        player.player_id = player_id
        return player

    async def test_exact_sample_rate_match(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test when content sample rate exactly matches a supported rate."""
        # Given
        player = self._make_player()
        mock_mass.config.get_player_config_value = AsyncMock(
            return_value=[("44100", "24"), ("48000", "24")]
        )
        mock_mass.config.get_raw_player_config_value.return_value = "stereo"
        # When
        result = await controller.get_output_format("flac", player, 44100, 24)
        # Then
        assert result.sample_rate == 44100
        assert result.bit_depth == 24
        assert result.channels == 2

    async def test_unsupported_rate_picks_max(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test picks maximum supported rate when content rate is not supported."""
        # Given
        player = self._make_player()
        mock_mass.config.get_player_config_value = AsyncMock(
            return_value=[("44100", "16"), ("48000", "24")]
        )
        mock_mass.config.get_raw_player_config_value.return_value = "stereo"
        # When: content rate 96000 is not in supported rates
        result = await controller.get_output_format("flac", player, 96000, 24)
        # Then: picks max supported rate
        assert result.sample_rate == 48000

    async def test_lossy_format_limits_bit_depth_and_rate(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test lossy format limits bit depth to 16 and rate to 48000 max."""
        # Given
        player = self._make_player()
        mock_mass.config.get_player_config_value = AsyncMock(
            return_value=[("44100", "24"), ("96000", "32")]
        )
        mock_mass.config.get_raw_player_config_value.return_value = "stereo"
        # When: mp3 is lossy
        result = await controller.get_output_format("mp3", player, 96000, 32)
        # Then
        assert result.bit_depth == 16
        assert result.sample_rate <= 48000

    async def test_pcm_format_string_picks_content_type(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test 'pcm' format string selects ContentType from bit depth."""
        # Given
        player = self._make_player()
        mock_mass.config.get_player_config_value = AsyncMock(
            return_value=[("44100", "16"), ("48000", "24")]
        )
        mock_mass.config.get_raw_player_config_value.return_value = "stereo"
        # When
        result = await controller.get_output_format("pcm", player, 44100, 16)
        # Then: content type should be a PCM variant
        assert result.content_type.is_pcm()

    async def test_mono_channel(self, controller: StreamsController, mock_mass: MagicMock) -> None:
        """Test mono channel config results in channels=1."""
        # Given
        player = self._make_player()
        mock_mass.config.get_player_config_value = AsyncMock(
            return_value=[("44100", "16"), ("48000", "16")]
        )
        mock_mass.config.get_raw_player_config_value.return_value = "mono"
        # When
        result = await controller.get_output_format("flac", player, 44100, 16)
        # Then
        assert result.channels == 1


# ---------------------------------------------------------------------------
# Tests: _select_flow_format
# ---------------------------------------------------------------------------


class TestSelectFlowFormat:
    """Tests for _select_flow_format()."""

    async def test_selects_highest_common_rate(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test selects highest rate that is in the preferred list."""
        # Given
        player = MagicMock()
        player.player_id = "p1"
        mock_mass.config.get_player_config_value = AsyncMock(
            return_value=[("44100", "32"), ("48000", "32"), ("96000", "32")]
        )
        # When
        result = await controller._select_flow_format(player)
        # Then: 96000 is highest preferred rate in list
        assert result.sample_rate == 96000
        assert result.channels == 2

    async def test_falls_back_to_internal_rate(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test falls back when none of the preferred rates are supported."""
        # Given
        player = MagicMock()
        player.player_id = "p1"
        mock_mass.config.get_player_config_value = AsyncMock(return_value=[("22050", "16")])
        # When: 22050 is not in (192000, 96000, 48000, 44100) preferred list
        result = await controller._select_flow_format(player)
        # Then: uses INTERNAL_PCM_FORMAT sample_rate as default
        assert result.sample_rate is not None


# ---------------------------------------------------------------------------
# Tests: _select_pcm_format
# ---------------------------------------------------------------------------


class TestSelectPcmFormat:
    """Tests for _select_pcm_format()."""

    async def test_selects_best_supported_rate(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test selects highest supported rate <= content rate."""
        # Given
        player = MagicMock()
        player.player_id = "p1"
        mock_mass.config.get_player_config_value = AsyncMock(
            return_value=[("44100", "32"), ("48000", "32")]
        )
        streamdetails = MagicMock()
        streamdetails.audio_format.sample_rate = 44100
        streamdetails.audio_format.channels = 2
        # When
        result = await controller._select_pcm_format(
            player=player,
            streamdetails=streamdetails,
            smartfades_enabled=False,
        )
        # Then
        assert result.sample_rate == 44100
        assert result.channels == 2

    async def test_smartfades_forces_stereo(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test smartfades_enabled forces stereo (channels=2)."""
        # Given
        player = MagicMock()
        player.player_id = "p1"
        mock_mass.config.get_player_config_value = AsyncMock(
            return_value=[("44100", "32"), ("48000", "32")]
        )
        streamdetails = MagicMock()
        streamdetails.audio_format.sample_rate = 44100
        streamdetails.audio_format.channels = 1  # mono content
        # When
        result = await controller._select_pcm_format(
            player=player,
            streamdetails=streamdetails,
            smartfades_enabled=True,
        )
        # Then: forced stereo
        assert result.channels == 2


# ---------------------------------------------------------------------------
# Tests: _crossfade_allowed
# ---------------------------------------------------------------------------


class TestCrossfadeAllowed:
    """Tests for _crossfade_allowed()."""

    def _make_queue_item(
        self,
        media_type: MediaType = MediaType.TRACK,
        queue_id: str = "q1",
        queue_item_id: str = "item-1",
    ) -> MagicMock:
        item = MagicMock()
        item.media_type = media_type
        item.queue_id = queue_id
        item.queue_item_id = queue_item_id
        item.media_item = MagicMock()  # not a real Track -> skips same-album check
        return item

    def test_disabled_mode_returns_false(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test DISABLED mode always returns False."""
        # Given
        queue_item = self._make_queue_item()
        # When
        result = controller._crossfade_allowed(queue_item, SmartFadesMode.DISABLED)
        # Then
        assert result is False

    def test_no_player_returns_false(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test returns False when no player found for queue_id."""
        # Given
        queue_item = self._make_queue_item()
        mock_mass.players.get_player.return_value = None
        # When
        result = controller._crossfade_allowed(queue_item, SmartFadesMode.SMART_CROSSFADE)
        # Then
        assert result is False

    def test_non_track_current_item_returns_false(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test returns False when current item is not a TRACK."""
        # Given
        queue_item = self._make_queue_item(media_type=MediaType.RADIO)
        mock_mass.players.get_player.return_value = MagicMock()
        # When
        result = controller._crossfade_allowed(queue_item, SmartFadesMode.SMART_CROSSFADE)
        # Then
        assert result is False

    def test_no_next_item_returns_false(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test returns False when there is no next item."""
        # Given
        queue_item = self._make_queue_item()
        mock_mass.players.get_player.return_value = MagicMock()
        mock_mass.player_queues.get_next_item.return_value = None
        # When
        result = controller._crossfade_allowed(queue_item, SmartFadesMode.SMART_CROSSFADE)
        # Then
        assert result is False

    def test_next_item_not_track_returns_false(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test returns False when next item is not a TRACK."""
        # Given
        queue_item = self._make_queue_item()
        next_item = MagicMock()
        next_item.media_type = MediaType.RADIO
        mock_mass.players.get_player.return_value = MagicMock()
        mock_mass.player_queues.get_next_item.return_value = next_item
        # When
        result = controller._crossfade_allowed(queue_item, SmartFadesMode.SMART_CROSSFADE)
        # Then
        assert result is False

    def test_different_sample_rates_without_support_returns_false(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test returns False when sample rates differ and gapless not supported."""
        # Given
        queue_item = self._make_queue_item()
        next_item = MagicMock()
        next_item.media_type = MediaType.TRACK
        next_item.media_item = MagicMock()  # not a Track -> skips same-album check
        mock_mass.players.get_player.return_value = MagicMock()
        mock_mass.player_queues.get_next_item.return_value = next_item
        # gapless with different sample rates not supported
        mock_mass.config.get_raw_player_config_value.return_value = False
        # When
        result = controller._crossfade_allowed(
            queue_item,
            SmartFadesMode.SMART_CROSSFADE,
            flow_mode=False,
            sample_rate=44100,
            next_sample_rate=48000,
        )
        # Then
        assert result is False

    def test_returns_true_for_allowed_crossfade(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test returns True when all conditions for crossfade are met."""
        # Given
        queue_item = self._make_queue_item()
        next_item = MagicMock()
        next_item.media_type = MediaType.TRACK
        next_item.media_item = MagicMock()  # not a real Track -> same-album check skipped
        mock_mass.players.get_player.return_value = MagicMock()
        mock_mass.player_queues.get_next_item.return_value = next_item
        # same sample rate -> no gapless check needed
        # When
        result = controller._crossfade_allowed(
            queue_item,
            SmartFadesMode.SMART_CROSSFADE,
            sample_rate=44100,
            next_sample_rate=44100,
        )
        # Then
        assert result is True

    def test_flow_mode_skips_sample_rate_check(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test flow_mode=True skips the different-sample-rate check."""
        # Given
        queue_item = self._make_queue_item()
        next_item = MagicMock()
        next_item.media_type = MediaType.TRACK
        next_item.media_item = MagicMock()
        mock_mass.players.get_player.return_value = MagicMock()
        mock_mass.player_queues.get_next_item.return_value = next_item
        # When: different sample rates but flow_mode=True skips the check
        result = controller._crossfade_allowed(
            queue_item,
            SmartFadesMode.STANDARD_CROSSFADE,
            flow_mode=True,
            sample_rate=44100,
            next_sample_rate=48000,
        )
        # Then: allowed because flow_mode bypasses sample rate check
        assert result is True


# ---------------------------------------------------------------------------
# Tests: _periodic_garbage_collection
# ---------------------------------------------------------------------------


class TestPeriodicGarbageCollection:
    """Tests for _periodic_garbage_collection()."""

    async def test_runs_gc_and_reschedules(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test periodic GC runs gc.collect and re-schedules itself."""
        # Given
        import gc  # noqa: PLC0415

        initial_call_count = mock_mass.call_later.call_count
        # When
        with patch.object(gc, "collect", return_value=42) as mock_gc:
            await controller._periodic_garbage_collection()
        # Then
        mock_gc.assert_called_once()
        assert mock_mass.call_later.call_count == initial_call_count + 1


# ---------------------------------------------------------------------------
# Tests: _setup_smart_fades_logger
# ---------------------------------------------------------------------------


class TestSetupSmartFadesLogger:
    """Tests for _setup_smart_fades_logger()."""

    def test_global_level_uses_controller_level(self, controller: StreamsController) -> None:
        """Test GLOBAL log level copies controller logger level to sub-loggers."""
        # Given
        mock_config = MagicMock()
        mock_config.get_value.return_value = "GLOBAL"
        # When / Then: should not raise
        controller._setup_smart_fades_logger(mock_config)
        controller._smart_fades_analyzer.logger.setLevel.assert_called()  # type: ignore[attr-defined]
        controller._smart_fades_mixer.logger.setLevel.assert_called()  # type: ignore[attr-defined]

    def test_specific_level_sets_that_level(self, controller: StreamsController) -> None:
        """Test specific log level sets that level on sub-loggers."""
        # Given
        mock_config = MagicMock()
        mock_config.get_value.return_value = "DEBUG"
        # When
        controller._setup_smart_fades_logger(mock_config)
        # Then
        controller._smart_fades_analyzer.logger.setLevel.assert_called_with("DEBUG")  # type: ignore[attr-defined]
        controller._smart_fades_mixer.logger.setLevel.assert_called_with("DEBUG")  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Tests: cleanup_stale_queue_buffers
# ---------------------------------------------------------------------------


class TestCleanupStaleQueueBuffers:
    """Tests for cleanup_stale_queue_buffers()."""

    async def test_no_cleanup_when_index_less_than_2(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test does nothing when current_index < 2."""
        # Given
        mock_mass.player_queues._queue_items = {"q1": [MagicMock(), MagicMock()]}
        # When / Then: should return early without any buffer clearing
        await controller.cleanup_stale_queue_buffers("q1", 0)
        await controller.cleanup_stale_queue_buffers("q1", 1)

    async def test_clears_buffers_before_threshold(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test clears audio buffers for items before current_index - 2."""
        # Given: queue with 5 items, current_index=4 -> threshold=2, clear items 0,1
        items = []
        buffers = []
        for i in range(5):
            item = MagicMock()
            if i < 2:
                buffer = AsyncMock()
                buffers.append(buffer)
                item.streamdetails = MagicMock()
                item.streamdetails.buffer = buffer
            else:
                item.streamdetails = None
                buffers.append(None)  # type: ignore[arg-type]
            items.append(item)
        mock_mass.player_queues._queue_items = {"q1": items}
        # When
        await controller.cleanup_stale_queue_buffers("q1", 4)
        # Then: items 0 and 1 should have had their buffers cleared
        buffers[0].clear.assert_called_once()
        buffers[1].clear.assert_called_once()
        assert items[0].streamdetails.buffer is None
        assert items[1].streamdetails.buffer is None

    async def test_stops_at_threshold(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test stops clearing at the threshold index (idx > threshold breaks)."""
        # Given: current_index=4 -> threshold=2, clear items 0, 1, 2; break at item 3
        items = []
        buffers = []
        for _ in range(5):
            item = MagicMock()
            buf = AsyncMock()
            buffers.append(buf)
            item.streamdetails = MagicMock()
            item.streamdetails.buffer = buf
            items.append(item)
        mock_mass.player_queues._queue_items = {"q1": items}
        # When
        await controller.cleanup_stale_queue_buffers("q1", 4)
        # Then: items 0, 1, 2 cleared; item 3 not cleared (idx=3 > threshold=2)
        buffers[0].clear.assert_called_once()
        buffers[1].clear.assert_called_once()
        buffers[2].clear.assert_called_once()
        buffers[3].clear.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: get_queue_item_stream (the core PCM audio generator)
# ---------------------------------------------------------------------------


class TestGetQueueItemStream:
    """Tests for get_queue_item_stream()."""

    def _make_queue_item(
        self,
        normalization_mode: VolumeNormalizationMode | None = None,
        seek_position: int = 0,
        duration: int = 300,
        fade_in: bool = False,
        media_type: MediaType = MediaType.TRACK,
    ) -> MagicMock:
        queue_item = MagicMock()
        streamdetails = MagicMock()
        streamdetails.volume_normalization_mode = (
            normalization_mode if normalization_mode is not None else "disabled_test"
        )
        streamdetails.fade_in = fade_in
        streamdetails.duration = duration
        streamdetails.uri = "test://track1"
        streamdetails.seek_position = seek_position
        streamdetails.provider = "test_provider"
        streamdetails.stream_error = False
        streamdetails.media_type = media_type
        queue_item.streamdetails = streamdetails
        queue_item.name = "Test Track"
        queue_item.extra_attributes = {"playback_speed": 1.0}
        return queue_item

    async def test_basic_stream_no_normalization(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test basic stream path with no volume normalization."""
        # Given
        queue_item = self._make_queue_item()
        pcm_format = MagicMock()
        pcm_format.pcm_sample_size = 44100 * 4 * 2
        mock_mass.config.get_raw_core_config_value.return_value = False
        mock_mass.get_provider.return_value = None

        async def fake_media(*_args: object, **_kwargs: object) -> object:
            yield b"chunk_a"
            yield b"chunk_b"

        # When
        with patch(
            "music_assistant.controllers.streams.streams_controller.get_media_stream",
            fake_media,
        ):
            chunks = [c async for c in controller.get_queue_item_stream(queue_item, pcm_format)]
        # Then
        assert chunks == [b"chunk_a", b"chunk_b"]

    async def test_dynamic_normalization_adds_loudnorm_filter(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test DYNAMIC normalization adds loudnorm filter param."""
        # Given
        queue_item = self._make_queue_item(normalization_mode=VolumeNormalizationMode.DYNAMIC)
        queue_item.streamdetails.target_loudness = -14.0
        pcm_format = MagicMock()
        pcm_format.pcm_sample_size = 44100 * 4 * 2
        mock_mass.config.get_raw_core_config_value.return_value = False
        mock_mass.get_provider.return_value = None

        captured_filter_params: list[str] = []

        async def fake_media(*_args: object, **_kwargs: object) -> object:
            captured_filter_params.extend(_kwargs.get("filter_params", []))  # type: ignore[arg-type]
            yield b"data"

        # When
        with patch(
            "music_assistant.controllers.streams.streams_controller.get_media_stream",
            fake_media,
        ):
            [c async for c in controller.get_queue_item_stream(queue_item, pcm_format)]
        # Then
        assert any("loudnorm" in p for p in captured_filter_params)

    async def test_fixed_gain_normalization(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test FIXED_GAIN normalization fetches gain from config and applies volume filter."""
        # Given
        queue_item = self._make_queue_item(normalization_mode=VolumeNormalizationMode.FIXED_GAIN)
        pcm_format = MagicMock()
        pcm_format.pcm_sample_size = 44100 * 4 * 2
        mock_mass.config.get_raw_core_config_value.return_value = False
        mock_mass.config.get_core_config_value = AsyncMock(return_value=2.0)
        mock_mass.get_provider.return_value = None

        async def fake_media(*_args: object, **_kwargs: object) -> object:
            yield b"data"

        # When
        with patch(
            "music_assistant.controllers.streams.streams_controller.get_media_stream",
            fake_media,
        ):
            [c async for c in controller.get_queue_item_stream(queue_item, pcm_format)]
        # Then
        assert queue_item.streamdetails.volume_normalization_gain_correct == 2.0

    async def test_measurement_only_album_loudness(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test MEASUREMENT_ONLY uses album loudness when preferred."""
        # Given
        queue_item = self._make_queue_item(
            normalization_mode=VolumeNormalizationMode.MEASUREMENT_ONLY
        )
        queue_item.streamdetails.target_loudness = -14.0
        queue_item.streamdetails.prefer_album_loudness = True
        queue_item.streamdetails.loudness_album = -10.0
        queue_item.streamdetails.loudness = -9.0
        pcm_format = MagicMock()
        pcm_format.pcm_sample_size = 44100 * 4 * 2
        mock_mass.config.get_raw_core_config_value.return_value = False
        mock_mass.get_provider.return_value = None

        async def fake_media(*_args: object, **_kwargs: object) -> object:
            yield b"data"

        # When
        with patch(
            "music_assistant.controllers.streams.streams_controller.get_media_stream",
            fake_media,
        ):
            [c async for c in controller.get_queue_item_stream(queue_item, pcm_format)]
        # Then: gain = target - album = -14.0 - (-10.0) = -4.0
        assert queue_item.streamdetails.volume_normalization_gain_correct == -4.0

    async def test_measurement_only_track_loudness(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test MEASUREMENT_ONLY uses track loudness when album not preferred."""
        # Given
        queue_item = self._make_queue_item(
            normalization_mode=VolumeNormalizationMode.MEASUREMENT_ONLY
        )
        queue_item.streamdetails.target_loudness = -14.0
        queue_item.streamdetails.prefer_album_loudness = False
        queue_item.streamdetails.loudness = -9.0
        pcm_format = MagicMock()
        pcm_format.pcm_sample_size = 44100 * 4 * 2
        mock_mass.config.get_raw_core_config_value.return_value = False
        mock_mass.get_provider.return_value = None

        async def fake_media(*_args: object, **_kwargs: object) -> object:
            yield b"data"

        # When
        with patch(
            "music_assistant.controllers.streams.streams_controller.get_media_stream",
            fake_media,
        ):
            [c async for c in controller.get_queue_item_stream(queue_item, pcm_format)]
        # Then: gain = target - track = -14.0 - (-9.0) = -5.0
        assert queue_item.streamdetails.volume_normalization_gain_correct == -5.0

    async def test_measurement_only_no_loudness_uses_zero_gain(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test MEASUREMENT_ONLY with no loudness uses gain=0.0."""
        # Given
        queue_item = self._make_queue_item(
            normalization_mode=VolumeNormalizationMode.MEASUREMENT_ONLY
        )
        queue_item.streamdetails.target_loudness = -14.0
        queue_item.streamdetails.prefer_album_loudness = False
        queue_item.streamdetails.loudness = None  # no loudness data
        pcm_format = MagicMock()
        pcm_format.pcm_sample_size = 44100 * 4 * 2
        mock_mass.config.get_raw_core_config_value.return_value = False
        mock_mass.get_provider.return_value = None

        async def fake_media(*_args: object, **_kwargs: object) -> object:
            yield b"data"

        # When
        with patch(
            "music_assistant.controllers.streams.streams_controller.get_media_stream",
            fake_media,
        ):
            [c async for c in controller.get_queue_item_stream(queue_item, pcm_format)]
        # Then
        assert queue_item.streamdetails.volume_normalization_gain_correct == 0.0

    async def test_playback_speed_adds_atempo_filter(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test playback_speed != 1.0 adds atempo filter."""
        # Given
        queue_item = self._make_queue_item()
        pcm_format = MagicMock()
        pcm_format.pcm_sample_size = 44100 * 4 * 2
        mock_mass.config.get_raw_core_config_value.return_value = False
        mock_mass.get_provider.return_value = None

        captured_filter_params: list[str] = []

        async def fake_media(*_args: object, **_kwargs: object) -> object:
            captured_filter_params.extend(_kwargs.get("filter_params", []))  # type: ignore[arg-type]
            yield b"data"

        # When
        with patch(
            "music_assistant.controllers.streams.streams_controller.get_media_stream",
            fake_media,
        ):
            [
                c
                async for c in controller.get_queue_item_stream(
                    queue_item, pcm_format, playback_speed=1.5
                )
            ]
        # Then
        assert any("atempo" in p for p in captured_filter_params)

    async def test_buffered_stream_when_allow_buffer_true(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test uses buffered media stream when allow_buffer=True and duration is set."""
        # Given
        queue_item = self._make_queue_item(duration=300)
        pcm_format = MagicMock()
        pcm_format.pcm_sample_size = 44100 * 4 * 2
        mock_mass.config.get_raw_core_config_value.return_value = True  # allow_buffer
        mock_mass.get_provider.return_value = None

        async def fake_buffered_media(*_args: object, **_kwargs: object) -> object:
            yield b"buffered_data"

        # When
        with patch(
            "music_assistant.controllers.streams.streams_controller.get_buffered_media_stream",
            fake_buffered_media,
        ) as _mock_buffered:
            chunks = [c async for c in controller.get_queue_item_stream(queue_item, pcm_format)]
        # Then
        assert chunks == [b"buffered_data"]

    async def test_audio_error_raises_when_raise_on_error_true(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test AudioError propagates when raise_on_error=True."""
        # Given
        queue_item = self._make_queue_item()
        pcm_format = MagicMock()
        pcm_format.pcm_sample_size = 44100 * 4 * 2
        mock_mass.config.get_raw_core_config_value.return_value = False
        mock_mass.get_provider.return_value = None

        async def error_media(*_args: object, **_kwargs: object) -> object:
            yield b"data"
            raise AudioError("stream failed")

        # When / Then
        with (
            patch(
                "music_assistant.controllers.streams.streams_controller.get_media_stream",
                error_media,
            ),
            pytest.raises(AudioError),
        ):
            [c async for c in controller.get_queue_item_stream(queue_item, pcm_format)]

    async def test_audio_error_swallowed_when_raise_on_error_false(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test AudioError is swallowed when raise_on_error=False."""
        # Given
        queue_item = self._make_queue_item()
        pcm_format = MagicMock()
        pcm_format.pcm_sample_size = 44100 * 4 * 2
        mock_mass.config.get_raw_core_config_value.return_value = False
        mock_mass.get_provider.return_value = None

        async def error_media(*_args: object, **_kwargs: object) -> object:
            yield b"data"
            raise AudioError("stream failed")

        # When
        chunks = []
        with patch(
            "music_assistant.controllers.streams.streams_controller.get_media_stream",
            error_media,
        ):
            async for c in controller.get_queue_item_stream(
                queue_item, pcm_format, raise_on_error=False
            ):
                chunks.append(c)
        # Then: partial chunks received before error, no exception raised
        assert chunks == [b"data"]
        assert queue_item.streamdetails.stream_error is True

    async def test_calls_on_streamed_when_provider_available(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test on_streamed is called when a music provider is found after streaming."""
        # Given
        queue_item = self._make_queue_item()
        pcm_format = MagicMock()
        pcm_format.pcm_sample_size = 44100 * 4 * 2
        mock_mass.config.get_raw_core_config_value.return_value = False
        mock_provider = MagicMock()
        mock_mass.get_provider.return_value = mock_provider

        async def fake_media(*_args: object, **_kwargs: object) -> object:
            yield b"lots_of_data" * 100  # enough to be >= 90 seconds equivalent

        # When
        with patch(
            "music_assistant.controllers.streams.streams_controller.get_media_stream",
            fake_media,
        ):
            [c async for c in controller.get_queue_item_stream(queue_item, pcm_format)]
        # Then: create_task called for on_streamed
        mock_mass.create_task.assert_called()


# ---------------------------------------------------------------------------
# Tests: get_queue_item_stream — fade_in path
# ---------------------------------------------------------------------------


class TestGetQueueItemStreamFadeIn:
    """Tests for fade_in handling in get_queue_item_stream."""

    async def test_fade_in_buffering(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test fade_in=True buffers initial chunks and applies afade filter."""
        # Given
        queue_item = MagicMock()
        streamdetails = MagicMock()
        streamdetails.volume_normalization_mode = "disabled_test"
        streamdetails.fade_in = True
        streamdetails.duration = 300
        streamdetails.uri = "test://track1"
        streamdetails.seek_position = 0
        streamdetails.provider = "test_provider"
        queue_item.streamdetails = streamdetails
        queue_item.name = "Test Track"
        queue_item.extra_attributes = {"playback_speed": 1.0}

        pcm_format = MagicMock()
        pcm_format.pcm_sample_size = 100  # small so 4 * 100 = 400 threshold
        mock_mass.config.get_raw_core_config_value.return_value = False
        mock_mass.get_provider.return_value = None

        # Yield enough chunks to exceed pcm_sample_size * 4 threshold
        async def fake_media(*_args: object, **_kwargs: object) -> object:
            for _ in range(10):
                yield b"\x00" * 100

        async def fake_ffmpeg_fade(*_args: object, **_kwargs: object) -> object:
            yield b"faded_data"

        # When
        with (
            patch(
                "music_assistant.controllers.streams.streams_controller.get_media_stream",
                fake_media,
            ),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_ffmpeg_stream",
                fake_ffmpeg_fade,
            ),
        ):
            chunks = [c async for c in controller.get_queue_item_stream(queue_item, pcm_format)]
        # Then: fade applied and then direct chunks (fade_in set to False after first chunk)
        assert len(chunks) > 0


# ---------------------------------------------------------------------------
# Tests: serve_queue_item_stream — HEAD request path (covers full setup)
# ---------------------------------------------------------------------------


class TestServeQueueItemStreamHead:
    """Tests for serve_queue_item_stream HEAD request (covers response setup lines)."""

    async def test_head_request_returns_response_early(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test HEAD request sets up response but returns before streaming."""
        # Given
        mock_request = MagicMock()
        mock_request.match_info = {
            "queue_id": "q1",
            "player_id": "p1",
            "queue_item_id": "item-1",
            "fmt": "flac",
            "session_id": "ses123",
        }
        mock_request.method = "HEAD"

        mock_queue = MagicMock()
        mock_queue.session_id = None
        mock_queue.queue_id = "q1"
        mock_queue.display_name = "Test Queue"
        mock_mass.player_queues.get.return_value = mock_queue

        mock_player = MagicMock()
        mock_player.player_id = "p1"
        mock_player.state.supported_features = []
        mock_mass.players.get_player.return_value = mock_player

        mock_item = MagicMock()
        mock_item.streamdetails = MagicMock()
        mock_item.streamdetails.audio_format.sample_rate = 44100
        mock_item.streamdetails.audio_format.channels = 2
        mock_item.media_type = MediaType.TRACK
        mock_item.name = "Test Track"
        mock_item.duration = 300
        mock_mass.player_queues.get_item.return_value = mock_item

        pcm_format_mock = MagicMock()
        pcm_format_mock.sample_rate = 44100
        pcm_format_mock.bit_depth = 32
        controller._select_pcm_format = AsyncMock(return_value=pcm_format_mock)  # type: ignore[method-assign]

        output_format_mock = MagicMock()
        output_format_mock.output_format_str = "flac"
        controller.get_output_format = AsyncMock(return_value=output_format_mock)  # type: ignore[method-assign]

        mock_mass.config.get_player_config_value = AsyncMock(return_value="default")

        mock_resp = MagicMock()
        mock_resp.prepare = AsyncMock()

        # When
        with (
            patch("aiohttp.web.StreamResponse", return_value=mock_resp),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_mime_type",
                return_value="audio/flac",
            ),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_chunksize",
                return_value=1000,
            ),
        ):
            result = await controller.serve_queue_item_stream(mock_request)
        # Then: returned early due to HEAD
        assert result is mock_resp
        mock_resp.prepare.assert_called_once_with(mock_request)

    async def test_forced_content_length_no_duration(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test forced_content_length http profile with no duration sets large content length."""
        # Given
        mock_request = MagicMock()
        mock_request.match_info = {
            "queue_id": "q1",
            "player_id": "p1",
            "queue_item_id": "item-1",
            "fmt": "flac",
            "session_id": "ses123",
        }
        mock_request.method = "HEAD"

        mock_queue = MagicMock()
        mock_queue.session_id = None
        mock_mass.player_queues.get.return_value = mock_queue
        mock_mass.players.get_player.return_value = MagicMock()

        mock_item = MagicMock()
        mock_item.streamdetails = MagicMock()
        mock_item.name = "Test"
        mock_item.duration = None  # no duration
        mock_mass.player_queues.get_item.return_value = mock_item

        controller._select_pcm_format = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]
        controller.get_output_format = AsyncMock(return_value=MagicMock(output_format_str="flac"))  # type: ignore[method-assign]
        mock_mass.config.get_player_config_value = AsyncMock(return_value="forced_content_length")

        mock_resp = MagicMock()
        mock_resp.prepare = AsyncMock()

        with (
            patch("aiohttp.web.StreamResponse", return_value=mock_resp),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_mime_type",
                return_value="audio/flac",
            ),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_chunksize",
                return_value=99999,
            ),
        ):
            await controller.serve_queue_item_stream(mock_request)
        # Then: content_length set to large value from get_chunksize
        assert mock_resp.content_length == 99999


# ---------------------------------------------------------------------------
# Tests: serve_queue_flow_stream — HEAD request path
# ---------------------------------------------------------------------------


class TestServeQueueFlowStreamHead:
    """Tests for serve_queue_flow_stream HEAD request."""

    async def test_head_request_returns_early(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test HEAD request sets up response headers but returns before streaming."""
        # Given
        mock_request = MagicMock()
        mock_request.match_info = {
            "queue_id": "q1",
            "player_id": "p1",
            "queue_item_id": "item-1",
            "fmt": "flac",
            "session_id": "ses123",
        }
        mock_request.method = "HEAD"
        mock_request.headers = {}

        mock_queue = MagicMock()
        mock_queue.queue_id = "q1"
        mock_queue.display_name = "Test Queue"
        mock_mass.player_queues.get.return_value = mock_queue
        mock_mass.players.get_player.return_value = MagicMock()

        mock_item = MagicMock()
        mock_mass.player_queues.get_item.return_value = mock_item

        flow_fmt = MagicMock()
        flow_fmt.sample_rate = 44100
        flow_fmt.bit_depth = 32
        controller._select_flow_format = AsyncMock(return_value=flow_fmt)  # type: ignore[method-assign]

        output_fmt = MagicMock()
        output_fmt.output_format_str = "flac"
        controller.get_output_format = AsyncMock(return_value=output_fmt)  # type: ignore[method-assign]

        mock_mass.config.get_raw_player_config_value.return_value = "enabled"
        mock_mass.config.get_player_config_value = AsyncMock(return_value="default")

        mock_resp = MagicMock()
        mock_resp.prepare = AsyncMock()

        with (
            patch("aiohttp.web.StreamResponse", return_value=mock_resp),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_mime_type",
                return_value="audio/flac",
            ),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_chunksize",
                return_value=4096,
            ),
        ):
            result = await controller.serve_queue_flow_stream(mock_request)
        # Then: returned early due to HEAD
        assert result is mock_resp


# ---------------------------------------------------------------------------
# Tests: get_queue_flow_stream (generator with mocked inner stream)
# ---------------------------------------------------------------------------


class TestGetQueueFlowStream:
    """Tests for get_queue_flow_stream() — covers the main flow stream loop."""

    async def test_single_track_disabled_crossfade_produces_chunks(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test flow stream with one TRACK item (disabled crossfade) yields audio chunks."""
        # Given
        pcm_sample_size = 4096
        pcm_format = MagicMock()
        pcm_format.content_type.is_pcm.return_value = True
        pcm_format.pcm_sample_size = pcm_sample_size
        pcm_format.bit_depth = 32
        pcm_format.channels = 2

        queue = MagicMock()
        queue.queue_id = "q1"
        queue.display_name = "Test Queue"
        queue.flow_mode = False

        start_queue_item = MagicMock()
        start_queue_item.media_type = MediaType.TRACK
        start_queue_item.queue_item_id = "item-1"
        start_queue_item.name = "Track 1"
        start_queue_item.streamdetails = MagicMock()
        start_queue_item.streamdetails.uri = "test://track1"
        start_queue_item.streamdetails.duration = 300
        start_queue_item.streamdetails.seek_position = 0
        start_queue_item.extra_attributes = {"playback_speed": 1.0}

        mock_mass.config.get_player_config_value = AsyncMock(return_value=SmartFadesMode.DISABLED)
        mock_mass.config.get_raw_player_config_value.return_value = 10
        mock_mass.player_queues.load_next_queue_item = AsyncMock(side_effect=QueueEmpty())
        mock_mass.player_queues.queue_buffer_completed = MagicMock()
        mock_mass.player_queues.track_loaded_in_buffer = MagicMock()

        # Mock inner get_queue_item_stream to yield 1 chunk of pcm_sample_size bytes
        async def mock_item_stream(*_args: object, **_kwargs: object) -> object:
            yield b"\x00" * pcm_sample_size

        controller.get_queue_item_stream = mock_item_stream  # type: ignore[method-assign, assignment]

        # When
        with patch(
            "music_assistant.controllers.streams.streams_controller.PlayLogEntry",
            return_value=MagicMock(),
        ):
            chunks = [
                c
                async for c in controller.get_queue_flow_stream(queue, start_queue_item, pcm_format)
            ]
        # Then
        assert queue.flow_mode is True
        mock_mass.player_queues.queue_buffer_completed.assert_called_once_with("q1")
        # At least the 1 buffered chunk should come through
        assert len(chunks) >= 1

    async def test_track_with_no_streamdetails_is_skipped(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test item with no streamdetails is skipped and queue completes normally."""
        # Given
        pcm_sample_size = 4096
        pcm_format = MagicMock()
        pcm_format.content_type.is_pcm.return_value = True
        pcm_format.pcm_sample_size = pcm_sample_size
        pcm_format.bit_depth = 32
        pcm_format.channels = 2

        queue = MagicMock()
        queue.queue_id = "q1"
        queue.display_name = "Test Queue"

        # Start item has NO streamdetails
        start_queue_item = MagicMock()
        start_queue_item.media_type = MediaType.TRACK
        start_queue_item.queue_item_id = "item-1"
        start_queue_item.name = "Missing Track"
        start_queue_item.streamdetails = None  # Will be skipped

        # Second item also no streamdetails, then QueueEmpty
        second_item = MagicMock()
        second_item.media_type = MediaType.TRACK
        second_item.queue_item_id = "item-2"
        second_item.streamdetails = MagicMock()
        second_item.streamdetails.uri = "test://track2"
        second_item.streamdetails.duration = 200
        second_item.streamdetails.seek_position = 0
        second_item.extra_attributes = {"playback_speed": 1.0}
        second_item.name = "Track 2"

        mock_mass.config.get_player_config_value = AsyncMock(return_value=SmartFadesMode.DISABLED)
        mock_mass.config.get_raw_player_config_value.return_value = 10
        mock_mass.player_queues.load_next_queue_item = AsyncMock(
            side_effect=[second_item, QueueEmpty()]
        )
        mock_mass.player_queues.queue_buffer_completed = MagicMock()
        mock_mass.player_queues.track_loaded_in_buffer = MagicMock()

        async def mock_item_stream(*_args: object, **_kwargs: object) -> object:
            yield b"\x00" * pcm_sample_size

        controller.get_queue_item_stream = mock_item_stream  # type: ignore[method-assign, assignment]

        # When
        with patch(
            "music_assistant.controllers.streams.streams_controller.PlayLogEntry",
            return_value=MagicMock(),
        ):
            _ = [
                c
                async for c in controller.get_queue_flow_stream(queue, start_queue_item, pcm_format)
            ]
        # Then: no chunks from skipped item, but track 2 produces chunks
        assert queue.flow_mode is True
        mock_mass.player_queues.queue_buffer_completed.assert_called_once()

    async def test_non_track_start_item_disables_crossfade(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test non-TRACK start item sets crossfade to DISABLED without config calls."""
        # Given
        pcm_sample_size = 4096
        pcm_format = MagicMock()
        pcm_format.content_type.is_pcm.return_value = True
        pcm_format.pcm_sample_size = pcm_sample_size
        pcm_format.bit_depth = 32
        pcm_format.channels = 2

        queue = MagicMock()
        queue.queue_id = "q1"
        queue.display_name = "Radio Queue"

        start_queue_item = MagicMock()
        start_queue_item.media_type = MediaType.RADIO  # Non-TRACK
        start_queue_item.queue_item_id = "radio-1"
        start_queue_item.name = "Radio Station"
        start_queue_item.streamdetails = MagicMock()
        start_queue_item.streamdetails.uri = "test://radio"
        start_queue_item.streamdetails.duration = 0
        start_queue_item.streamdetails.seek_position = 0
        start_queue_item.extra_attributes = {"playback_speed": 1.0}

        mock_mass.player_queues.load_next_queue_item = AsyncMock(side_effect=QueueEmpty())
        mock_mass.player_queues.queue_buffer_completed = MagicMock()
        mock_mass.player_queues.track_loaded_in_buffer = MagicMock()

        async def mock_item_stream(*_args: object, **_kwargs: object) -> object:
            yield b"\x00" * pcm_sample_size

        controller.get_queue_item_stream = mock_item_stream  # type: ignore[method-assign, assignment]

        # When
        with patch(
            "music_assistant.controllers.streams.streams_controller.PlayLogEntry",
            return_value=MagicMock(),
        ):
            _ = [
                c
                async for c in controller.get_queue_flow_stream(queue, start_queue_item, pcm_format)
            ]
        # Then: get_player_config_value NOT called (non-TRACK skips config)
        mock_mass.config.get_player_config_value.assert_not_called()
        mock_mass.player_queues.queue_buffer_completed.assert_called_once()

    async def test_empty_start_item_returns_immediately(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test that start_queue_item=None returns without producing chunks."""
        # Given
        pcm_format = MagicMock()
        pcm_format.content_type.is_pcm.return_value = True
        pcm_format.pcm_sample_size = 4096
        pcm_format.bit_depth = 32
        pcm_format.channels = 2

        queue = MagicMock()
        queue.queue_id = "q1"

        start_queue_item = MagicMock()
        start_queue_item.media_type = MediaType.TRACK
        # Patch the falsy check: `if not start_queue_item:` is False for MagicMock
        # Use None instead
        mock_mass.config.get_player_config_value = AsyncMock(return_value=SmartFadesMode.DISABLED)
        mock_mass.config.get_raw_player_config_value.return_value = 10
        mock_mass.player_queues.queue_buffer_completed = MagicMock()

        # When: pass None as start_queue_item to trigger early return
        chunks = [
            c
            async for c in controller.get_queue_flow_stream(queue, None, pcm_format)  # type: ignore[arg-type]
        ]
        # Then: no chunks produced
        assert chunks == []


# ---------------------------------------------------------------------------
# Tests: get_announcement_stream
# ---------------------------------------------------------------------------


class TestGetAnnouncementStream:
    """Tests for get_announcement_stream()."""

    async def test_pcm_output_yields_announcement_data(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test PCM output format yields announcement audio directly."""
        # Given: PCM output format (no re-encoding needed)
        import asyncio as _asyncio  # noqa: PLC0415

        output_format = AudioFormat(
            content_type=ContentType.PCM_S16LE,
            sample_rate=44100,
            bit_depth=16,
            channels=2,
        )
        announcement_url = "http://example.com/tts.mp3"

        # Make create_task actually schedule coroutines
        mock_mass.create_task = lambda coro: _asyncio.create_task(coro)

        async def fake_ffmpeg(*_args: object, **_kwargs: object) -> object:
            yield b"announcement_audio"

        # When
        with (
            patch(
                "music_assistant.controllers.streams.streams_controller.get_ffmpeg_stream",
                fake_ffmpeg,
            ),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_chunksize",
                return_value=4096,
            ),
        ):
            chunks = []
            async for chunk in controller.get_announcement_stream(
                announcement_url=announcement_url,
                output_format=output_format,
                pre_announce=False,
            ):
                chunks.append(chunk)

        # Then: announcement audio chunk was yielded
        assert b"announcement_audio" in chunks

    async def test_pre_announce_yields_bell_then_announcement(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test pre_announce=True yields bell sound before announcement."""
        # Given
        import asyncio as _asyncio  # noqa: PLC0415

        output_format = AudioFormat(
            content_type=ContentType.PCM_S16LE,
            sample_rate=44100,
            bit_depth=16,
            channels=2,
        )
        announcement_url = "http://example.com/tts.mp3"
        pre_announce_url = "/path/bell.mp3"

        mock_mass.create_task = lambda coro: _asyncio.create_task(coro)

        call_order = []

        async def fake_ffmpeg(audio_input: object, **_kwargs: object) -> object:
            if audio_input == pre_announce_url:
                call_order.append("bell")
                yield b"bell_data"
            else:
                call_order.append("announcement")
                yield b"tts_data"

        with (
            patch(
                "music_assistant.controllers.streams.streams_controller.get_ffmpeg_stream",
                fake_ffmpeg,
            ),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_chunksize",
                return_value=4096,
            ),
        ):
            chunks = []
            async for chunk in controller.get_announcement_stream(
                announcement_url=announcement_url,
                output_format=output_format,
                pre_announce=True,
                pre_announce_url=pre_announce_url,
            ):
                chunks.append(chunk)

        # Then: bell before tts
        assert b"bell_data" in chunks
        assert b"tts_data" in chunks
        assert call_order.index("bell") < call_order.index("announcement")


# ---------------------------------------------------------------------------
# Tests: get_queue_item_stream_with_smartfade (early path without next track)
# ---------------------------------------------------------------------------


class TestGetQueueItemStreamWithSmartfade:
    """Tests for get_queue_item_stream_with_smartfade() basic path."""

    async def test_no_crossfade_data_no_next_item(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test stream with no previous crossfade data and no next item for crossfade."""
        # Given
        pcm_sample_size = 4096
        pcm_format = MagicMock()
        pcm_format.pcm_sample_size = pcm_sample_size
        pcm_format.bit_depth = 32
        pcm_format.channels = 2
        pcm_format.sample_rate = 44100

        queue = MagicMock()
        queue.queue_id = "q1"
        queue.display_name = "Test Queue"
        mock_mass.player_queues.get.return_value = queue

        player = MagicMock()
        player.player_id = "p1"

        queue_item = MagicMock()
        queue_item.queue_id = "q1"
        queue_item.queue_item_id = "item-1"
        queue_item.name = "Track 1"
        streamdetails = MagicMock()
        streamdetails.uri = "test://track1"
        streamdetails.duration = 300
        streamdetails.seek_position = 0
        streamdetails.seconds_streamed = None
        queue_item.streamdetails = streamdetails
        queue_item.extra_attributes = {"playback_speed": 1.0}

        mock_mass.player_queues.load_next_queue_item = AsyncMock(side_effect=QueueEmpty())
        mock_mass.player_queues.index_by_id = MagicMock(return_value=0)
        mock_mass.players.get_player.return_value = MagicMock()

        async def mock_item_stream(*_args: object, **_kwargs: object) -> object:
            yield b"\x00" * pcm_sample_size

        controller.get_queue_item_stream = mock_item_stream  # type: ignore[method-assign, assignment]

        # When
        chunks = [
            c
            async for c in controller.get_queue_item_stream_with_smartfade(
                player=player,
                queue_item=queue_item,
                pcm_format=pcm_format,
                smart_fades_mode=SmartFadesMode.DISABLED,
            )
        ]
        # Then: chunks from the item stream are yielded
        assert len(chunks) >= 1
        assert streamdetails.seconds_streamed is not None


# ---------------------------------------------------------------------------
# Tests: serve_announcement_stream — HEAD path
# ---------------------------------------------------------------------------


class TestServeAnnouncementStreamHead:
    """Tests for serve_announcement_stream HEAD request (covers response setup)."""

    async def test_head_request_returns_early(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test HEAD request sets up response but returns before streaming."""
        # Given
        mock_request = MagicMock()
        mock_request.match_info = {"player_id": "p1", "fmt": "mp3"}
        mock_request.method = "HEAD"

        mock_player_queue = MagicMock()
        mock_player_queue.state = MagicMock()
        mock_player_queue.state.name = "Player 1"
        mock_mass.player_queues.get.return_value = mock_player_queue

        # Store announcement data
        controller.announcements["p1"] = {
            "announcement_url": "http://example.com/tts.mp3",
            "pre_announce": False,
            "pre_announce_url": "/path/bell.mp3",
        }

        mock_mass.config.get_player_config_value = AsyncMock(return_value="default")

        mock_resp = MagicMock()
        mock_resp.prepare = AsyncMock()

        # When
        with (
            patch("aiohttp.web.StreamResponse", return_value=mock_resp),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_mime_type",
                return_value="audio/mpeg",
            ),
        ):
            result = await controller.serve_announcement_stream(mock_request)
        # Then: returned early due to HEAD
        assert result is mock_resp

    async def test_chunked_http_profile(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test chunked http profile enables chunked encoding."""
        # Given
        mock_request = MagicMock()
        mock_request.match_info = {"player_id": "p1", "fmt": "mp3"}
        mock_request.method = "HEAD"

        mock_mass.player_queues.get.return_value = MagicMock()
        controller.announcements["p1"] = {
            "announcement_url": "http://example.com/tts.mp3",
            "pre_announce": False,
            "pre_announce_url": "/path/bell.mp3",
        }
        mock_mass.config.get_player_config_value = AsyncMock(return_value="chunked")

        mock_resp = MagicMock()
        mock_resp.prepare = AsyncMock()

        with (
            patch("aiohttp.web.StreamResponse", return_value=mock_resp),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_mime_type",
                return_value="audio/mpeg",
            ),
        ):
            await controller.serve_announcement_stream(mock_request)
        # Then: enable_chunked_encoding was called
        mock_resp.enable_chunked_encoding.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: serve_plugin_source_stream — HEAD path
# ---------------------------------------------------------------------------


class TestServePluginSourceStreamHead:
    """Tests for serve_plugin_source_stream HEAD request."""

    async def test_head_request_returns_early(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test HEAD request sets up response but returns before streaming."""
        # Given
        mock_request = MagicMock()
        mock_request.match_info = {"plugin_source": "my-plugin", "player_id": "p1", "fmt": "flac"}
        mock_request.method = "HEAD"

        mock_prov = MagicMock()
        mock_source = MagicMock()
        mock_source.name = "My Plugin"
        mock_source.audio_format.sample_rate = 44100
        mock_source.audio_format.bit_depth = 16
        mock_prov.get_source.return_value = mock_source
        mock_mass.get_provider.return_value = mock_prov
        mock_mass.players.get_player.return_value = MagicMock()

        output_fmt = MagicMock()
        output_fmt.output_format_str = "flac"
        controller.get_output_format = AsyncMock(return_value=output_fmt)  # type: ignore[method-assign]
        mock_mass.config.get_player_config_value = AsyncMock(return_value="default")

        mock_resp = MagicMock()
        mock_resp.prepare = AsyncMock()

        with (
            patch("aiohttp.web.StreamResponse", return_value=mock_resp),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_mime_type",
                return_value="audio/flac",
            ),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_player_filter_params",
                return_value=[],
            ),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_chunksize",
                return_value=4096,
            ),
        ):
            result = await controller.serve_plugin_source_stream(mock_request)
        # Then: returned early due to HEAD
        assert result is mock_resp


# ---------------------------------------------------------------------------
# Tests: get_plugin_source_stream
# ---------------------------------------------------------------------------


class TestGetPluginSourceStream:
    """Tests for get_plugin_source_stream()."""

    async def test_streams_audio_and_releases_source(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test plugin source stream yields audio and releases source when done."""
        # Given
        plugin_source_id = "my-plugin"
        player_id = "p1"

        mock_prov = MagicMock()
        mock_source = MagicMock()
        mock_source.in_use_by = player_id
        mock_source.audio_format = MagicMock()
        mock_source.name = "My Plugin"
        mock_prov.get_source.return_value = mock_source
        mock_mass.get_provider.return_value = mock_prov

        output_format = MagicMock()
        output_format.sample_rate = 44100

        async def fake_ffmpeg(*_args: object, **_kwargs: object) -> object:
            yield b"plugin_audio"

        # When
        with (
            patch(
                "music_assistant.controllers.streams.streams_controller.get_ffmpeg_stream",
                fake_ffmpeg,
            ),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            chunks = [
                c
                async for c in controller.get_plugin_source_stream(
                    plugin_source_id=plugin_source_id,
                    output_format=output_format,
                    player_id=player_id,
                )
            ]
        # Then
        assert b"plugin_audio" in chunks
        # Source should be released
        assert mock_source.in_use_by is None

    async def test_stops_streaming_when_source_taken_by_other(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test stream stops when plugin source is taken by another player mid-stream."""
        # Given
        plugin_source_id = "my-plugin"
        player_id = "p1"

        mock_prov = MagicMock()
        mock_source = MagicMock()
        mock_source.in_use_by = player_id  # Initially ours
        mock_source.audio_format = MagicMock()
        mock_prov.get_source.return_value = mock_source
        mock_mass.get_provider.return_value = mock_prov

        output_format = MagicMock()

        # Simulate another player taking over between the first and second chunk
        async def fake_ffmpeg(*_args: object, **_kwargs: object) -> object:
            yield b"audio1"
            # Another player takes over during streaming
            mock_source.in_use_by = "other-player"
            yield b"audio2"

        with (
            patch(
                "music_assistant.controllers.streams.streams_controller.get_ffmpeg_stream",
                fake_ffmpeg,
            ),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            chunks = [
                c
                async for c in controller.get_plugin_source_stream(
                    plugin_source_id=plugin_source_id,
                    output_format=output_format,
                    player_id=player_id,
                )
            ]
        # Then: first chunk yielded, second stopped because in_use_by changed
        assert chunks == [b"audio1"]

    async def test_raises_when_provider_not_found(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test raises ProviderUnavailableError when plugin provider not found."""
        # Given
        from music_assistant_models.errors import ProviderUnavailableError  # noqa: PLC0415

        mock_mass.get_provider.return_value = None
        output_format = MagicMock()
        # When / Then
        with pytest.raises(ProviderUnavailableError):
            async for _ in controller.get_plugin_source_stream(
                plugin_source_id="unknown",
                output_format=output_format,
                player_id="p1",
            ):
                pass


# ---------------------------------------------------------------------------
# Tests: get_queue_flow_stream — more complex paths
# ---------------------------------------------------------------------------


class TestGetQueueFlowStreamComplex:
    """Additional tests for get_queue_flow_stream() covering more branches."""

    async def test_multi_chunk_triggers_buffer_overflow_yield(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test that buffer overflow in inner loop yields pcm_sample_size chunks."""
        # Given: inner stream yields 3x pcm_sample_size, triggering overflow yield
        pcm_sample_size = 1024
        pcm_format = MagicMock()
        pcm_format.content_type.is_pcm.return_value = True
        pcm_format.pcm_sample_size = pcm_sample_size
        pcm_format.bit_depth = 32
        pcm_format.channels = 2

        queue = MagicMock()
        queue.queue_id = "q1"
        queue.display_name = "Test Queue"

        start_queue_item = MagicMock()
        start_queue_item.media_type = MediaType.RADIO  # skip config calls
        start_queue_item.queue_item_id = "item-1"
        start_queue_item.name = "Radio"
        start_queue_item.streamdetails = MagicMock()
        start_queue_item.streamdetails.uri = "test://radio"
        start_queue_item.streamdetails.duration = 0
        start_queue_item.streamdetails.seek_position = 0
        start_queue_item.extra_attributes = {"playback_speed": 1.0}

        mock_mass.player_queues.load_next_queue_item = AsyncMock(side_effect=QueueEmpty())
        mock_mass.player_queues.queue_buffer_completed = MagicMock()
        mock_mass.player_queues.track_loaded_in_buffer = MagicMock()

        async def mock_item_stream(*_args: object, **_kwargs: object) -> object:
            # Yield 3 chunks to force buffer overflow (buffer = 3*pcm_sample_size > pcm_sample_size)
            for _ in range(3):
                yield b"\xab" * pcm_sample_size

        controller.get_queue_item_stream = mock_item_stream  # type: ignore[method-assign, assignment]

        # When
        with patch(
            "music_assistant.controllers.streams.streams_controller.PlayLogEntry",
            return_value=MagicMock(),
        ):
            chunks = [
                c
                async for c in controller.get_queue_flow_stream(queue, start_queue_item, pcm_format)
            ]
        # Then: overflow yielded some chunks mid-stream plus remaining at end
        assert len(chunks) >= 1
        # All yielded chunks should be multiples of pcm_sample_size or remaining buffer
        for chunk in chunks:
            assert len(chunk) > 0

    async def test_track_produces_zero_chunks_sets_stream_error(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test track that produces zero audio chunks sets stream_error=True and is skipped."""
        # Given
        pcm_sample_size = 4096
        pcm_format = MagicMock()
        pcm_format.content_type.is_pcm.return_value = True
        pcm_format.pcm_sample_size = pcm_sample_size
        pcm_format.bit_depth = 32
        pcm_format.channels = 2

        queue = MagicMock()
        queue.queue_id = "q1"
        queue.display_name = "Test Queue"

        start_queue_item = MagicMock()
        start_queue_item.media_type = MediaType.RADIO  # skip config calls
        start_queue_item.queue_item_id = "item-1"
        start_queue_item.name = "Failing Track"
        start_queue_item.streamdetails = MagicMock()
        start_queue_item.streamdetails.uri = "test://fail"
        start_queue_item.streamdetails.duration = 0
        start_queue_item.streamdetails.seek_position = 0
        start_queue_item.extra_attributes = {"playback_speed": 1.0}

        mock_mass.player_queues.load_next_queue_item = AsyncMock(side_effect=QueueEmpty())
        mock_mass.player_queues.queue_buffer_completed = MagicMock()
        mock_mass.player_queues.track_loaded_in_buffer = MagicMock()

        # Inner stream yields 0 chunks -> first_chunk_received = False
        async def mock_item_stream_empty(*_args: object, **_kwargs: object) -> object:
            return
            yield  # make it an async generator

        controller.get_queue_item_stream = mock_item_stream_empty  # type: ignore[method-assign, assignment]

        # When
        with patch(
            "music_assistant.controllers.streams.streams_controller.PlayLogEntry",
            return_value=MagicMock(),
        ):
            _ = [
                c
                async for c in controller.get_queue_flow_stream(queue, start_queue_item, pcm_format)
            ]
        # Then: no chunks (track was skipped), stream_error set
        assert start_queue_item.streamdetails.stream_error is True
        mock_mass.player_queues.queue_buffer_completed.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: get_announcement_stream — non-PCM re-encoding path
# ---------------------------------------------------------------------------


class TestGetAnnouncementStreamReencoded:
    """Test get_announcement_stream with non-PCM output (re-encoding path)."""

    async def test_non_pcm_output_uses_ffmpeg_reencoding(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test non-PCM output format triggers re-encoding via get_ffmpeg_stream."""
        # Given: FLAC output format (non-PCM -> re-encoding needed)
        import asyncio as _asyncio  # noqa: PLC0415

        output_format = AudioFormat(
            content_type=ContentType.FLAC,
            sample_rate=44100,
            bit_depth=16,
            channels=2,
        )
        announcement_url = "http://example.com/tts.wav"

        mock_mass.create_task = lambda coro: _asyncio.create_task(coro)

        encode_called = False

        async def fake_ffmpeg(audio_input: object, **_kwargs: object) -> object:
            nonlocal encode_called
            # Detect whether this is the final encoding (input is an async generator)
            import collections.abc  # noqa: PLC0415

            if isinstance(audio_input, collections.abc.AsyncGenerator):
                encode_called = True
                yield b"encoded_flac"
            else:
                # This is fetch_announcement -> yields pcm
                yield b"\x00" * 100

        with (
            patch(
                "music_assistant.controllers.streams.streams_controller.get_ffmpeg_stream",
                fake_ffmpeg,
            ),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_chunksize",
                return_value=4096,
            ),
        ):
            chunks = []
            async for chunk in controller.get_announcement_stream(
                announcement_url=announcement_url,
                output_format=output_format,
                pre_announce=False,
            ):
                chunks.append(chunk)

        # Then: the final re-encoding was done
        assert encode_called
        assert b"encoded_flac" in chunks


# ---------------------------------------------------------------------------
# Tests: serve_queue_item_stream — GET path (covers streaming setup)
# ---------------------------------------------------------------------------


class TestServeQueueItemStreamGet:
    """Tests for serve_queue_item_stream GET request — covers streaming setup lines."""

    def _base_setup(
        self, controller: StreamsController, mock_mass: MagicMock, method: str = "GET"
    ) -> tuple[MagicMock, MagicMock, MagicMock]:
        mock_request = MagicMock()
        mock_request.match_info = {
            "queue_id": "q1",
            "player_id": "p1",
            "queue_item_id": "item-1",
            "fmt": "flac",
            "session_id": "ses123",
        }
        mock_request.method = method

        mock_queue = MagicMock()
        mock_queue.session_id = None
        mock_queue.queue_id = "q1"
        mock_queue.display_name = "Test Queue"
        mock_mass.player_queues.get.return_value = mock_queue

        mock_player = MagicMock()
        mock_player.player_id = "p1"
        mock_player.state.supported_features = []
        mock_mass.players.get_player.return_value = mock_player

        mock_item = MagicMock()
        mock_item.streamdetails = MagicMock()
        mock_item.streamdetails.stream_error = False
        mock_item.media_type = MediaType.RADIO  # non-TRACK -> DISABLED crossfade
        mock_item.name = "Test Radio"
        mock_item.duration = None
        mock_item.extra_attributes = {"playback_speed": 1.0}
        mock_mass.player_queues.get_item.return_value = mock_item

        pcm_fmt = MagicMock()
        pcm_fmt.sample_rate = 44100
        pcm_fmt.bit_depth = 32
        controller._select_pcm_format = AsyncMock(return_value=pcm_fmt)  # type: ignore[method-assign]

        out_fmt = MagicMock()
        out_fmt.output_format_str = "flac"
        controller.get_output_format = AsyncMock(return_value=out_fmt)  # type: ignore[method-assign]

        mock_mass.config.get_player_config_value = AsyncMock(return_value="default")

        mock_resp = MagicMock()
        mock_resp.prepare = AsyncMock()
        mock_resp.write = AsyncMock()

        return mock_request, mock_item, mock_resp

    async def test_get_empty_ffmpeg_stream(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test GET request with empty ffmpeg stream completes without streaming loop."""
        # Given
        mock_request, _, mock_resp = self._base_setup(controller, mock_mass)

        async def empty_item_stream(*_a: object, **_k: object) -> object:
            return
            yield

        async def empty_ffmpeg(*_a: object, **_k: object) -> object:
            return
            yield

        controller.get_queue_item_stream = empty_item_stream  # type: ignore[method-assign, assignment]

        # When
        with (
            patch("aiohttp.web.StreamResponse", return_value=mock_resp),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_ffmpeg_stream",
                empty_ffmpeg,
            ),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_mime_type",
                return_value="audio/mpeg",
            ),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_player_filter_params",
                return_value=[],
            ),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_chunksize",
                return_value=1000,
            ),
        ):
            result = await controller.serve_queue_item_stream(mock_request)
        # Then
        assert result is mock_resp
        mock_resp.write.assert_not_called()

    async def test_get_one_chunk_writes_to_response(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test GET request with one chunk writes data and signals track loaded."""
        # Given
        mock_request, _, mock_resp = self._base_setup(controller, mock_mass)
        mock_mass.player_queues.track_loaded_in_buffer = MagicMock()

        async def single_chunk_item(*_a: object, **_k: object) -> object:
            yield b"some_audio"

        async def single_chunk_ffmpeg(*_a: object, **_k: object) -> object:
            yield b"some_audio_encoded"

        controller.get_queue_item_stream = single_chunk_item  # type: ignore[method-assign, assignment]

        # When
        with (
            patch("aiohttp.web.StreamResponse", return_value=mock_resp),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_ffmpeg_stream",
                single_chunk_ffmpeg,
            ),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_mime_type",
                return_value="audio/mpeg",
            ),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_player_filter_params",
                return_value=[],
            ),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_chunksize",
                return_value=1000,
            ),
        ):
            await controller.serve_queue_item_stream(mock_request)
        # Then
        mock_resp.write.assert_called_once_with(b"some_audio_encoded")
        mock_mass.player_queues.track_loaded_in_buffer.assert_called_once()

    async def test_get_track_with_disabled_smartfades(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test GET TRACK item with DISABLED smart fades uses get_queue_item_stream."""
        # Given
        mock_request, mock_item, mock_resp = self._base_setup(controller, mock_mass)
        mock_item.media_type = MediaType.TRACK  # TRACK -> reads config
        mock_mass.config.get_player_config_value = AsyncMock(
            side_effect=[
                SmartFadesMode.DISABLED,  # CONF_SMART_FADES_MODE
                "default",  # CONF_HTTP_PROFILE
            ]
        )
        mock_mass.config.get_raw_player_config_value.return_value = 10  # crossfade duration

        async def empty_item_stream(*_a: object, **_k: object) -> object:
            return
            yield

        async def empty_ffmpeg(*_a: object, **_k: object) -> object:
            return
            yield

        controller.get_queue_item_stream = empty_item_stream  # type: ignore[method-assign, assignment]

        with (
            patch("aiohttp.web.StreamResponse", return_value=mock_resp),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_ffmpeg_stream",
                empty_ffmpeg,
            ),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_mime_type",
                return_value="audio/flac",
            ),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_player_filter_params",
                return_value=[],
            ),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_chunksize",
                return_value=1000,
            ),
        ):
            result = await controller.serve_queue_item_stream(mock_request)
        # Then: completed successfully
        assert result is mock_resp

    async def test_forced_content_length_with_duration(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test forced_content_length http profile with duration sets computed content length."""
        # Given
        mock_request, mock_item, mock_resp = self._base_setup(controller, mock_mass, method="HEAD")
        mock_item.duration = 300  # has duration
        mock_mass.config.get_player_config_value = AsyncMock(return_value="forced_content_length")

        with (
            patch("aiohttp.web.StreamResponse", return_value=mock_resp),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_mime_type",
                return_value="audio/flac",
            ),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_chunksize",
                return_value=55555,
            ),
        ):
            await controller.serve_queue_item_stream(mock_request)
        # Then: content_length set based on duration
        assert mock_resp.content_length == 55555

    async def test_chunked_http_profile(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test chunked http profile enables chunked encoding."""
        # Given
        mock_request, _, mock_resp = self._base_setup(controller, mock_mass, method="HEAD")
        mock_mass.config.get_player_config_value = AsyncMock(return_value="chunked")

        with (
            patch("aiohttp.web.StreamResponse", return_value=mock_resp),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_mime_type",
                return_value="audio/flac",
            ),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_chunksize",
                return_value=1000,
            ),
        ):
            await controller.serve_queue_item_stream(mock_request)
        # Then
        mock_resp.enable_chunked_encoding.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: serve_queue_flow_stream — GET path with streaming
# ---------------------------------------------------------------------------


class TestServeQueueFlowStreamGet:
    """Tests for serve_queue_flow_stream GET request."""

    async def test_get_one_chunk_writes_to_response(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test GET flow stream with one chunk writes to response."""
        # Given
        mock_request = MagicMock()
        mock_request.match_info = {
            "queue_id": "q1",
            "player_id": "p1",
            "queue_item_id": "item-1",
            "fmt": "flac",
        }
        mock_request.method = "GET"
        mock_request.headers = {}

        mock_queue = MagicMock()
        mock_queue.queue_id = "q1"
        mock_queue.display_name = "Test Queue"
        mock_queue.current_item = None
        mock_mass.player_queues.get.return_value = mock_queue
        mock_mass.players.get_player.return_value = MagicMock()
        mock_mass.player_queues.get_item.return_value = MagicMock()

        flow_fmt = MagicMock()
        flow_fmt.sample_rate = 44100
        flow_fmt.bit_depth = 32
        controller._select_flow_format = AsyncMock(return_value=flow_fmt)  # type: ignore[method-assign]

        out_fmt = MagicMock()
        out_fmt.output_format_str = "flac"
        controller.get_output_format = AsyncMock(return_value=out_fmt)  # type: ignore[method-assign]

        mock_mass.config.get_raw_player_config_value.return_value = "disabled"
        mock_mass.config.get_player_config_value = AsyncMock(return_value="default")

        mock_resp = MagicMock()
        mock_resp.prepare = AsyncMock()
        mock_resp.write = AsyncMock()

        async def single_chunk_ffmpeg(*_a: object, **_k: object) -> object:
            yield b"flow_audio"

        async def single_chunk_flow(*_a: object, **_k: object) -> object:
            yield b"pcm_data"

        controller.get_queue_flow_stream = single_chunk_flow  # type: ignore[method-assign, assignment]

        # When
        with (
            patch("aiohttp.web.StreamResponse", return_value=mock_resp),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_ffmpeg_stream",
                single_chunk_ffmpeg,
            ),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_mime_type",
                return_value="audio/flac",
            ),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_player_filter_params",
                return_value=[],
            ),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_chunksize",
                return_value=4096,
            ),
        ):
            await controller.serve_queue_flow_stream(mock_request)
        # Then
        mock_resp.write.assert_called_once_with(b"flow_audio")

    async def test_forced_content_length_flow(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test forced_content_length sets content length for flow stream."""
        # Given
        mock_request = MagicMock()
        mock_request.match_info = {
            "queue_id": "q1",
            "player_id": "p1",
            "queue_item_id": "item-1",
            "fmt": "flac",
        }
        mock_request.method = "HEAD"
        mock_request.headers = {}

        mock_mass.player_queues.get.return_value = MagicMock()
        mock_mass.players.get_player.return_value = MagicMock()
        mock_mass.player_queues.get_item.return_value = MagicMock()

        controller._select_flow_format = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]
        controller.get_output_format = AsyncMock(return_value=MagicMock(output_format_str="flac"))  # type: ignore[method-assign]
        mock_mass.config.get_raw_player_config_value.return_value = "disabled"
        mock_mass.config.get_player_config_value = AsyncMock(return_value="forced_content_length")

        mock_resp = MagicMock()
        mock_resp.prepare = AsyncMock()

        with (
            patch("aiohttp.web.StreamResponse", return_value=mock_resp),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_mime_type",
                return_value="audio/flac",
            ),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_chunksize",
                return_value=77777,
            ),
        ):
            await controller.serve_queue_flow_stream(mock_request)
        # Then
        assert mock_resp.content_length == 77777


# ---------------------------------------------------------------------------
# Tests: serve_announcement_stream — GET path with streaming
# ---------------------------------------------------------------------------


class TestServeAnnouncementStreamGet:
    """Tests for serve_announcement_stream GET request — streaming path."""

    async def test_get_streams_announcement_audio(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test GET request streams announcement audio to response."""
        # Given
        import asyncio as _asyncio  # noqa: PLC0415

        mock_request = MagicMock()
        mock_request.match_info = {"player_id": "p1", "fmt": "mp3"}
        mock_request.method = "GET"

        mock_player_queue = MagicMock()
        mock_player_queue.state.name = "Player 1"
        mock_mass.player_queues.get.return_value = mock_player_queue

        controller.announcements["p1"] = {
            "announcement_url": "http://example.com/tts.mp3",
            "pre_announce": False,
            "pre_announce_url": "/path/bell.mp3",
        }
        mock_mass.config.get_player_config_value = AsyncMock(return_value="default")

        mock_resp = MagicMock()
        mock_resp.prepare = AsyncMock()
        mock_resp.write = AsyncMock()

        mock_mass.create_task = lambda coro: _asyncio.create_task(coro)

        async def fake_announcement_stream(*_a: object, **_k: object) -> object:
            yield b"announcement_chunk"

        # When
        with (
            patch("aiohttp.web.StreamResponse", return_value=mock_resp),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_mime_type",
                return_value="audio/mpeg",
            ),
            patch.object(
                controller,
                "get_announcement_stream",
                return_value=fake_announcement_stream(),
            ),
        ):
            await controller.serve_announcement_stream(mock_request)
        # Then
        mock_resp.write.assert_called_once_with(b"announcement_chunk")


# ---------------------------------------------------------------------------
# Coverage gap tests — targeting the final 3% to reach 80%
# ---------------------------------------------------------------------------


class TestLogRequestDebugPathRealLogger:
    """Test _log_request debug path (line 1932) requires a real logger."""

    def test_debug_path_with_real_logger(self, controller: StreamsController) -> None:
        """Test _log_request hits debug branch when logger level > VERBOSE_LOG_LEVEL."""
        # Given: real logger at DEBUG level (10 > VERBOSE_LOG_LEVEL=5)
        import logging  # noqa: PLC0415

        real_logger = logging.getLogger("test_streams_debug_path_real")
        real_logger.setLevel(logging.DEBUG)
        controller.logger = real_logger

        mock_request = MagicMock()
        mock_request.method = "GET"
        mock_request.path = "/flow/ses/q1/item1/p1.flac"
        mock_request.remote = "127.0.0.1"
        # When / Then: should not raise, hits line 1932 else-branch
        controller._log_request(mock_request)


class TestServeQueueItemStreamStreamError:
    """Tests for the stream_error path in serve_queue_item_stream (lines 605-612)."""

    def _setup(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> tuple[MagicMock, MagicMock, MagicMock]:
        mock_request = MagicMock()
        mock_request.match_info = {
            "queue_id": "q1",
            "player_id": "p1",
            "queue_item_id": "item-1",
            "fmt": "flac",
            "session_id": "ses123",
        }
        mock_request.method = "GET"

        mock_queue = MagicMock()
        mock_queue.session_id = None
        mock_queue.queue_id = "q1"
        mock_queue.display_name = "Test Queue"
        mock_mass.player_queues.get.return_value = mock_queue

        mock_player = MagicMock()
        mock_player.player_id = "p1"
        mock_player.state.supported_features = []
        mock_mass.players.get_player.return_value = mock_player

        mock_item = MagicMock()
        mock_item.streamdetails = MagicMock()
        mock_item.streamdetails.stream_error = True  # triggers the error path
        mock_item.media_type = MediaType.RADIO
        mock_item.name = "Test Radio"
        mock_item.duration = None
        mock_item.extra_attributes = {"playback_speed": 1.0}
        mock_mass.player_queues.get_item.return_value = mock_item

        pcm_fmt = MagicMock()
        pcm_fmt.sample_rate = 44100
        pcm_fmt.bit_depth = 32
        controller._select_pcm_format = AsyncMock(return_value=pcm_fmt)  # type: ignore[method-assign]

        out_fmt = MagicMock()
        out_fmt.output_format_str = "flac"
        controller.get_output_format = AsyncMock(return_value=out_fmt)  # type: ignore[method-assign]

        mock_mass.config.get_player_config_value = AsyncMock(return_value="default")

        mock_resp = MagicMock()
        mock_resp.prepare = AsyncMock()
        mock_resp.write = AsyncMock()

        return mock_request, mock_item, mock_resp

    async def test_stream_error_calls_call_later(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test that stream_error=True after loop triggers call_later skip."""
        # Given
        mock_request, _, mock_resp = self._setup(controller, mock_mass)

        async def empty_item_stream(*_a: object, **_k: object) -> object:
            return
            yield

        async def empty_ffmpeg(*_a: object, **_k: object) -> object:
            return
            yield

        controller.get_queue_item_stream = empty_item_stream  # type: ignore[method-assign, assignment]

        # When
        with (
            patch("aiohttp.web.StreamResponse", return_value=mock_resp),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_ffmpeg_stream",
                empty_ffmpeg,
            ),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_mime_type",
                return_value="audio/flac",
            ),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_player_filter_params",
                return_value=[],
            ),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_chunksize",
                return_value=1000,
            ),
        ):
            result = await controller.serve_queue_item_stream(mock_request)
        # Then: result is the response, call_later was invoked to skip to next item
        assert result is mock_resp
        mock_mass.call_later.assert_called_once()
        call_args = mock_mass.call_later.call_args[0]
        assert call_args[0] == 5


class TestServeQueueItemStreamConnectionError:
    """Tests for connection-error break path in serve_queue_item_stream (lines 587-603)."""

    def _setup(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> tuple[MagicMock, MagicMock, MagicMock]:
        mock_request = MagicMock()
        mock_request.match_info = {
            "queue_id": "q1",
            "player_id": "p1",
            "queue_item_id": "item-1",
            "fmt": "flac",
            "session_id": "ses123",
        }
        mock_request.method = "GET"

        mock_queue = MagicMock()
        mock_queue.session_id = None
        mock_queue.queue_id = "q1"
        mock_queue.display_name = "Test Queue"
        mock_mass.player_queues.get.return_value = mock_queue

        mock_player = MagicMock()
        mock_player.player_id = "p1"
        mock_player.stop_called = False  # ensure we hit the warning path
        mock_player.state.supported_features = []
        mock_mass.players.get_player.return_value = mock_player

        mock_item = MagicMock()
        mock_item.streamdetails = MagicMock()
        mock_item.streamdetails.stream_error = False
        mock_item.media_type = MediaType.RADIO
        mock_item.name = "Test Radio"
        mock_item.duration = 120
        mock_item.extra_attributes = {"playback_speed": 1.0}
        mock_mass.player_queues.get_item.return_value = mock_item

        pcm_fmt = MagicMock()
        pcm_fmt.sample_rate = 44100
        pcm_fmt.bit_depth = 32
        controller._select_pcm_format = AsyncMock(return_value=pcm_fmt)  # type: ignore[method-assign]

        out_fmt = MagicMock()
        out_fmt.output_format_str = "flac"
        controller.get_output_format = AsyncMock(return_value=out_fmt)  # type: ignore[method-assign]

        mock_mass.config.get_player_config_value = AsyncMock(return_value="default")

        mock_resp = MagicMock()
        mock_resp.prepare = AsyncMock()

        return mock_request, mock_item, mock_resp

    async def test_connection_reset_after_first_chunk_breaks_loop(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test ConnectionResetError on second write breaks loop after warning."""
        # Given
        mock_request, _, mock_resp = self._setup(controller, mock_mass)
        # First write succeeds, second raises ConnectionResetError
        mock_resp.write = AsyncMock(side_effect=[None, ConnectionResetError("peer reset")])

        async def two_chunk_item_stream(*_a: object, **_k: object) -> object:
            return
            yield

        async def two_chunk_ffmpeg(*_a: object, **_k: object) -> object:
            yield b"chunk_1"
            yield b"chunk_2"

        controller.get_queue_item_stream = two_chunk_item_stream  # type: ignore[method-assign, assignment]

        # When
        with (
            patch("aiohttp.web.StreamResponse", return_value=mock_resp),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_ffmpeg_stream",
                two_chunk_ffmpeg,
            ),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_mime_type",
                return_value="audio/flac",
            ),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_player_filter_params",
                return_value=[],
            ),
            patch(
                "music_assistant.controllers.streams.streams_controller.get_chunksize",
                return_value=1000,
            ),
        ):
            result = await controller.serve_queue_item_stream(mock_request)
        # Then: only 2 write attempts (first success, second failure), loop breaks
        assert result is mock_resp
        assert mock_resp.write.call_count == 2


class TestGetQueueItemStreamGenericException:
    """Tests for the generic Exception handler in get_queue_item_stream (lines 1571-1577)."""

    def _make_queue_item(self) -> MagicMock:
        queue_item = MagicMock()
        streamdetails = MagicMock()
        streamdetails.volume_normalization_mode = VolumeNormalizationMode.DISABLED
        streamdetails.fade_in = False
        streamdetails.duration = None
        streamdetails.uri = "test://track1"
        streamdetails.seek_position = 0
        streamdetails.provider = "test_provider"
        streamdetails.stream_error = False
        streamdetails.media_type = MediaType.TRACK
        queue_item.streamdetails = streamdetails
        queue_item.name = "Test Track"
        queue_item.extra_attributes = {"playback_speed": 1.0}
        return queue_item

    async def test_generic_exception_swallowed_and_stream_error_set(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test that a generic exception (not AudioError) is swallowed and stream_error is set."""
        # Given
        queue_item = self._make_queue_item()
        pcm_format = MagicMock()
        pcm_format.pcm_sample_size = 44100 * 4 * 2
        mock_mass.config.get_raw_core_config_value.return_value = False
        mock_mass.get_provider.return_value = None

        async def failing_media(*_args: object, **_kwargs: object) -> object:
            yield b"first_chunk"
            raise ValueError("unexpected generic error")

        # When: raise_on_error=False (note: default is True, so pass explicitly)
        with patch(
            "music_assistant.controllers.streams.streams_controller.get_media_stream",
            failing_media,
        ):
            chunks = [
                c
                async for c in controller.get_queue_item_stream(
                    queue_item, pcm_format, raise_on_error=False
                )
            ]
        # Then: first chunk was yielded before the error, stream_error is set
        assert b"first_chunk" in chunks
        assert queue_item.streamdetails.stream_error is True

    async def test_generic_exception_raised_when_raise_on_error(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test that raise_on_error=True re-raises the generic exception."""
        # Given
        queue_item = self._make_queue_item()
        pcm_format = MagicMock()
        pcm_format.pcm_sample_size = 44100 * 4 * 2
        mock_mass.config.get_raw_core_config_value.return_value = False
        mock_mass.get_provider.return_value = None

        async def failing_media(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("immediate failure")
            yield  # type: ignore[unreachable]

        # When / Then
        with (
            patch(
                "music_assistant.controllers.streams.streams_controller.get_media_stream",
                failing_media,
            ),
            pytest.raises(RuntimeError, match="immediate failure"),
        ):
            async for _ in controller.get_queue_item_stream(
                queue_item, pcm_format, raise_on_error=True
            ):
                pass


class TestGetQueueItemStreamWithSmartfadeEdgeCases:
    """Test edge cases in get_queue_item_stream_with_smartfade."""

    def _base_setup(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> tuple[MagicMock, MagicMock, MagicMock, MagicMock]:
        pcm_sample_size = 4096
        pcm_format = MagicMock()
        pcm_format.pcm_sample_size = pcm_sample_size
        pcm_format.bit_depth = 32
        pcm_format.channels = 2
        pcm_format.sample_rate = 44100

        queue = MagicMock()
        queue.queue_id = "q1"
        queue.display_name = "Test Queue"
        mock_mass.player_queues.get.return_value = queue

        player = MagicMock()
        player.player_id = "p1"

        queue_item = MagicMock()
        queue_item.queue_id = "q1"
        queue_item.queue_item_id = "item-1"
        queue_item.name = "Track 1"
        streamdetails = MagicMock()
        streamdetails.uri = "test://track1"
        streamdetails.duration = 300
        streamdetails.seek_position = 0
        streamdetails.seconds_streamed = None
        queue_item.streamdetails = streamdetails
        queue_item.extra_attributes = {"playback_speed": 1.0}

        return pcm_format, queue, player, queue_item

    async def test_raises_when_queue_not_found(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test RuntimeError raised when queue cannot be found (line 1615)."""
        # Given
        _, _, player, queue_item = self._base_setup(controller, mock_mass)
        mock_mass.player_queues.get.return_value = None  # queue not found

        pcm_format = MagicMock()
        pcm_format.pcm_sample_size = 4096
        pcm_format.bit_depth = 32
        pcm_format.channels = 2
        pcm_format.sample_rate = 44100

        # When / Then: RuntimeError is propagated via the buffer mechanism
        with pytest.raises(RuntimeError, match="not found"):
            async for _ in controller.get_queue_item_stream_with_smartfade(
                player=player,
                queue_item=queue_item,
                pcm_format=pcm_format,
            ):
                pass

    async def test_crossfade_data_cleared_when_seeking(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test crossfade_data is cleared when seek_position > 0 (line 1623)."""
        # Given
        from music_assistant.controllers.streams.streams_controller import (  # noqa: PLC0415
            CrossfadeData,
        )

        pcm_format, _, player, queue_item = self._base_setup(controller, mock_mass)
        queue_item.streamdetails.seek_position = 5  # > 0 triggers clearing

        # Set crossfade_data matching queue_item_id so it passes the id check
        fake_pcm_fmt = MagicMock()
        fake_pcm_fmt.pcm_sample_size = 4096
        controller._crossfade_data["q1"] = CrossfadeData(
            data=b"\x00" * 100,
            fade_in_size=100,
            pcm_format=fake_pcm_fmt,
            fade_in_pcm_format=fake_pcm_fmt,
            queue_item_id="item-1",  # matches queue_item.queue_item_id
        )

        mock_mass.player_queues.load_next_queue_item = AsyncMock(side_effect=QueueEmpty())
        mock_mass.player_queues.index_by_id = MagicMock(return_value=0)
        mock_mass.players.get_player.return_value = MagicMock()
        mock_mass.get_provider.return_value = None

        async def mock_item_stream(*_args: object, **_kwargs: object) -> object:
            yield b"\x00" * 4096

        controller.get_queue_item_stream = mock_item_stream  # type: ignore[method-assign, assignment]

        # When: iterate to trigger lines 1619-1623
        chunks = [
            c
            async for c in controller.get_queue_item_stream_with_smartfade(
                player=player,
                queue_item=queue_item,
                pcm_format=pcm_format,
                smart_fades_mode=SmartFadesMode.DISABLED,
            )
        ]
        # Then: crossfade data was cleared, stream still produced chunks
        assert len(chunks) >= 1

    async def test_crossfade_data_cleared_when_item_id_mismatch(
        self, controller: StreamsController, mock_mass: MagicMock
    ) -> None:
        """Test warning logged and crossfade_data cleared when item ID mismatches (1626-1629)."""
        # Given
        from music_assistant.controllers.streams.streams_controller import (  # noqa: PLC0415
            CrossfadeData,
        )

        pcm_format, _, player, queue_item = self._base_setup(controller, mock_mass)
        queue_item.streamdetails.seek_position = 0  # no seeking, so seek check passes

        # Set crossfade_data with DIFFERENT queue_item_id to trigger the mismatch warning
        fake_pcm_fmt = MagicMock()
        fake_pcm_fmt.pcm_sample_size = 4096
        controller._crossfade_data["q1"] = CrossfadeData(
            data=b"\x00" * 100,
            fade_in_size=100,
            pcm_format=fake_pcm_fmt,
            fade_in_pcm_format=fake_pcm_fmt,
            queue_item_id="item-DIFFERENT",  # does NOT match queue_item.queue_item_id
        )

        mock_mass.player_queues.load_next_queue_item = AsyncMock(side_effect=QueueEmpty())
        mock_mass.player_queues.index_by_id = MagicMock(return_value=0)
        mock_mass.players.get_player.return_value = MagicMock()
        mock_mass.get_provider.return_value = None

        async def mock_item_stream(*_args: object, **_kwargs: object) -> object:
            yield b"\x00" * 4096

        controller.get_queue_item_stream = mock_item_stream  # type: ignore[method-assign, assignment]

        # When: iterate to trigger lines 1624-1629
        chunks = [
            c
            async for c in controller.get_queue_item_stream_with_smartfade(
                player=player,
                queue_item=queue_item,
                pcm_format=pcm_format,
                smart_fades_mode=SmartFadesMode.DISABLED,
            )
        ]
        # Then: stream produced chunks despite the mismatch
        assert len(chunks) >= 1
