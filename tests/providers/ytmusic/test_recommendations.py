"""Test YouTube Music's two-method recommendations contract."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping, RecommendationFolder, UniqueList

from music_assistant.providers.ytmusic import YoutubeMusicProvider

GET_HOME_PATH = "music_assistant.providers.ytmusic.get_home"


def _make_home_data() -> list[dict[str, Any]]:
    """
    Build two home sections: one with a parseable playlist item, one empty.

    Enough to assert the server-derived folder item_ids and per-row item extraction.
    Built fresh per call because the provider's parser mutates the raw item dicts.
    """
    return [
        {
            "title": "Listen again",
            "contents": [{"playlistId": "PL123", "title": "Morning Mix"}],
        },
        {"title": "Quick picks", "contents": []},
    ]


class _FakeCache:
    """Dict-backed stand-in for the mass cache and task scheduling used by @use_cache."""

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}
        self.background_tasks: list[asyncio.Future[Any]] = []

    async def get_with_freshness(self, key: str, **kwargs: Any) -> tuple[Any, bool, bool]:
        if key in self.store:
            return self.store[key], True, True
        return None, False, False

    async def set(self, key: str, data: Any, **kwargs: Any) -> None:
        self.store[key] = data

    def create_task(self, target: Any, *args: Any, **kwargs: Any) -> asyncio.Future[Any]:
        task: asyncio.Future[Any] = asyncio.ensure_future(target)
        self.background_tasks.append(task)
        return task

    async def flush(self) -> None:
        """Let scheduled background cache-store tasks complete."""
        await asyncio.gather(*self.background_tasks)


@pytest.fixture
def fake_cache() -> _FakeCache:
    """Return a fresh fake cache."""
    return _FakeCache()


@pytest.fixture
def provider(fake_cache: _FakeCache) -> YoutubeMusicProvider:
    """Return a YoutubeMusicProvider instance with mocked dependencies."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "ytmusic"
    config = MagicMock()
    config.instance_id = "ytmusic--test"
    config.get_value.return_value = "GLOBAL"
    prov = YoutubeMusicProvider(mass, manifest, config)
    prov._headers = {}
    prov._yt_user = None
    prov.language = "en"
    mass.cache.get_with_freshness = fake_cache.get_with_freshness
    mass.cache.set = fake_cache.set
    mass.create_task = fake_cache.create_task
    return prov


def _stub_mixed_for_you(provider: YoutubeMusicProvider) -> AsyncMock:
    """Attach a _get_mixed_for_you_folder stub returning a folder with one item."""
    folder = RecommendationFolder(
        name="Mixed for you",
        item_id=f"{provider.instance_id}_mixed_for_you",
        provider=provider.instance_id,
        icon="mdi:shuffle-variant",
    )
    folder.items.append(
        ItemMapping(
            media_type=MediaType.PLAYLIST,
            item_id="RDTMAK5uy_mix1",
            provider=provider.instance_id,
            name="My Mix 1",
        )
    )
    mock = AsyncMock(return_value=folder)
    provider._get_mixed_for_you_folder = mock  # type: ignore[method-assign]
    return mock


async def test_get_recommendations_returns_rows_without_items(
    provider: YoutubeMusicProvider,
    fake_cache: _FakeCache,
) -> None:
    """The rows call returns home section rows plus the static mixed_for_you descriptor."""
    mixed_mock = _stub_mixed_for_you(provider)
    with patch(
        GET_HOME_PATH, new_callable=AsyncMock, return_value=_make_home_data()
    ) as get_home_mock:
        result = await provider.get_recommendations()
    await fake_cache.flush()

    get_home_mock.assert_awaited_once()
    # the mixed_for_you row is a static descriptor: its dedicated fetch must not run
    mixed_mock.assert_not_awaited()
    assert [f.item_id for f in result] == [
        f"{provider.instance_id}_Listen again",
        f"{provider.instance_id}_Quick picks",
        f"{provider.instance_id}_mixed_for_you",
    ]
    assert all(len(f.items) == 0 for f in result)
    assert result[0].name == "Listen again"
    mixed_row = result[2]
    assert mixed_row.name == "Mixed for you"
    assert mixed_row.translation_key == "mixed_for_you"
    assert mixed_row.icon == "mdi:shuffle-variant"
    assert mixed_row.provider == provider.instance_id


async def test_rows_and_items_share_one_cached_payload_fetch(
    provider: YoutubeMusicProvider,
    fake_cache: _FakeCache,
) -> None:
    """The rows call and a later items call are served from one cached payload fetch."""
    with patch(
        GET_HOME_PATH, new_callable=AsyncMock, return_value=_make_home_data()
    ) as get_home_mock:
        rows = await provider.get_recommendations()
        # let the background cache-store task complete before the next call
        await fake_cache.flush()
        items = await provider.get_recommendation_items(f"{provider.instance_id}_Listen again")

    assert get_home_mock.await_count == 1
    assert len(rows) == 3
    assert [item.item_id for item in items] == ["PL123"]


async def test_get_recommendation_items_section_row(
    provider: YoutubeMusicProvider,
    fake_cache: _FakeCache,
) -> None:
    """An items call for a home section row fetches the payload, not the mixed_for_you chain."""
    mixed_mock = _stub_mixed_for_you(provider)
    with patch(
        GET_HOME_PATH, new_callable=AsyncMock, return_value=_make_home_data()
    ) as get_home_mock:
        items = await provider.get_recommendation_items(f"{provider.instance_id}_Listen again")
    await fake_cache.flush()

    get_home_mock.assert_awaited_once()
    mixed_mock.assert_not_awaited()
    assert [item.item_id for item in items] == ["PL123"]
    assert items[0].name == "Morning Mix"


async def test_get_recommendation_items_mixed_for_you_row(
    provider: YoutubeMusicProvider,
) -> None:
    """An items call for the mixed_for_you row uses its dedicated fetch, not get_home."""
    mixed_mock = _stub_mixed_for_you(provider)
    with patch(
        GET_HOME_PATH, new_callable=AsyncMock, return_value=_make_home_data()
    ) as get_home_mock:
        items = await provider.get_recommendation_items(f"{provider.instance_id}_mixed_for_you")

    get_home_mock.assert_not_awaited()
    mixed_mock.assert_awaited_once()
    assert [item.item_id for item in items] == ["RDTMAK5uy_mix1"]


async def test_get_recommendation_items_unknown_id_returns_empty(
    provider: YoutubeMusicProvider,
    fake_cache: _FakeCache,
) -> None:
    """An items call for an unknown row id returns an empty UniqueList."""
    with patch(GET_HOME_PATH, new_callable=AsyncMock, return_value=_make_home_data()):
        result = await provider.get_recommendation_items(f"{provider.instance_id}_bogus")
    await fake_cache.flush()

    assert isinstance(result, UniqueList)
    assert len(result) == 0
