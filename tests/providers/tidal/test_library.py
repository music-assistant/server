"""Test Tidal Library Manager."""

import json
import pathlib
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from aiohttp.client_exceptions import ClientError
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

    provider.redirect_cached_id = AsyncMock(side_effect=lambda item_id: item_id)
    provider.resolve_live_track_id = AsyncMock(return_value=None)

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


_COLLECTION_CASES = [
    (MediaType.ARTIST, "userCollectionArtists", "artists"),
    (MediaType.ALBUM, "userCollectionAlbums", "albums"),
    (MediaType.TRACK, "userCollectionTracks", "tracks"),
    (MediaType.PLAYLIST, "userCollectionPlaylists", "playlists"),
]


@pytest.mark.parametrize(("media_type", "collection", "resource_type"), _COLLECTION_CASES)
async def test_add_item(
    media_type: MediaType,
    collection: str,
    resource_type: str,
    library_manager: TidalLibraryManager,
    provider_mock: Mock,
) -> None:
    """Test add_item POSTs to the official user collection endpoint."""
    item = Mock(item_id="123", media_type=media_type)
    # Only consulted for the TRACK/POST healing path; harmless for other media types.
    provider_mock.api.write_jsonapi.return_value = {"data": [{"type": resource_type, "id": "123"}]}

    assert await library_manager.add_item(item) is True
    provider_mock.api.write_jsonapi.assert_called_with(
        "POST",
        f"{collection}/me/relationships/items",
        {"data": [{"type": resource_type, "id": "123"}]},
    )
    provider_mock.resolve_live_track_id.assert_not_called()


@pytest.mark.parametrize(("media_type", "collection", "resource_type"), _COLLECTION_CASES)
async def test_remove_item(
    media_type: MediaType,
    collection: str,
    resource_type: str,
    library_manager: TidalLibraryManager,
    provider_mock: Mock,
) -> None:
    """Test remove_item DELETEs from the official user collection endpoint."""
    assert await library_manager.remove_item("123", media_type) is True
    provider_mock.api.write_jsonapi.assert_called_with(
        "DELETE",
        f"{collection}/me/relationships/items",
        {"data": [{"type": resource_type, "id": "123"}]},
    )


async def test_add_mix_strips_prefix(
    library_manager: TidalLibraryManager, provider_mock: Mock
) -> None:
    """Test a mix favorite is written to the playlists collection without the mix_ prefix."""
    item = Mock(item_id="mix_abc123", media_type=MediaType.PLAYLIST)

    await library_manager.add_item(item)
    provider_mock.api.write_jsonapi.assert_called_with(
        "POST",
        "userCollectionPlaylists/me/relationships/items",
        {"data": [{"type": "playlists", "id": "abc123"}]},
    )


async def test_add_item_returns_false_on_error(
    library_manager: TidalLibraryManager, provider_mock: Mock
) -> None:
    """Test add_item returns False when the write fails."""
    provider_mock.api.write_jsonapi.side_effect = ClientError()
    item = Mock(item_id="123", media_type=MediaType.TRACK)

    assert await library_manager.add_item(item) is False


async def test_add_item_track_stale_id_heals(
    library_manager: TidalLibraryManager, provider_mock: Mock
) -> None:
    """Test a stale favorite track id omitted from the response is resolved and retried."""
    provider_mock.api.write_jsonapi.return_value = {"data": []}
    provider_mock.resolve_live_track_id = AsyncMock(return_value="123_live")
    item = Mock(item_id="123", media_type=MediaType.TRACK)

    assert await library_manager.add_item(item) is True

    provider_mock.resolve_live_track_id.assert_called_once_with("123")
    assert provider_mock.api.write_jsonapi.call_count == 2
    provider_mock.api.write_jsonapi.assert_called_with(
        "POST",
        "userCollectionTracks/me/relationships/items",
        {"data": [{"type": "tracks", "id": "123_live"}]},
    )


async def test_add_item_track_no_data_in_response(
    library_manager: TidalLibraryManager, provider_mock: Mock
) -> None:
    """Test a 204-style response without a "data" key skips healing entirely."""
    provider_mock.api.write_jsonapi.return_value = {"success": True}
    item = Mock(item_id="123", media_type=MediaType.TRACK)

    assert await library_manager.add_item(item) is True

    provider_mock.resolve_live_track_id.assert_not_called()
    assert provider_mock.api.write_jsonapi.call_count == 1


async def test_add_item_track_stale_id_unresolvable(
    library_manager: TidalLibraryManager, provider_mock: Mock
) -> None:
    """Test a stale favorite track id that cannot be resolved results in no retry POST."""
    provider_mock.api.write_jsonapi.return_value = {"data": []}
    # resolve_live_track_id default (from fixture) already returns None.
    item = Mock(item_id="123", media_type=MediaType.TRACK)

    assert await library_manager.add_item(item) is True

    provider_mock.resolve_live_track_id.assert_called_once_with("123")
    assert provider_mock.api.write_jsonapi.call_count == 1


async def test_add_item_non_track_no_healing(
    library_manager: TidalLibraryManager, provider_mock: Mock
) -> None:
    """Test a non-track favorite add never consults the healing resolver."""
    item = Mock(item_id="123", media_type=MediaType.ALBUM)

    assert await library_manager.add_item(item) is True

    provider_mock.resolve_live_track_id.assert_not_called()
    assert provider_mock.api.write_jsonapi.call_count == 1


async def test_remove_item_track_no_healing(
    library_manager: TidalLibraryManager, provider_mock: Mock
) -> None:
    """Test a track favorite removal (DELETE) never consults the healing resolver."""
    assert await library_manager.remove_item("123", MediaType.TRACK) is True

    provider_mock.resolve_live_track_id.assert_not_called()
    assert provider_mock.api.write_jsonapi.call_count == 1


async def test_add_item_unsupported_media_type_returns_false(
    library_manager: TidalLibraryManager, provider_mock: Mock
) -> None:
    """Test add_item returns False for a media type without a collection mapping."""
    item = Mock(item_id="123", media_type=MediaType.RADIO)

    assert await library_manager.add_item(item) is False
    provider_mock.api.write_jsonapi.assert_not_called()


async def test_remove_mix_strips_prefix(
    library_manager: TidalLibraryManager, provider_mock: Mock
) -> None:
    """Test a mix is removed from the playlists collection without the mix_ prefix."""
    await library_manager.remove_item("mix_abc123", MediaType.PLAYLIST)
    provider_mock.api.write_jsonapi.assert_called_with(
        "DELETE",
        "userCollectionPlaylists/me/relationships/items",
        {"data": [{"type": "playlists", "id": "abc123"}]},
    )


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
