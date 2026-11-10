"""Tests for the lazy step engine of the Rotation playlists."""

from __future__ import annotations

import hashlib
import logging
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from multidict import CIMultiDict, CIMultiDictProxy
from music_assistant_models.enums import AlbumType, MediaType, SortField
from music_assistant_models.media_items import Album, Artist, ItemMapping, Track
from music_assistant_models.media_items.provider_mapping import ProviderMapping
from music_assistant_models.unique_list import UniqueList
from yarl import URL

from music_assistant.helpers.throttle_retry import RequestPriority, current_priority
from music_assistant.providers.rotation_playlist.constants import BLOCKING_SORT_MAX_ITEMS
from music_assistant.providers.rotation_playlist.engine import (
    Node,
    RunContext,
    iter_units,
    playable_tracks,
    unit_tracks,
)
from music_assistant.providers.rotation_playlist.models import (
    Condition,
    ExpandStep,
    FilterStep,
    FlattenStep,
    ItemKind,
    LimitStep,
    SortKey,
    SortStep,
    SourceStep,
    Step,
    StepsError,
)

DIE_DREI = "Die drei ???"
GLOCKEN = "Böser die Glocken nie klingen"
GLOCKEN_TRACKS = [
    f"{GLOCKEN} (Inhaltsangabe)",
    f"{GLOCKEN} (Titelmusik)",
    "Tag 01 - Unbekannter Herkunft (Teil 01)",
    "Tag 01 - Unbekannter Herkunft (Teil 02)",
]
PAPAGEI_TRACKS = [f"Der Super-Papagei (Kapitel {number})" for number in range(1, 5)]
OTHER_ALBUMS = {
    "TKKG": ["Die Jagd nach den Millionendieben", "Der blinde Hellseher"],
    "Fünf Freunde": ["Fünf Freunde beim Wanderzirkus", "Fünf Freunde auf geheimnisvollen Spuren"],
}


@dataclass
class FakeLibrary:
    """A small library: 3 artists, 6 albums, 4 tracks each."""

    artists: list[Artist] = field(default_factory=list)
    albums: list[Album] = field(default_factory=list)
    tracks: dict[str, list[Track]] = field(default_factory=dict)
    provider_albums: dict[tuple[str, str], list[Album]] = field(default_factory=dict)


def _mapping(item_id: str, instance: str = "filesystem") -> ProviderMapping:
    return ProviderMapping(item_id=item_id, provider_domain=instance, provider_instance=instance)


def _ref(media_type: MediaType, item_id: str, name: str) -> ItemMapping:
    return ItemMapping(item_id=item_id, provider="library", name=name, media_type=media_type)


def _album(item_id: str, name: str, artist: Artist, **kwargs: Any) -> Album:
    return Album(
        item_id=item_id,
        provider=kwargs.pop("provider", "library"),
        name=name,
        provider_mappings=kwargs.pop("provider_mappings", {_mapping(f"album{item_id}")}),
        artists=UniqueList([_ref(MediaType.ARTIST, artist.item_id, artist.name)]),
        **kwargs,
    )


def _track(item_id: str, name: str, number: int, album: Album, duration: int = 600) -> Track:
    return Track(
        item_id=item_id,
        provider="library",
        name=name,
        provider_mappings={_mapping(f"track{item_id}")},
        artists=album.artists,
        album=_ref(MediaType.ALBUM, album.item_id, album.name),
        track_number=number,
        duration=duration,
    )


def _library() -> FakeLibrary:
    library = FakeLibrary()
    album_specs = {DIE_DREI: [(GLOCKEN, GLOCKEN_TRACKS), ("Der Super-Papagei", PAPAGEI_TRACKS)]}
    for artist_name, names in OTHER_ALBUMS.items():
        album_specs[artist_name] = [
            (name, [f"{name} (Kapitel {number})" for number in range(1, 5)]) for name in names
        ]
    for artist_number, (artist_name, specs) in enumerate(album_specs.items(), start=1):
        artist = Artist(
            item_id=str(artist_number),
            provider="library",
            name=artist_name,
            provider_mappings={_mapping(f"artist{artist_number}")},
        )
        library.artists.append(artist)
        for album_name, track_names in specs:
            album_id = str(len(library.albums) + 1)
            album = _album(album_id, album_name, artist, year=1990 + len(library.albums))
            library.albums.append(album)
            library.tracks[album_id] = [
                _track(f"{album_id}{number}", name, number, album)
                for number, name in enumerate(track_names, start=1)
            ]
    return library


