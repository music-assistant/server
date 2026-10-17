"""Tests for the Subsonic library browsing endpoints."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from multidict import MultiDict, MultiDictProxy
from music_assistant_models.auth import User, UserRole
from music_assistant_models.enums import MediaType, SortDirection, SortField
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Album, Artist, ItemMapping, Track

from music_assistant.providers.subsonic_server.browse import BrowseEndpoints
from music_assistant.providers.subsonic_server.ids import to_subsonic_id
from music_assistant.providers.subsonic_server.request import SubsonicRequest
from music_assistant.providers.subsonic_server.response import SubsonicError

NAVIDROME = "opensubsonic--Bnc7DaHk"
MEMBER = User(user_id="member-id", username="member", role=UserRole.USER)


def _artist(item_id: str, name: str) -> Artist:
    return Artist(item_id=item_id, provider="library", name=name, provider_mappings=set())


def _album(item_id: str, name: str = "Album") -> Album:
    return Album(item_id=item_id, provider="library", name=name, provider_mappings=set())


def _track(item_id: str, provider: str = "library", duration: int = 100) -> Track:
    return Track(
        item_id=item_id,
        provider=provider,
        name=f"Track {item_id}",
        provider_mappings=set(),
        duration=duration,
        album=ItemMapping(
            media_type=MediaType.ALBUM, item_id="5", provider="library", name="Album"
        ),
    )


def _endpoints(stats_rows: list[dict[str, Any]] | None = None) -> tuple[BrowseEndpoints, MagicMock]:
    mass = MagicMock()
    mass.music.database.get_rows_from_query = AsyncMock(return_value=stats_rows or [])
    controllers = {
        MediaType.ARTIST: mass.music.artists,
        MediaType.ALBUM: mass.music.albums,
        MediaType.TRACK: mass.music.tracks,
        MediaType.PLAYLIST: mass.music.playlists,
    }
    mass.music.get_controller.side_effect = controllers.__getitem__
    return BrowseEndpoints(mass), mass


def _request(**params: str) -> SubsonicRequest:
    return SubsonicRequest(params=MultiDictProxy(MultiDict(params)), user=MEMBER)


async def _call(endpoints: BrowseEndpoints, name: str, **params: str) -> Any:
    return await endpoints.endpoints()[name](_request(**params))


async def test_get_artists_groups_by_letter_and_skips_artists_without_albums() -> None:
    """Artists are indexed by the first letter of their sort name; track-only artists are left out."""
    endpoints, mass = _endpoints(
        stats_rows=[
            {"artist_id": 1, "album_count": 2},
            {"artist_id": 2, "album_count": 1},
            {"artist_id": 4, "album_count": 1},
        ]
    )
    artists = [
        _artist("1", "!!!"),
        _artist("2", "Air"),
        _artist("3", "Alone"),
        _artist("4", "Björk"),
    ]

    async def iter_artists(**_kwargs: Any) -> AsyncGenerator[Artist]:
        for artist in artists:
            yield artist

    mass.music.artists.iter_library_items = iter_artists
    body = await _call(endpoints, "getArtists")
    index = body["artists"]["index"]
    assert [entry["name"] for entry in index] == ["#", "A", "B"]
    assert [artist["id"] for artist in index[1]["artist"]] == ["ar-2"]
    assert index[0]["artist"][0]["albumCount"] == 2


async def test_get_album_lists_library_and_provider_tracks() -> None:
    """GetAlbum returns every album track, including those only on the provider."""
    endpoints, mass = _endpoints()
    mass.music.albums.get_library_item = AsyncMock(return_value=_album("5"))
    mass.music.albums.tracks = AsyncMock(
        return_value=[_track("10", duration=100), _track("abc", provider=NAVIDROME, duration=50)]
    )
    body = await _call(endpoints, "getAlbum", id="al-5")
    album = body["album"]
    assert album["songCount"] == 2
    assert album["duration"] == 150
    assert [song["id"] for song in album["song"]] == [
        "tr-10",
        to_subsonic_id(MediaType.TRACK, "abc", NAVIDROME),
    ]
    mass.music.albums.tracks.assert_awaited_once_with("5", "library", limit=0)


async def test_get_album_of_provider_album() -> None:
    """A provider album id is looked up on its provider."""
    endpoints, mass = _endpoints()
    album = Album(item_id="xyz", provider=NAVIDROME, name="Remote", provider_mappings=set())
    mass.music.albums.get = AsyncMock(return_value=album)
    mass.music.albums.tracks = AsyncMock(return_value=[])
    album_id = to_subsonic_id(MediaType.ALBUM, "xyz", NAVIDROME)
    body = await _call(endpoints, "getAlbum", id=album_id)
    assert body["album"]["id"] == album_id
    mass.music.albums.get.assert_awaited_once_with("xyz", NAVIDROME)


async def test_unknown_item_is_not_found() -> None:
    """A library id that does not exist gives error 70."""
    endpoints, mass = _endpoints()
    mass.music.albums.get_library_item = AsyncMock(side_effect=MediaNotFoundError("gone"))
    with pytest.raises(SubsonicError) as err:
        await _call(endpoints, "getAlbum", id="al-404")
    assert err.value.code == 70


async def test_album_list_uses_library_sort_and_stats() -> None:
    """GetAlbumList2 maps the list type onto a library sort and fills the track stats."""
    endpoints, mass = _endpoints(stats_rows=[{"album_id": 7, "song_count": 9, "duration": 1800}])
    mass.music.albums.library_items = AsyncMock(return_value=[_album("7")])
    body = await _call(endpoints, "getAlbumList2", type="newest", size="5", offset="10")
    album = body["albumList2"]["album"][0]
    assert (album["songCount"], album["duration"]) == (9, 1800)
    kwargs = mass.music.albums.library_items.await_args.kwargs
    assert kwargs["sort_field"] == SortField.TIMESTAMP_ADDED
    assert kwargs["sort_direction"] == SortDirection.DESC
    assert (kwargs["limit"], kwargs["offset"]) == (5, 10)


async def test_album_list_by_year_descending() -> None:
    """A reversed year range lists the albums from newest to oldest year."""
    endpoints, mass = _endpoints()
    mass.music.albums.get_library_items_by_query = AsyncMock(return_value=[])
    await _call(endpoints, "getAlbumList2", type="byYear", fromYear="2020", toYear="2010")
    kwargs = mass.music.albums.get_library_items_by_query.await_args.kwargs
    assert kwargs["sort_direction"] == SortDirection.DESC
    assert kwargs["extra_query_params"] == {"year_low": 2010, "year_high": 2020}


async def test_album_list_starred_and_unknown_type() -> None:
    """The starred list filters on favorites; an unknown type is an error."""
    endpoints, mass = _endpoints()
    mass.music.albums.library_items = AsyncMock(return_value=[])
    await _call(endpoints, "getAlbumList2", type="starred")
    assert mass.music.albums.library_items.await_args.kwargs["favorite"] is True
    with pytest.raises(SubsonicError):
        await _call(endpoints, "getAlbumList2", type="bogus")


async def test_search3_empty_query_lists_everything() -> None:
    """An empty (or quoted empty) query searches without a filter, with the given offsets."""
    endpoints, mass = _endpoints()
    mass.music.artists.library_items = AsyncMock(return_value=[])
    mass.music.albums.library_items = AsyncMock(return_value=[])
    mass.music.tracks.library_items = AsyncMock(return_value=[_track("1")])
    body = await _call(
        endpoints, "search3", query='""', artistCount="0", albumCount="0", songOffset="40"
    )
    assert [song["id"] for song in body["searchResult3"]["song"]] == ["tr-1"]
    assert body["searchResult3"]["artist"] == []
    mass.music.artists.library_items.assert_not_awaited()
    kwargs = mass.music.tracks.library_items.await_args.kwargs
    assert kwargs["search"] is None
    assert kwargs["offset"] == 40


@pytest.mark.parametrize(
    ("data", "content_type"),
    [
        (b"\xff\xd8\xff\xe0jpeg", "image/jpeg"),
        (b"\x89PNG\r\n", "image/png"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8", "image/webp"),
    ],
)
async def test_get_cover_art(data: bytes, content_type: str) -> None:
    """Cover art is served as an image, with the requested size rounded up to a thumb size."""
    endpoints, mass = _endpoints()
    mass.music.albums.get_library_item = AsyncMock(return_value=_album("5"))
    mass.metadata.get_image_data_for_item = AsyncMock(return_value=data)
    response = await _call(endpoints, "getCoverArt", id="al-5", size="300")
    assert isinstance(response, web.Response)
    assert response.content_type == content_type
    assert response.body == data
    assert mass.metadata.get_image_data_for_item.await_args.kwargs["size"] == 512


async def test_get_cover_art_missing() -> None:
    """An item without an image gives error 70."""
    endpoints, mass = _endpoints()
    mass.music.albums.get_library_item = AsyncMock(return_value=_album("5"))
    mass.metadata.get_image_data_for_item = AsyncMock(return_value=None)
    with pytest.raises(SubsonicError) as err:
        await _call(endpoints, "getCoverArt", id="al-5")
    assert err.value.code == 70


async def test_top_songs_by_artist_name() -> None:
    """GetTopSongs resolves the artist by exact name when no id is given."""
    endpoints, mass = _endpoints()
    mass.music.artists.library_items = AsyncMock(
        return_value=[_artist("8", "Air Supply"), _artist("9", "Air")]
    )
    mass.music.artists.top_tracks = AsyncMock(return_value=[_track("1")])
    body = await _call(endpoints, "getTopSongs", artist="air", count="5")
    assert [song["id"] for song in body["topSongs"]["song"]] == ["tr-1"]
    mass.music.artists.top_tracks.assert_awaited_once_with("9", "library", limit=5)


async def test_songs_by_unknown_genre() -> None:
    """An unknown genre gives an empty list."""
    endpoints, mass = _endpoints()
    mass.music.genres.library_items = AsyncMock(return_value=[])
    body = await _call(endpoints, "getSongsByGenre", genre="Polka")
    assert body == {"songsByGenre": {"song": []}}
