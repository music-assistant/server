"""
Tests for the whole-albums Autoplay mode.

When the queue runs out, Autoplay adds the next album of the last track's artist, in the artist
order. The tests resolve the albums through a real MusicAssistant with a real SQLite database,
reusing the artist order stubs.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from music_assistant_models.enums import AlbumType, ArtistType, MediaType
from music_assistant_models.errors import LoginFailed, MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    Artist,
    ItemMapping,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.constants import VARIOUS_ARTISTS_MBID, VARIOUS_ARTISTS_NAME
from music_assistant.controllers.player_queues import autoplay
from music_assistant.controllers.player_queues.autoplay import Autoplay, albums_mode_available
from music_assistant.controllers.player_queues.constants import ArtistOrder
from tests.controllers.player_queues.test_artist_album_order import (
    _add_album,
    _add_artist,
    _library_mapping,
    _providers_available,
    _set_order,
    _set_selection,
    _streaming_album,
    _stub_provider,
)

if TYPE_CHECKING:
    from music_assistant_models.player_queue import PlayerQueue

    from music_assistant.controllers.player_queues.media_resolver import ResolvedItem
    from music_assistant.mass import MusicAssistant

__all__ = ["_providers_available"]


def _album_names(albums: list[list[Album]] | None) -> list[str] | None:
    """Return the names of the albums' first copies, in order."""
    return None if albums is None else [copies[0].name for copies in albums]


def _provider_artist() -> Artist:
    """Build an artist as a streaming provider lists it, not in the library."""
    item_id = uuid4().hex
    return Artist(
        item_id=item_id,
        provider="streaming",
        name="ABBA",
        provider_mappings={
            ProviderMapping(
                item_id=item_id, provider_domain="streaming", provider_instance="streaming"
            )
        },
    )


async def _artist_with_years(
    mass: MusicAssistant, *years: int | None
) -> tuple[Artist, dict[int | None, Album]]:
    """Add a library artist with one album per year, named after it. Returns the albums too."""
    artist = await _add_artist(mass)
    albums = {year: await _add_album(mass, artist, f"Album {year}", year) for year in years}
    return artist, albums


async def test_release_order_plays_the_next_newer_album_then_wraps(mass: MusicAssistant) -> None:
    """By release, the albums after the one that played come first, then the oldest unplayed."""
    artist, albums = await _artist_with_years(mass, 1995, 1998, 2005, 2010, 2015)
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)

    result = await mass.player_queues.artist_album_candidates(
        artist, [albums[2005], albums[2010]], albums[2005]
    )

    assert _album_names(result) == ["Album 2015", "Album 1995", "Album 1998"]


async def test_every_album_played_leaves_no_candidate(mass: MusicAssistant) -> None:
    """An album with a track in the queue counts as played, so all played leaves none."""
    artist, albums = await _artist_with_years(mass, 1995, 1998)
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)

    result = await mass.player_queues.artist_album_candidates(
        artist, list(albums.values()), albums[1998]
    )

    assert result == []


async def test_a_queued_album_matches_its_other_copy(mass: MusicAssistant) -> None:
    """A queued provider copy of a library album counts as that album played."""
    artist, albums = await _artist_with_years(mass, 1995, 1998)
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)
    copy = replace(
        albums[1995],
        item_id="copy",
        provider="streaming",
        provider_mappings={
            ProviderMapping(
                item_id="copy", provider_domain="streaming", provider_instance="streaming"
            )
        },
    )

    result = await mass.player_queues.artist_album_candidates(artist, [copy], None)

    assert _album_names(result) == ["Album 1998"]


