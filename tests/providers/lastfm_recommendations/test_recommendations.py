"""Tests for the Last.fm Recommendations two-method recommendations contract."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, call, patch

import pytest
from music_assistant_models.enums import ExternalID, MediaType
from music_assistant_models.media_items import (
    Artist,
    ItemMapping,
    RecommendationFolder,
    Track,
    UniqueList,
)

from music_assistant.providers.lastfm_recommendations import (
    SUPPORTED_FEATURES,
    LastFMRecommendationsProvider,
)
from music_assistant.providers.lastfm_recommendations.constants import (
    ARTIST_TOP_TRACKS_FETCH_LIMIT,
)
from tests.common import use_real_create_task

INSTANCE_ID = "lastfm_recommendations--test1"


@pytest.fixture
def mass_mock() -> Mock:
    """Return a mock MusicAssistant instance."""
    mass = Mock()
    mass.http_session = AsyncMock()
    return mass


@pytest.fixture
def manifest_mock() -> Mock:
    """Return a mock provider manifest."""
    manifest = Mock()
    manifest.domain = "lastfm_recommendations"
    return manifest


@pytest.fixture
def config_mock() -> Mock:
    """Return a mock provider config."""
    config = Mock()
    config.name = "Last.fm Test"
    config.instance_id = INSTANCE_ID
    config.get_value.side_effect = lambda key, default=None: {"log_level": "INFO"}.get(key, default)
    return config


@pytest.fixture
async def provider(
    mass_mock: Mock, manifest_mock: Mock, config_mock: Mock
) -> LastFMRecommendationsProvider:
    """Return an initialized provider with a mocked API client and untouched mass mock."""
    provider = LastFMRecommendationsProvider(
        mass_mock, manifest_mock, config_mock, SUPPORTED_FEATURES
    )
    with patch("music_assistant.providers.lastfm_recommendations.LastFMAPIClient") as api_cls:
        api_cls.return_value = Mock()
        await provider.handle_async_init()
    # forget the init-time calls so tests can assert zero backend/mass activity
    mass_mock.reset_mock()
    return provider


def _make_folders() -> list[RecommendationFolder]:
    """Build two folders with items, shaped like the background builder's output."""
    return [
        RecommendationFolder(
            item_id=f"{INSTANCE_ID}_similar_artists",
            name="Discover Similar Artists",
            translation_key="discover_similar_artists",
            translation_params=["5"],
            provider=INSTANCE_ID,
            items=UniqueList(
                [
                    ItemMapping(
                        media_type=MediaType.ARTIST,
                        item_id="a1",
                        provider=INSTANCE_ID,
                        name="Artist One",
                    )
                ]
            ),
            subtitle="Based on your top 5 artists",
            icon="mdi-account-music-outline",
        ),
        RecommendationFolder(
            item_id=f"{INSTANCE_ID}_chart_top_tracks",
            name="Global Top Tracks",
            translation_key="global_top_tracks",
            provider=INSTANCE_ID,
            items=UniqueList(
                [
                    ItemMapping(
                        media_type=MediaType.TRACK,
                        item_id="t1",
                        provider=INSTANCE_ID,
                        name="Track One",
                    )
                ]
            ),
            subtitle="Most popular tracks worldwide",
            icon="mdi-chart-box",
        ),
    ]


async def test_get_recommendations_returns_rows_without_items(
    provider: LastFMRecommendationsProvider, mass_mock: Mock
) -> None:
    """Rows mirror the in-memory folders' identity, stripped of items, with zero backend I/O."""
    folders = _make_folders()
    provider._recommendation_folders = folders

    rows = await provider.get_recommendations()

    assert [row.item_id for row in rows] == [
        f"{INSTANCE_ID}_similar_artists",
        f"{INSTANCE_ID}_chart_top_tracks",
    ]
    similar_artists = rows[0]
    assert len(similar_artists.items) == 0
    assert similar_artists.provider == INSTANCE_ID
    assert similar_artists.name == "Discover Similar Artists"
    assert similar_artists.translation_key == "discover_similar_artists"
    assert similar_artists.translation_params == ["5"]
    assert similar_artists.icon == "mdi-account-music-outline"
    assert similar_artists.subtitle == "Based on your top 5 artists"
    # stripping produced fresh copies: the in-memory folders keep their items
    assert similar_artists is not folders[0]
    assert len(folders[0].items) == 1
    # static rows: no backend or mass activity at all
    assert cast("Mock", provider.api).mock_calls == []
    assert mass_mock.mock_calls == []


async def test_get_recommendations_empty_before_first_build(
    provider: LastFMRecommendationsProvider,
) -> None:
    """Before the background builder has run, there are no rows."""
    assert await provider.get_recommendations() == []


