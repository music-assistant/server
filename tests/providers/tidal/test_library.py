"""Test Tidal Library Manager."""

import json
import pathlib
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp.client_exceptions import ClientError
from music_assistant_models.enums import MediaType

from music_assistant.providers.tidal.jsonapi import JsonApiDocument
from music_assistant.providers.tidal.library import TidalLibraryManager

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures" / "v2"


def _load_doc(name: str) -> JsonApiDocument:
    with open(FIXTURES_DIR / name) as f:
        return JsonApiDocument(json.load(f))


def _load_raw(name: str) -> dict[str, Any]:
    with open(FIXTURES_DIR / name) as f:
        data: dict[str, Any] = json.load(f)
        return data


def _break_resource(raw: dict[str, Any], resource_id: str) -> dict[str, Any]:
    """Blank the name or title of one included resource so its parser rejects it."""
    for resource in raw["included"]:
        if resource["id"] == resource_id:
            attributes = resource["attributes"]
            attributes["name" if "name" in attributes else "title"] = None
            return raw
    raise AssertionError(f"resource {resource_id} not in fixture")


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
    # every linkage item feeds the churn cache (the read passes replaceMedia)
    assert provider_mock.note_replaced_track.call_count == 20


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


_LIBRARY_READ_CASES = [
    ("get_artists", "lib_artists.json", MediaType.ARTIST, 19),
    ("get_albums", "lib_albums.json", MediaType.ALBUM, 19),
    ("get_tracks", "lib_tracks.json", MediaType.TRACK, 19),
    ("get_playlists", "lib_playlists.json", MediaType.PLAYLIST, 19),
]


@pytest.mark.parametrize(
    ("method_name", "fixture_name", "media_type", "expected_count"), _LIBRARY_READ_CASES
)
async def test_library_listing_skips_unparsable_item(
    method_name: str,
    fixture_name: str,
    media_type: MediaType,
    expected_count: int,
    library_manager: TidalLibraryManager,
    provider_mock: Mock,
) -> None:
    """Test one unusable item does not prevent later library items from being yielded."""
    raw = _load_raw(fixture_name)
    broken_id = raw["data"][0]["id"]
    doc = JsonApiDocument(_break_resource(raw, broken_id))

    async def _pages(*_a: Any, **_k: Any) -> Any:
        yield doc

    provider_mock.api.paginate_jsonapi = _pages

    items = [item async for item in getattr(library_manager, method_name)()]

    assert len(items) == expected_count
    assert all(item.item_id != broken_id for item in items)
    provider_mock.logger.warning.assert_not_called()
    skipped_media_type, skipped_id, err = provider_mock.report_skipped_sync_item.call_args.args
    assert skipped_media_type == media_type
    assert skipped_id == broken_id
    assert isinstance(err, (AttributeError, KeyError, TypeError, ValueError))


async def test_library_playlists_report_unparsable_mix_id(
    library_manager: TidalLibraryManager, provider_mock: Mock
) -> None:
    """Test an unusable mix is protected under its prefixed library item ID."""
    raw = _load_raw("lib_playlists.json")
    mix = next(
        resource
        for resource in raw["included"]
        if resource["type"] == "playlists" and resource["attributes"].get("playlistType") == "MIX"
    )
    broken_id = mix["id"]
    doc = JsonApiDocument(_break_resource(raw, broken_id))

    async def _pages(*_a: Any, **_k: Any) -> Any:
        yield doc

    provider_mock.api.paginate_jsonapi = _pages

    playlists = [item async for item in library_manager.get_playlists()]

    assert all(item.item_id != f"mix_{broken_id}" for item in playlists)
    skipped_media_type, skipped_id, err = provider_mock.report_skipped_sync_item.call_args.args
    assert skipped_media_type == MediaType.PLAYLIST
    assert skipped_id == f"mix_{broken_id}"
    assert isinstance(err, (AttributeError, KeyError, TypeError, ValueError))


async def test_library_playlists_hold_cleanup_when_item_type_is_unreadable(
    library_manager: TidalLibraryManager, provider_mock: Mock
) -> None:
    """Test an unidentifiable playlist item holds cleanup without ending the listing."""
    raw = _load_raw("lib_playlists.json")
    playlist = next(resource for resource in raw["included"] if resource["type"] == "playlists")
    playlist["attributes"] = None
    doc = JsonApiDocument(raw)

    async def _pages(*_a: Any, **_k: Any) -> Any:
        yield doc

    provider_mock.api.paginate_jsonapi = _pages

    playlists = [item async for item in library_manager.get_playlists()]

    assert len(playlists) == 19
    skipped_media_type, skipped_id, err = provider_mock.report_skipped_sync_item.call_args.args
    assert skipped_media_type == MediaType.PLAYLIST
    assert skipped_id is None
    assert isinstance(err, AttributeError)


@pytest.mark.parametrize(
    ("method_name", "fixture_name", "_media_type", "_expected_count"), _LIBRARY_READ_CASES
)
async def test_library_listing_fetch_failure_propagates(
    method_name: str,
    fixture_name: str,
    _media_type: MediaType,
    _expected_count: int,
    library_manager: TidalLibraryManager,
    provider_mock: Mock,
) -> None:
    """Test a later page failure aborts the listing instead of returning partial results."""

    async def _pages(*_a: Any, **_k: Any) -> Any:
        yield _load_doc(fixture_name)
        raise ClientError("connection lost")

    provider_mock.api.paginate_jsonapi = _pages

    with pytest.raises(ClientError):
        [item async for item in getattr(library_manager, method_name)()]
    provider_mock.report_skipped_sync_item.assert_not_called()


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
    """Test a favorite track reported NOT_FOUND in meta.skipped is resolved and retried."""
    provider_mock.api.write_jsonapi.return_value = {
        "meta": {"skipped": [{"id": "123", "reason": "NOT_FOUND", "type": "tracks"}]}
    }
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


