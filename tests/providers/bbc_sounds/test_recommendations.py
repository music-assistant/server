"""Test BBC Sounds get_recommendations / get_recommendation_items via the payload mixin."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from auntie_sounds import SoundsClient
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping, RecommendationFolder
from music_assistant_models.unique_list import UniqueList

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
    # Reading an attribute the spec doesn't have raises, so configuring the method
    # through its existing attribute catches a rename here instead of at runtime
    client = Mock(spec=SoundsClient)
    client.get_recommendation_folders.return_value = sub_items
    client.is_signed_in = True
    provider.client = client
    conversions = dict(zip(sub_items, payload, strict=True))
    adaptor = Mock()
    adaptor.new_object = AsyncMock(side_effect=lambda item, **_kwargs: conversions[item])
    provider.adaptor = adaptor
    return client


@pytest.mark.asyncio
@pytest.mark.usefixtures("cache_tasks")
async def test_get_recommendations_returns_rows_without_items(
    provider: BBCSoundsProvider,
) -> None:
    """get_recommendations returns the payload folders as rows, stripped of items."""
    client = _stub_api(provider, _make_payload())

    rows = await provider.get_recommendations()

    client.get_recommendation_folders.assert_awaited_once_with()
    assert [row.item_id for row in rows] == [MUSIC_ROW_ID, PODCAST_ROW_ID]
    assert all(len(row.items) == 0 for row in rows)


@pytest.mark.asyncio
async def test_rows_and_items_share_one_payload_fetch(
    provider: BBCSoundsProvider,
    cache_tasks: list[asyncio.Future[Any]],
) -> None:
    """Items calls for both rows are served from the payload cached by the rows call."""
    payload = _make_payload()
    client = _stub_api(provider, payload)

    rows = await provider.get_recommendations()
    # let the background cache-store task complete before the items calls
    await asyncio.gather(*cache_tasks)
    music_items = await provider.get_recommendation_items(MUSIC_ROW_ID)
    podcast_items = await provider.get_recommendation_items(PODCAST_ROW_ID)

    client.get_recommendation_folders.assert_awaited_once()
    assert len(rows) == 2
    assert music_items == payload[0].items
    assert podcast_items == payload[1].items


@pytest.mark.asyncio
@pytest.mark.usefixtures("cache_tasks")
async def test_get_recommendation_items_unknown_id_returns_empty(
    provider: BBCSoundsProvider,
) -> None:
    """An item_id not present in the payload yields an empty UniqueList."""
    _stub_api(provider, _make_payload())

    result = await provider.get_recommendation_items("bogus_row")

    assert isinstance(result, UniqueList)
    assert len(result) == 0


@pytest.mark.asyncio
@pytest.mark.usefixtures("cache_tasks")
async def test_not_logged_in_returns_empty_without_backend_calls(
    provider: BBCSoundsProvider,
) -> None:
    """Without a login, both methods return empty and never touch the backend."""
    client = _stub_api(provider, _make_payload())
    client.is_signed_in = False

    rows = await provider.get_recommendations()
    items = await provider.get_recommendation_items(MUSIC_ROW_ID)

    client.get_recommendation_folders.assert_not_awaited()
    assert rows == []
    assert isinstance(items, UniqueList)
    assert len(items) == 0