def _page(items: list[Any]) -> AsyncMock:
    async def library_items(
        limit: int = 500,
        offset: int = 0,
        sort_field: SortField | None = None,
        random_seed: int | None = None,
        favorite: bool | None = None,
        provider: list[str] | None = None,
        **_kwargs: Any,
    ) -> list[Any]:
        result = [
            item
            for item in items
            if (favorite is None or item.favorite is favorite)
            and (
                provider is None
                or any(
                    mapping.provider_instance in provider and mapping.in_library
                    for mapping in item.provider_mappings
                )
            )
        ]
        if sort_field == SortField.RANDOM:
            # a stable order that follows the seed, like the random order of the library query
            result.sort(key=lambda item: _seeded_key(random_seed, item))
        return result[offset : offset + limit]

    return AsyncMock(side_effect=library_items)


def _seeded_key(seed: int | None, item: Any) -> str:
    return hashlib.sha256(f"{seed}:{item.uri}".encode()).hexdigest()


def _mass(library: FakeLibrary) -> MagicMock:
    mass = MagicMock()
    mass.music.albums.library_items = _page(library.albums)
    mass.music.artists.library_items = _page(library.artists)
    all_tracks = [track for tracks in library.tracks.values() for track in tracks]
    mass.music.tracks.library_items = _page(all_tracks)
    mass.music.albums.library_count = AsyncMock(return_value=len(library.albums))
    mass.music.artists.library_count = AsyncMock(return_value=len(library.artists))
    mass.music.tracks.library_count = AsyncMock(return_value=len(all_tracks))

    async def album_tracks(item_id: str, _provider: str, **_kwargs: Any) -> list[Track]:
        # listed in reverse, so a sort step is needed to get the track order
        return list(reversed(library.tracks.get(item_id, [])))

    async def artist_albums(item_id: str, provider: str, **_kwargs: Any) -> list[Album]:
        if provider != "library":
            return library.provider_albums.get((provider, item_id), [])
        return [album for album in library.albums if album.artists[0].item_id == item_id]

    async def artist_tracks(item_id: str, provider: str, **_kwargs: Any) -> list[Track]:
        return [
            track
            for album in await artist_albums(item_id, provider)
            for track in library.tracks[album.item_id]
        ]

    mass.music.albums.tracks = AsyncMock(side_effect=album_tracks)
    mass.music.artists.albums = AsyncMock(side_effect=artist_albums)
    mass.music.artists.tracks = AsyncMock(side_effect=artist_tracks)
    mass.music.get_unique_providers = MagicMock(return_value=["filesystem", "spotify"])
    mass.music.providers = []
    return mass


def _die_drei_steps() -> list[Step]:
    return [
        SourceStep(ItemKind.ALBUM),
        FilterStep("keep", "all", [Condition("artist", "is", "library://artist/1")]),
        SortStep([SortKey("random")]),
        ExpandStep(ItemKind.TRACK),
        FilterStep(
            "remove",
            "any",
            [
                Condition("title", "contains", "Inhaltsangabe"),
                Condition("title", "contains", "Titelmusik"),
            ],
        ),
        SortStep([SortKey("track_number")]),
    ]


async def _units(steps: list[Step], ctx: RunContext) -> list[Node]:
    return [node async for node in iter_units(steps, ctx)]


def _names(tracks: list[Track]) -> list[str]:
    return [track.name for track in tracks]


async def test_die_drei_groups_and_orders() -> None:
    """The units are the two Die drei ??? albums, each in track order without the intro."""
    ctx = RunContext(_mass(_library()), seed=7)
    units = await _units(_die_drei_steps(), ctx)
    assert sorted(unit.item.name for unit in units) == [
        "Böser die Glocken nie klingen",
        "Der Super-Papagei",
    ]
    by_name = {unit.item.name: unit_tracks(unit) for unit in units}
    assert _names(by_name[GLOCKEN]) == GLOCKEN_TRACKS[2:]
    assert _names(by_name["Der Super-Papagei"]) == PAPAGEI_TRACKS


