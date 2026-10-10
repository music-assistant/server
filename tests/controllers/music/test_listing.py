"""Tests for the shared pipeline of the listings assembled in memory."""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime

import pytest
from music_assistant_models.api import SortOptionInfo
from music_assistant_models.enums import ListingType, SortDirection, SortField
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    BrowseFolder,
    Genre,
    MediaItem,
    Playlist,
    Podcast,
    PodcastEpisode,
    Radio,
    Track,
)

from music_assistant.controllers.music.listing import (
    _SORT_KEYS,
    apply_listing,
    resolve_listing_sort,
)
from music_assistant.controllers.music.sorting import LIBRARY_LISTINGS, LISTING_SORT_OPTIONS

from .helpers import create_album, create_track

# the item type each listing holds; a playlist also holds other playable items and a browse
# folder also holds media items, which sort on the same attributes
LISTING_ITEM_TYPES: dict[ListingType, type[MediaItem | BrowseFolder]] = {
    ListingType.LIBRARY_ARTISTS: Artist,
    ListingType.LIBRARY_ALBUMS: Album,
    ListingType.LIBRARY_TRACKS: Track,
    ListingType.LIBRARY_PLAYLISTS: Playlist,
    ListingType.LIBRARY_RADIOS: Radio,
    ListingType.LIBRARY_AUDIOBOOKS: Audiobook,
    ListingType.LIBRARY_PODCASTS: Podcast,
    ListingType.LIBRARY_GENRES: Genre,
    ListingType.ALBUM_TRACKS: Track,
    ListingType.ARTIST_ALBUMS: Album,
    ListingType.ARTIST_TRACKS: Track,
    ListingType.ARTIST_APPEARS_ON: Album,
    ListingType.ARTIST_DISCOGRAPHY: Album,
    ListingType.ARTIST_TOP_TRACKS: Track,
    ListingType.ARTIST_TOP_ALBUMS: Album,
    ListingType.ARTIST_AUDIOBOOKS: Audiobook,
    ListingType.SIMILAR_ARTISTS: Artist,
    ListingType.SIMILAR_TRACKS: Track,
    ListingType.TRACK_ALBUMS: Album,
    ListingType.VERSIONS: MediaItem,
    ListingType.PLAYLIST_TRACKS: Track,
    ListingType.PODCAST_EPISODES: PodcastEpisode,
    ListingType.GENRE_TRACKS: Track,
    ListingType.GENRE_ALBUMS: Album,
    ListingType.BROWSE: BrowseFolder,
}
# the item attribute each in-memory sort field reads
SORT_FIELD_ATTRIBUTES = {
    SortField.NAME: "name",
    SortField.SORT_NAME: "sort_name",
    SortField.TIMESTAMP_ADDED: "date_added",
    SortField.LAST_PLAYED: "last_played",
    SortField.DURATION: "duration",
    SortField.YEAR: "year",
    SortField.POSITION: "position",
    SortField.ARTIST_NAME: "artists",
    SortField.TRACK_NUMBER: "track_number",
    SortField.ALBUM_NAME: "album",
    SortField.PROVIDER: "provider",
}
# the listings that sort in SQL, on the columns of the library rather than on the items
SQL_LISTINGS = {*LIBRARY_LISTINGS.values(), ListingType.GENRE_TRACKS, ListingType.GENRE_ALBUMS}


def _track(  # noqa: PLR0913
    item_id: str,
    name: str,
    *,
    artist: str,
    album: str,
    duration: int,
    disc_number: int,
    track_number: int,
    position: int,
    date_added: datetime | None,
    provider: str,
    last_played: int,
) -> Track:
    track = create_track(provider, item_id, name=name, duration=duration)
    track.artists[0].name = artist
    track.album = create_album(provider, f"{item_id}_album", name=album)
    track.disc_number, track.track_number = disc_number, track_number
    track.position, track.date_added, track.last_played = position, date_added, last_played
    return track


@pytest.fixture
def tracks() -> list[Track]:
    """Return a listing of tracks, in the order their source lists them."""
    return [
        _track(
            "t1",
            "Éclair",
            artist="Zed",
            album="Beta",
            duration=300,
            disc_number=1,
            track_number=3,
            position=3,
            date_added=datetime(2024, 1, 3, tzinfo=UTC),
            provider="spotify_1",
            last_played=30,
        ),
        _track(
            "t2",
            "The Apple",
            artist="Anna",
            album="Gamma",
            duration=100,
            disc_number=1,
            track_number=1,
            position=1,
            date_added=datetime(2024, 1, 1, tzinfo=UTC),
            provider="qobuz_1",
            last_played=10,
        ),
        _track(
            "t3",
            "Banana",
            artist="Mike",
            album="Alpha",
            duration=200,
            disc_number=2,
            track_number=1,
            position=2,
            date_added=None,
            provider="deezer_1",
            last_played=0,
        ),
        _track(
            "t4",
            "banana live",
            artist="Anna",
            album="Alpha",
            duration=200,
            disc_number=1,
            track_number=2,
            position=4,
            date_added=datetime(2024, 1, 2, tzinfo=UTC),
            provider="qobuz_1",
            last_played=20,
        ),
    ]