@pytest.mark.parametrize(
    ("namesake", "expected"),
    [
        ("same artist", ["Album 2005"]),
        ("other artist", ["Album 1995", "Album 2005"]),
        ("fails to load", ["Album 1995", "Album 2005"]),
    ],
)
async def test_a_queued_mapping_of_an_unlinked_copy_is_compared_in_full(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch, namesake: str, expected: list[str]
) -> None:
    """A queued mapping named like an album is loaded, so an unlinked copy of it counts as played."""
    artist, albums = await _artist_with_years(mass, 1995, 1998, 2005)
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)
    copy = replace(
        albums[1995],
        item_id="copy",
        provider="other",
        provider_mappings={
            ProviderMapping(item_id="copy", provider_domain="other", provider_instance="other")
        },
    )
    if namesake == "other artist":
        copy = replace(copy, artists=UniqueList([_provider_artist()]))
        copy.artists[0].name = "Not ABBA"
    lookup = _stub_album_lookup(
        mass, monkeypatch, [copy], MediaNotFoundError if namesake == "fails to load" else None
    )
    played: list[Album | ItemMapping] = [
        ItemMapping.from_item(copy),
        # matches its album by ID, so it needs no loading
        ItemMapping.from_item(albums[1998]),
        # named like none of the artist's albums
        ItemMapping(media_type=MediaType.ALBUM, item_id="x", provider="other", name="Elsewhere"),
    ]

    first = await mass.player_queues.artist_album_candidates(artist, played, None)
    again = await mass.player_queues.artist_album_candidates(artist, played, None)

    assert _album_names(first) == _album_names(again) == expected
    # only the namesake loads, and once it has, a similar artist's call reuses it
    loaded = [call.args[0] for call in lookup.await_args_list]
    assert set(loaded) == {"copy"}
    assert namesake == "fails to load" or len(loaded) == 1


@pytest.mark.parametrize("anchor_kind", ["single", "compilation", "other artist"])
async def test_an_anchor_outside_the_list_is_placed_by_its_year(
    mass: MusicAssistant, anchor_kind: str
) -> None:
    """A single, a compilation or another artist's album counts as before its year's albums."""
    artist, _ = await _artist_with_years(mass, 1995, 1998, 2005)
    if anchor_kind == "single":
        anchor = await _add_album(mass, artist, "Zebra", 1998, album_type=AlbumType.SINGLE)
    else:
        name = VARIOUS_ARTISTS_NAME if anchor_kind == "compilation" else "Other"
        other = await mass.music.artists.add_item_to_library(
            Artist(item_id="0", provider="library", name=name, provider_mappings=_library_mapping())
        )
        anchor = await _add_album(mass, other, "Zebra", 1998)
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)

    result = await mass.player_queues.artist_album_candidates(artist, [anchor], anchor)

    assert _album_names(result) == ["Album 1998", "Album 2005", "Album 1995"]


@pytest.mark.parametrize("anchor_year", ["no anchor", "no year"])
async def test_without_an_anchor_release_order_starts_from_the_oldest(
    mass: MusicAssistant, anchor_year: str
) -> None:
    """With no anchor, or an anchor without a year, the oldest album comes first."""
    artist, _ = await _artist_with_years(mass, 2005, None, 1995)
    anchor = None
    if anchor_year == "no year":
        anchor = await _add_album(mass, artist, "Zebra", None, album_type=AlbumType.SINGLE)
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)

    result = await mass.player_queues.artist_album_candidates(artist, [], anchor)

    assert _album_names(result) == ["Album 1995", "Album 2005", "Album None"]


async def test_random_order_picks_only_unplayed_albums(mass: MusicAssistant) -> None:
    """Random albums leave out the played ones."""
    artist, albums = await _artist_with_years(mass, 1995, 1998, 2005)
    _set_order(mass, ArtistOrder.RANDOM_ALBUMS)

    result = await mass.player_queues.artist_album_candidates(artist, [albums[1998]], None)

    assert sorted(_album_names(result) or []) == ["Album 1995", "Album 2005"]


async def test_an_artist_with_only_singles_has_no_candidate(mass: MusicAssistant) -> None:
    """Singles are left out, so an artist with only singles has no album to play."""
    artist = await _add_artist(mass)
    await _add_album(mass, artist, "Waterloo", 1974, album_type=AlbumType.SINGLE)
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)

    assert await mass.player_queues.artist_album_candidates(artist, [], None) == []


async def test_an_artist_outside_the_library_gets_all_provider_albums(
    mass: MusicAssistant,
) -> None:
    """An artist outside the library plays all its provider albums under a library selection."""
    _stub_provider(mass, [_streaming_album("Arrival", 1976), _streaming_album("Waterloo", 1974)])
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)
    _set_selection(mass, "library_tracks")

    result = await mass.player_queues.artist_album_candidates(_provider_artist(), [], None)

    assert _album_names(result) == ["Waterloo", "Arrival"]


async def test_albums_that_cannot_be_listed_give_none(mass: MusicAssistant) -> None:
    """When no provider can list the albums, there are none to play."""
    resolver = mass.player_queues._media_resolver
    resolver._provider_artist_albums = AsyncMock(return_value=None)  # type: ignore[method-assign]
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)

    assert await mass.player_queues.artist_album_candidates(_provider_artist(), [], None) is None