async def test_random_pushdown_uses_seed() -> None:
    """A random sort moves into the library query with the run's seed."""
    mass = _mass(_library())
    ctx = RunContext(mass, seed=42)
    await _units(_die_drei_steps(), ctx)
    kwargs = mass.music.albums.library_items.await_args.kwargs
    assert kwargs["sort_field"] == SortField.RANDOM
    assert kwargs["random_seed"] == 42
    assert kwargs["limit"] == 500
    assert kwargs["offset"] == 0


async def test_same_seed_same_order_other_seed_other() -> None:
    """An in-memory random order depends only on the seed."""
    steps: list[Step] = [
        SourceStep(ItemKind.ALBUM),
        ExpandStep(ItemKind.TRACK),
        FlattenStep(),
        SortStep([SortKey("random")]),
    ]
    library = _library()

    async def order(seed: int) -> list[str]:
        return [
            str(node.item.uri) for node in await _units(steps, RunContext(_mass(library), seed))
        ]

    assert await order(1) == await order(1)
    assert await order(1) != await order(2)


async def test_limit_inside_group_and_top_level() -> None:
    """A limit after expand cuts each group; at depth 0 it cuts the units."""
    steps: list[Step] = [
        SourceStep(ItemKind.ALBUM),
        ExpandStep(ItemKind.TRACK),
        SortStep([SortKey("track_number")]),
        LimitStep(count=2),
    ]
    units = await _units(steps, RunContext(_mass(_library()), seed=1))
    assert len(units) == 6
    assert all(len(unit_tracks(unit)) == 2 for unit in units)
    assert _names(unit_tracks(units[0])) == GLOCKEN_TRACKS[:2]
    top_steps: list[Step] = [steps[0], LimitStep(count=1), *steps[1:3]]
    top = await _units(top_steps, RunContext(_mass(_library()), seed=1))
    assert [unit.item.name for unit in top] == [GLOCKEN]
    assert len(unit_tracks(top[0])) == 4


async def test_limit_minutes_includes_the_crossing_item() -> None:
    """A duration limit takes items until the total reaches the minutes."""
    steps: list[Step] = [
        SourceStep(ItemKind.ALBUM),
        ExpandStep(ItemKind.TRACK),
        LimitStep(minutes=15),
    ]
    units = await _units(steps, RunContext(_mass(_library()), seed=1))
    # tracks are 10 minutes each: the second one crosses 15 minutes and is kept
    assert all(len(unit_tracks(unit)) == 2 for unit in units)
    top = await _units([steps[0], LimitStep(minutes=50)], RunContext(_mass(_library()), seed=1))
    # albums without a duration count as 0 minutes
    assert len(top) == 6


async def test_limit_stops_pulling() -> None:
    """A limit at depth 0 stops reading library pages."""
    album = _library().albums[0]
    mass = _mass(_library())
    mass.music.albums.library_items = _page([album] * 1200)
    steps: list[Step] = [SourceStep(ItemKind.ALBUM), LimitStep(count=3)]
    ctx = RunContext(mass, seed=1)
    assert len(await _units(steps, ctx)) == 3
    assert mass.music.albums.library_items.await_count == 1
    assert [(stat.count, stat.exact) for stat in ctx.stats] == [(3, False), (3, True)]


async def test_source_reads_all_pages() -> None:
    """The source pages through the library until a short page."""
    album = _library().albums[0]
    mass = _mass(_library())
    mass.music.albums.library_items = _page([album] * 1200)
    ctx = RunContext(mass, seed=1)
    assert len(await _units([SourceStep(ItemKind.ALBUM)], ctx)) == 1200
    offsets = [call.kwargs["offset"] for call in mass.music.albums.library_items.await_args_list]
    assert offsets == [0, 500, 1000]
    assert ctx.stats[0].exact