def _ids(items: list[Track]) -> list[str]:
    return [item.item_id for item in items]


@pytest.mark.parametrize(
    ("listing", "field", "direction", "expected"),
    [
        # names sort on their normalized form, so case and accents do not count
        (ListingType.PLAYLIST_TRACKS, SortField.NAME, SortDirection.ASC, ["t3", "t4", "t1", "t2"]),
        (ListingType.PLAYLIST_TRACKS, SortField.NAME, SortDirection.DESC, ["t2", "t1", "t4", "t3"]),
        # the sort name moves the article to the back
        (ListingType.ARTIST_TRACKS, SortField.SORT_NAME, SortDirection.ASC, ["t2", "t3", "t4", "t1"]),
        # a tie keeps the listing order, in both directions
        (ListingType.ALBUM_TRACKS, SortField.DURATION, SortDirection.ASC, ["t2", "t3", "t4", "t1"]),
        (ListingType.ALBUM_TRACKS, SortField.DURATION, SortDirection.DESC, ["t1", "t3", "t4", "t2"]),
        # the disc comes before the track number
        (ListingType.ALBUM_TRACKS, SortField.TRACK_NUMBER, SortDirection.ASC, ["t2", "t4", "t1", "t3"]),
        (ListingType.PLAYLIST_TRACKS, SortField.POSITION, SortDirection.ASC, ["t2", "t3", "t1", "t4"]),
        (ListingType.PLAYLIST_TRACKS, SortField.POSITION, SortDirection.DESC, ["t4", "t1", "t3", "t2"]),
        # an item without a date is the oldest
        (ListingType.PLAYLIST_TRACKS, SortField.TIMESTAMP_ADDED, SortDirection.ASC, ["t3", "t2", "t4", "t1"]),
        (ListingType.PLAYLIST_TRACKS, SortField.TIMESTAMP_ADDED, SortDirection.DESC, ["t1", "t4", "t2", "t3"]),
        (ListingType.PLAYLIST_TRACKS, SortField.ARTIST_NAME, SortDirection.ASC, ["t2", "t4", "t3", "t1"]),
        (ListingType.PLAYLIST_TRACKS, SortField.ALBUM_NAME, SortDirection.ASC, ["t3", "t4", "t1", "t2"]),
        (ListingType.VERSIONS, SortField.PROVIDER, SortDirection.ASC, ["t3", "t2", "t4", "t1"]),
        (ListingType.LIBRARY_TRACKS, SortField.LAST_PLAYED, SortDirection.ASC, ["t3", "t2", "t4", "t1"]),
        # the original order has no direction
        (ListingType.VERSIONS, SortField.ORIGINAL, SortDirection.DESC, ["t1", "t2", "t3", "t4"]),
    ],
)  # fmt: skip
def test_apply_listing_sorts_on_every_field(
    tracks: list[Track],
    listing: ListingType,
    field: SortField,
    direction: SortDirection,
    expected: list[str],
) -> None:
    """Every sort field orders the listing on the item attribute it stands for, stably."""
    result = apply_listing(tracks, listing, sort_field=field, sort_direction=direction)
    assert _ids(result) == expected


def test_apply_listing_sorts_albums_by_year() -> None:
    """Albums sort on their year, an unknown year counting as the oldest."""
    albums = [
        create_album("qobuz_1", "a1"),
        create_album("qobuz_1", "a2"),
        create_album("qobuz_1", "a3"),
    ]
    albums[0].year, albums[1].year, albums[2].year = 2001, None, 1999
    listing = ListingType.ARTIST_ALBUMS
    assert [a.item_id for a in apply_listing(albums, listing, sort_field=SortField.YEAR)] == [
        "a1",
        "a3",
        "a2",
    ]
    ascending = apply_listing(
        albums, listing, sort_field=SortField.YEAR, sort_direction=SortDirection.ASC
    )
    assert [a.item_id for a in ascending] == ["a2", "a3", "a1"]


def test_apply_listing_uses_the_listing_default(tracks: list[Track]) -> None:
    """Without a sort the listing's first option applies, with its own default direction."""
    assert _ids(apply_listing(tracks, ListingType.ALBUM_TRACKS)) == ["t2", "t4", "t1", "t3"]
    # podcast episodes list the newest first, playlist tracks the first first
    assert _ids(apply_listing(tracks, ListingType.PODCAST_EPISODES)) == ["t4", "t1", "t3", "t2"]
    assert _ids(apply_listing(tracks, ListingType.PLAYLIST_TRACKS)) == ["t2", "t3", "t1", "t4"]
    # a direction without a field applies to the default field
    reverse = apply_listing(tracks, ListingType.ALBUM_TRACKS, sort_direction=SortDirection.DESC)
    assert _ids(reverse) == ["t3", "t1", "t4", "t2"]


def test_apply_listing_shuffles_for_random(tracks: list[Track]) -> None:
    """A random sort returns every item, in some order."""
    result = apply_listing(tracks, ListingType.LIBRARY_TRACKS, sort_field=SortField.RANDOM)
    assert sorted(_ids(result)) == ["t1", "t2", "t3", "t4"]


