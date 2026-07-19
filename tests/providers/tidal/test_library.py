"""Test Tidal Library Manager."""

import json
import pathlib
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping

from music_assistant.providers.tidal.jsonapi import JsonApiDocument
from music_assistant.providers.tidal.library import TidalLibraryManager

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures" / "v2"


def _load_doc(name: str) -> JsonApiDocument:
    with open(FIXTURES_DIR / name) as f:
        return JsonApiDocument(json.load(f))


@pytest.fixture
def provider_mock() -> Mock:
    """Return a mock provider."""
    provider = Mock()
    provider.domain = "tidal"
    provider.instance_id = "tidal_instance"
    provider.auth.user_id = "12345"
    provider.api = AsyncMock()
    provider.api.get.return_value = {"items": []}
    provider.api.paginate = MagicMock()

    # Configure async iterator for paginate
    async def async_iter(*_args: Any, **_kwargs: Any) -> AsyncGenerator[Any]:
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


async def test_get_artists(library_manager: TidalLibraryManager, provider_mock: Mock) -> None:
    """Test library artists read from the official userCollection endpoint."""
    doc = _load_doc("lib_artists.json")

    async def _pages(*_a: Any, **_k: Any) -> Any:
        yield doc

    provider_mock.api.paginate_jsonapi = _pages

    artists = [a async for a in library_manager.get_artists()]

    assert len(artists) == 20
    assert artists[0].date_added is not None  # from the addedAt linkage meta


async def test_get_albums(library_manager: TidalLibraryManager, provider_mock: Mock) -> None:
    """Test library albums read from the official userCollection endpoint."""
    doc = _load_doc("lib_albums.json")

    async def _pages(*_a: Any, **_k: Any) -> Any:
        yield doc

    provider_mock.api.paginate_jsonapi = _pages

    albums = [a async for a in library_manager.get_albums()]

    assert len(albums) == 20
    assert albums[0].date_added is not None


async def test_get_tracks(library_manager: TidalLibraryManager, provider_mock: Mock) -> None:
    """Test library tracks read from the official userCollection endpoint."""
    doc = _load_doc("lib_tracks.json")

    async def _pages(*_a: Any, **_k: Any) -> Any:
        yield doc

    provider_mock.api.paginate_jsonapi = _pages

    tracks = [t async for t in library_manager.get_tracks()]

    assert len(tracks) == 20
    assert tracks[0].date_added is not None


async def test_get_playlists(library_manager: TidalLibraryManager, provider_mock: Mock) -> None:
    """Test library playlists include mixes and the virtual favorites playlist."""
    doc = _load_doc("lib_playlists.json")

    async def _pages(*_a: Any, **_k: Any) -> Any:
        yield doc

    provider_mock.api.paginate_jsonapi = _pages

    playlists = [p async for p in library_manager.get_playlists()]

    # 19 resolved playlists + the trailing virtual "favorite tracks" playlist
    assert playlists[-1].item_id == "favorite_tracks"
    # mixes come through the same collection with the "mix_" item id prefix
    assert any(p.item_id.startswith("mix_") for p in playlists)
    assert any(not p.item_id.startswith("mix_") for p in playlists[:-1])


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


async def test_get_playlists_includes_favorite_tracks(
    library_manager: TidalLibraryManager, provider_mock: Mock
) -> None:
    """Test that get_playlists yields the virtual favorite tracks playlist."""
    empty = JsonApiDocument({"data": [], "included": []})

    async def _pages(*_a: Any, **_k: Any) -> Any:
        yield empty

    provider_mock.api.paginate_jsonapi = _pages

    playlists = [p async for p in library_manager.get_playlists()]

    assert len(playlists) == 1
    assert playlists[0].item_id == "favorite_tracks"
    assert playlists[0].name == "Favorite Tracks"
    assert not playlists[0].is_editable
