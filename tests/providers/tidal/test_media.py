"""Test Tidal Media Manager."""

import json
import pathlib
from typing import Any, cast
from unittest.mock import Mock, patch

import pytest
from aiohttp.client_exceptions import ClientError
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    RateLimited,
    ResourceTemporarilyUnavailable,
    RetriesExhausted,
)
from music_assistant_models.media_items import Playlist

from music_assistant.providers.tidal.jsonapi import JsonApiDocument
from music_assistant.providers.tidal.media import TidalMediaManager

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures" / "v2"
LEGACY_FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"


def _load_raw(name: str) -> dict[str, Any]:
    with open(FIXTURES_DIR / name) as f:
        data: dict[str, Any] = json.load(f)
        return data


def _load_doc(name: str) -> JsonApiDocument:
    return JsonApiDocument(_load_raw(name))


def _break_resource(raw: dict[str, Any], resource_id: str) -> dict[str, Any]:
    """Blank the title of one included resource, which its parser cannot handle."""
    for resource in raw["included"]:
        if resource["id"] == resource_id:
            attributes = resource["attributes"]
            # artists carry a name where tracks and albums carry a title
            attributes["name" if "name" in attributes else "title"] = None
            return raw
    raise AssertionError(f"resource {resource_id} not in fixture")


def _corrupt_resource(
    raw: dict[str, Any], resource_id: str, attribute: str | None, value: Any
) -> dict[str, Any]:
    """Corrupt one included resource in a fixture."""
    for resource in raw["included"]:
        if resource["id"] != resource_id:
            continue
        if attribute is None:
            resource["attributes"] = value
        else:
            resource["attributes"][attribute] = value
        return raw
    raise AssertionError(f"resource {resource_id} not in fixture")


async def test_search(media_manager: TidalMediaManager, provider_mock: Mock) -> None:
    """Test search reads all types from the official searchResults endpoint."""
    provider_mock.api.get_jsonapi.return_value = _load_doc("search.json")

    results = await media_manager.search(
        "lukas graham",
        [MediaType.ARTIST, MediaType.ALBUM, MediaType.TRACK, MediaType.PLAYLIST],
        limit=5,
    )

    # 20 of each in the fixture, capped to the limit
    assert len(results.tracks) == 5
    assert len(results.albums) == 5
    assert len(results.artists) == 5
    assert len(results.playlists) == 5
    assert results.tracks[0].name == "7 Years"
    first_playlist = results.playlists[0]
    assert isinstance(first_playlist, Playlist)
    assert first_playlist.is_editable is False

    provider_mock.api.get_jsonapi.assert_called_with(
        "searchResults",
        params={"filter[query]": "lukas graham"},
        include=[
            "tracks.artists",
            "tracks.albums.coverArt",
            "albums.coverArt",
            "artists.profileArt",
            "playlists.coverArt",
        ],
    )


