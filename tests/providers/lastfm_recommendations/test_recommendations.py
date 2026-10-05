"""Tests for the Last.fm Recommendations two-method recommendations contract."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping, RecommendationFolder, UniqueList

from music_assistant.providers.lastfm_recommendations import (
    SUPPORTED_FEATURES,
    LastFMRecommendationsProvider,
)

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
