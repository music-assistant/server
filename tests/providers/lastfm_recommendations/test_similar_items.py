"""Tests for how many lookups the Last.fm similar tracks and artists listings cost."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from music_assistant_models.enums import MediaType, ProviderFeature, ProviderStatus
from music_assistant_models.media_items import Artist, ItemMapping, Track, UniqueList

from music_assistant.controllers.music.constants import (
    DYNAMIC_RADIO_BASE_SAMPLE_SIZE,
    DYNAMIC_RADIO_DYNAMIC_TARGET,
    SIMILAR_ITEMS_LIMIT,
)
from music_assistant.providers.lastfm_recommendations import (
    SUPPORTED_FEATURES,
    LastFMRecommendationsProvider,
    parsers,
)
from music_assistant.providers.lastfm_recommendations.constants import (
    SIMILAR_ITEMS_FETCH_LIMIT,
    SIMILAR_ITEMS_MISS_LIMIT,
)
from music_assistant.providers.radio_playlist import RadioPlaylistProvider

INSTANCE_ID = "lastfm_recommendations--test1"
PARSE_TRACK = "music_assistant.providers.lastfm_recommendations.recommendations.parse_track"
PARSE_ARTIST = "music_assistant.providers.lastfm_recommendations.recommendations.parse_artist"
REF_TRACK = Track(item_id="ref", provider="library", name="Seed", provider_mappings=set())


class _FakeCache:
    """In-memory stand-in for the cache controller."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def get(self, key: str, **_kwargs: Any) -> Any:
        return self.data.get(key)

    async def set(self, key: str, data: Any, **_kwargs: Any) -> None:
        self.data[key] = data


def _artist(name: str, provider: str = "spotify") -> ItemMapping:
    return ItemMapping(media_type=MediaType.ARTIST, item_id=name, provider=provider, name=name)


def _track(index: int, provider: str) -> Track:
    return Track(
        item_id=str(index),
        provider=provider,
        name=f"Song {index}",
        provider_mappings=set(),
        artists=UniqueList([_artist(f"Artist {index}", provider)]),
    )


def _lastfm_tracks(count: int, start: int = 0) -> list[dict[str, Any]]:
    return [
        {"name": f"Song {index}", "artist": {"name": f"Artist {index}"}}
        for index in range(start, start + count)
    ]


def _index_of(text: str) -> int:
    """Return the number in the "Artist N - Song N" text a lookup was made for."""
    return int(text.rsplit(" ", 1)[-1])


def _mass(in_library: set[int], on_service: set[int]) -> Mock:
    """
    Return a mass stand-in with one music service, and no MusicBrainz provider.

    :param in_library: Numbers of the Last.fm titles the library holds.
    :param on_service: Numbers of the Last.fm titles the music service finds by name.
    """
    service = Mock(
        instance_id="spotify--1",
        domain="spotify",
        is_streaming_provider=True,
        available=True,
        supported_features={ProviderFeature.LIBRARY_TRACKS, ProviderFeature.LIBRARY_ARTISTS},
    )
    service.name = "Spotify"

    async def _library_tracks(search: str, **_kwargs: Any) -> list[Track]:
        index = _index_of(search)
        return [_track(index, "library")] if index in in_library else []

    async def _search_tracks(query: str, *_args: Any, **_kwargs: Any) -> list[Track]:
        index = _index_of(query)
        return [_track(index, "spotify")] if index in on_service else []

    async def _library_artists(search: str, **_kwargs: Any) -> list[Artist]:
        index = _index_of(search)
        if index not in in_library:
            return []
        return [
            Artist(item_id=str(index), provider="library", name=search, provider_mappings=set())
        ]

    async def _search_artists(query: str, *_args: Any, **_kwargs: Any) -> list[Artist]:
        index = _index_of(query)
        if index not in on_service:
            return []
        return [Artist(item_id=str(index), provider="spotify", name=query, provider_mappings=set())]

    mass = Mock()
    mass.cache = _FakeCache()
    mass.get_provider = Mock(return_value=None)
    mass.music.providers = [service]
    mass.config.get_provider_configs = AsyncMock(return_value=[Mock(status=ProviderStatus.LOADED)])
    for ctrl in (mass.music.tracks, mass.music.artists):
        ctrl.get_library_item_by_external_id = AsyncMock(return_value=None)
        ctrl.get_library_item_by_external_ids = AsyncMock(return_value=None)
    mass.music.tracks.library_items = AsyncMock(side_effect=_library_tracks)
    mass.music.tracks.search = AsyncMock(side_effect=_search_tracks)
    mass.music.artists.library_items = AsyncMock(side_effect=_library_artists)
    mass.music.artists.search = AsyncMock(side_effect=_search_artists)
    return mass


async def _provider(mass: Mock) -> LastFMRecommendationsProvider:
    """Return a provider on the given mass, with a mocked Last.fm API client."""
    manifest = Mock(domain="lastfm_recommendations")
    config = Mock(instance_id=INSTANCE_ID)
    config.name = "Last.fm Test"
    config.get_value.side_effect = lambda key, default=None: {"log_level": "INFO"}.get(key, default)
    provider = LastFMRecommendationsProvider(mass, manifest, config, SUPPORTED_FEATURES)
    with patch("music_assistant.providers.lastfm_recommendations.LastFMAPIClient") as api_cls:
        api_cls.return_value = Mock()
        await provider.handle_async_init()
    return provider