async def test_count_cap_marks_counts_inexact() -> None:
    """Reading stops at the count cap and the counts become "at least"."""
    album = _library().albums[0]
    mass = _mass(_library())
    mass.music.albums.library_items = _page([album] * 1200)
    ctx = RunContext(mass, seed=1, count_cap=700)
    assert len(await _units([SourceStep(ItemKind.ALBUM)], ctx)) == 700
    assert [(stat.count, stat.exact) for stat in ctx.stats] == [(700, False)]


async def test_blocking_sort_cap_raises() -> None:
    """A sort that needs more than the cap of items asks for a limit before it."""
    track = _library().tracks["1"][0]
    mass = _mass(_library())
    mass.music.tracks.library_items = _page([track] * (BLOCKING_SORT_MAX_ITEMS + 1))
    steps: list[Step] = [SourceStep(ItemKind.TRACK), SortStep([SortKey("track_number")])]
    with pytest.raises(StepsError, match="Only the first' step before step 2"):
        await _units(steps, RunContext(mass, seed=1))
    mass.music.tracks.library_items = _page([track] * BLOCKING_SORT_MAX_ITEMS)
    assert len(await _units(steps, RunContext(mass, seed=1))) == BLOCKING_SORT_MAX_ITEMS


async def test_blocking_sort_counts_all_it_read() -> None:
    """A limit after a blocking sort does not make the sort's count partial."""
    tracks = _library().tracks
    mass = _mass(_library())
    mass.music.tracks.library_items = _page([*tracks["1"], *tracks["2"], *tracks["3"][:2]])
    steps: list[Step] = [
        SourceStep(ItemKind.TRACK),
        SortStep([SortKey("track_number")]),
        LimitStep(count=3),
    ]
    ctx = RunContext(mass, seed=1)
    assert len(await _units(steps, ctx)) == 3
    assert [(stat.count, stat.exact) for stat in ctx.stats] == [(10, True), (10, True), (3, True)]


async def test_pushed_filter_keeps_the_source_count() -> None:
    """A filter done by the library query still shows the whole library at the source."""
    library = _library()
    library.albums[0].favorite = True
    library.albums[3].favorite = True
    mass = _mass(library)
    steps: list[Step] = [
        SourceStep(ItemKind.ALBUM),
        SortStep([SortKey("random")]),
        FilterStep("keep", "all", [Condition("favorite", "is", True)]),
        FilterStep("keep", "all", [Condition("favorite", "is", True)]),
    ]
    ctx = RunContext(mass, seed=1)
    units = await _units(steps, ctx)
    assert sorted(unit.item.item_id for unit in units) == ["1", "4"]
    assert mass.music.albums.library_items.await_args.kwargs["favorite"] is True
    assert [(stat.count, stat.exact) for stat in ctx.stats] == [
        (6, True),
        (6, True),
        (2, True),
        (2, True),
    ]


async def test_year_sort_runs_in_memory() -> None:
    """A year sort is not given to the library query, so missing years stay last."""
    library = _library()
    library.albums[0].year = None
    mass = _mass(library)
    steps: list[Step] = [SourceStep(ItemKind.ALBUM), SortStep([SortKey("year")])]
    units = await _units(steps, RunContext(mass, seed=1))
    assert "sort_field" not in mass.music.albums.library_items.await_args.kwargs
    assert [unit.item.item_id for unit in units] == ["2", "3", "4", "5", "6", "1"]


async def test_provider_album_tracks_at_low_priority() -> None:
    """A provider album lists all its tracks at low priority, whatever the scope."""
    library = _library()
    artist = library.artists[0]
    provider_album = _album(
        "sp1", GLOCKEN, artist, provider="spotify", provider_mappings={_mapping("sp1", "spotify")}
    )
    library.tracks["sp1"] = library.tracks["1"]
    mass = _mass(library)
    priorities: list[tuple[str, RequestPriority, bool]] = []
    listing = mass.music.albums.tracks.side_effect

    async def record(item_id: str, provider: str, **kwargs: Any) -> list[Track]:
        priorities.append((item_id, current_priority(), kwargs["in_library_only"]))
        return list(await listing(item_id, provider, **kwargs))

    mass.music.albums.tracks = AsyncMock(side_effect=record)
    mass.music.albums.library_items = _page([library.albums[1], provider_album])
    steps: list[Step] = [SourceStep(ItemKind.ALBUM), ExpandStep(ItemKind.TRACK)]
    ctx = RunContext(mass, seed=1)
    units = await _units(steps, ctx)
    assert [len(unit_tracks(unit)) for unit in units] == [4, 4]
    assert priorities == [
        ("2", RequestPriority.NORMAL, True),
        ("sp1", RequestPriority.LOW, False),
    ]
    priorities.clear()
    await playable_tracks(Node(provider_album), ctx)
    assert priorities == [("sp1", RequestPriority.LOW, False)]


