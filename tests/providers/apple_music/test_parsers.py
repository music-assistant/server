"""Regression tests for Apple Music parser and library fallbacks."""

from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Album, ItemMapping

from music_assistant.providers.apple_music.library import _TRACK_PAGE_SIZE, AppleMusicLibraryManager
from music_assistant.providers.apple_music.media import AppleMusicMediaManager
from music_assistant.providers.apple_music.parsers import (
    parse_album,
    parse_artwork_image,
    parse_track,
)

BLOBSTORE_ARTWORK = {
    "url": "https://store-033.blobstore.apple.com/pic/image?X-Amz-Signature=abc",
    "width": 1200,
    "height": 1200,
}
CATALOG_ARTWORK = {
    "url": "https://is1-ssl.mzstatic.com/image/thumb/Music/{w}x{h}bb.jpg",
    "width": 3000,
    "height": 3000,
}


def _stream(items: list[dict[str, Any]]) -> MagicMock:
    """Mock api.iter_all_items: a sync callable returning a fresh async iterator over items."""

    async def _gen(*_args: Any, **_kwargs: Any) -> Any:
        for item in items:
            yield item

    return MagicMock(side_effect=_gen)


def _create_provider_mock() -> MagicMock:
    """Create a provider mock with the minimum shape expected by parser functions."""
    provider = MagicMock()
    provider.instance_id = "apple_music_test"
    provider.domain = "apple_music"
    provider.logger = MagicMock()
    provider._storefront = "us"
    provider.mass.cache.get = AsyncMock(return_value=None)
    provider.mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    provider.mass.cache.set = AsyncMock()
    return provider


def test_parse_album_keeps_album_without_catalog_url() -> None:
    """Albums without catalog URL should still be parsed for library visibility."""
    provider = _create_provider_mock()
    album_obj = {
        "id": "l.album1",
        "type": "library-albums",
        "attributes": {
            "name": "Uploaded Album",
            "artistName": "Uploaded Artist",
            "playParams": {"id": "l.album1"},
        },
        "relationships": {},
    }

    result = parse_album(provider, album_obj)

    assert isinstance(result, Album)
    assert result.name == "Uploaded Album"
    mapping = next(iter(result.provider_mappings))
    assert mapping.url is None


def _make_album_obj(attributes: dict[str, Any], relationships: dict[str, Any]) -> dict[str, Any]:
    """Create a catalog album object for parse_album."""
    return {
        "id": "1234567890",
        "type": "albums",
        "attributes": {"name": "Test Album", **attributes},
        "relationships": relationships,
    }


def _artists_relationship(*names: str) -> dict[str, Any]:
    """Create an artists relationship payload with the given artist names."""
    return {
        "artists": {
            "data": [
                {"id": f"artist{idx}", "type": "artists", "attributes": {"name": name}}
                for idx, name in enumerate(names)
            ]
        }
    }


def test_parse_album_compilation_uses_album_level_artist_name() -> None:
    """Compilations must show the album-level artist, not a contributing artist."""
    provider = _create_provider_mock()
    album_obj = _make_album_obj(
        {"artistName": "Various Artists", "isCompilation": True},
        _artists_relationship("Paul McCartney"),
    )

    result = parse_album(provider, album_obj)

    assert isinstance(result, Album)
    assert [artist.name for artist in result.artists] == ["Various Artists"]


def test_parse_album_compilation_without_artist_name_keeps_related_artists() -> None:
    """A compilation without artistName must fall back to the related artists."""
    provider = _create_provider_mock()
    album_obj = _make_album_obj(
        {"isCompilation": True},
        _artists_relationship("Paul McCartney"),
    )

    result = parse_album(provider, album_obj)

    assert isinstance(result, Album)
    assert [artist.name for artist in result.artists] == ["Paul McCartney"]


def test_parse_album_single_artist_compilation_keeps_rich_artist() -> None:
    """A greatest-hits compilation by one artist must keep the full artist object."""
    provider = _create_provider_mock()
    album_obj = _make_album_obj(
        {"artistName": "Eminem", "isCompilation": True},
        _artists_relationship("Eminem"),
    )

    result = parse_album(provider, album_obj)

    assert isinstance(result, Album)
    assert len(result.artists) == 1
    assert result.artists[0].name == "Eminem"
    assert result.artists[0].item_id == "artist0"