@pytest.mark.parametrize(
    ("order", "selection", "available"),
    [
        (ArtistOrder.ALBUMS_BY_RELEASE, "all_tracks", True),
        (ArtistOrder.RANDOM_ALBUMS, "library_tracks", True),
        (ArtistOrder.SHUFFLED_TRACKS, "all_tracks", False),
        (ArtistOrder.ALBUMS_BY_RELEASE, "top_tracks", False),
    ],
)
def test_the_albums_mode_needs_an_album_order_and_no_top_tracks(
    mass: MusicAssistant, order: ArtistOrder, selection: str, available: bool
) -> None:
    """Whole albums can play only when the artist order plays albums, and not top tracks."""
    _set_order(mass, order)
    _set_selection(mass, selection)

    assert albums_mode_available(mass) is available


def _queue(mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch, *tracks: Track) -> PlayerQueue:
    """Build a queue holding the given tracks, wired into the queue controller."""
    queue_data = SimpleNamespace(items=[SimpleNamespace(media_item=track) for track in tracks])
    queue_data.userid = None
    queue_data.queue = SimpleNamespace(queue_id="q1", display_name="Queue")
    monkeypatch.setattr(mass.player_queues, "queue_data", lambda _queue_id: queue_data)
    return cast("PlayerQueue", queue_data.queue)


def _names(entries: list[ResolvedItem]) -> list[str]:
    """Return the names of Autoplay's album tracks, in order."""
    return [entry.item.name for entry in entries]


def _queued_track(artists: list[Artist | ItemMapping], album: Album | ItemMapping | None) -> Track:
    """Build a queued track by the given artists, on the given album."""
    return Track(
        item_id=uuid4().hex,
        provider="library",
        name="Queued",
        provider_mappings=_library_mapping(),
        artists=UniqueList(artists),
        album=album,
    )


def _stub_album_lookup(
    mass: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
    albums: list[Album],
    error: type[Exception] | None = None,
) -> AsyncMock:
    """Let MA load the given provider albums by ID, or fail with the error. Returns the lookup."""
    by_id = {album.item_id: album for album in albums}
    get = mass.music.albums.get

    async def _get(item_id: str, *args: Any, **kwargs: Any) -> Album:
        if item_id not in by_id:
            return await get(item_id, *args, **kwargs)
        if error is not None:
            raise error("gone")
        return by_id[item_id]

    lookup = AsyncMock(side_effect=_get)
    monkeypatch.setattr(mass.music.albums, "get", lookup)
    return lookup


async def _library_artist(mass: MusicAssistant, name: str, **kwargs: Any) -> Artist:
    """Add a library artist with the given name."""
    return await mass.music.artists.add_item_to_library(
        Artist(
            item_id="0",
            provider="library",
            name=name,
            provider_mappings=_library_mapping(),
            **kwargs,
        )
    )


async def test_the_next_album_follows_the_album_artist(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A "feat." track plays on with its album artist's next album, and loads only that one."""
    artist = await _add_artist(mass)
    waterloo = await _add_album(mass, artist, "Waterloo", 1974)
    await _add_album(mass, artist, "Voulez-Vous", 1979)
    guest = await _library_artist(mass, "Guest")
    queue = _queue(mass, monkeypatch, _queued_track([guest], waterloo))
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)
    lookup = AsyncMock(wraps=mass.music.albums.playback_listing)
    monkeypatch.setattr(mass.music.albums, "playback_listing", lookup)

    result = await Autoplay(mass.player_queues).get_album_tracks(queue)

    assert _names(result) == ["Voulez-Vous 1", "Voulez-Vous 2", "Voulez-Vous 3"]
    assert lookup.await_count == 1


