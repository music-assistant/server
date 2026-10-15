"""Tests for the artist listings: searched, sorted and paged on the server."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import (
    AlbumType,
    ArtistType,
    MediaType,
    ProviderFeature,
    ProviderSharing,
    SortDirection,
    SortField,
)
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    MediaCollection,
    MediaItemCollection,
    MediaItemMetadata,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.controllers.music.constants import SIMILAR_ITEMS_LIMIT
from music_assistant.controllers.music.listing_cache import Listing
from tests.common import set_music_source_access

from .helpers import create_album, create_track

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

# the names of the three items each listing holds, in the order the source lists them
NAMES = ("Zebra", "Apple", "Mango")


@pytest.fixture(name="mass")
def mass_fixture(music_mass_with_cache: MusicAssistant) -> MusicAssistant:
    """Run on a library-only instance with a cache store: provider listings are cached."""
    return music_mass_with_cache


def _mapping(item_id: str) -> ProviderMapping:
    """Return a mapping on the test provider that is in the provider's library."""
    return ProviderMapping(
        item_id=item_id, provider_domain="qobuz", provider_instance="qobuz_1", in_library=True
    )


def _artist(item_id: str = "artist1", name: str = "Test Artist") -> Artist:
    """Return an artist as the test provider lists it."""
    return Artist(
        item_id=item_id, provider="qobuz_1", name=name, provider_mappings={_mapping(item_id)}
    )


def _track(item_id: str, name: str, duration: int = 200, album: str = "Album") -> Track:
    """Return a track of the test artist on an album of the given name."""
    track = create_track("qobuz_1", item_id, name=name, isrc=f"ISRC{item_id}", duration=duration)
    track.provider_mappings = {_mapping(item_id)}
    track.artists = UniqueList([_artist()])
    track.album = create_album("qobuz_1", f"{item_id}_album", name=album, artist_item_id="artist1")
    return track


def _album(item_id: str, name: str, year: int | None = None) -> Album:
    """Return an album of the test artist released in the given year."""
    album = create_album("qobuz_1", item_id, name=name, artist_item_id="artist1")
    album.provider_mappings = {_mapping(item_id)}
    album.year = year
    return album


def _items(kind: str) -> list[Any]:
    """Return three items of the given kind, named after NAMES, in that order."""
    if kind == "track":
        return [_track(f"t{index}", name) for index, name in enumerate(NAMES)]
    if kind == "album":
        return [_album(f"a{index}", name) for index, name in enumerate(NAMES)]
    return [_artist(f"s{index}", name) for index, name in enumerate(NAMES)]


async def _library_artist_id(mass: MusicAssistant) -> str:
    """Return the library id of the test artist, once its items are in the library."""
    artist = await mass.music.artists.get_library_item_by_prov_id("artist1", "qobuz_1")
    assert artist is not None
    return str(artist.item_id)


def _names(items: list[Any]) -> list[str]:
    return [item.name for item in items]


async def test_library_artist_tracks_follow_the_listing_contract(mass: MusicAssistant) -> None:
    """The library tracks of an artist are searched, sorted and paged, by sort name by default."""
    for track in (
        _track("t1", "Zebra", 300, "Alpha"),
        _track("t2", "Apple", 100, "Omega"),
        _track("t3", "Mango", 200, "Delta"),
    ):
        await mass.music.tracks.add_item_to_library(track)
    artist_id = await _library_artist_id(mass)

    async def _tracks(**kwargs: Any) -> list[str]:
        return _names(await mass.music.artists.tracks(artist_id, "library", **kwargs))

    assert await _tracks() == ["Apple", "Mango", "Zebra"]
    assert await _tracks(sort_field=SortField.DURATION, sort_direction=SortDirection.DESC) == [
        "Zebra",
        "Mango",
        "Apple",
    ]
    assert await _tracks(sort_field=SortField.ALBUM_NAME) == ["Zebra", "Mango", "Apple"]
    assert await _tracks(search="omega") == ["Apple"]
    assert await _tracks(sort_field=SortField.NAME, limit=1, offset=1) == ["Mango"]
    assert await _tracks(limit=0) == ["Apple", "Mango", "Zebra"]
    with pytest.raises(InvalidDataError):
        await _tracks(sort_field=SortField.YEAR)