def test_parse_album_compilation_keeps_catalog_artist_details() -> None:
    """A compilation must keep complete catalog details for a library artist."""
    provider = _create_provider_mock()
    album_obj = _make_album_obj(
        {"artistName": "Eminem", "isCompilation": True},
        {
            "artists": {
                "data": [
                    {
                        "id": "l.artist0",
                        "type": "library-artists",
                        "relationships": {
                            "catalog": {
                                "data": [
                                    {
                                        "id": "artist0",
                                        "type": "artists",
                                        "attributes": {"name": "Eminem"},
                                    }
                                ]
                            }
                        },
                    }
                ]
            }
        },
    )

    result = parse_album(provider, album_obj)

    assert isinstance(result, Album)
    assert len(result.artists) == 1
    assert result.artists[0].name == "Eminem"
    assert result.artists[0].item_id == "artist0"


def test_parse_album_dj_mix_compilation_keeps_multiple_artists() -> None:
    """A DJ-mix compilation with multiple related artists must keep them all."""
    provider = _create_provider_mock()
    album_obj = _make_album_obj(
        {"artistName": "Pete Tong & Boy George", "isCompilation": True},
        _artists_relationship("Pete Tong", "Boy George"),
    )

    result = parse_album(provider, album_obj)

    assert isinstance(result, Album)
    assert [artist.name for artist in result.artists] == ["Pete Tong", "Boy George"]


@pytest.mark.parametrize(
    "artist_obj",
    [
        {"id": "80204262", "type": "artists"},
        {"id": "80204262", "type": "artists", "attributes": {}},
        {
            "id": "l.artist.80204262",
            "type": "library-artists",
            "relationships": {"catalog": {"data": [{"id": "80204262", "type": "artists"}]}},
        },
    ],
)
def test_parse_album_compilation_ignores_placeholder_artist_stub(
    artist_obj: dict[str, Any],
) -> None:
    """An unresolvable artist stub must not become the album artist."""
    provider = _create_provider_mock()
    album_obj = _make_album_obj(
        {"artistName": "Verschillende artiesten", "isCompilation": True},
        {"artists": {"data": [artist_obj]}},
    )

    result = parse_album(provider, album_obj)

    assert isinstance(result, Album)
    assert [artist.name for artist in result.artists] == ["Verschillende artiesten"]


def test_parse_album_compilation_with_empty_artists_uses_artist_name() -> None:
    """A compilation without any related artists must use the album-level artist."""
    provider = _create_provider_mock()
    album_obj = _make_album_obj(
        {"artistName": "Various Artists", "isCompilation": True},
        {"artists": {"data": []}},
    )

    result = parse_album(provider, album_obj)

    assert isinstance(result, Album)
    assert [artist.name for artist in result.artists] == ["Various Artists"]


def test_parse_album_regular_album_keeps_related_artists() -> None:
    """A regular album must keep the artists from the relationships."""
    provider = _create_provider_mock()
    album_obj = _make_album_obj(
        {"artistName": "Paul McCartney"},
        _artists_relationship("Paul McCartney"),
    )

    result = parse_album(provider, album_obj)

    assert isinstance(result, Album)
    assert [artist.name for artist in result.artists] == ["Paul McCartney"]


def test_parse_track_falls_back_to_album_name_when_relationship_missing() -> None:
    """Track parsing should keep album info from albumName if no album relation is present."""
    provider = _create_provider_mock()
    track_obj = {
        "id": "track1",
        "type": "songs",
        "attributes": {
            "name": "Track 1",
            "artistName": "Artist 1",
            "albumName": "Album 1",
            "durationInMillis": 180000,
            "playParams": {"id": "track1"},
        },
        "relationships": {},
    }

    result = parse_track(provider, track_obj)

    assert isinstance(result.album, ItemMapping)
    assert result.album.name == "Album 1"