@pytest.mark.parametrize(
    ("search", "expected"),
    [
        ("eclair", ["t1"]),
        ("ÉCLAIR", ["t1"]),
        ("banana live", ["t4"]),
        ("ban", ["t3", "t4"]),
        # the album and the artist names count too
        ("alpha", ["t3", "t4"]),
        ("anna", ["t2", "t4"]),
        ("nothing", []),
    ],
)
def test_apply_listing_searches_names_without_case_and_accents(
    tracks: list[Track], search: str, expected: list[str]
) -> None:
    """The search matches a normalized substring of the name, album name or artist names."""
    result = apply_listing(
        tracks, ListingType.PLAYLIST_TRACKS, search=search, sort_field=SortField.POSITION
    )
    assert _ids(result) == expected


def test_apply_listing_pages(tracks: list[Track]) -> None:
    """Without a limit, or with 0, everything is returned; an offset past the end nothing."""
    listing = ListingType.PLAYLIST_TRACKS
    assert len(apply_listing(tracks, listing)) == 4
    assert _ids(apply_listing(tracks, listing, sort_field=SortField.POSITION, limit=None)) == [
        "t2",
        "t3",
        "t1",
        "t4",
    ]
    assert _ids(apply_listing(tracks, listing, sort_field=SortField.POSITION, limit=0)) == [
        "t2",
        "t3",
        "t1",
        "t4",
    ]
    assert _ids(
        apply_listing(tracks, listing, sort_field=SortField.POSITION, limit=2, offset=1)
    ) == ["t3", "t1"]
    assert _ids(
        apply_listing(tracks, listing, sort_field=SortField.POSITION, limit=2, offset=3)
    ) == ["t4"]
    assert apply_listing(tracks, listing, sort_field=SortField.POSITION, offset=10) == []


def test_apply_listing_rejects_a_field_the_listing_lacks(tracks: list[Track]) -> None:
    """A sort field the listing does not offer, or one only the database holds, is rejected."""
    with pytest.raises(InvalidDataError):
        apply_listing(tracks, ListingType.ALBUM_TRACKS, sort_field=SortField.YEAR)
    with pytest.raises(InvalidDataError):
        apply_listing(tracks, ListingType.LIBRARY_TRACKS, sort_field=SortField.PLAY_COUNT)


def test_resolve_listing_sort_defaults_and_precedence() -> None:
    """Nothing requested means the listing default; the typed field wins over the legacy key."""
    assert resolve_listing_sort(ListingType.ALBUM_TRACKS) == (
        SortField.TRACK_NUMBER,
        SortDirection.ASC,
    )
    assert resolve_listing_sort(ListingType.PODCAST_EPISODES) == (
        SortField.POSITION,
        SortDirection.DESC,
    )
    assert resolve_listing_sort(ListingType.PODCAST_EPISODES, sort_direction=SortDirection.ASC) == (
        SortField.POSITION,
        SortDirection.ASC,
    )
    assert resolve_listing_sort(ListingType.PLAYLIST_TRACKS, SortField.NAME) == (
        SortField.NAME,
        SortDirection.ASC,
    )
    assert resolve_listing_sort(ListingType.PLAYLIST_TRACKS, order_by="duration_desc") == (
        SortField.DURATION,
        SortDirection.DESC,
    )
    assert resolve_listing_sort(
        ListingType.PLAYLIST_TRACKS, SortField.NAME, None, "duration_desc"
    ) == (
        SortField.NAME,
        SortDirection.ASC,
    )
    # a field without a direction ignores the one given
    assert resolve_listing_sort(ListingType.VERSIONS, SortField.ORIGINAL, SortDirection.DESC) == (
        SortField.ORIGINAL,
        None,
    )


def test_resolve_listing_sort_rejects_what_the_listing_lacks() -> None:
    """A field the listing does not offer and an unknown legacy key are rejected."""
    with pytest.raises(InvalidDataError):
        resolve_listing_sort(ListingType.ALBUM_TRACKS, SortField.YEAR)
    with pytest.raises(InvalidDataError):
        resolve_listing_sort(ListingType.ALBUM_TRACKS, order_by="not_a_sort_key")


def test_every_listing_offers_sort_fields_its_items_carry() -> None:
    """Every listing has a row, and a listing sorted in memory only offers what its items carry."""
    for listing in ListingType:
        options = LISTING_SORT_OPTIONS[listing]
        assert options, f"{listing} offers no sort options"
        item_attributes = {field.name for field in fields(LISTING_ITEM_TYPES[listing])}
        for option in options:
            assert isinstance(option, SortOptionInfo)
            assert option.supports_direction == (option.default_direction is not None)
            if listing in SQL_LISTINGS or option.field in (SortField.ORIGINAL, SortField.RANDOM):
                continue
            assert option.field in _SORT_KEYS, f"{option.field} has no in-memory sort key"
            attribute = SORT_FIELD_ATTRIBUTES[option.field]
            assert attribute in item_attributes, f"{listing} items carry no {attribute}"