async def test_library_artist_tracks_can_filter_to_the_current_users_favorites(
    mass: MusicAssistant,
) -> None:
    """The favorite-only filter only returns tracks liked by the current user."""
    for track in (_track("t1", "Zebra"), _track("t2", "Apple")):
        await mass.music.tracks.add_item_to_library(track)
    artist_id = await _library_artist_id(mass)
    tracks = await mass.music.artists.tracks(artist_id, "library")
    liked_track = tracks[0]
    favorite_user = User(user_id="user-a", username="user-a", role=UserRole.USER)
    await mass.music.favorites.set(
        MediaType.TRACK, int(liked_track.item_id), True, [favorite_user.user_id]
    )

    with patch(
        "music_assistant.controllers.music.media.base.get_current_user",
        return_value=favorite_user,
    ):
        favorites = await mass.music.artists.tracks(artist_id, "library", favorite=True)

    assert [track.item_id for track in favorites] == [liked_track.item_id]


async def test_library_artist_albums_follow_the_listing_contract(mass: MusicAssistant) -> None:
    """The library albums of an artist are searched, sorted and paged, by sort name by default."""
    for album in (
        _album("a1", "Zebra", 2005),
        _album("a2", "Apple", 2010),
        _album("a3", "Mango", 1999),
    ):
        await mass.music.albums.add_item_to_library(album)
    artist_id = await _library_artist_id(mass)

    async def _albums(**kwargs: Any) -> list[str]:
        return _names(await mass.music.artists.albums(artist_id, "library", **kwargs))

    assert await _albums() == ["Apple", "Mango", "Zebra"]
    # the newest first
    assert await _albums(sort_field=SortField.YEAR) == ["Apple", "Zebra", "Mango"]
    assert await _albums(sort_field=SortField.YEAR, sort_direction=SortDirection.ASC) == [
        "Mango",
        "Zebra",
        "Apple",
    ]
    assert await _albums(search="mang") == ["Mango"]
    assert await _albums(limit=2, offset=2) == ["Zebra"]
    with pytest.raises(InvalidDataError):
        await _albums(sort_field=SortField.DURATION)


async def test_library_artist_albums_filter_types_before_paging(mass: MusicAssistant) -> None:
    """Album type filters are applied before the requested page is sliced."""
    album = _album("a1", "Album")
    album.album_type = AlbumType.ALBUM
    single = _album("a2", "Single")
    single.album_type = AlbumType.SINGLE
    ep = _album("a3", "EP")
    ep.album_type = AlbumType.EP
    for item in (album, single, ep):
        await mass.music.albums.add_item_to_library(item)
    artist_id = await _library_artist_id(mass)

    page = await mass.music.artists.albums(
        artist_id,
        "library",
        album_types=[AlbumType.SINGLE, AlbumType.EP],
        sort_field=SortField.NAME,
        limit=1,
        offset=1,
    )

    assert [item.name for item in page] == ["Single"]


async def test_library_artist_albums_filter_favorites_before_paging(
    mass: MusicAssistant,
) -> None:
    """Artist album favorites are scoped to the current user and applied before paging."""
    albums = [
        await mass.music.albums.add_item_to_library(_album("a1", "Zebra")),
        await mass.music.albums.add_item_to_library(_album("a2", "Apple")),
    ]
    artist_id = await _library_artist_id(mass)
    favorite_user = User(user_id="user-a", username="user-a", role=UserRole.USER)
    await mass.music.favorites.set(
        MediaType.ALBUM, int(albums[0].item_id), True, [favorite_user.user_id]
    )

    with patch(
        "music_assistant.controllers.music.media.base.get_current_user",
        return_value=favorite_user,
    ):
        page = await mass.music.artists.albums(
            artist_id,
            "library",
            favorite=True,
            sort_field=SortField.NAME,
            limit=1,
            offset=0,
        )

    assert [item.name for item in page] == ["Zebra"]


async def test_appears_on_follows_the_listing_contract(mass: MusicAssistant) -> None:
    """The albums an artist appears on stay newest first by default, and sort, search and page."""
    for index, (name, year) in enumerate((("Early", 1990), ("Late", 2020), ("Middle", 2005))):
        album = create_album("qobuz_1", f"album{index}", name, "Host Artist", "host")
        album.year = year
        track = _track(f"t{index}", f"Song {index}")
        track.album = album
        await mass.music.tracks.add_item_to_library(track)
    artist_id = await _library_artist_id(mass)

    async def _albums(**kwargs: Any) -> list[str]:
        return _names(await mass.music.artists.appears_on(artist_id, "library", **kwargs))

    assert await _albums() == ["Late", "Middle", "Early"]
    assert await _albums(sort_field=SortField.NAME) == ["Early", "Late", "Middle"]
    assert await _albums(search="mid") == ["Middle"]
    assert await _albums(limit=1, offset=1) == ["Middle"]
    with pytest.raises(InvalidDataError):
        await _albums(sort_field=SortField.DURATION)