@pytest.mark.parametrize(
    "album_kind",
    [
        "various artists",
        "various artists spelled",
        "various artists by mbid",
        "no album",
        "missing album",
        "album times out",
    ],
)
async def test_the_track_artist_seeds_when_the_album_has_no_artist_of_its_own(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch, album_kind: str
) -> None:
    """A compilation, or an album that can't be loaded, plays on with the track's artist."""
    artist = await _add_artist(mass)
    await _add_album(mass, artist, "Waterloo", 1974)
    await _add_album(mass, artist, "Voulez-Vous", 1979)
    album: Album | ItemMapping | None = None
    if album_kind.startswith("various artists"):
        various = await _library_artist(mass, VARIOUS_ARTISTS_NAME)
        album = await _add_album(mass, various, "Hits", 1976, album_type=AlbumType.COMPILATION)
    if album_kind == "various artists spelled":
        # a provider copy keeps the provider's spelling, which the library would normalise
        assert isinstance(album, Album)
        album.artists[0] = ItemMapping(
            media_type=MediaType.ARTIST, item_id="va", provider="streaming", name="various artists"
        )
        monkeypatch.setattr(mass.music.albums, "get", AsyncMock(return_value=album))
    elif album_kind == "various artists by mbid":
        # a provider may name Various Artists its own way; its MusicBrainz ID still tells
        assert isinstance(album, Album)
        album.artists[0] = Artist(
            item_id="va", provider="streaming", name="Diverse", provider_mappings=set()
        )
        album.artists[0].mbid = VARIOUS_ARTISTS_MBID
        monkeypatch.setattr(mass.music.albums, "get", AsyncMock(return_value=album))
    elif album_kind == "missing album":
        album = ItemMapping(
            media_type=MediaType.ALBUM, item_id="999", provider="library", name="Gone"
        )
    elif album_kind == "album times out":
        album = ItemMapping(
            media_type=MediaType.ALBUM, item_id="999", provider="streaming", name="Slow"
        )
        monkeypatch.setattr(mass.music.albums, "get", AsyncMock(side_effect=TimeoutError))
    queue = _queue(mass, monkeypatch, _queued_track([artist], album))
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)

    result = await Autoplay(mass.player_queues).get_album_tracks(queue)

    expected = "Voulez-Vous" if album_kind.startswith("various artists") else "Waterloo"
    assert _names(result) == [f"{expected} {n}" for n in (1, 2, 3)]


@pytest.mark.parametrize("seed", ["no artist", "missing artist", "artist times out", "author"])
async def test_no_album_without_a_music_artist(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch, seed: str
) -> None:
    """
    A track without an artist, with one that can't be loaded, or by an author, gets no album.

    Only the author is a seed, so only its similar artists are asked.
    """
    lookup = AsyncMock(return_value=[])
    monkeypatch.setattr(mass.music.artists, "similar_artists", lookup)
    artists: list[Artist | ItemMapping] = []
    if seed in ("missing artist", "artist times out"):
        artists = [
            ItemMapping(media_type=MediaType.ARTIST, item_id="999", provider="library", name="Gone")
        ]
    if seed == "artist times out":
        monkeypatch.setattr(mass.music.artists, "get", AsyncMock(side_effect=TimeoutError))
    if seed == "author":
        author = await _library_artist(mass, "Author", artist_type=ArtistType.AUTHOR)
        await _add_album(mass, author, "Book", 2000)
        artists = [author]
    queue = _queue(mass, monkeypatch, _queued_track(artists, None))
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)

    assert await Autoplay(mass.player_queues).get_album_tracks(queue) == []
    assert lookup.await_count == (seed == "author")


@pytest.mark.parametrize(
    ("order", "selection"),
    [(ArtistOrder.SHUFFLED_TRACKS, "all_tracks"), (ArtistOrder.ALBUMS_BY_RELEASE, "top_tracks")],
)
async def test_no_album_when_the_mode_cannot_play(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch, order: ArtistOrder, selection: str
) -> None:
    """A saved albums mode adds no album once the artist settings rule it out."""
    artist = await _add_artist(mass)
    await _add_album(mass, artist, "Waterloo", 1974)
    queue = _queue(mass, monkeypatch, _queued_track([artist], None))
    _set_order(mass, order)
    _set_selection(mass, selection)

    assert await Autoplay(mass.player_queues).get_album_tracks(queue) == []


