"""Unit tests for StreamsController.

Covers: parse_pcm_info (pure function), resolve_stream_url (URL generation),
get_plugin_source_url, cleanup_queue_audio_data.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError

from music_assistant.controllers.streams.streams_controller import (
    StreamsController,
    parse_pcm_info,
)

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
