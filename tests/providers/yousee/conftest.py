"""Shared fixtures for YouSee Musik provider tests."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from music_assistant.providers.yousee import SUPPORTED_FEATURES
from music_assistant.providers.yousee.provider import YouSeeMusikProvider


@pytest.fixture
def background_tasks() -> list[asyncio.Future[Any]]:
    """Collect the tasks scheduled via mass.create_task so tests can await them."""
    return []


@pytest.fixture
def provider(background_tasks: list[asyncio.Future[Any]]) -> YouSeeMusikProvider:
    """Create a real YouSeeMusikProvider with mocked mass and a dict-backed cache."""
    mass = Mock()
    manifest = Mock()
    manifest.domain = "yousee"
    config = Mock()
    config.instance_id = "yousee--test123"
    config.name = "YouSee Test"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {
        "log_level": "GLOBAL",
        "yousee_quality": 320,
    }.get(key, default)

    cache_store: dict[str, Any] = {}

    async def _cache_get(key: str, **_kwargs: Any) -> tuple[Any, bool, bool]:
        if key in cache_store:
            return cache_store[key], True, True
        return None, False, False

    async def _cache_set(key: str, data: Any, **_kwargs: Any) -> None:
        cache_store[key] = data

    def _create_task(target: Any, *_args: Any, **_kwargs: Any) -> asyncio.Future[Any]:
        task: asyncio.Future[Any] = asyncio.ensure_future(target)
        background_tasks.append(task)
        return task

    mass.cache.get_with_freshness = AsyncMock(side_effect=_cache_get)
    mass.cache.set = AsyncMock(side_effect=_cache_set)
    mass.create_task = Mock(side_effect=_create_task)
    return YouSeeMusikProvider(mass, manifest, config, SUPPORTED_FEATURES)