@pytest.mark.parametrize(("failing", "expected"), [(1, "B"), (2, "C"), (3, None)])
async def test_an_album_that_fails_is_skipped_up_to_three_times(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch, failing: int, expected: str | None
) -> None:
    """An album that fails to load, or whose tracks are all disliked, gives way to the next."""
    albums = [_streaming_album(name, 1970 + n) for n, name in enumerate("ABCD")]
    lookup = _stub_provider(mass, albums)
    names = {album.item_id: album.name for album in albums}
    tracks = lookup.side_effect

    def _tracks(item_id: str, *args: Any, **kwargs: Any) -> list[Track]:
        if names[item_id] == "A":
            raise MediaNotFoundError("gone")
        return cast("list[Track]", tracks(item_id, *args, **kwargs))

    lookup.side_effect = _tracks

    async def _without_disliked(_mass: Any, _userid: Any, items: list[Track]) -> list[Track]:
        disliked = "BC"[: failing - 1]
        return [track for track in items if track.name[0] not in disliked]

    monkeypatch.setattr(autoplay, "without_disliked_tracks", _without_disliked)
    queue = _queue(mass, monkeypatch, _queued_track([_provider_artist()], None))
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)

    result = await Autoplay(mass.player_queues).get_album_tracks(queue)

    assert _names(result) == ([f"{expected} 1", f"{expected} 2"] if expected else [])


async def _similar_setup(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch, similar: dict[str, tuple[int, ...]]
) -> tuple[PlayerQueue, Artist, AsyncMock]:
    """
    Queue the only album of a library artist, whose similar artists have albums of those years.

    A similar artist named "Played" has its albums in the queue too. Returns the lookup mock.
    """
    seed = await _add_artist(mass)
    last = _queued_track([seed], await _add_album(mass, seed, "Waterloo", 1974))
    queued = []
    artists = []
    for name, years in similar.items():
        artist = await _library_artist(mass, name)
        artists.append(artist)
        for year in years:
            album = await _add_album(mass, artist, f"{name} {year}", year)
            if name == "Played":
                queued.append(_queued_track([seed], album))
    lookup = AsyncMock(return_value=artists)
    monkeypatch.setattr(mass.music.artists, "similar_artists", lookup)
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)
    return _queue(mass, monkeypatch, *queued, last), seed, lookup


def _album_of(entries: list[ResolvedItem]) -> set[str]:
    """Return the album names the tracks are named after."""
    return {name.rsplit(" ", 1)[0] for name in _names(entries)}