@dataclass
class FakeLoader:
    """Answers the loads of the engine from fixed values and records each call."""

    values: dict[tuple[ItemKind, str], dict[str, Any]] = field(default_factory=dict)
    calls: list[tuple[ItemKind, list[int], frozenset[str]]] = field(default_factory=list)

    async def __call__(
        self, _mass: Any, kind: ItemKind, item_ids: list[int], names: frozenset[str]
    ) -> dict[int, dict[str, Any]]:
        """Record the call and return the fixed values of the asked fields."""
        self.calls.append((kind, list(item_ids), names))
        return {
            item_id: {
                name: value
                for name, value in self.values.get((kind, str(item_id)), {}).items()
                if name in names
            }
            for item_id in item_ids
        }


@pytest.fixture
def loader(monkeypatch: pytest.MonkeyPatch) -> FakeLoader:
    """Replace the library loads of the engine with a fake."""
    fake = FakeLoader()
    monkeypatch.setattr("music_assistant.providers.rotation_playlist.engine.load_values", fake)
    return fake


async def test_play_count_sort_goes_to_the_query_for_artists_and_tracks(
    loader: FakeLoader,
) -> None:
    """Artist and track play sorts go to the library query; album plays count their tracks."""
    mass = _mass(_library())
    for kind, controller in ((ItemKind.ARTIST, "artists"), (ItemKind.TRACK, "tracks")):
        for name, sort_field in (
            ("play_count", SortField.PLAY_COUNT),
            ("last_played", SortField.LAST_PLAYED),
        ):
            await _units([SourceStep(kind), SortStep([SortKey(name)])], RunContext(mass, seed=1))
            library_items = getattr(mass.music, controller).library_items
            assert library_items.await_args.kwargs["sort_field"] == sort_field
    assert loader.calls == []
    loader.values = {
        (ItemKind.ALBUM, "2"): {"play_count": 5},
        (ItemKind.ALBUM, "4"): {"play_count": 1},
    }
    steps: list[Step] = [
        SourceStep(ItemKind.ALBUM),
        FilterStep("keep", "all", [Condition("favorite", "is", True)]),
        SortStep([SortKey("play_count", descending=True)]),
    ]
    await _units(steps, RunContext(mass, seed=1))
    kwargs = mass.music.albums.library_items.await_args.kwargs
    assert "sort_field" not in kwargs
    assert kwargs["favorite"] is True
    units = await _units([steps[0], steps[2]], RunContext(mass, seed=1))
    assert [unit.item.item_id for unit in units][:2] == ["2", "4"]
    assert loader.calls[-1][2] == frozenset({"play_count"})


async def test_loaded_values_come_once_per_page(loader: FakeLoader) -> None:
    """Play counts are loaded for a whole page of the source, then filter and order in memory."""
    library = _library()
    loader.values = {
        (ItemKind.ALBUM, album.item_id): {"play_count": int(album.item_id) % 3}
        for album in library.albums
    }
    mass = _mass(library)
    steps: list[Step] = [
        SourceStep(ItemKind.ALBUM),
        LimitStep(count=5),
        FilterStep("keep", "all", [Condition("play_count", "more_than", 0)]),
        SortStep([SortKey("play_count", descending=True), SortKey("name")]),
    ]
    units = await _units(steps, RunContext(mass, seed=1))
    assert [unit.item.item_id for unit in units] == ["2", "5", "1", "4"]
    assert loader.calls == [
        (ItemKind.ALBUM, [1, 2, 3, 4, 5, 6], frozenset({"play_count"})),
    ]