async def test_search_single_type_passes_query_verbatim(
    media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test search requests only the includes for the wanted types, query as a filter."""
    provider_mock.api.get_jsonapi.return_value = _load_doc("search.json")

    results = await media_manager.search("AC/DC", [MediaType.ARTIST])

    assert results.tracks == []
    provider_mock.api.get_jsonapi.assert_called_with(
        "searchResults", params={"filter[query]": "AC/DC"}, include=["artists.profileArt"]
    )


async def test_search_no_types_short_circuits(
    media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test search with no supported media types does not hit the API."""
    results = await media_manager.search("x", [])

    assert results.tracks == []
    provider_mock.api.get_jsonapi.assert_not_called()


@patch("music_assistant.providers.tidal.media.parse_artist_v2")
async def test_get_artist(
    mock_parse_artist: Mock, media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test get_artist uses the official JSON:API endpoint."""
    doc = Mock()
    provider_mock.api.get_jsonapi.return_value = doc
    mock_parse_artist.return_value = Mock(item_id="1")

    artist = await media_manager.get_artist("1")

    assert artist.item_id == "1"
    provider_mock.api.get_jsonapi.assert_called_with(
        "artists/1", include=["profileArt", "biography"]
    )
    mock_parse_artist.assert_called_once_with(provider_mock, doc, doc.data)


@patch("music_assistant.providers.tidal.media.parse_album_v2")
async def test_get_album(
    mock_parse_album: Mock, media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test get_album uses the official JSON:API endpoint."""
    doc = Mock()
    provider_mock.api.get_jsonapi.return_value = doc
    mock_parse_album.return_value = Mock(item_id="1")

    album = await media_manager.get_album("1")

    assert album.item_id == "1"
    provider_mock.api.get_jsonapi.assert_called_with(
        "albums/1", include=["artists", "coverArt", "genres"]
    )
    mock_parse_album.assert_called_once_with(provider_mock, doc, doc.data)


@patch("music_assistant.providers.tidal.media.parse_track_v2")
async def test_get_track(
    mock_parse_track: Mock, media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test get_track fetches the official track and applies unofficial lyrics."""
    doc = Mock()
    provider_mock.api.get_jsonapi.return_value = doc
    track = Mock(item_id="1")
    track.metadata = Mock()
    mock_parse_track.return_value = track
    provider_mock.api.get.return_value = {"lyrics": "Test Lyrics", "subtitles": "Synced"}

    result = await media_manager.get_track("1")

    assert result.item_id == "1"
    provider_mock.api.get_jsonapi.assert_called_with(
        "tracks/1", include=["artists", "albums", "albums.coverArt", "genres", "credits"]
    )
    provider_mock.api.get.assert_called_with("tracks/1/lyrics")
    assert track.metadata.lyrics == "Test Lyrics"
    assert track.metadata.lrc_lyrics == "Synced"


@patch("music_assistant.providers.tidal.media.parse_track_v2")
async def test_get_track_tolerates_lyrics_failure(
    mock_parse_track: Mock, media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test get_track still returns the track when the lyrics lookup fails."""
    doc = Mock()
    provider_mock.api.get_jsonapi.return_value = doc
    track = Mock(item_id="1")
    mock_parse_track.return_value = track
    provider_mock.api.get.side_effect = RetriesExhausted("lyrics lookup failed")

    result = await media_manager.get_track("1")

    assert result.item_id == "1"
    mock_parse_track.assert_called_once_with(provider_mock, doc, doc.data)


async def test_get_album_tracks(media_manager: TidalMediaManager, provider_mock: Mock) -> None:
    """Test album tracks read from the official relationship endpoint with ordering."""
    doc = _load_doc("album_items.json")

    async def _pages(*_a: Any, **_k: Any) -> Any:
        yield doc

    provider_mock.api.paginate_jsonapi = _pages

    tracks = await media_manager.get_album_tracks("58756127")

    assert len(tracks) == 11
    assert tracks[0].name == "7 Years"
    assert tracks[0].track_number == 1
    assert tracks[0].disc_number == 1
    assert tracks[0].album is not None


async def test_get_album_tracks_skips_videos(
    media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test a music video in the items relationship is not parsed as a track."""
    raw = _load_raw("album_items.json")
    raw["data"].insert(
        0, {"id": "99999", "type": "videos", "meta": {"volumeNumber": 1, "trackNumber": 1}}
    )
    raw["included"].append(
        {"id": "99999", "type": "videos", "attributes": {"title": "Bonus Video"}}
    )
    doc = JsonApiDocument(raw)

    async def _pages(*_a: Any, **_k: Any) -> Any:
        yield doc

    provider_mock.api.paginate_jsonapi = _pages

    tracks = await media_manager.get_album_tracks("58756127")

    assert len(tracks) == 11
    assert all(track.item_id != "99999" for track in tracks)


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        (None, None),
        ("copyright", "invalid"),
        ("externalLinks", ["invalid"]),
    ],
    ids=["null-attributes", "string-copyright", "string-external-link"],
)
async def test_get_album_tracks_skips_unparsable_track(
    attribute: str | None, value: Any, media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test one unusable track does not take the rest of the album down with it."""
    doc = JsonApiDocument(
        _corrupt_resource(_load_raw("album_items.json"), "58756128", attribute, value)
    )

    async def _pages(*_a: Any, **_k: Any) -> Any:
        yield doc

    provider_mock.api.paginate_jsonapi = _pages

    tracks = await media_manager.get_album_tracks("58756127")

    assert len(tracks) == 10
    assert all(track.item_id != "58756128" for track in tracks)
    provider_mock.logger.warning.assert_called_once()


def test_process_tracks_skips_track_with_invalid_media_metadata(
    media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test a malformed playlist track is skipped without dropping valid siblings."""
    with open(LEGACY_FIXTURES_DIR / "tracks" / "track.json", encoding="utf-8") as f:
        malformed = json.load(f)
    malformed["item"]["mediaMetadata"] = "invalid"
    valid = {
        "id": 2,
        "title": "Valid Track",
        "duration": 120,
        "artists": [{"id": 2, "name": "Valid Artist"}],
    }

    tracks = media_manager._process_tracks([malformed, valid], 0)

    assert [track.item_id for track in tracks] == ["2"]
    provider_mock.logger.warning.assert_called_once()


def test_process_tracks_skips_track_with_invalid_item_wrapper(
    media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test logging a malformed track wrapper does not abort the collection."""
    valid = {
        "id": 2,
        "title": "Valid Track",
        "duration": 120,
        "artists": [{"id": 2, "name": "Valid Artist"}],
    }

    tracks = media_manager._process_tracks([{"item": "invalid"}, valid], 0)

    assert [track.item_id for track in tracks] == ["2"]
    provider_mock.logger.warning.assert_called_once()


def test_process_tracks_skips_non_mapping_item(
    media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test logging a non-mapping track does not abort the collection."""
    invalid = cast("dict[str, Any]", None)
    valid = {
        "id": 2,
        "title": "Valid Track",
        "duration": 120,
        "artists": [{"id": 2, "name": "Valid Artist"}],
    }

    tracks = media_manager._process_tracks([invalid, valid], 0)

    assert [track.item_id for track in tracks] == ["2"]
    provider_mock.logger.warning.assert_called_once()


@pytest.mark.parametrize(
    "error",
    [
        ClientError("connection lost"),
        LoginFailed("login failed"),
        RateLimited("rate limited", backoff_time=1),
        ResourceTemporarilyUnavailable("temporarily unavailable"),
        MediaNotFoundError("not found"),
    ],
    ids=lambda error: type(error).__name__,
)
@patch("music_assistant.providers.tidal.media.parse_track")
def test_process_tracks_propagates_fetch_errors(
    mock_parse_track: Mock,
    error: Exception,
    media_manager: TidalMediaManager,
) -> None:
    """Test fetch-level errors are never treated as skippable item errors."""
    mock_parse_track.side_effect = error

    with pytest.raises(type(error)):
        media_manager._process_tracks([{"id": "1"}], 0)


async def test_get_album_tracks_keeps_tracks_from_earlier_pages(
    media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test an unusable track on a later page does not discard the earlier pages."""
    first_page = _load_doc("album_items.json")
    second_page = JsonApiDocument(
        {
            "data": [
                {"id": "998", "type": "tracks", "meta": {"volumeNumber": 1, "trackNumber": 12}},
                {"id": "999", "type": "tracks", "meta": {"volumeNumber": 1, "trackNumber": 13}},
            ],
            "included": [
                {"id": "998", "type": "tracks", "attributes": {"title": None}},
                {
                    "id": "999",
                    "type": "tracks",
                    "attributes": {"title": "Hidden Track", "duration": "PT3M0S"},
                },
            ],
        }
    )

    async def _pages(*_a: Any, **_k: Any) -> Any:
        yield first_page
        yield second_page

    provider_mock.api.paginate_jsonapi = _pages

    tracks = await media_manager.get_album_tracks("58756127")

    assert len(tracks) == 12
    assert tracks[0].name == "7 Years"
    assert tracks[-1].name == "Hidden Track"
    assert tracks[-1].track_number == 13


async def test_get_album_tracks_fetch_failure_propagates(
    media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test a failure to read a page is raised instead of returning a partial album."""

    async def _pages(*_a: Any, **_k: Any) -> Any:
        yield _load_doc("album_items.json")
        raise ClientError("connection lost")

    provider_mock.api.paginate_jsonapi = _pages

    with pytest.raises(ClientError):
        await media_manager.get_album_tracks("58756127")


async def test_get_album_tracks_missing_album_still_not_found(
    media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test an album that is gone is still reported as not found."""

    async def _pages(*_a: Any, **_k: Any) -> Any:
        for _ in ():  # never yields: the api client raises while fetching the first page
            yield
        raise MediaNotFoundError("Item not found: albums/1/relationships/items")

    provider_mock.api.paginate_jsonapi = _pages

    with pytest.raises(MediaNotFoundError):
        await media_manager.get_album_tracks("1")


async def test_get_artist_albums(media_manager: TidalMediaManager, provider_mock: Mock) -> None:
    """Test artist albums read from the official relationship endpoint."""
    doc = _load_doc("artist_albums.json")

    async def _pages(*_a: Any, **_k: Any) -> Any:
        yield doc

    provider_mock.api.paginate_jsonapi = _pages

    albums = await media_manager.get_artist_albums("4184211")

    assert len(albums) == 20
    assert all(album.item_id for album in albums)


async def test_get_artist_toptracks(media_manager: TidalMediaManager, provider_mock: Mock) -> None:
    """Test artist top tracks read from the official relationship endpoint."""
    provider_mock.api.get_jsonapi.return_value = _load_doc("artist_toptracks.json")

    tracks = await media_manager.get_artist_toptracks("4184211")

    assert len(tracks) == 20
    provider_mock.api.get_jsonapi.assert_called_with(
        "artists/4184211/relationships/tracks",
        params={"collapseBy": "FINGERPRINT"},
        include=["tracks.artists", "tracks.albums.coverArt"],
        replace_media="tracks",
    )


async def test_get_artist_tracks(media_manager: TidalMediaManager, provider_mock: Mock) -> None:
    """Test the full artist track list paginated from the official relationship endpoint."""
    doc = _load_doc("artist_toptracks.json")
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def _pages(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        yield doc
        yield doc

    provider_mock.api.paginate_jsonapi = _pages

    tracks = await media_manager.get_artist_tracks("4184211")

    assert len(tracks) == 40
    assert all(track.item_id for track in tracks)
    assert calls == [
        (
            ("artists/4184211/relationships/tracks",),
            {
                "params": {"collapseBy": "FINGERPRINT"},
                "include": ["tracks.artists", "tracks.albums.coverArt"],
                "replace_media": "tracks",
            },
        )
    ]


async def test_get_artist_albums_skips_unparsable_album(
    media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test one unusable album does not empty the artist's discography."""
    doc = JsonApiDocument(_break_resource(_load_raw("artist_albums.json"), "541774424"))

    async def _pages(*_a: Any, **_k: Any) -> Any:
        yield doc

    provider_mock.api.paginate_jsonapi = _pages

    albums = await media_manager.get_artist_albums("4184211")

    assert len(albums) == 19
    assert all(album.item_id != "541774424" for album in albums)


async def test_get_artist_toptracks_skips_unparsable_track(
    media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test one unusable track does not empty the artist's top tracks."""
    provider_mock.api.get_jsonapi.return_value = JsonApiDocument(
        _break_resource(_load_raw("artist_toptracks.json"), "58503071")
    )

    tracks = await media_manager.get_artist_toptracks("4184211")

    assert len(tracks) == 19
    assert all(track.item_id != "58503071" for track in tracks)


async def test_get_artist_toptracks_fetch_failure_propagates(
    media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test a failure to read the top tracks is raised instead of reported as empty."""
    provider_mock.api.get_jsonapi.side_effect = ClientError("connection lost")

    with pytest.raises(ClientError):
        await media_manager.get_artist_toptracks("4184211")


async def test_get_similar_tracks_respects_limit(
    media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test similar tracks are read from the official endpoint and capped to the limit."""
    provider_mock.api.get_jsonapi.return_value = _load_doc("similar_tracks.json")

    tracks = await media_manager.get_similar_tracks("58756128", limit=5)

    assert len(tracks) == 5


async def test_get_similar_tracks_fills_the_limit_around_an_unparsable_track(
    media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test the limit is still filled when one of the similar tracks cannot be parsed."""
    raw = _load_raw("similar_tracks.json")
    provider_mock.api.get_jsonapi.return_value = JsonApiDocument(
        _break_resource(raw, raw["data"][0]["id"])
    )

    tracks = await media_manager.get_similar_tracks("58756128", limit=5)

    assert len(tracks) == 5
    assert all(track.item_id != raw["data"][0]["id"] for track in tracks)


async def test_get_similar_artists(media_manager: TidalMediaManager, provider_mock: Mock) -> None:
    """Test similar artists read from the official relationship endpoint."""
    provider_mock.api.get_jsonapi.return_value = _load_doc("similar_artists.json")

    artists = await media_manager.get_similar_artists("4184211")

    assert len(artists) == 20
    provider_mock.api.get_jsonapi.assert_called_with(
        "artists/4184211/relationships/similarArtists",
        include=["similarArtists.profileArt"],
    )


async def test_get_similar_artists_skips_unparsable_artist(
    media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test one unusable artist does not empty the similar artists."""
    raw = _load_raw("similar_artists.json")
    provider_mock.api.get_jsonapi.return_value = JsonApiDocument(
        _break_resource(raw, raw["data"][0]["id"])
    )

    artists = await media_manager.get_similar_artists("4184211")

    assert len(artists) == 19
    assert all(artist.item_id != raw["data"][0]["id"] for artist in artists)


async def test_search_skips_unparsable_track(
    media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test one unusable track does not drop the whole search result."""
    raw = _load_raw("search.json")
    broken_id = raw["data"][0]["relationships"]["tracks"]["data"][0]["id"]
    provider_mock.api.get_jsonapi.return_value = JsonApiDocument(_break_resource(raw, broken_id))

    results = await media_manager.search("lukas graham", [MediaType.TRACK], limit=5)

    assert len(results.tracks) == 4
    assert all(track.item_id != broken_id for track in results.tracks)


@patch("music_assistant.providers.tidal.media.parse_track_v2")
async def test_favorite_tracks_skips_unparsable_track(
    mock_parse_track: Mock, media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test one unusable favourite does not fail the whole favourites playlist."""
    doc = JsonApiDocument(
        {
            "data": [
                {"type": "tracks", "id": "1"},
                {"type": "tracks", "id": "2"},
                {"type": "tracks", "id": "3"},
            ],
            "included": [
                {"type": "tracks", "id": "1", "attributes": {}},
                {"type": "tracks", "id": "2", "attributes": {}},
                {"type": "tracks", "id": "3", "attributes": {}},
            ],
        }
    )

    async def _pages(*_a: Any, **_k: Any) -> Any:
        yield doc

    provider_mock.api.paginate_jsonapi = _pages
    mock_parse_track.side_effect = [Mock(item_id="1"), KeyError("id"), Mock(item_id="3")]

    tracks = await media_manager.get_playlist_tracks("favorite_tracks", page=0)

    assert [track.item_id for track in tracks] == ["1", "3"]
    assert [track.position for track in tracks] == [1, 2]


@patch("music_assistant.providers.tidal.media.parse_playlist")
async def test_get_playlist(
    mock_parse_playlist: Mock, media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test get_playlist."""
    provider_mock.api.get.return_value = {"uuid": "1", "title": "Test Playlist"}
    mock_parse_playlist.return_value = Mock(item_id="1")

    playlist = await media_manager.get_playlist("1")

    assert playlist.item_id == "1"
    provider_mock.api.get.assert_called_with("playlists/1")
    mock_parse_playlist.assert_called_once()


@patch("music_assistant.providers.tidal.media.parse_track")
async def test_get_playlist_tracks(
    mock_parse_track: Mock, media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test get_playlist_tracks."""
    provider_mock.api.get.return_value = {"items": [{"id": 1}]}
    mock_parse_track.return_value = Mock(item_id="1")

    tracks = await media_manager.get_playlist_tracks("1")

    assert len(tracks) == 1
    assert tracks[0].item_id == "1"
    provider_mock.api.get.assert_called_with(
        "playlists/1/tracks",
        params={"limit": 200, "offset": 0},
    )


async def test_get_playlist_favorite_tracks(
    media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test get_playlist returns the virtual favorite tracks playlist without an API call."""
    playlist = await media_manager.get_playlist("favorite_tracks")

    assert playlist.item_id == "favorite_tracks"
    assert playlist.name == "Favorite Tracks"
    assert not playlist.is_editable
    provider_mock.api.get.assert_not_called()


@patch("music_assistant.providers.tidal.media.parse_track_v2")
async def test_get_playlist_tracks_favorite_tracks(
    mock_parse_track: Mock, media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test get_playlist_tracks walks the official collection with sequential positions."""
    doc = JsonApiDocument(
        {
            "data": [
                {"type": "tracks", "id": "1"},
                {"type": "tracks", "id": "2"},
            ],
            "included": [
                {"type": "tracks", "id": "1", "attributes": {}},
                {"type": "tracks", "id": "2", "attributes": {}},
            ],
        }
    )

    async def _pages(*_a: Any, **_k: Any) -> Any:
        yield doc

    provider_mock.api.paginate_jsonapi = _pages
    mock_parse_track.side_effect = [Mock(item_id="1"), Mock(item_id="2")]

    tracks = await media_manager.get_playlist_tracks("favorite_tracks", page=0)

    assert len(tracks) == 2
    assert tracks[0].item_id == "1"
    assert tracks[0].position == 1
    assert tracks[1].item_id == "2"
    assert tracks[1].position == 2


@patch("music_assistant.providers.tidal.media.parse_track_v2")
async def test_favorite_tracks_walk_feeds_churn_cache(
    mock_parse_track: Mock, media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """
    Test the favourites walk hands each linkage item to note_replaced_track.

    The read is made with replaceMedia, so Tidal already computed any stale->live
    replacement pairs; discarding them would waste the largest healing source.
    """
    items = [{"type": "tracks", "id": "1"}, {"type": "tracks", "id": "2"}]
    doc = JsonApiDocument(
        {
            "data": items,
            "included": [
                {"type": "tracks", "id": "1", "attributes": {}},
                {"type": "tracks", "id": "2", "attributes": {}},
            ],
        }
    )

    async def _pages(*_a: Any, **_k: Any) -> Any:
        yield doc

    provider_mock.api.paginate_jsonapi = _pages
    mock_parse_track.side_effect = [Mock(item_id="1"), Mock(item_id="2")]

    await media_manager.get_playlist_tracks("favorite_tracks", page=0)

    assert provider_mock.note_replaced_track.call_count == 2
    provider_mock.note_replaced_track.assert_any_call(items[0])
    provider_mock.note_replaced_track.assert_any_call(items[1])


async def test_get_playlist_tracks_favorite_tracks_later_page(
    media_manager: TidalMediaManager, provider_mock: Mock
) -> None:
    """Test get_playlist_tracks returns nothing for pages after the first, without a fetch."""
    provider_mock.api.paginate_jsonapi = Mock()

    tracks = await media_manager.get_playlist_tracks("favorite_tracks", page=1)

    assert tracks == []
    provider_mock.api.paginate_jsonapi.assert_not_called()