async def test_a_similar_artist_plays_next_when_the_artist_runs_out(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first similar artist with an album left plays its oldest album."""
    queue, seed, lookup = await _similar_setup(
        mass, monkeypatch, {"Played": (1980,), "Empty": (), "Next": (1990, 1985)}
    )

    result = await Autoplay(mass.player_queues).get_album_tracks(queue)

    assert _album_of(result) == {"Next 1985"}
    lookup.assert_awaited_once_with(seed.item_id, "library", limit=10)


async def test_at_most_three_similar_artists_are_tried(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Similar artists without an album left count, so a fourth is not tried."""
    queue, _seed, _lookup = await _similar_setup(
        mass, monkeypatch, {"Played": (1980,), "Empty": (), "Other": (), "Late": (1990,)}
    )

    assert await Autoplay(mass.player_queues).get_album_tracks(queue) == []


async def test_a_failed_similar_artists_lookup_leaves_the_pick_to_auto(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider error while asking for similar artists gives no album, so Auto picks."""
    queue, _seed, lookup = await _similar_setup(mass, monkeypatch, {"Next": (1990,)})
    lookup.side_effect = LoginFailed("expired")

    assert await Autoplay(mass.player_queues).get_album_tracks(queue) == []


async def test_no_similar_artists_lookup_when_the_provider_has_none(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An artist outside the library whose provider lacks similar artists is not looked up."""
    _stub_provider(mass, [])
    lookup = AsyncMock(return_value=[])
    monkeypatch.setattr(mass.music.artists, "similar_artists", lookup)
    queue = _queue(mass, monkeypatch, _queued_track([_provider_artist()], None))
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)

    assert await Autoplay(mass.player_queues).get_album_tracks(queue) == []
    lookup.assert_not_awaited()


async def test_after_an_auto_track_the_albums_of_its_artist_play(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After an Auto batch, the last track's artist plays on from after that track's album."""
    artist = await _add_artist(mass)
    waterloo = await _add_album(mass, artist, "Waterloo", 1974)
    guest = await _library_artist(mass, "Guest")
    await _add_album(mass, guest, "Early", 1985)
    middle = await _add_album(mass, guest, "Middle", 1990)
    await _add_album(mass, guest, "Late", 1995)
    queue = _queue(
        mass, monkeypatch, _queued_track([artist], waterloo), _queued_track([guest], middle)
    )
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)

    result = await Autoplay(mass.player_queues).get_album_tracks(queue)

    assert _names(result) == ["Late 1", "Late 2", "Late 3"]


async def test_auto_picks_the_first_album_not_played_yet(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auto's tracks give the next album: no album, singles and played albums are skipped."""
    played, picked, later = (_streaming_album(name, 1970) for name in "ABC")
    single, mapped_single = (
        replace(_streaming_album(name, 1970), album_type=AlbumType.SINGLE) for name in "ST"
    )
    lookup = _stub_provider(mass, [played, picked, later, single, mapped_single])
    _stub_album_lookup(mass, monkeypatch, [played, picked, later, single, mapped_single])
    artist = _provider_artist()
    queue = _queue(mass, monkeypatch, _queued_track([artist], played))
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)
    auto = [
        _queued_track([artist], None),
        _queued_track([artist], single),
        # a provider often gives only a mapping, which tells no album type
        _queued_track([artist], ItemMapping.from_item(mapped_single)),
        _queued_track([artist], ItemMapping.from_item(mapped_single)),
        _queued_track([artist], ItemMapping.from_item(played)),
        _queued_track([artist], ItemMapping.from_item(picked)),
        _queued_track([artist], ItemMapping.from_item(later)),
    ]

    result = await Autoplay(mass.player_queues).get_album_of_tracks(queue, auto)

    assert _names(result) == ["B 1", "B 2"]
    assert lookup.await_count == 1


@pytest.mark.parametrize("error", [MediaNotFoundError, LoginFailed])
@pytest.mark.parametrize(
    ("offered", "failing", "expected", "tries"),
    [("AB", "A", "B", 2), ("AB", "AB", None, 2), ("ABCD", "ABCD", None, 3)],
)
async def test_auto_tries_each_album_once(
    mass: MusicAssistant,
    monkeypatch: pytest.MonkeyPatch,
    error: type[Exception],
    offered: str,
    failing: str,
    expected: str | None,
    tries: int,
) -> None:
    """An album that fails is tried once, up to 3 albums; then Auto's loose tracks stay."""
    albums = [_streaming_album(name, 1970) for name in "ABCD"]
    lookup = _stub_provider(mass, albums)
    _stub_album_lookup(mass, monkeypatch, albums)
    library_lookup = AsyncMock(wraps=mass.music.albums.get_library_item_by_prov_id)
    monkeypatch.setattr(mass.music.albums, "get_library_item_by_prov_id", library_lookup)
    names = {album.item_id: album.name for album in albums}
    tracks = lookup.side_effect

    def _tracks(item_id: str, *args: Any, **kwargs: Any) -> list[Track]:
        if names[item_id] in failing:
            raise error("gone")
        return cast("list[Track]", tracks(item_id, *args, **kwargs))

    lookup.side_effect = _tracks
    artist = _provider_artist()
    queue = _queue(mass, monkeypatch, _queued_track([artist], None))
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)
    first, *others = (ItemMapping.from_item(album) for album in albums if album.name in offered)
    auto = [_queued_track([artist], album) for album in (first, first, *others)]

    result = await Autoplay(mass.player_queues).get_album_of_tracks(queue, auto)

    assert _names(result) == ([f"{expected} 1", f"{expected} 2"] if expected else [])
    assert lookup.await_count == tries
    # once to find Auto's album in the library, once more to load it, as playing it does
    assert library_lookup.await_count == 2 * tries


async def test_an_album_auto_cannot_load_counts_as_a_failure(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An album of Auto's that fails to load is one of the 3 failures, then loose tracks stay."""
    albums = [_streaming_album(name, 1970) for name in "ABCD"]
    lookup = _stub_provider(mass, albums)
    album_lookup = _stub_album_lookup(mass, monkeypatch, albums, TimeoutError)
    artist = _provider_artist()
    queue = _queue(mass, monkeypatch, _queued_track([artist], None))
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)
    auto = [_queued_track([artist], ItemMapping.from_item(album)) for album in albums]

    result = await Autoplay(mass.player_queues).get_album_of_tracks(queue, auto)

    assert result == []
    assert album_lookup.await_count == 3
    lookup.assert_not_awaited()