@pytest.mark.parametrize(
    ("method", "fetch", "kind", "default"),
    [
        # sorted by sort name unless asked otherwise
        ("tracks", "get_provider_artist_tracks", "track", ["Apple", "Mango", "Zebra"]),
        ("albums", "get_provider_artist_albums", "album", ["Apple", "Mango", "Zebra"]),
        # a ranking keeps the provider's order
        ("top_tracks", "get_provider_artist_toptracks", "track", list(NAMES)),
        ("top_albums", "get_provider_artist_topalbums", "album", list(NAMES)),
        ("similar_artists", "get_provider_artist_similar_artists", "artist", list(NAMES)),
    ],
)
async def test_provider_artist_listings_are_assembled_once(
    mass: MusicAssistant, method: str, fetch: str, kind: str, default: list[str]
) -> None:
    """A provider artist's listing is fetched once, from the instance its provider resolves to."""
    items = _items(kind)
    # the provider tracks listing reports whether it is complete itself
    provider_fetch = AsyncMock(
        return_value=Listing(items) if fetch == "get_provider_artist_tracks" else items
    )

    async def _listing(provider: str = "qobuz_1", **kwargs: Any) -> list[str]:
        return _names(await getattr(mass.music.artists, method)("artist_x", provider, **kwargs))

    with (
        patch.object(mass, "get_provider", return_value=SimpleNamespace(instance_id="qobuz_1")),
        patch.object(mass.music.artists, fetch, provider_fetch),
    ):
        assert await _listing() == default
        # the domain and the instance it resolves to share one listing
        assert await _listing("qobuz") == default
        assert await _listing(sort_field=SortField.NAME) == ["Apple", "Mango", "Zebra"]
        assert await _listing(search="ango") == ["Mango"]
        assert await _listing(sort_field=SortField.NAME, limit=1, offset=1) == ["Mango"]
        assert await _listing(limit=0) == default
    provider_fetch.assert_awaited_once()
    assert provider_fetch.await_args is not None
    assert provider_fetch.await_args.args == ("artist_x", "qobuz_1")


@pytest.mark.parametrize(
    ("method", "fetch", "kind"),
    [
        ("top_tracks", "get_library_artist_toptracks", "track"),
        ("top_albums", "get_library_artist_topalbums", "album"),
        ("similar_artists", "get_library_artist_similar_artists", "artist"),
    ],
)
async def test_ranked_artist_listings_keep_the_ranking_by_default(
    mass: MusicAssistant, method: str, fetch: str, kind: str
) -> None:
    """A library artist's ranking is listed as the providers rank it unless asked otherwise."""
    library_fetch = AsyncMock(return_value=Listing(_items(kind)))
    with patch.object(mass.music.artists, fetch, library_fetch):
        listing = await getattr(mass.music.artists, method)("1", "library")
        assert _names(listing) == list(NAMES)
        listing = await getattr(mass.music.artists, method)(
            "1", "library", sort_field=SortField.NAME, sort_direction=SortDirection.DESC
        )
        assert _names(listing) == ["Zebra", "Mango", "Apple"]
    # the aggregation over the artist's providers is cached too
    library_fetch.assert_awaited_once()


@pytest.mark.parametrize(
    ("method", "fetch", "kind"),
    [
        ("top_tracks", "get_library_artist_toptracks", "track"),
        ("top_albums", "get_library_artist_topalbums", "album"),
        ("similar_artists", "get_library_artist_similar_artists", "artist"),
    ],
)
async def test_library_artist_ranking_is_kept_per_provider_filter(
    mass: MusicAssistant, method: str, fetch: str, kind: str
) -> None:
    """A library artist's ranking limited to one provider is a listing of its own."""
    library_fetch = AsyncMock(return_value=Listing(_items(kind)))
    with patch.object(mass.music.artists, fetch, library_fetch):
        for _ in range(2):
            await getattr(mass.music.artists, method)("1", "library")
            await getattr(mass.music.artists, method)("1", "library", provider_filter="qobuz_1")
    assert [call.args for call in library_fetch.await_args_list] == [
        ("1", None),
        ("1", "qobuz_1"),
    ]