async def _similar_tracks(
    mass: Mock, lastfm_tracks: list[dict[str, Any]], limit: int
) -> tuple[list[Track], AsyncMock]:
    """Return the similar tracks for a listing that needs `limit` tracks, and the resolver spy."""
    provider = await _provider(mass)
    provider.api.get_similar_tracks = AsyncMock(return_value=lastfm_tracks)  # type: ignore[method-assign]
    resolve = AsyncMock(wraps=parsers.parse_track)
    with patch(PARSE_TRACK, resolve):
        tracks = await provider.get_similar_tracks(REF_TRACK, limit=limit)
    provider.api.get_similar_tracks.assert_awaited_once_with(
        "Unknown Artist", "Seed", None, SIMILAR_ITEMS_FETCH_LIMIT
    )
    return tracks, resolve


@pytest.mark.asyncio
async def test_library_titles_cost_no_lookup() -> None:
    """A listing the library can fill is not looked up on any music service."""
    mass = _mass(in_library=set(range(25)), on_service=set(range(25)))
    tracks, resolve = await _similar_tracks(mass, _lastfm_tracks(25), limit=10)

    resolve.assert_not_awaited()
    mass.music.tracks.search.assert_not_awaited()
    assert [(track.provider, track.item_id) for track in tracks] == [
        ("library", str(index)) for index in range(10)
    ]


@pytest.mark.asyncio
async def test_lookups_stop_once_enough_tracks_are_found() -> None:
    """Titles found on a music service are only looked up until the listing is full."""
    mass = _mass(in_library=set(), on_service=set(range(25)))
    tracks, resolve = await _similar_tracks(mass, _lastfm_tracks(25), limit=10)

    assert resolve.await_count == 10
    assert mass.music.tracks.search.await_count == 10
    assert [track.item_id for track in tracks] == [str(index) for index in range(10)]


@pytest.mark.asyncio
async def test_a_few_misses_still_fill_the_listing() -> None:
    """Titles no music service has are skipped, the next most similar titles fill their place."""
    mass = _mass(in_library=set(), on_service=set(range(25)) - {1, 5})
    tracks, resolve = await _similar_tracks(mass, _lastfm_tracks(25), limit=10)

    assert resolve.await_count == 12
    assert [track.item_id for track in tracks] == [
        str(index) for index in range(12) if index not in (1, 5)
    ]


@pytest.mark.parametrize("limit", [10, SIMILAR_ITEMS_LIMIT])
@pytest.mark.asyncio
async def test_titles_found_nowhere_are_searched_a_limited_number_of_times(limit: int) -> None:
    """A run of misses stops at the miss limit; the titles after it are not searched."""
    mass = _mass(in_library=set(), on_service=set())
    tracks, resolve = await _similar_tracks(mass, _lastfm_tracks(25), limit=limit)

    assert tracks == []
    assert resolve.await_count == SIMILAR_ITEMS_MISS_LIMIT
    assert mass.music.tracks.search.await_count == SIMILAR_ITEMS_MISS_LIMIT
    # the titles left over are tried again by a next listing, not remembered as missing
    misses = [key for key in mass.cache.data if key.startswith("miss_")]
    assert len(misses) == SIMILAR_ITEMS_MISS_LIMIT


@pytest.mark.asyncio
async def test_library_titles_deeper_in_the_list_count_before_lookups() -> None:
    """Library titles anywhere in Last.fm's list fill the listing before a service is asked."""
    mass = _mass(in_library={1, 30, 40}, on_service=set(range(50)))
    tracks, resolve = await _similar_tracks(mass, _lastfm_tracks(50), limit=5)

    # two lookups fill the listing, both for Last.fm's most similar titles
    assert [call.args[0]["name"] for call in resolve.await_args_list] == ["Song 0", "Song 2"]
    assert [(track.provider, track.item_id) for track in tracks] == [
        ("spotify", "0"),
        ("library", "1"),
        ("spotify", "2"),
        ("library", "30"),
        ("library", "40"),
    ]


@pytest.mark.asyncio
async def test_titles_deeper_in_the_list_only_fill_in_for_misses() -> None:
    """The most similar titles are looked up first, deeper titles only replace their misses."""
    mass = _mass(in_library=set(), on_service=set(range(5, 50)))
    lastfm_tracks = _lastfm_tracks(50)
    lastfm_tracks[20]["mbid"] = "mbid-20"
    tracks, resolve = await _similar_tracks(mass, lastfm_tracks, limit=10)

    assert [call.args[0]["name"] for call in resolve.await_args_list] == [
        f"Song {index}" for index in (*range(10), 20, 10, 11, 12, 13)
    ]
    assert [track.item_id for track in tracks] == [str(index) for index in (*range(5, 14), 20)]