async def test_auto_skips_the_provider_copy_of_a_played_library_album(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auto's provider track of an album that played from the library counts as played."""
    heard, picked = (_streaming_album(name, 1970) for name in "AB")
    _stub_provider(mass, [heard, picked])
    _stub_album_lookup(mass, monkeypatch, [heard, picked])
    artist = await _add_artist(mass)
    library_copy = await mass.music.albums.add_item_to_library(
        replace(
            heard,
            item_id="0",
            provider="library",
            artists=UniqueList([artist]),
            provider_mappings={*heard.provider_mappings, *_library_mapping()},
        )
    )
    queue = _queue(mass, monkeypatch, _queued_track([artist], ItemMapping.from_item(library_copy)))
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)
    auto = [_queued_track([artist], ItemMapping.from_item(album)) for album in (heard, picked)]

    result = await Autoplay(mass.player_queues).get_album_of_tracks(queue, auto)

    assert _names(result) == ["B 1", "B 2"]


async def test_auto_skips_an_unlinked_copy_of_a_played_album(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auto's copy of a played album on another provider, not linked to it, counts as played."""
    artist = _provider_artist()
    heard, other_copy, picked = (
        replace(_streaming_album(name, 1970, provider), artists=UniqueList([artist]))
        for name, provider in (("A", "streaming"), ("A", "other"), ("B", "streaming"))
    )
    _stub_provider(mass, [heard, other_copy, picked])
    _stub_album_lookup(mass, monkeypatch, [heard, other_copy, picked])
    queue = _queue(mass, monkeypatch, _queued_track([artist], ItemMapping.from_item(heard)))
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)
    auto = [_queued_track([artist], ItemMapping.from_item(album)) for album in (other_copy, picked)]

    result = await Autoplay(mass.player_queues).get_album_of_tracks(queue, auto)

    assert _names(result) == ["B 1", "B 2"]


async def test_auto_keeps_loose_tracks_when_the_mode_cant_play(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the shuffled artist order, Auto's tracks are not widened to their album."""
    album = _streaming_album("A", 1970)
    lookup = _stub_provider(mass, [album])
    artist = _provider_artist()
    queue = _queue(mass, monkeypatch, _queued_track([artist], None))
    _set_order(mass, ArtistOrder.SHUFFLED_TRACKS)

    result = await Autoplay(mass.player_queues).get_album_of_tracks(
        queue, [_queued_track([artist], ItemMapping.from_item(album))]
    )

    assert result == []
    lookup.assert_not_awaited()


@pytest.mark.parametrize("ends", ["all played", "can't be listed", "3 fail"])
async def test_a_similar_artist_plays_next_whatever_ends_the_artist(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch, ends: str
) -> None:
    """Albums all played, not listed, or 3 failing to load: a similar artist plays next."""
    queue, seed, _lookup = await _similar_setup(mass, monkeypatch, {"Next": (1990,)})
    queues = mass.player_queues
    if ends == "can't be listed":
        candidates = queues.artist_album_candidates

        async def _unlisted(artist: Artist, *args: Any) -> list[list[Album]] | None:
            return None if artist.item_id == seed.item_id else await candidates(artist, *args)

        monkeypatch.setattr(queues, "artist_album_candidates", _unlisted)
    if ends == "3 fail":
        for year in (1975, 1976, 1977):
            await _add_album(mass, seed, f"Broken {year}", year)
        # a 4th album that would load: the 3 failures end the artist first
        await _add_album(mass, seed, "Good", 1978)
        copy_items = queues.album_copy_items

        async def _failing(copies: list[Album], *args: Any) -> list[ResolvedItem]:
            if copies[0].name.startswith("Broken"):
                raise MediaNotFoundError("gone")
            return await copy_items(copies, *args)

        monkeypatch.setattr(queues, "album_copy_items", _failing)

    result = await Autoplay(queues).get_album_tracks(queue)

    assert _album_of(result) == {"Next 1990"}


async def test_a_disliked_track_is_left_out_of_the_album(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The album plays without the tracks its user disliked."""
    _stub_provider(mass, [_streaming_album("A", 1970)])

    async def _without_disliked(_mass: Any, _userid: Any, items: list[Track]) -> list[Track]:
        return [track for track in items if track.name != "A 1"]

    monkeypatch.setattr(autoplay, "without_disliked_tracks", _without_disliked)
    queue = _queue(mass, monkeypatch, _queued_track([_provider_artist()], None))
    _set_order(mass, ArtistOrder.ALBUMS_BY_RELEASE)

    assert _names(await Autoplay(mass.player_queues).get_album_tracks(queue)) == ["A 2"]