async def test_provider_artist_tracks_follow_the_calling_users_music_sources(
    mass: MusicAssistant,
) -> None:
    """The tracks a library album adds to a provider artist are those the calling user may see."""
    artist = await mass.music.artists.add_item_to_library(_artist())
    album = await mass.music.albums.add_item_to_library(_album("album1", "Album"))
    for number, (name, source) in enumerate((("Shared", "qobuz_1"), ("Private", "tidal_1")), 1):
        track = _track(f"t{number}", name)
        track.provider_mappings = {
            ProviderMapping(
                item_id=f"t{number}",
                provider_domain=source.split("_", maxsplit=1)[0],
                provider_instance=source,
                in_library=True,
            )
        }
        track.artists = UniqueList([artist])
        track.album = album
        track.track_number = number
        await mass.music.tracks.add_item_to_library(track)
    # the second source is the private account of the first user
    set_music_source_access(
        mass,
        {
            "qobuz_1": None,
            "tidal_1": ProviderAccess(owner="owner", sharing=ProviderSharing.PRIVATE),
        },
    )
    # the provider lists no artist tracks of its own, so the artist's albums are listed
    provider = SimpleNamespace(
        instance_id="qobuz_1", available=True, supports_feature=lambda _feature: False
    )

    async def _tracks_as(user_id: str) -> list[str]:
        user = User(user_id=user_id, username=user_id, role=UserRole.USER)
        with patch(
            "music_assistant.controllers.music.media.base.get_current_user", return_value=user
        ):
            return _names(await mass.music.artists.tracks("artist1", "qobuz_1"))

    with (
        patch.object(mass, "get_provider", return_value=provider),
        patch.object(
            mass.music.artists,
            "get_provider_artist_albums",
            AsyncMock(return_value=[_album("album1", "Album")]),
        ),
        patch.object(mass.music.albums, "_get_provider_album_tracks", AsyncMock(return_value=[])),
    ):
        assert await _tracks_as("owner") == ["Private", "Shared"]
        assert await _tracks_as("guest") == ["Shared"]


async def test_library_artist_listing_missing_a_provider_is_assembled_again(
    mass: MusicAssistant,
) -> None:
    """An aggregation one of the artist's providers failed to contribute to is not kept."""
    fetch = AsyncMock(return_value=Listing([_track("t1", "One")], complete=False))
    with patch.object(mass.music.artists, "get_library_artist_toptracks", fetch):
        for _ in range(2):
            assert _names(await mass.music.artists.top_tracks("1", "library")) == ["One"]
    assert fetch.await_count == 2


@pytest.mark.parametrize(
    ("method", "fetch", "kind", "feature"),
    [
        ("top_tracks", "get_provider_artist_toptracks", "track", ProviderFeature.ARTIST_TOPTRACKS),
        ("top_albums", "get_provider_artist_topalbums", "album", ProviderFeature.ARTIST_TOPALBUMS),
        (
            "similar_artists",
            "get_provider_artist_similar_artists",
            "artist",
            ProviderFeature.SIMILAR_ARTISTS,
        ),
    ],
)
async def test_library_artist_ranking_missing_a_source_that_is_not_loaded_is_assembled_again(
    mass: MusicAssistant, method: str, fetch: str, kind: str, feature: ProviderFeature
) -> None:
    """A library artist's ranking that misses a source that is not loaded is not kept."""
    artist = _artist()
    artist.provider_mappings.add(
        ProviderMapping(
            item_id="artist1", provider_domain="tidal", provider_instance="tidal_1", in_library=True
        )
    )
    library_artist = await mass.music.artists.add_item_to_library(artist)
    # the first source of the artist is loaded, the second is not
    provider = SimpleNamespace(
        instance_id="qobuz_1",
        domain="qobuz",
        is_streaming_provider=True,
        supported_features={feature},
    )
    provider_fetch = AsyncMock(return_value=_items(kind))
    with (
        patch.object(
            mass,
            "get_provider",
            side_effect=lambda instance, *_args, **_kwargs: (
                provider if instance == "qobuz_1" else None
            ),
        ),
        patch.object(mass.music.artists, fetch, provider_fetch),
    ):
        for _ in range(2):
            listing = await getattr(mass.music.artists, method)(
                str(library_artist.item_id), "library"
            )
            assert _names(listing) == list(NAMES)
    assert provider_fetch.await_count == 2


async def test_provider_artist_tracks_missing_an_album_are_assembled_again(
    mass: MusicAssistant,
) -> None:
    """Provider artist tracks one album failed to contribute to are not kept."""
    fetch = AsyncMock(return_value=Listing([_track("t1", "One")], complete=False))
    with (
        patch.object(mass, "get_provider", return_value=SimpleNamespace(instance_id="qobuz_1")),
        patch.object(mass.music.artists, "get_provider_artist_tracks", fetch),
    ):
        for _ in range(2):
            assert _names(await mass.music.artists.tracks("artist_x", "qobuz_1")) == ["One"]
    assert fetch.await_count == 2


