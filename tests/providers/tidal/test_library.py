"""Test Tidal Library Manager."""

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping

from music_assistant.providers.tidal.library import TidalLibraryManager


@pytest.fixture
def provider_mock() -> Mock:
    """Return a mock provider."""
    provider = Mock()
    provider.domain = "tidal"
    provider.instance_id = "tidal_instance"
    provider.auth.user_id = "12345"
    provider.api = AsyncMock()
    provider.api.get_data.return_value = {"items": []}
    provider.api.paginate = MagicMock()

    # Configure async iterator for paginate
    async def async_iter(*_args: Any, **_kwargs: Any) -> AsyncGenerator[Any, None]:
        for item in provider.api.paginate.return_value:
            yield item

    provider.api.paginate.side_effect = async_iter
    provider.api.paginate.return_value = []

    provider.logger = Mock()

    def get_item_mapping(media_type: MediaType, key: str, name: str) -> ItemMapping:
        return ItemMapping(
            media_type=media_type,
            item_id=key,
            provider=provider.instance_id,
            name=name,
        )

    provider.get_item_mapping.side_effect = get_item_mapping

    return provider


@pytest.fixture
def library_manager(provider_mock: Mock) -> TidalLibraryManager:
    """Return a TidalLibraryManager instance."""
    return TidalLibraryManager(provider_mock)


@patch("music_assistant.providers.tidal.library.parse_artist")
async def test_get_artists(
    mock_parse_artist: Mock, library_manager: TidalLibraryManager, provider_mock: Mock
) -> None:
    """Test get_artists."""
    provider_mock.api.paginate.return_value = [{"id": 1, "name": "Test Artist"}]
    mock_parse_artist.return_value = Mock(item_id="1")

    artists = [a async for a in library_manager.get_artists()]

    assert len(artists) == 1
    assert artists[0].item_id == "1"
    provider_mock.api.paginate.assert_called_with(
        "users/12345/favorites/artists",
        nested_key="item",
    )
    mock_parse_artist.assert_called_once()


@patch("music_assistant.providers.tidal.library.parse_album")
async def test_get_albums(
    mock_parse_album: Mock, library_manager: TidalLibraryManager, provider_mock: Mock
) -> None:
    """Test get_albums."""
    provider_mock.api.paginate.return_value = [{"id": 1, "title": "Test Album"}]
    mock_parse_album.return_value = Mock(item_id="1")

    albums = [a async for a in library_manager.get_albums()]

    assert len(albums) == 1
    assert albums[0].item_id == "1"
    provider_mock.api.paginate.assert_called_with(
        "users/12345/favorites/albums",
        nested_key="item",
    )
    mock_parse_album.assert_called_once()


@patch("music_assistant.providers.tidal.library.parse_track")
async def test_get_tracks(
    mock_parse_track: Mock, library_manager: TidalLibraryManager, provider_mock: Mock
) -> None:
    """Test get_tracks."""
    provider_mock.api.paginate.return_value = [{"id": 1, "title": "Test Track"}]
    mock_parse_track.return_value = Mock(item_id="1")

    tracks = [t async for t in library_manager.get_tracks()]

    assert len(tracks) == 1
    assert tracks[0].item_id == "1"
    provider_mock.api.paginate.assert_called_with(
        "users/12345/favorites/tracks",
        nested_key="item",
    )
    mock_parse_track.assert_called_once()


@patch("music_assistant.providers.tidal.library.parse_playlist")
async def test_get_playlists(
    mock_parse_playlist: Mock, library_manager: TidalLibraryManager, provider_mock: Mock
) -> None:
    """Test get_playlists."""
    # Mock mixes response
    mixes_response = [{"id": "mix_1", "title": "Mix 1"}]
    # Mock playlists response
    playlists_response = [{"uuid": "pl_1", "title": "Playlist 1"}]

    # Configure paginate side effect
    async def paginate_side_effect(
        endpoint: str, **_kwargs: Any
    ) -> AsyncGenerator[dict[str, Any], None]:
        if "mixes" in endpoint:
            for item in mixes_response:
                yield item
        else:
            for item in playlists_response:
                yield item

    provider_mock.api.paginate.side_effect = paginate_side_effect

    # Setup mock return values
    mock_parse_playlist.side_effect = [
        Mock(item_id="mix_1"),
        Mock(item_id="pl_1"),
    ]

    playlists = [p async for p in library_manager.get_playlists()]

    assert len(playlists) == 2
    assert playlists[0].item_id == "mix_1"
    assert playlists[1].item_id == "pl_1"
    assert mock_parse_playlist.call_count == 2


async def test_add_item_artist(library_manager: TidalLibraryManager, provider_mock: Mock) -> None:
    """Test add_item for artist."""
    item = Mock(item_id="123", media_type=MediaType.ARTIST)
    await library_manager.add_item(item)

    provider_mock.api.post.assert_called_with(
        "users/12345/favorites/artists",
        data={"artistId": "123"},
        as_form=True,
    )


async def test_add_item_album(library_manager: TidalLibraryManager, provider_mock: Mock) -> None:
    """Test add_item for album."""
    item = Mock(item_id="123", media_type=MediaType.ALBUM)
    await library_manager.add_item(item)

    provider_mock.api.post.assert_called_with(
        "users/12345/favorites/albums",
        data={"albumId": "123"},
        as_form=True,
    )


async def test_add_item_track(library_manager: TidalLibraryManager, provider_mock: Mock) -> None:
    """Test add_item for track."""
    item = Mock(item_id="123", media_type=MediaType.TRACK)
    await library_manager.add_item(item)

    provider_mock.api.post.assert_called_with(
        "users/12345/favorites/tracks",
        data={"trackId": "123"},
        as_form=True,
    )


async def test_add_item_playlist(library_manager: TidalLibraryManager, provider_mock: Mock) -> None:
    """Test add_item for playlist."""
    item = Mock(item_id="123", media_type=MediaType.PLAYLIST)
    await library_manager.add_item(item)

    provider_mock.api.post.assert_called_with(
        "users/12345/favorites/playlists",
        data={"uuids": "123"},
        as_form=True,
    )


async def test_remove_item_artist(
    library_manager: TidalLibraryManager, provider_mock: Mock
) -> None:
    """Test remove_item for artist."""
    await library_manager.remove_item("123", MediaType.ARTIST)

    provider_mock.api.delete.assert_called_with("users/12345/favorites/artists/123")


async def test_remove_item_album(library_manager: TidalLibraryManager, provider_mock: Mock) -> None:
    """Test remove_item for album."""
    await library_manager.remove_item("123", MediaType.ALBUM)

    provider_mock.api.delete.assert_called_with("users/12345/favorites/albums/123")


async def test_remove_item_track(library_manager: TidalLibraryManager, provider_mock: Mock) -> None:
    """Test remove_item for track."""
    await library_manager.remove_item("123", MediaType.TRACK)

    provider_mock.api.delete.assert_called_with("users/12345/favorites/tracks/123")


async def test_remove_item_playlist(
    library_manager: TidalLibraryManager, provider_mock: Mock
) -> None:
    """Test remove_item for playlist."""
    await library_manager.remove_item("123", MediaType.PLAYLIST)

    provider_mock.api.delete.assert_called_with("users/12345/favorites/playlists/123")
