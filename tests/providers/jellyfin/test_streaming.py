"""Tests for Jellyfin streaming module."""

from unittest import mock

import pytest
from aiojellyfin import Connection
from music_assistant_models.enums import MediaType, StreamType

from music_assistant.providers.jellyfin.streaming import JellyfinStreaming


class TestJellyfinStreaming:
    """Tests for JellyfinStreaming class."""

    @pytest.fixture
    def mock_client(self) -> mock.MagicMock:
        """Create a mock Jellyfin connection client."""
        return mock.MagicMock(spec=Connection)

    @pytest.fixture
    def streaming(self, mock_client: mock.MagicMock) -> JellyfinStreaming:
        """Create a JellyfinStreaming instance with mocked client."""
        return JellyfinStreaming(
            client=mock_client,
            logger=mock.MagicMock(),
            instance_id="test-instance",
        )

    def test_streaming_initialization(self, mock_client: mock.MagicMock) -> None:
        """Test streaming helper initialization."""
        logger = mock.MagicMock()
        stream = JellyfinStreaming(mock_client, logger, "test-id")

        # Verify the instance was created successfully without accessing protected members
        assert stream is not None
        assert isinstance(stream, JellyfinStreaming)

    @pytest.mark.asyncio
    async def test_get_stream_details(
        self, streaming: JellyfinStreaming, mock_client: mock.MagicMock
    ) -> None:
        """Test getting stream details for a track."""
        mock_client.get_track = mock.AsyncMock(
            return_value={
                "Id": "track-123",
                "RunTimeTicks": 180000000,  # 3 minutes
                "MediaSources": [
                    {
                        "Codec": "flac",
                        "Channels": 2,
                    }
                ],
            }
        )
        mock_client.audio_url = mock.MagicMock(return_value="http://localhost:8096/audio/track-123")

        stream_details = await streaming.get_stream_details("track-123", MediaType.TRACK)

        assert stream_details.item_id == "track-123"
        assert stream_details.provider == "test-instance"
        assert stream_details.stream_type == StreamType.HTTP
        assert stream_details.duration == 18  # 180000000 / 10000000
        assert stream_details.can_seek is True
        mock_client.get_track.assert_called_once_with("track-123")

    @pytest.mark.asyncio
    async def test_get_stream_details_error(
        self, streaming: JellyfinStreaming, mock_client: mock.MagicMock
    ) -> None:
        """Test error handling when getting stream details."""
        mock_client.get_track = mock.AsyncMock(side_effect=Exception("Not found"))

        with pytest.raises(Exception, match="Not found"):
            await streaming.get_stream_details("invalid-id", MediaType.TRACK)

    @pytest.mark.asyncio
    async def test_get_similar_tracks(
        self, streaming: JellyfinStreaming, mock_client: mock.MagicMock
    ) -> None:
        """Test getting similar tracks."""
        mock_client.get_similar_tracks = mock.AsyncMock(return_value={"Items": []})

        similar_tracks = await streaming.get_similar_tracks("track-123", limit=25)

        assert isinstance(similar_tracks, list)
        assert len(similar_tracks) == 0
        mock_client.get_similar_tracks.assert_called_once_with(
            "track-123", limit=25, fields=mock.ANY
        )

    @pytest.mark.asyncio
    async def test_get_similar_tracks_custom_limit(
        self, streaming: JellyfinStreaming, mock_client: mock.MagicMock
    ) -> None:
        """Test getting similar tracks with custom limit."""
        mock_client.get_similar_tracks = mock.AsyncMock(return_value={"Items": []})

        await streaming.get_similar_tracks("track-123", limit=10)

        mock_client.get_similar_tracks.assert_called_once_with(
            "track-123", limit=10, fields=mock.ANY
        )

    @pytest.mark.asyncio
    async def test_get_similar_tracks_default_limit(
        self, streaming: JellyfinStreaming, mock_client: mock.MagicMock
    ) -> None:
        """Test getting similar tracks uses default limit of 25."""
        mock_client.get_similar_tracks = mock.AsyncMock(return_value={"Items": []})

        await streaming.get_similar_tracks("track-123")

        mock_client.get_similar_tracks.assert_called_once_with(
            "track-123", limit=25, fields=mock.ANY
        )

    @pytest.mark.asyncio
    async def test_get_similar_tracks_error(
        self, streaming: JellyfinStreaming, mock_client: mock.MagicMock
    ) -> None:
        """Test error handling when getting similar tracks."""
        mock_client.get_similar_tracks = mock.AsyncMock(side_effect=Exception("Service error"))

        with pytest.raises(Exception, match="Service error"):
            await streaming.get_similar_tracks("invalid-id")