async def test_similar_artists_page_the_listing_the_providers_are_asked_for(
    mass: MusicAssistant,
) -> None:
    """The limit pages the similar artists; the providers are always asked for the full listing."""
    fetch = AsyncMock(return_value=_items("artist"))
    with (
        patch.object(mass, "get_provider", return_value=SimpleNamespace(instance_id="qobuz_1")),
        patch.object(mass.music.artists, "get_provider_artist_similar_artists", fetch),
    ):
        first = await mass.music.artists.similar_artists("artist_x", "qobuz_1", limit=2)
        rest = await mass.music.artists.similar_artists("artist_x", "qobuz_1", limit=2, offset=2)
    assert _names(first + rest) == list(NAMES)
    fetch.assert_awaited_once_with("artist_x", "qobuz_1", limit=SIMILAR_ITEMS_LIMIT)


@pytest.mark.parametrize(
    ("helper", "controller", "artist_type"),
    [
        ("get_library_artist_tracks", "tracks", ArtistType.SINGER),
        ("get_library_artist_albums", "albums", ArtistType.SINGER),
        ("get_library_author_narrator_audiobooks", "audiobooks", ArtistType.AUTHOR),
    ],
)
async def test_library_artist_listings_are_the_whole_set(
    mass: MusicAssistant, helper: str, controller: str, artist_type: ArtistType
) -> None:
    """The library part of an artist listing is read without the listing page size."""
    artist = Artist(
        item_id="1",
        provider="library",
        name="Test Artist",
        artist_type=artist_type,
        provider_mappings=set(),
    )
    query = AsyncMock(return_value=[])
    args: tuple[Any, ...] = ("1", artist_type) if controller == "audiobooks" else ("1",)
    with (
        patch.object(mass.music.artists, "get_library_item", AsyncMock(return_value=artist)),
        patch.object(getattr(mass.music, controller), "get_library_items_by_query", query),
    ):
        await getattr(mass.music.artists, helper)(*args)
    assert query.await_args is not None
    assert query.await_args.kwargs["limit"] == 0


async def _seed_author_audiobooks(mass: MusicAssistant) -> str:
    """Seed a library author of two books in a series and one on its own, return its id."""
    author = await mass.music.artists.add_item_to_library(
        Artist(
            item_id="author1",
            provider="qobuz_1",
            name="Test Author",
            artist_type=ArtistType.AUTHOR,
            provider_mappings={_mapping("author1")},
        )
    )
    for index, (name, duration, series) in enumerate(
        (("Zebra", 300, None), ("Apple", 100, "Saga"), ("Mango", 200, "Saga"))
    ):
        metadata = MediaItemMetadata()
        if series:
            metadata.collections = UniqueList(
                [MediaItemCollection(title=series, sequence=float(index))]
            )
        await mass.music.audiobooks.add_item_to_library(
            Audiobook(
                item_id=f"book{index}",
                provider="qobuz_1",
                name=name,
                duration=duration,
                authors=UniqueList([author]),
                metadata=metadata,
                provider_mappings={_mapping(f"book{index}")},
            )
        )
    return str(author.item_id)


async def test_artist_audiobooks_follow_the_listing_contract(mass: MusicAssistant) -> None:
    """An author's audiobooks are searched, sorted and paged, by sort name by default."""
    author_id = await _seed_author_audiobooks(mass)

    async def _books(**kwargs: Any) -> list[str]:
        books = await mass.music.artists.audiobooks(
            author_id, "library", in_library_only=True, **kwargs
        )
        return _names(books)

    assert await _books() == ["Apple", "Mango", "Zebra"]
    assert await _books(sort_field=SortField.DURATION, sort_direction=SortDirection.DESC) == [
        "Zebra",
        "Mango",
        "Apple",
    ]
    assert await _books(search="zeb") == ["Zebra"]
    assert await _books(limit=1, offset=2) == ["Zebra"]
    with pytest.raises(InvalidDataError):
        await _books(sort_field=SortField.YEAR)


async def test_collapsed_artist_audiobooks_sort_a_collection_by_its_title(
    mass: MusicAssistant,
) -> None:
    """A collapsed collection sorts and searches by its title, and has no duration of its own."""
    author_id = await _seed_author_audiobooks(mass)

    async def _books(**kwargs: Any) -> list[Audiobook | MediaCollection[Audiobook]]:
        return await mass.music.artists.audiobooks(
            author_id, "library", in_library_only=True, collapse_collections=True, **kwargs
        )

    books = await _books()
    assert _names(books) == ["Saga", "Zebra"]
    assert isinstance(books[0], MediaCollection)
    assert _names(await _books(sort_field=SortField.DURATION)) == ["Saga", "Zebra"]
    assert _names(await _books(search="saga")) == ["Saga"]