@pytest.mark.asyncio
async def test_titles_with_a_musicbrainz_id_are_looked_up_first() -> None:
    """A title MusicBrainz may link is looked up before a title that can only be searched."""
    mass = _mass(in_library=set(), on_service=set(range(25)))
    lastfm_tracks = _lastfm_tracks(25)
    lastfm_tracks[3]["mbid"] = "mbid-3"
    tracks, resolve = await _similar_tracks(mass, lastfm_tracks, limit=5)

    assert [call.args[0]["name"] for call in resolve.await_args_list] == [
        "Song 3",
        "Song 0",
        "Song 1",
        "Song 2",
        "Song 4",
    ]
    # the listing keeps Last.fm's order
    assert [track.item_id for track in tracks] == ["0", "1", "2", "3", "4"]


@pytest.mark.asyncio
async def test_a_near_match_in_the_library_is_not_listed_in_its_place() -> None:
    """A library track with almost the same title does not stand in for the Last.fm title."""
    mass = _mass(in_library=set(), on_service={1})
    sibling = Track(
        item_id="99",
        provider="library",
        name="Song 2",
        provider_mappings=set(),
        artists=UniqueList([_artist("Artist 1", "library")]),
    )
    mass.music.tracks.library_items = AsyncMock(return_value=[sibling])
    tracks, _resolve = await _similar_tracks(mass, _lastfm_tracks(1, start=1), limit=10)

    assert [(track.provider, track.item_id) for track in tracks] == [("spotify", "1")]


@pytest.mark.asyncio
async def test_remembered_miss_still_lists_the_library_copy() -> None:
    """A title no music service has is still listed when the library holds it."""
    mass = _mass(in_library={0}, on_service=set())
    mass.cache.data["miss_track_Artist 0_Song 0"] = True
    tracks, resolve = await _similar_tracks(mass, _lastfm_tracks(1), limit=10)

    resolve.assert_not_awaited()
    assert [(track.provider, track.item_id) for track in tracks] == [("library", "0")]


@pytest.mark.asyncio
async def test_similar_artists_stop_once_enough_are_found() -> None:
    """Similar artists are bounded the same way: library first, lookups until the row is full."""
    mass = _mass(in_library={2}, on_service=set(range(25)))
    provider = await _provider(mass)
    provider.api.get_similar_artists = AsyncMock(  # type: ignore[method-assign]
        return_value=[{"name": f"Artist {index}"} for index in range(25)]
    )
    resolve = AsyncMock(wraps=parsers.parse_artist)
    ref_artist = Artist(item_id="ref", provider="library", name="Seed", provider_mappings=set())
    with patch(PARSE_ARTIST, resolve):
        artists = await provider.get_similar_artists(ref_artist, limit=10)

    provider.api.get_similar_artists.assert_awaited_once_with(
        "Seed", None, SIMILAR_ITEMS_FETCH_LIMIT
    )
    assert resolve.await_count == 9
    assert mass.music.artists.search.await_count == 9
    assert [(artist.provider, artist.item_id) for artist in artists] == [
        ("library" if index == 2 else "spotify", str(index)) for index in range(10)
    ]


@pytest.mark.parametrize(
    ("on_service", "max_searches"),
    [
        # every title found: the top-up stops once it collected its tracks
        (set(range(1000)), DYNAMIC_RADIO_DYNAMIC_TARGET),
        # nothing found: each base track is asked twice, the second time with a lookup
        (set(), DYNAMIC_RADIO_BASE_SAMPLE_SIZE * 2 * SIMILAR_ITEMS_MISS_LIMIT),
    ],
)
@pytest.mark.asyncio
async def test_radio_top_up_searches_a_bounded_number_of_titles(
    on_service: set[int], max_searches: int
) -> None:
    """One radio top-up seeded from Last.fm's similar tracks searches a bounded number of titles."""
    mass = _mass(in_library=set(), on_service=on_service)
    provider = await _provider(mass)
    base_tracks = [
        Track(item_id=str(index), provider="library", name=f"Base {index}", provider_mappings=set())
        for index in range(DYNAMIC_RADIO_BASE_SAMPLE_SIZE)
    ]
    # every base track has its own similar titles
    provider.api.get_similar_tracks = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda _artist, name, _mbid, _limit: _lastfm_tracks(
            25, start=100 * (int(name.rsplit(" ", 1)[-1]) + 1)
        )
    )

    async def _similar_from_lastfm(item_id: str, *_args: Any, **_kwargs: Any) -> list[Track]:
        return await provider.get_similar_tracks(base_tracks[int(item_id)], SIMILAR_ITEMS_LIMIT)

    radio = RadioPlaylistProvider.__new__(RadioPlaylistProvider)
    radio_mass = MagicMock()
    radio_mass.music.tracks.similar_tracks = AsyncMock(side_effect=_similar_from_lastfm)
    radio_mass.player_queues.get_tracks_for_playback = AsyncMock(return_value=base_tracks)
    radio.mass = radio_mass
    album_seed = Mock(media_type=MediaType.ALBUM, item_id="album", provider="library")

    await radio.get_dynamic_tracks([cast("Any", album_seed)], target_size=25)

    assert mass.music.tracks.search.await_count <= max_searches
