"""Test Deezer recommendations() row filtering via the `wanted` parameter."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from deezer_python_gql.generated.get_recently_played import (
    GetRecentlyPlayedMeRecentlyPlayedEdgesNodeFlow,
)

from music_assistant.providers.deezer.provider import DeezerProvider

ALL_ROW_IDS = {
    "made_for_you",
    "recommended_playlists",
    "recommended_artist_playlists",
    "recommended_tracks",
    "new_releases",
    "mood_flows",
    "genre_flows",
    "recently_played",
}


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


@pytest.mark.asyncio
async def test_recommendations_wanted_none_fetches_all_rows(provider: DeezerProvider) -> None:
    """wanted=None (default) issues every backend fetch and builds all rows."""
    _install_cache_mocks(provider)
    gql = _stub_gql_client(provider)

    result = await provider.recommendations()

    gql.get_recommendations.assert_awaited_once()
    gql.get_flow_config_tracks.assert_awaited_once()
    gql.get_made_for_me.assert_awaited_once()
    gql.get_flow_configs.assert_awaited_once()
    gql.get_recently_played.assert_awaited_once()
    assert {f.item_id for f in result} == ALL_ROW_IDS


@pytest.mark.asyncio
async def test_recommendations_wanted_recently_played_only(provider: DeezerProvider) -> None:
    """wanted={recently_played} issues only that row's fetch and returns only that row."""
    _install_cache_mocks(provider)
    gql = _stub_gql_client(provider)

    result = await provider.recommendations(wanted={"recently_played"})

    gql.get_recently_played.assert_awaited_once()
    gql.get_recommendations.assert_not_awaited()
    gql.get_flow_config_tracks.assert_not_awaited()
    gql.get_made_for_me.assert_not_awaited()
    gql.get_flow_configs.assert_not_awaited()
    assert [f.item_id for f in result] == ["recently_played"]


@pytest.mark.asyncio
async def test_recommendations_wanted_new_releases_issues_shared_fetch(
    provider: DeezerProvider,
) -> None:
    """wanted={new_releases} still issues the shared get_recommendations fetch."""
    _install_cache_mocks(provider)
    gql = _stub_gql_client(provider)

    result = await provider.recommendations(wanted={"new_releases"})

    gql.get_recommendations.assert_awaited_once()
    gql.get_flow_config_tracks.assert_not_awaited()
    gql.get_made_for_me.assert_not_awaited()
    gql.get_flow_configs.assert_not_awaited()
    gql.get_recently_played.assert_not_awaited()
    # the shared fetch feeds all four rows; the controller filters to the wanted ones
    assert {f.item_id for f in result} == {
        "recommended_playlists",
        "recommended_artist_playlists",
        "recommended_tracks",
        "new_releases",
    }


@pytest.mark.asyncio
async def test_recommendations_wanted_genre_flows_issues_flow_configs_fetch(
    provider: DeezerProvider,
) -> None:
    """wanted={genre_flows} still issues the get_flow_configs fetch shared with mood_flows."""
    _install_cache_mocks(provider)
    gql = _stub_gql_client(provider)

    result = await provider.recommendations(wanted={"genre_flows"})

    gql.get_flow_configs.assert_awaited_once()
    gql.get_recommendations.assert_not_awaited()
    gql.get_flow_config_tracks.assert_not_awaited()
    gql.get_made_for_me.assert_not_awaited()
    gql.get_recently_played.assert_not_awaited()
    # one fetch feeds both flow rows; the controller filters to the wanted one
    assert {f.item_id for f in result} == {"mood_flows", "genre_flows"}