async def test_add_item_track_paginated_listing_does_not_false_heal(
    library_manager: TidalLibraryManager, provider_mock: Mock
) -> None:
    """
    Test a successful add is not misread as stale just because data omits the id.

    The response data is the paginated collection listing, not an accepted-items echo;
    a newly added track appends at the end and is absent from page one. With an empty
    meta.skipped, that must not trigger a heal that would add a different ISRC match.
    """
    provider_mock.api.write_jsonapi.return_value = {
        "data": [{"type": "tracks", "id": "999"}],
        "meta": {"skipped": []},
    }
    provider_mock.resolve_live_track_id = AsyncMock(return_value="123_live")
    item = Mock(item_id="123", media_type=MediaType.TRACK)

    assert await library_manager.add_item(item) is True

    provider_mock.resolve_live_track_id.assert_not_called()
    assert provider_mock.api.write_jsonapi.call_count == 1


async def test_add_item_track_already_present_no_heal(
    library_manager: TidalLibraryManager, provider_mock: Mock
) -> None:
    """Test an ALREADY_PRESENT skip is a success and never triggers healing."""
    provider_mock.api.write_jsonapi.return_value = {
        "meta": {"skipped": [{"id": "123", "reason": "ALREADY_PRESENT", "type": "tracks"}]}
    }
    provider_mock.resolve_live_track_id = AsyncMock(return_value="123_live")
    item = Mock(item_id="123", media_type=MediaType.TRACK)

    assert await library_manager.add_item(item) is True

    provider_mock.resolve_live_track_id.assert_not_called()
    assert provider_mock.api.write_jsonapi.call_count == 1


async def test_add_item_track_no_skipped_meta(
    library_manager: TidalLibraryManager, provider_mock: Mock
) -> None:
    """Test a response without a skipped meta (e.g. a 204 body) skips healing entirely."""
    provider_mock.api.write_jsonapi.return_value = {"success": True}
    item = Mock(item_id="123", media_type=MediaType.TRACK)

    assert await library_manager.add_item(item) is True

    provider_mock.resolve_live_track_id.assert_not_called()
    assert provider_mock.api.write_jsonapi.call_count == 1


async def test_add_item_track_stale_id_unresolvable(
    library_manager: TidalLibraryManager, provider_mock: Mock
) -> None:
    """Test an unresolvable NOT_FOUND id reports failure: nothing was added."""
    provider_mock.api.write_jsonapi.return_value = {
        "meta": {"skipped": [{"id": "123", "reason": "NOT_FOUND", "type": "tracks"}]}
    }
    # resolve_live_track_id default (from fixture) already returns None.
    item = Mock(item_id="123", media_type=MediaType.TRACK)

    assert await library_manager.add_item(item) is False

    provider_mock.resolve_live_track_id.assert_called_once_with("123")
    assert provider_mock.api.write_jsonapi.call_count == 1


async def test_add_item_track_healed_retry_also_skipped(
    library_manager: TidalLibraryManager, provider_mock: Mock
) -> None:
    """Test failure is reported when the healed id is itself rejected as NOT_FOUND."""
    provider_mock.api.write_jsonapi.side_effect = [
        {"meta": {"skipped": [{"id": "123", "reason": "NOT_FOUND", "type": "tracks"}]}},
        {"meta": {"skipped": [{"id": "123_live", "reason": "NOT_FOUND", "type": "tracks"}]}},
    ]
    provider_mock.resolve_live_track_id = AsyncMock(return_value="123_live")
    item = Mock(item_id="123", media_type=MediaType.TRACK)

    assert await library_manager.add_item(item) is False

    assert provider_mock.api.write_jsonapi.call_count == 2


async def test_add_item_non_track_no_healing(
    library_manager: TidalLibraryManager, provider_mock: Mock
) -> None:
    """Test a non-track favorite add never consults the healing resolver."""
    item = Mock(item_id="123", media_type=MediaType.ALBUM)

    assert await library_manager.add_item(item) is True

    provider_mock.resolve_live_track_id.assert_not_called()
    assert provider_mock.api.write_jsonapi.call_count == 1


async def test_remove_item_track_redirects_cached_stale_id(
    library_manager: TidalLibraryManager, provider_mock: Mock
) -> None:
    """
    Test a track removal maps a cache-known stale id to the live id in the collection.

    A DELETE for a churned id is skipped server-side while looking successful, so the
    cache-only redirect must rewrite it to the live id actually stored.
    """
    provider_mock.redirect_cached_id = AsyncMock(return_value="123_live")

    assert await library_manager.remove_item("123", MediaType.TRACK) is True

    provider_mock.redirect_cached_id.assert_called_once_with("123")
    provider_mock.api.write_jsonapi.assert_called_with(
        "DELETE",
        "userCollectionTracks/me/relationships/items",
        {"data": [{"type": "tracks", "id": "123_live"}]},
    )


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
