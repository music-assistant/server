"""Test Deezer's two-method recommendations contract (rows + per-row items)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from deezer_python_gql.generated.get_recently_played import (
    GetRecentlyPlayedMeRecentlyPlayedEdgesNodeFlow,
)
from music_assistant_models.unique_list import UniqueList

from music_assistant.providers.deezer.provider import DeezerProvider
from tests.common import use_real_create_task

ALL_ROW_IDS = [
    "made_for_you",
    "recommended_playlists",
    "recommended_artist_playlists",
    "recommended_tracks",
    "new_releases",
    "mood_flows",
    "genre_flows",
    "recently_played",
]


def _playlist_node(node_id: str, title: str) -> SimpleNamespace:
    return SimpleNamespace(id=node_id, title=title, picture=None, owner=None)


def _recs_data() -> SimpleNamespace:
    """Minimal get_recommendations result feeding all four shared-fetch rows."""
    track = SimpleNamespace(
        id="tr1",
        title="Hot Song",
        duration=200,
        contributors=SimpleNamespace(edges=[]),
        album=None,
        media=None,
        is_explicit=False,
    )
    album = SimpleNamespace(
        id="al1",
        display_title="New Album",
        contributors=SimpleNamespace(edges=[]),
        cover=None,
        type_=None,
        release_date=None,
    )
    return SimpleNamespace(
        recommendations=SimpleNamespace(
            playlists=SimpleNamespace(
                edges=[SimpleNamespace(node=_playlist_node("pl1", "Editorial"))]
            ),
            artist_playlists=SimpleNamespace(
                edges=[SimpleNamespace(node=_playlist_node("pl2", "Artist Mix"))]
            ),
            hot_tracks=[track],
            new_releases=SimpleNamespace(edges=[SimpleNamespace(node=album)]),
        )
    )


def _flow_configs_data() -> SimpleNamespace:
    """Minimal get_flow_configs result with one mood and one genre flow."""

    def config_edge(node_id: str, title: str) -> SimpleNamespace:
        return SimpleNamespace(
            node=SimpleNamespace(
                id=node_id,
                title=title,
                visuals=SimpleNamespace(hardware_square_icon=None),
            )
        )

    return SimpleNamespace(
        flow_configs=SimpleNamespace(
            moods=SimpleNamespace(edges=[config_edge("chill", "Chill")]),
            genres=SimpleNamespace(edges=[config_edge("genre-rock", "Rock")]),
        )
    )


def _recently_played_data() -> SimpleNamespace:
    """Minimal get_recently_played result with a single Flow node."""
    flow_node = GetRecentlyPlayedMeRecentlyPlayedEdgesNodeFlow.model_construct(
        typename__="Flow", id="flow", title="My Flow", cover=None
    )
    return SimpleNamespace(recently_played=SimpleNamespace(edges=[SimpleNamespace(node=flow_node)]))


def _stub_gql_client(provider: DeezerProvider) -> Mock:
    """Attach a gql_client stub with canned data for every recommendations fetch."""
    gql = Mock()
    gql.get_recommendations = AsyncMock(return_value=_recs_data())
    gql.get_flow_config_tracks = AsyncMock(return_value=None)  # Flow cover -> None
    gql.get_made_for_me = AsyncMock(return_value=None)  # smart tracklists -> []
    gql.get_flow_configs = AsyncMock(return_value=_flow_configs_data())
    gql.get_recently_played = AsyncMock(return_value=_recently_played_data())
    provider.gql_client = gql
    return gql


def _install_cache_mocks(provider: DeezerProvider) -> None:
    """Make the @use_cache decorator treat every call as a cache miss."""
    provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        return_value=(None, False, False)
    )
    provider.mass.cache.set = AsyncMock()  # type: ignore[method-assign]
    use_real_create_task(provider.mass)


class _FakeCache:
    """Dict-backed stand-in for the mass cache, so a second call is a cache hit."""

    def __init__(self, provider: DeezerProvider) -> None:
        self._store: dict[str, Any] = {}
        self.background_tasks: list[asyncio.Future[Any]] = []
        provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
            side_effect=self._get
        )
        provider.mass.cache.set = AsyncMock(side_effect=self._set)  # type: ignore[method-assign]
        provider.mass.create_task = Mock(  # type: ignore[method-assign]
            side_effect=self._create_task
        )

    async def _get(self, key: str, **kwargs: Any) -> tuple[Any, bool, bool]:
        if key in self._store:
            return self._store[key], True, True
        return None, False, False

    async def _set(self, key: str, data: Any, **kwargs: Any) -> None:
        self._store[key] = data

    def _create_task(self, target: Any, *args: Any, **kwargs: Any) -> asyncio.Future[Any]:
        task: asyncio.Future[Any] = asyncio.ensure_future(target)
        self.background_tasks.append(task)
        return task

    async def flush(self) -> None:
        """Let pending background cache-store tasks complete."""
        await asyncio.gather(*self.background_tasks)
        self.background_tasks.clear()


@pytest.mark.asyncio
async def test_get_recommendations_static_rows_zero_backend_calls(
    provider: DeezerProvider,
) -> None:
    """get_recommendations returns the static row descriptors without any backend fetch."""
    gql = _stub_gql_client(provider)

    rows = await provider.get_recommendations()

    assert [row.item_id for row in rows] == ALL_ROW_IDS
    assert [row.translation_key for row in rows] == ALL_ROW_IDS
    assert all(row.provider == provider.instance_id for row in rows)
    assert all(len(row.items) == 0 for row in rows)
    gql.get_recommendations.assert_not_awaited()
    gql.get_flow_config_tracks.assert_not_awaited()
    gql.get_made_for_me.assert_not_awaited()
    gql.get_flow_configs.assert_not_awaited()
    gql.get_recently_played.assert_not_awaited()


@pytest.mark.asyncio
async def test_items_made_for_you(provider: DeezerProvider) -> None:
    """made_for_you items trigger only the Flow cover and made-for-me fetches."""
    _install_cache_mocks(provider)
    gql = _stub_gql_client(provider)

    items = await provider.get_recommendation_items("made_for_you")

    gql.get_flow_config_tracks.assert_awaited_once()
    gql.get_made_for_me.assert_awaited_once()
    gql.get_recommendations.assert_not_awaited()
    gql.get_flow_configs.assert_not_awaited()
    gql.get_recently_played.assert_not_awaited()
    assert [item.name for item in items] == ["Flow"]


@pytest.mark.asyncio
async def test_items_recently_played(provider: DeezerProvider) -> None:
    """recently_played items trigger only the recently-played fetch."""
    _install_cache_mocks(provider)
    gql = _stub_gql_client(provider)

    items = await provider.get_recommendation_items("recently_played")

    gql.get_recently_played.assert_awaited_once()
    gql.get_recommendations.assert_not_awaited()
    gql.get_flow_config_tracks.assert_not_awaited()
    gql.get_made_for_me.assert_not_awaited()
    gql.get_flow_configs.assert_not_awaited()
    assert [item.name for item in items] == ["My Flow"]


@pytest.mark.asyncio
async def test_items_payload_rows_share_one_gql_fetch(provider: DeezerProvider) -> None:
    """The four gql-backed rows are all served from one cached get_recommendations fetch."""
    cache = _FakeCache(provider)
    gql = _stub_gql_client(provider)

    playlists = await provider.get_recommendation_items("recommended_playlists")
    await cache.flush()
    artist_playlists = await provider.get_recommendation_items("recommended_artist_playlists")
    tracks = await provider.get_recommendation_items("recommended_tracks")
    albums = await provider.get_recommendation_items("new_releases")

    gql.get_recommendations.assert_awaited_once()
    assert [item.name for item in playlists] == ["Editorial"]
    assert [item.name for item in artist_playlists] == ["Artist Mix"]
    assert [item.name for item in tracks] == ["Hot Song"]
    assert [item.name for item in albums] == ["New Album"]
    gql.get_flow_config_tracks.assert_not_awaited()
    gql.get_made_for_me.assert_not_awaited()
    gql.get_flow_configs.assert_not_awaited()
    gql.get_recently_played.assert_not_awaited()


@pytest.mark.asyncio
async def test_items_flow_rows_share_one_fetch(provider: DeezerProvider) -> None:
    """mood_flows and genre_flows are both served from one cached get_flow_configs fetch."""
    cache = _FakeCache(provider)
    gql = _stub_gql_client(provider)

    mood_items = await provider.get_recommendation_items("mood_flows")
    await cache.flush()
    genre_items = await provider.get_recommendation_items("genre_flows")

    gql.get_flow_configs.assert_awaited_once()
    assert [item.name for item in mood_items] == ["Flow: Chill"]
    assert [item.name for item in genre_items] == ["Flow: Rock"]
    gql.get_recommendations.assert_not_awaited()
    gql.get_flow_config_tracks.assert_not_awaited()
    gql.get_made_for_me.assert_not_awaited()
    gql.get_recently_played.assert_not_awaited()


@pytest.mark.asyncio
async def test_items_unknown_id_returns_empty(provider: DeezerProvider) -> None:
    """An unknown row item_id yields an empty result without any backend fetch."""
    gql = _stub_gql_client(provider)

    result = await provider.get_recommendation_items("bogus_row")

    assert isinstance(result, UniqueList)
    assert len(result) == 0
    gql.get_recommendations.assert_not_awaited()
    gql.get_flow_config_tracks.assert_not_awaited()
    gql.get_made_for_me.assert_not_awaited()
    gql.get_flow_configs.assert_not_awaited()
    gql.get_recently_played.assert_not_awaited()