async def test_no_loads_without_a_loaded_field(loader: FakeLoader) -> None:
    """Steps that use no loaded field cause no extra queries; a pushed sort needs none either."""
    mass = _mass(_library())
    await _units(_die_drei_steps(), RunContext(mass, seed=1))
    pushed: list[Step] = [SourceStep(ItemKind.ARTIST), SortStep([SortKey("play_count")])]
    await _units(pushed, RunContext(mass, seed=1))
    assert loader.calls == []


async def test_expanded_children_are_loaded_per_unit(loader: FakeLoader) -> None:
    """The albums of an artist are loaded together, and the values survive a flatten."""
    library = _library()
    loader.values = {
        (ItemKind.ALBUM, "1"): {"track_count": 4},
        (ItemKind.ALBUM, "2"): {"track_count": 12},
        (ItemKind.ALBUM, "3"): {"track_count": 14},
    }
    steps: list[Step] = [
        SourceStep(ItemKind.ARTIST),
        ExpandStep(ItemKind.ALBUM),
        FilterStep("keep", "all", [Condition("track_count", "more_than", 10)]),
        FlattenStep(),
        SortStep([SortKey("track_count", descending=True)]),
    ]
    units = await _units(steps, RunContext(_mass(library), seed=1))
    assert [unit.item.item_id for unit in units] == ["3", "2"]
    assert [call[1] for call in loader.calls] == [[1, 2], [3, 4], [5, 6]]
    assert all(call[0] == ItemKind.ALBUM for call in loader.calls)


async def test_provider_items_are_not_loaded(loader: FakeLoader) -> None:
    """Items that are not in the library are not loaded; they count as never played."""
    library = _library()
    artist = library.artists[0]
    artist.provider_mappings.add(_mapping("drei", "spotify"))
    new = _album("sp3", "Die Spur des Spielers", artist, provider="spotify", year=2001)
    library.provider_albums[("spotify", "drei")] = [new]
    loader.values = {
        (ItemKind.ALBUM, "1"): {"play_count": 0},
        (ItemKind.ALBUM, "2"): {"play_count": 3},
    }
    steps: list[Step] = [
        SourceStep(ItemKind.ARTIST),
        FilterStep("keep", "all", [Condition("name", "is", DIE_DREI)]),
        ExpandStep(ItemKind.ALBUM, scope="providers"),
        FilterStep("keep", "all", [Condition("play_count", "less_than", 1)]),
    ]
    units = await _units(steps, RunContext(_mass(library), seed=1))
    assert units[0].children is not None
    assert [child.item.item_id for child in units[0].children] == ["1", "sp3"]
    assert loader.calls == [(ItemKind.ALBUM, [1, 2], frozenset({"play_count"}))]


async def test_filter_pushdown_provider_and_album_type() -> None:
    """Provider and album type filters go to the library query."""
    mass = _mass(_library())
    provider = MagicMock(instance_id="spotify--abc", domain="spotify")
    mass.music.providers = [provider]
    steps: list[Step] = [
        SourceStep(ItemKind.ALBUM),
        FilterStep(
            "keep",
            "all",
            [Condition("provider", "is", "spotify"), Condition("album_type", "is", "album")],
        ),
    ]
    await _units(steps, RunContext(mass, seed=1))
    kwargs = mass.music.albums.library_items.await_args.kwargs
    assert kwargs["provider"] == ["spotify--abc"]
    assert kwargs["album_types"] == [AlbumType.ALBUM]


async def test_provider_filter_keeps_the_same_items_pushed_or_not() -> None:
    """A provider filter keeps the items in that provider's library, also when it runs in memory."""
    library = _library()
    library.albums[0].provider_mappings.add(
        ProviderMapping(
            item_id="a1",
            provider_domain="spotify",
            provider_instance="spotify--abc",
            in_library=True,
        )
    )
    library.albums[1].provider_mappings.add(
        ProviderMapping(
            item_id="a2",
            provider_domain="spotify",
            provider_instance="spotify--abc",
            in_library=False,
        )
    )
    mass = _mass(library)
    mass.music.providers = [MagicMock(instance_id="spotify--abc", domain="spotify")]
    on_spotify = FilterStep("keep", "all", [Condition("provider", "is", "spotify")])
    pushed: list[Step] = [SourceStep(ItemKind.ALBUM), on_spotify]
    # the in-memory year order stops the filter after it from going into the query
    in_memory: list[Step] = [SourceStep(ItemKind.ALBUM), SortStep([SortKey("year")]), on_spotify]
    pushed_units = await _units(pushed, RunContext(mass, seed=1))
    assert mass.music.albums.library_items.await_args.kwargs["provider"] == ["spotify--abc"]
    in_memory_units = await _units(in_memory, RunContext(mass, seed=1))
    assert "provider" not in mass.music.albums.library_items.await_args.kwargs
    assert [unit.item.name for unit in pushed_units] == [library.albums[0].name]
    assert [unit.item.name for unit in in_memory_units] == [library.albums[0].name]