def test_parse_track_library_song_uses_library_album_name_fallback() -> None:
    """library-songs should fall back to library albumName when catalog attributes miss it."""
    provider = _create_provider_mock()
    track_obj = {
        "id": "i.librarytrack1",
        "type": "library-songs",
        "attributes": {
            "name": "Track 1 (Library)",
            "artistName": "Artist 1",
            "albumName": "Album From Library",
            "playParams": {"catalogId": "123456789"},
            "durationInMillis": 180000,
        },
        "relationships": {
            "catalog": {
                "data": [
                    {
                        "id": "123456789",
                        "attributes": {
                            "name": "Track 1 (Catalog)",
                            "artistName": "Artist 1",
                            "durationInMillis": 180000,
                            "playParams": {"id": "123456789"},
                        },
                    }
                ]
            }
        },
    }

    result = parse_track(provider, track_obj)

    assert isinstance(result.album, ItemMapping)
    assert result.album.name == "Album From Library"


@pytest.mark.asyncio
async def test_media_manager_get_playlist_applies_can_edit_hint() -> None:
    """Catalog playlist fetch should honor library editability hint when provided."""
    provider = _create_provider_mock()
    provider.api_client = MagicMock()
    provider.api_client.get_data = AsyncMock(
        return_value={
            "data": [
                {
                    "id": "pl.catalog1",
                    "attributes": {
                        "name": "Catalog Playlist",
                        "playParams": {"globalId": "pl.catalog1"},
                    },
                }
            ]
        }
    )

    manager = AppleMusicMediaManager(provider)

    playlist = await manager.get_playlist("pl.catalog1", can_edit_hint=True)

    assert playlist.is_editable is True


@pytest.mark.asyncio
async def test_library_playlists_preserve_can_edit_for_catalog_copy() -> None:
    """Library playlist sync should preserve canEdit when loading via catalog copy."""
    provider = _create_provider_mock()
    provider.api_client = MagicMock()
    provider.api_client.get_all_items = AsyncMock(
        return_value=[
            {
                "id": "p.library1",
                "attributes": {
                    "hasCatalog": True,
                    "canEdit": True,
                    "playParams": {"globalId": "pl.catalog1"},
                },
            }
        ]
    )
    provider.api_client.get_ratings = AsyncMock(return_value={})
    provider.media_manager = MagicMock()
    provider.media_manager.get_playlist = AsyncMock(return_value=MagicMock())

    manager = AppleMusicLibraryManager(provider)

    _ = [playlist async for playlist in manager.get_library_playlists()]

    provider.media_manager.get_playlist.assert_called_once_with(
        "pl.catalog1",
        ANY,
        can_edit_hint=True,
        library_id_override="p.library1",
    )


@pytest.mark.asyncio
async def test_library_tracks_request_includes_album_relations() -> None:
    """Library track sync should request catalog/album/artist relations from Apple API."""
    provider = _create_provider_mock()
    provider.api_client = MagicMock()
    provider.api_client.iter_all_items = _stream([])
    provider.api_client.get_ratings = AsyncMock(return_value={})

    manager = AppleMusicLibraryManager(provider)
    _ = [track async for track in manager.get_library_tracks()]

    provider.api_client.iter_all_items.assert_called_once_with(
        "me/library/songs", include="catalog,albums,artists", page_size=_TRACK_PAGE_SIZE
    )


@pytest.mark.asyncio
async def test_library_tracks_falls_back_to_library_item_when_catalog_missing() -> None:
    """Library sync should still parse track if catalog endpoint omits a catalog ID."""
    provider = _create_provider_mock()
    provider.api_client = MagicMock()
    provider.api_client.iter_all_items = _stream(
        [
            {
                "id": "i.librarytrack2",
                "type": "library-songs",
                "attributes": {
                    "name": "Missing Catalog Track",
                    "artistName": "Artist 2",
                    "albumName": "Album 2",
                    "playParams": {"catalogId": "999999"},
                    "durationInMillis": 180000,
                },
                "relationships": {
                    "catalog": {
                        "data": [
                            {
                                "id": "999999",
                                "attributes": {
                                    "name": "Missing Catalog Track",
                                    "artistName": "Artist 2",
                                    "durationInMillis": 180000,
                                    "playParams": {"id": "999999"},
                                },
                            }
                        ]
                    }
                },
            }
        ]
    )
    provider.api_client.get_data = AsyncMock(return_value={"data": []})
    provider.api_client.get_ratings = AsyncMock(return_value={"999999": True})

    manager = AppleMusicLibraryManager(provider)
    tracks = [track async for track in manager.get_library_tracks()]

    assert len(tracks) == 1
    assert tracks[0].name == "Missing Catalog Track"
    assert isinstance(tracks[0].album, ItemMapping)
    assert tracks[0].album.name == "Album 2"


