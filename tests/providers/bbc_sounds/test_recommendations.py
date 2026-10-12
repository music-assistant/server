"""Test BBC Sounds get_recommendations / get_recommendation_items via the payload mixin."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping, RecommendationFolder
from music_assistant_models.unique_list import UniqueList
from sounds import MenuRecommendationOptions, SoundsClient

from music_assistant.providers.bbc_sounds import BBCSoundsProvider

MUSIC_ROW_ID = "listen_to_music"
PODCAST_ROW_ID = "recommended_podcasts"


def _folder(item_id: str, name: str) -> RecommendationFolder:
    """Build a payload folder with one item, as the adaptor would produce it."""
    return RecommendationFolder(
        item_id=item_id,
        provider="bbc_sounds",
        name=name,
        items=UniqueList(
            [
                ItemMapping(
                    media_type=MediaType.TRACK,
                    item_id=f"{item_id}_item",
                    provider="bbc_sounds",
                    name=f"{name} item",
                )
            ]
        ),
    )


def _make_payload() -> list[RecommendationFolder]:
    """Build the two-folder payload used by all tests."""
    return [
        _folder(MUSIC_ROW_ID, "Listen to Music"),
        _folder(PODCAST_ROW_ID, "Recommended Podcasts"),
    ]


def _stub_api(provider: BBCSoundsProvider, payload: list[RecommendationFolder]) -> Mock:
    """Attach a stubbed sounds client + adaptor serving the given payload folders."""
    sub_items = [Mock() for _ in payload]
    menu = Mock()
    menu.sub_items = sub_items
    # spec_set so a renamed/removed client method fails here instead of at runtime
    client = Mock(spec_set=SoundsClient)
    client.get_menu.return_value = menu
    provider.client = client
    conversions = dict(zip(sub_items, payload, strict=True))
    adaptor = Mock()
    adaptor.new_object = AsyncMock(side_effect=lambda item, **_kwargs: conversions[item])
    provider.adaptor = adaptor
    return client


def _install_cache_mocks(provider: BBCSoundsProvider) -> list[asyncio.Future[Any]]:
    """Back the mixin's @use_cache with a dict store; return the background store tasks."""
    store: dict[str, Any] = {}
    background_tasks: list[asyncio.Future[Any]] = []

    async def _cache_get(key: str, **_kwargs: Any) -> tuple[Any, bool, bool]:
        if key in store:
            return store[key], True, True
        return None, False, False

    async def _cache_set(key: str, data: Any, **_kwargs: Any) -> None:
        store[key] = data

    def _create_task(target: Any, *_args: Any, **_kwargs: Any) -> asyncio.Future[Any]:
        task: asyncio.Future[Any] = asyncio.ensure_future(target)
        background_tasks.append(task)
        return task

    provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        side_effect=_cache_get
    )
    provider.mass.cache.set = AsyncMock(side_effect=_cache_set)  # type: ignore[method-assign]
    provider.mass.create_task = Mock(side_effect=_create_task)  # type: ignore[method-assign]
    return background_tasks


@pytest.mark.asyncio
async def test_get_recommendations_returns_rows_without_items(
    provider: BBCSoundsProvider,
) -> None:
    """get_recommendations returns the payload folders as rows, stripped of items."""
    _install_cache_mocks(provider)
    client = _stub_api(provider, _make_payload())

    rows = await provider.get_recommendations()

    client.get_menu.assert_awaited_once_with(recommendations=MenuRecommendationOptions.ONLY)
    assert [row.item_id for row in rows] == [MUSIC_ROW_ID, PODCAST_ROW_ID]
    assert all(len(row.items) == 0 for row in rows)


@pytest.mark.asyncio
async def test_rows_and_items_share_one_payload_fetch(
    provider: BBCSoundsProvider,
) -> None:
    """Items calls for both rows are served from the payload cached by the rows call."""
    background_tasks = _install_cache_mocks(provider)
    payload = _make_payload()
    client = _stub_api(provider, payload)

    rows = await provider.get_recommendations()
    # let the background cache-store task complete before the items calls
    await asyncio.gather(*background_tasks)
    music_items = await provider.get_recommendation_items(MUSIC_ROW_ID)
    podcast_items = await provider.get_recommendation_items(PODCAST_ROW_ID)

    client.get_menu.assert_awaited_once()
    assert len(rows) == 2
    assert music_items == payload[0].items
    assert podcast_items == payload[1].items


@pytest.mark.asyncio
async def test_get_recommendation_items_unknown_id_returns_empty(
    provider: BBCSoundsProvider,
) -> None:
    """An item_id not present in the payload yields an empty UniqueList."""
    _install_cache_mocks(provider)
    _stub_api(provider, _make_payload())

    result = await provider.get_recommendation_items("bogus_row")

    assert isinstance(result, UniqueList)
    assert len(result) == 0


@pytest.mark.asyncio
async def test_not_logged_in_returns_empty_without_backend_calls(
    provider: BBCSoundsProvider,
) -> None:
    """Without a login, both methods return empty and never touch the backend."""
    _install_cache_mocks(provider)
    client = _stub_api(provider, _make_payload())
    provider.logged_in = False

    rows = await provider.get_recommendations()
    items = await provider.get_recommendation_items(MUSIC_ROW_ID)

    client.get_menu.assert_not_awaited()
    assert rows == []
    assert isinstance(items, UniqueList)
    assert len(items) == 0