async def test_flatten_then_random() -> None:
    """After flatten all tracks are mixed, each one exactly once."""
    library = _library()
    steps: list[Step] = [
        SourceStep(ItemKind.ALBUM),
        ExpandStep(ItemKind.TRACK),
        FlattenStep(),
        SortStep([SortKey("random")]),
    ]
    units = await _units(steps, RunContext(_mass(library), seed=3))
    uris = [str(unit.item.uri) for unit in units]
    expected = [str(track.uri) for tracks in library.tracks.values() for track in tracks]
    assert all(unit.children is None for unit in units)
    assert sorted(uris) == sorted(expected)
    assert uris != expected


async def test_collect_removed_and_stats() -> None:
    """Removed tracks stay on their album and every step reports its count."""
    ctx = RunContext(_mass(_library()), seed=7, collect_removed=True)
    units = await _units(_die_drei_steps(), ctx)
    glocken = next(unit for unit in units if unit.item.name == GLOCKEN)
    assert sorted(node.item.name for node in glocken.removed) == sorted(GLOCKEN_TRACKS[:2])
    assert [stat.count for stat in ctx.stats] == [6, 2, 2, 8, 6, 6]
    assert [stat.step for stat in ctx.stats] == [1, 2, 3, 4, 5, 6]
    assert all(stat.exact for stat in ctx.stats)
    plain = RunContext(_mass(_library()), seed=7)
    assert all(not unit.removed for unit in await _units(_die_drei_steps(), plain))


async def test_stopping_early_marks_counts_inexact() -> None:
    """A consumer that stops early leaves counts that are "at least"."""
    ctx = RunContext(_mass(_library()), seed=1)
    async with aclosing(iter_units([SourceStep(ItemKind.ALBUM)], ctx)) as units:
        await anext(units)
    assert [(stat.count, stat.exact) for stat in ctx.stats] == [(1, False)]


async def test_expand_artist_to_provider_albums_dedupes_copies() -> None:
    """Provider albums join the library albums, copies of the same album count once."""
    library = _library()
    artist = library.artists[0]
    artist.provider_mappings.add(_mapping("drei", "spotify"))
    glocken_copy = _album(
        "sp1",
        GLOCKEN,
        artist,
        provider="spotify",
        provider_mappings={_mapping("sp1", "spotify")},
        year=1990,
    )
    linked = _album(
        "sp2", "Linked", artist, provider="spotify", provider_mappings={_mapping("album2")}
    )
    new = _album("sp3", "Die Spur des Spielers", artist, provider="spotify", year=2001)
    library.provider_albums[("spotify", "drei")] = [glocken_copy, linked, new]
    mass = _mass(library)
    priorities: list[RequestPriority] = []
    listing = mass.music.artists.albums.side_effect

    async def record(*args: Any, **kwargs: Any) -> list[Album]:
        priorities.append(current_priority())
        return list(await listing(*args, **kwargs))

    mass.music.artists.albums = AsyncMock(side_effect=record)
    steps: list[Step] = [
        SourceStep(ItemKind.ARTIST),
        FilterStep("keep", "all", [Condition("name", "is", DIE_DREI)]),
        ExpandStep(ItemKind.ALBUM, scope="providers"),
    ]
    units = await _units(steps, RunContext(mass, seed=1))
    assert len(units) == 1
    assert units[0].children is not None
    names = [child.item.name for child in units[0].children]
    assert names == [GLOCKEN, "Der Super-Papagei", "Die Spur des Spielers"]
    assert priorities == [RequestPriority.NORMAL, RequestPriority.LOW, RequestPriority.LOW]