@pytest.mark.asyncio
async def test_library_tracks_replaces_weak_catalog_album_mapping() -> None:
    """A weak catalog albumName mapping should be replaced by library album relation."""
    provider = _create_provider_mock()
    provider.api_client = MagicMock()
    provider.api_client.iter_all_items = _stream(
        [
            {
                "id": "i.librarytrack5",
                "type": "library-songs",
                "attributes": {
                    "name": "Weak Catalog Album Mapping",
                    "artistName": "Artist 5",
                    "playParams": {"catalogId": "555555"},
                    "durationInMillis": 180000,
                },
                "relationships": {
                    "catalog": {
                        "data": [
                            {
                                "id": "555555",
                                "attributes": {
                                    "name": "Weak Catalog Album Mapping",
                                    "artistName": "Artist 5",
                                    "albumName": "Name Only From Catalog",
                                    "durationInMillis": 180000,
                                    "playParams": {"id": "555555"},
                                },
                            }
                        ]
                    },
                    "albums": {
                        "data": [
                            {
                                "id": "l.album5",
                                "type": "library-albums",
                                "attributes": {
                                    "name": "Resolved Library Album",
                                    "artistName": "Artist 5",
                                    "playParams": {"id": "l.album5"},
                                },
                            }
                        ]
                    },
                },
            }
        ]
    )
    provider.api_client.get_data = AsyncMock(
        return_value={
            "data": [
                {
                    "id": "555555",
                    "type": "songs",
                    "attributes": {
                        "name": "Weak Catalog Album Mapping",
                        "artistName": "Artist 5",
                        "albumName": "Name Only From Catalog",
                        "durationInMillis": 180000,
                        "playParams": {"id": "555555"},
                    },
                    "relationships": {},
                }
            ]
        }
    )
    provider.api_client.get_ratings = AsyncMock(return_value={"555555": True})

    manager = AppleMusicLibraryManager(provider)
    tracks = [track async for track in manager.get_library_tracks()]

    assert len(tracks) == 1
    assert tracks[0].album is not None
    assert tracks[0].album.item_id == "l.album5"
    assert tracks[0].album.name == "Resolved Library Album"


@pytest.mark.asyncio
async def test_library_tracks_fetches_detail_when_list_item_has_no_album() -> None:
    """Library-only tracks should be reparsed from detail endpoint when album is missing."""
    provider = _create_provider_mock()
    provider.api_client = MagicMock()
    provider.api_client.iter_all_items = _stream(
        [
            {
                "id": "i.librarytrack3",
                "type": "library-songs",
                "attributes": {
                    "name": "No Album In List",
                    "artistName": "Artist 3",
                    "playParams": {"id": "i.librarytrack3", "isLibrary": True},
                    "durationInMillis": 180000,
                },
                "relationships": {},
            }
        ]
    )
    provider.api_client.get_data = AsyncMock(
        return_value={
            "data": [
                {
                    "id": "i.librarytrack3",
                    "type": "library-songs",
                    "attributes": {
                        "name": "No Album In List",
                        "artistName": "Artist 3",
                        "albumName": "Album From Detail",
                        "playParams": {"id": "i.librarytrack3", "isLibrary": True},
                        "durationInMillis": 180000,
                    },
                    "relationships": {},
                }
            ]
        }
    )
    provider.api_client.get_ratings = AsyncMock(return_value={"i.librarytrack3": True})

    manager = AppleMusicLibraryManager(provider)
    tracks = [track async for track in manager.get_library_tracks()]

    assert len(tracks) == 1
    assert isinstance(tracks[0].album, ItemMapping)
    assert tracks[0].album.name == "Album From Detail"
    provider.api_client.get_data.assert_called_once_with(
        "me/library/songs/i.librarytrack3", include="catalog,albums,artists"
    )