async def test_get_recommendation_items_returns_row_items(
    provider: LastFMRecommendationsProvider, mass_mock: Mock
) -> None:
    """One row's items come straight from the matching in-memory folder, with zero backend I/O."""
    folders = _make_folders()
    provider._recommendation_folders = folders

    items = await provider.get_recommendation_items(f"{INSTANCE_ID}_chart_top_tracks")

    assert items == folders[1].items
    assert [item.item_id for item in items] == ["t1"]
    assert cast("Mock", provider.api).mock_calls == []
    assert mass_mock.mock_calls == []


async def test_get_recommendation_items_unknown_id_returns_empty(
    provider: LastFMRecommendationsProvider,
) -> None:
    """An unknown row item_id yields an empty UniqueList."""
    provider._recommendation_folders = _make_folders()

    result = await provider.get_recommendation_items("bogus_row")

    assert isinstance(result, UniqueList)
    assert len(result) == 0


async def test_refresh_recommendations_serves_previous_generation_during_rebuild(
    provider: LastFMRecommendationsProvider,
) -> None:
    """Old rows keep being served mid-rebuild; only the final swap replaces them."""
    old_folders = _make_folders()
    provider._recommendation_folders = old_folders

    new_folder = RecommendationFolder(
        item_id=f"{INSTANCE_ID}_new_row",
        name="New Row",
        translation_key="new_row",
        provider=INSTANCE_ID,
        items=UniqueList(),
    )

    builder_started = asyncio.Event()
    resume_builder = asyncio.Event()

    async def gated_builder() -> AsyncIterator[RecommendationFolder]:
        builder_started.set()
        await resume_builder.wait()
        yield new_folder

    provider.recommendations_manager.build_recommendation_folders = gated_builder  # type: ignore[method-assign]

    refresh_task = asyncio.create_task(provider._refresh_recommendations())
    await builder_started.wait()

    # Mid-rebuild: the previous generation's rows and items are still served.
    mid_rows = await provider.get_recommendations()
    assert [row.item_id for row in mid_rows] == [
        f"{INSTANCE_ID}_similar_artists",
        f"{INSTANCE_ID}_chart_top_tracks",
    ]
    mid_items = await provider.get_recommendation_items(f"{INSTANCE_ID}_chart_top_tracks")
    assert [item.item_id for item in mid_items] == ["t1"]

    resume_builder.set()
    await refresh_task

    # After the swap: only the newly built generation is served.
    new_rows = await provider.get_recommendations()
    assert [row.item_id for row in new_rows] == [f"{INSTANCE_ID}_new_row"]


async def test_artist_toptrack_titles_are_listed_without_resolving_them(
    provider: LastFMRecommendationsProvider, mass_mock: Mock
) -> None:
    """Top track titles come in Last.fm's ranking, nameless entries dropped, nothing resolved."""
    mass_mock.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    mass_mock.cache.set = AsyncMock()
    use_real_create_task(mass_mock)
    provider.api.get_artist_top_tracks = AsyncMock(  # type: ignore[method-assign]
        return_value=[{"name": "Creep"}, {"name": ""}, {"playcount": "1"}, {"name": "Nude"}]
    )
    resolve = AsyncMock()
    provider.recommendations_manager.get_or_resolve_track = resolve  # type: ignore[method-assign]
    artist = Artist(item_id="1", provider="library", name="Radiohead", provider_mappings=set())

    assert await provider.get_artist_toptrack_titles(artist, 10) == ["Creep", "Nude"]

    provider.api.get_artist_top_tracks.assert_awaited_once_with(
        "Radiohead", None, ARTIST_TOP_TRACKS_FETCH_LIMIT
    )
    resolve.assert_not_awaited()


async def test_artist_toptracks_rank_library_tracks_with_one_lastfm_request(
    provider: LastFMRecommendationsProvider, mass_mock: Mock
) -> None:
    """Top tracks are the artist's library tracks in Last.fm's ranking, nothing is resolved."""
    _use_dict_cache(mass_mock)
    provider.api.get_artist_top_tracks = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            {"name": "Creep", "mbid": "mbid-creep"},
            {"name": "Not In Library", "mbid": ""},
            {"name": "Nude"},
            {"name": "Karma Police", "mbid": "mbid-karma"},
            {"name": "Nude", "mbid": ""},
            {"name": "Lucky", "mbid": ""},
        ]
    )
    resolve = AsyncMock()
    provider.recommendations_manager.get_or_resolve_track = resolve  # type: ignore[method-assign]
    library_tracks = [
        # the recording id match wins over the name match
        _library_track("1", "Creep"),
        _library_track("2", "Creep (Acoustic)", mbid="mbid-creep"),
        _library_track("3", "Nude", version="Live"),
        _library_track("4", "Nude"),
        _library_track("5", "Karma Police"),
        _library_track("6", "Lucky"),
    ]
    mass_mock.music.artists.get_library_artist_tracks = AsyncMock(return_value=library_tracks)
    artist = Artist(item_id="1", provider="library", name="Radiohead", provider_mappings=set())

    with patch(
        "music_assistant.providers.lastfm_recommendations.recommendations.parse_track"
    ) as parse_track:
        tracks = await provider.get_artist_toptracks(artist, limit=4)

    resolve.assert_not_awaited()
    parse_track.assert_not_called()
    provider.api.get_artist_top_tracks.assert_awaited_once_with(
        "Radiohead", None, ARTIST_TOP_TRACKS_FETCH_LIMIT
    )
    # one library read, no search on a music provider and no MusicBrainz lookup
    assert mass_mock.music.mock_calls == [call.artists.get_library_artist_tracks("1")]
    mass_mock.get_provider.assert_not_called()
    # the second "Nude" lists the remaining (live) version, "Lucky" falls outside the limit
    assert [track.item_id for track in tracks] == ["2", "4", "5", "3"]