async def test_expand_artist_to_tracks() -> None:
    """Artists become groups of their library tracks."""
    steps: list[Step] = [
        SourceStep(ItemKind.ARTIST),
        ExpandStep(ItemKind.TRACK),
        FilterStep("keep", "all", [Condition("title", "contains", "Herkunft")]),
    ]
    units = await _units(steps, RunContext(_mass(_library()), seed=1))
    # the artists left without tracks are dropped
    assert [unit.item.name for unit in units] == [DIE_DREI]
    assert _names(unit_tracks(units[0])) == GLOCKEN_TRACKS[2:]


async def test_playable_tracks_expands_album_leaf() -> None:
    """An album at the end of the steps plays complete, in the album's order."""
    mass = _mass(_library())
    ctx = RunContext(mass, seed=1)
    units = await _units([SourceStep(ItemKind.ALBUM), LimitStep(count=1)], ctx)
    assert unit_tracks(units[0]) == []
    tracks = await playable_tracks(units[0], ctx)
    assert _names(tracks) == list(reversed(GLOCKEN_TRACKS))
    assert mass.music.albums.tracks.await_args.kwargs["in_library_only"] is True
    grouped = await _units(_die_drei_steps(), RunContext(mass, seed=7))
    assert await playable_tracks(grouped[0], ctx) == unit_tracks(grouped[0])


SECRET_URL = URL("https://api.example/rest/getAlbum?u=me&t=SECRETTOKEN")


def _html_error() -> aiohttp.ContentTypeError:
    """Return the error of an HTML answer where JSON was expected; it names the full url."""
    request = aiohttp.RequestInfo(SECRET_URL, "GET", CIMultiDictProxy(CIMultiDict()), SECRET_URL)
    return aiohttp.ContentTypeError(request, (), status=502, message="bad gateway")


@pytest.mark.parametrize("where", ["playable", "expand", "artist_albums"])
async def test_fetch_errors_are_logged_without_the_url(
    where: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed listing is logged by its type and status, never with the request url."""
    library = _library()
    mass = _mass(library)
    mass.music.albums.tracks = AsyncMock(side_effect=_html_error())
    ctx = RunContext(mass, seed=1)
    with caplog.at_level(logging.DEBUG):
        if where == "playable":
            units = await _units([SourceStep(ItemKind.ALBUM), LimitStep(count=1)], ctx)
            assert await playable_tracks(units[0], ctx) == []
        elif where == "expand":
            assert await _units(_die_drei_steps(), ctx) == []
        else:
            mass.music.artists.albums = AsyncMock(side_effect=[[], _html_error()])
            steps: list[Step] = [
                SourceStep(ItemKind.ARTIST),
                FilterStep("keep", "all", [Condition("name", "is", DIE_DREI)]),
                ExpandStep(ItemKind.ALBUM, scope="providers"),
            ]
            await _units(steps, ctx)
    assert "SECRETTOKEN" not in caplog.text
    assert "api.example" not in caplog.text
    assert "ContentTypeError" in caplog.text
    assert "502" in caplog.text
    assert ctx.fetch_failed is True


async def test_steps_without_a_source_raise_a_steps_error() -> None:
    """Steps that do not start with a source are refused with a steps error."""
    ctx = RunContext(_mass(_library()), seed=1)
    with pytest.raises(StepsError) as err:
        await _units([SortStep([SortKey("name")])], ctx)
    assert err.value.errors[0].step == 1


async def test_engine_logs_to_the_logger_of_the_run(caplog: pytest.LogCaptureFixture) -> None:
    """The engine logs to the logger it is given, so the provider's log level applies."""
    mass = _mass(_library())
    mass.music.albums.tracks = AsyncMock(side_effect=TimeoutError())
    logger = logging.getLogger("music_assistant.Rotations")
    ctx = RunContext(mass, seed=1, logger=logger)
    with caplog.at_level(logging.WARNING):
        assert await _units(_die_drei_steps(), ctx) == []
    assert {record.name for record in caplog.records} == {"music_assistant.Rotations"}