@pytest.mark.asyncio
async def test_library_tracks_fetches_detail_for_album_name_only_mapping() -> None:
    """List items with only albumName fallback should be upgraded to a resolvable album id."""
    provider = _create_provider_mock()
    provider.api_client = MagicMock()
    provider.api_client.iter_all_items = _stream(
        [
            {
                "id": "i.librarytrack4",
                "type": "library-songs",
                "attributes": {
                    "name": "Album Name Only",
                    "artistName": "Artist 4",
                    "albumName": "Album Only From List",
                    "playParams": {"id": "i.librarytrack4", "isLibrary": True},
                    "durationInMillis": 180000,
                },
                "relationships": {},
            }
        ]
    )
    provider.api_client.get_data = AsyncMock(
        return_value={
            "data": [
                {
                    "id": "i.librarytrack4",
                    "type": "library-songs",
                    "attributes": {
                        "name": "Album Name Only",
                        "artistName": "Artist 4",
                        "playParams": {"id": "i.librarytrack4", "isLibrary": True},
                        "durationInMillis": 180000,
                    },
                    "relationships": {
                        "albums": {
                            "data": [
                                {
                                    "id": "l.album4",
                                    "type": "library-albums",
                                    "attributes": {
                                        "name": "Resolved Album",
                                        "artistName": "Artist 4",
                                        "playParams": {"id": "l.album4"},
                                    },
                                }
                            ]
                        }
                    },
                }
            ]
        }
    )
    provider.api_client.get_ratings = AsyncMock(return_value={"i.librarytrack4": True})

    manager = AppleMusicLibraryManager(provider)
    tracks = [track async for track in manager.get_library_tracks()]

    assert len(tracks) == 1
    assert tracks[0].album is not None
    assert tracks[0].album.item_id == "l.album4"
    assert tracks[0].album.name == "Resolved Album"
    provider.api_client.get_data.assert_called_once_with(
        "me/library/songs/i.librarytrack4", include="catalog,albums,artists"
    )


@pytest.mark.asyncio
async def test_catalog_backed_playlist_uses_library_id_as_item_id() -> None:
    """
    Catalog-backed library playlists must use the library ID as item_id.

    When a playlist hasCatalog=True, Apple only accepts write operations
    (add tracks) against the library endpoint using the library ID (p.XXXXX),
    not the catalog global ID (pl.u-...).
    """
    provider = _create_provider_mock()
    provider.api_client = MagicMock()
    provider.api_client.get_all_items = AsyncMock(
        return_value=[
            {
                "id": "p.myLibraryPlaylist",
                "attributes": {
                    "hasCatalog": True,
                    "canEdit": True,
                    "playParams": {"globalId": "pl.u-abcd1234"},
                    "name": "My Public Playlist",
                    "curatorName": "me",
                    "artwork": None,
                },
            }
        ]
    )
    provider.api_client.get_ratings = AsyncMock(return_value={})
    provider.api_client.get_data = AsyncMock(
        return_value={
            "data": [
                {
                    "id": "pl.u-abcd1234",
                    "attributes": {
                        "name": "My Public Playlist",
                        "curatorName": "me",
                        "playParams": {"globalId": "pl.u-abcd1234"},
                        "canEdit": True,
                    },
                }
            ]
        }
    )

    manager = AppleMusicLibraryManager(provider)
    # Exercise parse_playlist through the real media manager.
    provider.media_manager = AppleMusicMediaManager(provider)

    playlists = [pl async for pl in manager.get_library_playlists()]

    assert len(playlists) == 1
    playlist = playlists[0]
    # Must use library ID so add_playlist_tracks targets /me/library/playlists/{id}/tracks.
    assert playlist.item_id == "p.myLibraryPlaylist"
    assert all(pm.item_id == "p.myLibraryPlaylist" for pm in playlist.provider_mappings)


