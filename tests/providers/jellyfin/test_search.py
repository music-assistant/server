"""Tests for Jellyfin search module."""

from unittest import mock

import pytest
from aiojellyfin import Connection

from music_assistant.providers.jellyfin.search import JellyfinSearch


class TestJellyfinSearch:
    """Tests for JellyfinSearch class."""

    @pytest.fixture
    def mock_client(self) -> mock.MagicMock:
        """Create a mock Jellyfin connection client."""
        return mock.MagicMock(spec=Connection)

    @pytest.fixture
    def search(self, mock_client: mock.MagicMock) -> JellyfinSearch:
        """Create a JellyfinSearch instance with mocked client."""
        return JellyfinSearch(
            client=mock_client,
            logger=mock.MagicMock(),
            instance_id="test-instance",
        )

    def test_search_initialization(self, mock_client: mock.MagicMock) -> None:
        """Test search helper initialization."""
        logger = mock.MagicMock()
        search = JellyfinSearch(mock_client, logger, "test-id")

        assert search is not None

    @pytest.mark.asyncio
    async def test_search_track_type(
        self, search: JellyfinSearch, mock_client: mock.MagicMock
    ) -> None:
        """Test search_track returns list."""
        mock_client.tracks = mock.MagicMock()
        (
            mock_client.tracks.search_term.return_value.limit.return_value.enable_userdata.return_value.fields.return_value.request
        ) = mock.AsyncMock(return_value={"Items": []})

        result = await search.search_track("query", limit=10)

        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_search_album_type(
        self, search: JellyfinSearch, mock_client: mock.MagicMock
    ) -> None:
        """Test search_album returns list."""
        mock_client.albums = mock.MagicMock()
        (
            mock_client.albums.search_term.return_value.limit.return_value.enable_userdata.return_value.fields.return_value.request
        ) = mock.AsyncMock(return_value={"Items": []})

        result = await search.search_album("query", limit=10)

        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_search_artist_type(
        self, search: JellyfinSearch, mock_client: mock.MagicMock
    ) -> None:
        """Test search_artist returns list."""
        mock_client.artists = mock.MagicMock()
        (
            mock_client.artists.search_term.return_value.limit.return_value.enable_userdata.return_value.fields.return_value.request
        ) = mock.AsyncMock(return_value={"Items": []})

        result = await search.search_artist("query", limit=10)

        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_search_playlist_type(
        self, search: JellyfinSearch, mock_client: mock.MagicMock
    ) -> None:
        """Test search_playlist returns list."""
        # Mock the chained call:
        # client.playlists.search_term(...).limit(...).enable_userdata().request()
        mock_request = mock.AsyncMock(return_value={"Items": []})
        mock_chain = mock.MagicMock()
        (
            mock_chain.search_term.return_value.limit.return_value.enable_userdata.return_value.request
        ) = mock_request
        mock_client.playlists = mock_chain

        result = await search.search_playlist("query", limit=10)

        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_search_track_error(
        self, search: JellyfinSearch, mock_client: mock.MagicMock
    ) -> None:
        """Test error handling in search_track."""
        mock_client.tracks = mock.MagicMock()
        mock_client.tracks.search_term.side_effect = Exception("Search error")

        with pytest.raises(Exception, match="Search error"):
            await search.search_track("query", limit=10)
