"""Tests for Jellyfin library module."""

from unittest import mock

import pytest
from aiojellyfin import Connection

from music_assistant.providers.jellyfin.library import JellyfinLibrary


class TestJellyfinLibrary:
    """Tests for JellyfinLibrary class."""

    @pytest.fixture
    def mock_client(self) -> mock.MagicMock:
        """Create a mock Jellyfin connection client."""
        return mock.MagicMock(spec=Connection)

    @pytest.fixture
    def library(self, mock_client: mock.MagicMock) -> JellyfinLibrary:
        """Create a JellyfinLibrary instance with mocked client."""
        return JellyfinLibrary(
            client=mock_client,
            logger=mock.MagicMock(),
            instance_id="test-instance",
            domain="jellyfin",
        )

    def test_library_initialization(self, mock_client: mock.MagicMock) -> None:
        """Test library helper initialization."""
        logger = mock.MagicMock()
        lib = JellyfinLibrary(mock_client, logger, "test-id", "jellyfin")

        # Verify initialization by checking that the instance was created successfully
        assert lib is not None
        assert isinstance(lib, JellyfinLibrary)

    @pytest.mark.asyncio
    async def test_get_album_error_handling(
        self, library: JellyfinLibrary, mock_client: mock.MagicMock
    ) -> None:
        """Test get_album error handling."""
        mock_client.get_album = mock.AsyncMock(side_effect=Exception("Not found"))

        with pytest.raises(Exception, match="Not found"):
            await library.get_album("invalid-id")

    @pytest.mark.asyncio
    async def test_get_artist_error_handling(
        self, library: JellyfinLibrary, mock_client: mock.MagicMock
    ) -> None:
        """Test get_artist error handling."""
        mock_client.get_artist = mock.AsyncMock(side_effect=Exception("Not found"))

        with pytest.raises(Exception, match="Not found"):
            await library.get_artist("invalid-id")

    @pytest.mark.asyncio
    async def test_get_track_error_handling(
        self, library: JellyfinLibrary, mock_client: mock.MagicMock
    ) -> None:
        """Test get_track error handling."""
        mock_client.get_track = mock.AsyncMock(side_effect=Exception("Not found"))

        with pytest.raises(Exception, match="Not found"):
            await library.get_track("invalid-id")

    @pytest.mark.asyncio
    async def test_get_playlist_error_handling(
        self, library: JellyfinLibrary, mock_client: mock.MagicMock
    ) -> None:
        """Test get_playlist error handling."""
        mock_client.get_playlist = mock.AsyncMock(side_effect=Exception("Not found"))

        with pytest.raises(Exception, match="Not found"):
            await library.get_playlist("invalid-id")

    @pytest.mark.asyncio
    async def test_get_artist_albums_calls_client(
        self, library: JellyfinLibrary, mock_client: mock.MagicMock
    ) -> None:
        """Test get_artist_albums calls client."""
        mock_client.get_artist_albums = mock.AsyncMock(return_value={"Items": []})

        albums = await library.get_artist_albums("artist-id")

        assert isinstance(albums, list)

    @pytest.mark.asyncio
    async def test_get_album_tracks_calls_client(
        self, library: JellyfinLibrary, mock_client: mock.MagicMock
    ) -> None:
        """Test get_album_tracks calls client."""
        # Mock the chained call: client.tracks.parent(...).enable_userdata().fields(...).request()
        mock_request = mock.AsyncMock(return_value={"Items": []})
        mock_chain = mock.MagicMock()
        mock_chain.parent.return_value.enable_userdata.return_value.fields.return_value.request = (
            mock_request
        )
        mock_client.tracks = mock_chain

        tracks = await library.get_album_tracks("album-id")

        assert isinstance(tracks, list)

    @pytest.mark.asyncio
    async def test_get_playlist_tracks_calls_client(
        self, library: JellyfinLibrary, mock_client: mock.MagicMock
    ) -> None:
        """Test get_playlist_tracks calls client."""
        # Mock the chained call:
        # client.tracks.in_playlist(...).enable_userdata().fields(...)
        # .limit(...).start_index(...).request()
        mock_request = mock.AsyncMock(return_value={"Items": []})
        mock_chain = mock.MagicMock()
        (
            mock_chain.in_playlist.return_value.enable_userdata.return_value.fields.return_value.limit.return_value.start_index.return_value.request
        ) = mock_request
        mock_client.tracks = mock_chain

        tracks = await library.get_playlist_tracks("playlist-id", page=0)

        assert isinstance(tracks, list)
        mock_request.assert_called_once()