@pytest.mark.asyncio
async def test_get_playlist_by_library_id_keeps_library_item_id() -> None:
    """Direct library playlist fetch must keep the library playlist ID."""
    provider = _create_provider_mock()
    provider.api_client = MagicMock()
    provider.api_client.get_data = AsyncMock(
        return_value={
            "data": [
                {
                    "id": "p.myLibraryPlaylist",
                    "attributes": {
                        "name": "My Public Playlist",
                        "curatorName": "me",
                        "playParams": {"globalId": "pl.u-abcd1234"},
                        "canEdit": True,
                    },
                }
            ]
        }
    )

    manager = AppleMusicMediaManager(provider)
    playlist = await manager.get_playlist("p.myLibraryPlaylist")

    provider.api_client.get_data.assert_called_once_with("me/library/playlists/p.myLibraryPlaylist")
    assert playlist.item_id == "p.myLibraryPlaylist"
    assert all(pm.item_id == "p.myLibraryPlaylist" for pm in playlist.provider_mappings)


@pytest.mark.asyncio
async def test_get_album_uses_library_endpoint_for_library_id() -> None:
    """get_album should use me/library/albums/{id} for library IDs (l. prefix)."""
    provider = _create_provider_mock()
    provider.api_client = MagicMock()
    provider.api_client.get_data = AsyncMock(
        return_value={
            "data": [
                {
                    "id": "l.PnamISl",
                    "type": "library-albums",
                    "attributes": {
                        "name": "Uploaded Album",
                        "artistName": "Uploaded Artist",
                        "playParams": {"id": "l.PnamISl", "isLibrary": True},
                    },
                    "relationships": {},
                }
            ]
        }
    )
    provider.api_client.get_ratings = AsyncMock(return_value={})

    manager = AppleMusicMediaManager(provider)
    album = await manager.get_album("l.PnamISl")

    provider.api_client.get_data.assert_called_once_with(
        "me/library/albums/l.PnamISl", include="catalog,artists"
    )
    assert isinstance(album, Album)
    assert album.name == "Uploaded Album"


@pytest.mark.asyncio
async def test_get_album_tracks_uses_library_endpoint_for_library_id() -> None:
    """get_album_tracks should use me/library/albums/{id}/tracks for library IDs."""
    provider = _create_provider_mock()
    provider.api_client = MagicMock()
    provider.api_client.get_data = AsyncMock(
        side_effect=[
            # First call: get_album_tracks endpoint
            {
                "data": [
                    {
                        "id": "i.track1",
                        "type": "library-songs",
                        "attributes": {
                            "name": "Track 1",
                            "artistName": "Uploaded Artist",
                            "durationInMillis": 180000,
                            "playParams": {"id": "i.track1", "isLibrary": True},
                        },
                        "relationships": {},
                    }
                ]
            },
            # Second call: get_album (called internally)
            {
                "data": [
                    {
                        "id": "l.PnamISl",
                        "type": "library-albums",
                        "attributes": {
                            "name": "Uploaded Album",
                            "artistName": "Uploaded Artist",
                            "playParams": {"id": "l.PnamISl", "isLibrary": True},
                        },
                        "relationships": {},
                    }
                ]
            },
        ]
    )
    provider.api_client.get_ratings = AsyncMock(return_value={})

    manager = AppleMusicMediaManager(provider)
    tracks = await manager.get_album_tracks("l.PnamISl")

    first_call_args = provider.api_client.get_data.call_args_list[0]
    assert first_call_args[0][0] == "me/library/albums/l.PnamISl/tracks"
    assert len(tracks) == 1
    assert tracks[0].name == "Track 1"


@pytest.mark.asyncio
async def test_get_album_uses_catalog_endpoint_for_catalog_id() -> None:
    """get_album should continue using the catalog endpoint for numeric catalog IDs."""
    provider = _create_provider_mock()
    provider.api_client = MagicMock()
    provider.api_client.get_data = AsyncMock(
        return_value={
            "data": [
                {
                    "id": "123456789",
                    "type": "albums",
                    "attributes": {
                        "name": "Catalog Album",
                        "artistName": "Catalog Artist",
                        "playParams": {"id": "123456789"},
                        "url": "https://music.apple.com/album/123456789",
                    },
                    "relationships": {},
                }
            ]
        }
    )
    provider.api_client.get_ratings = AsyncMock(return_value={})

    manager = AppleMusicMediaManager(provider)
    await manager.get_album("123456789")

    provider.api_client.get_data.assert_called_once_with(
        "catalog/us/albums/123456789", include="artists"
    )