async def test_artist_toptracks_reuse_cached_ranking_with_current_library(
    provider: LastFMRecommendationsProvider, mass_mock: Mock
) -> None:
    """A second call reuses the cached Last.fm ranking but picks up newly added library tracks."""
    _use_dict_cache(mass_mock)
    provider.api.get_artist_top_tracks = AsyncMock(  # type: ignore[method-assign]
        return_value=[{"name": "Creep", "mbid": ""}, {"name": "Nude", "mbid": ""}]
    )
    provider.recommendations_manager.get_or_resolve_track = AsyncMock()  # type: ignore[method-assign]
    mass_mock.music.artists.get_library_artist_tracks = AsyncMock(
        return_value=[_library_track("1", "Nude")]
    )
    artist = Artist(item_id="1", provider="library", name="Radiohead", provider_mappings=set())

    assert [track.item_id for track in await provider.get_artist_toptracks(artist)] == ["1"]
    mass_mock.music.artists.get_library_artist_tracks.return_value = [
        _library_track("1", "Nude"),
        _library_track("2", "Creep"),
    ]
    assert [track.item_id for track in await provider.get_artist_toptracks(artist)] == ["2", "1"]
    # the titles share the same cached fetch
    assert await provider.get_artist_toptrack_titles(artist, 1) == ["Creep"]

    provider.api.get_artist_top_tracks.assert_awaited_once()


async def test_artist_toptracks_empty_for_non_library_artist(
    provider: LastFMRecommendationsProvider, mass_mock: Mock
) -> None:
    """An artist that is not a library item has no top tracks and costs no request."""
    provider.api.get_artist_top_tracks = AsyncMock()  # type: ignore[method-assign]
    artist = Artist(item_id="abc", provider="spotify--x", name="Radiohead", provider_mappings=set())

    assert await provider.get_artist_toptracks(artist) == []
    provider.api.get_artist_top_tracks.assert_not_awaited()
    assert not mass_mock.music.mock_calls


async def test_artist_toptracks_are_matched_off_the_event_loop(
    provider: LastFMRecommendationsProvider, mass_mock: Mock
) -> None:
    """The title matching runs in a worker thread, so an artist with many tracks does not stall."""
    _use_dict_cache(mass_mock)
    provider.api.get_artist_top_tracks = AsyncMock(  # type: ignore[method-assign]
        return_value=[{"name": "Creep", "mbid": ""}]
    )
    mass_mock.music.artists.get_library_artist_tracks = AsyncMock(
        return_value=[_library_track("1", "Creep")]
    )
    artist = Artist(item_id="1", provider="library", name="Radiohead", provider_mappings=set())
    threads: list[threading.Thread] = []

    def record_thread(*_args: Any) -> list[Track]:
        threads.append(threading.current_thread())
        return []

    with patch(
        "music_assistant.providers.lastfm_recommendations._match_library_tracks",
        side_effect=record_thread,
    ):
        await provider.get_artist_toptracks(artist)

    assert threads
    assert threads[0] is not threading.current_thread()


def _use_dict_cache(mass_mock: Mock) -> None:
    """Back the mocked cache with a dict so cached methods hit on a second call."""
    store: dict[str, Any] = {}

    async def _get(key: str, **_kwargs: Any) -> tuple[Any, bool, bool]:
        return store.get(key), True, key in store

    async def _set(key: str, data: Any, **_kwargs: Any) -> None:
        store[key] = data

    mass_mock.cache.get_with_freshness = AsyncMock(side_effect=_get)
    mass_mock.cache.set = AsyncMock(side_effect=_set)
    use_real_create_task(mass_mock)


def _library_track(item_id: str, name: str, version: str = "", mbid: str | None = None) -> Track:
    """Build a library track, optionally with a MusicBrainz recording id."""
    return Track(
        item_id=item_id,
        provider="library",
        name=name,
        version=version,
        provider_mappings=set(),
        external_ids={(ExternalID.MB_RECORDING, mbid)} if mbid else set(),
    )