def test_parse_artwork_image_stores_stable_token_for_expiring_urls() -> None:
    """Blobstore artwork (presigned, expiring) is stored as a resolvable token."""
    provider = _create_provider_mock()

    image = parse_artwork_image(
        provider, MediaType.ALBUM, "l.album1", {"artwork": BLOBSTORE_ARTWORK}
    )

    assert image is not None
    assert image.path == "album/l.album1"
    assert image.remotely_accessible is False
    assert image.provider == "apple_music_test"


def test_parse_artwork_image_keeps_permanent_cdn_urls() -> None:
    """Mzstatic artwork is permanent and stored as a directly accessible URL."""
    provider = _create_provider_mock()

    image = parse_artwork_image(
        provider, MediaType.ALBUM, "1234567890", {"artwork": CATALOG_ARTWORK}
    )

    assert image is not None
    assert image.path == "https://is1-ssl.mzstatic.com/image/thumb/Music/1000x1000bb.jpg"
    assert image.remotely_accessible is True


def test_parse_artwork_image_without_artwork() -> None:
    """Items without (usable) artwork produce no image."""
    provider = _create_provider_mock()

    assert parse_artwork_image(provider, MediaType.ALBUM, "x", {}) is None
    assert parse_artwork_image(provider, MediaType.ALBUM, "x", {"artwork": {"width": 1}}) is None


def test_parse_album_stores_artwork_token_for_library_album() -> None:
    """A library album with blobstore artwork ends up with a token image."""
    provider = _create_provider_mock()
    album_obj = {
        "id": "l.album1",
        "type": "library-albums",
        "attributes": {
            "name": "Uploaded Album",
            "artistName": "Uploaded Artist",
            "playParams": {"id": "l.album1"},
            "artwork": BLOBSTORE_ARTWORK,
        },
        "relationships": {},
    }

    album = parse_album(provider, album_obj)

    assert isinstance(album, Album)
    assert [(image.path, image.remotely_accessible) for image in album.metadata.images or []] == [
        ("album/l.album1", False)
    ]


async def test_get_artwork_url_returns_fresh_signed_url() -> None:
    """The artwork token resolves to the current signed URL from the api."""
    provider = _create_provider_mock()
    provider.api_client.get_data = AsyncMock(
        return_value={"data": [{"id": "l.album1", "attributes": {"artwork": BLOBSTORE_ARTWORK}}]}
    )

    manager = AppleMusicMediaManager(provider)
    url = await manager.get_artwork_url("album", "l.album1")

    assert url == BLOBSTORE_ARTWORK["url"]
    provider.api_client.get_data.assert_called_once_with(
        "me/library/albums/l.album1", include="catalog"
    )


async def test_get_artwork_url_falls_back_to_catalog_attributes() -> None:
    """A library item without own artwork resolves via its catalog counterpart."""
    provider = _create_provider_mock()
    provider.api_client.get_data = AsyncMock(
        return_value={
            "data": [
                {
                    "id": "l.album1",
                    "attributes": {"name": "Uploaded Album"},
                    "relationships": {
                        "catalog": {
                            "data": [
                                {"id": "123456789", "attributes": {"artwork": CATALOG_ARTWORK}}
                            ]
                        }
                    },
                }
            ]
        }
    )

    manager = AppleMusicMediaManager(provider)
    url = await manager.get_artwork_url("album", "l.album1")

    assert url == "https://is1-ssl.mzstatic.com/image/thumb/Music/1000x1000bb.jpg"


async def test_get_artwork_url_unknown_media_type() -> None:
    """An unknown token media type resolves to nothing instead of raising."""
    provider = _create_provider_mock()
    manager = AppleMusicMediaManager(provider)

    assert await manager.get_artwork_url("bogus", "1") is None
