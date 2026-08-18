"""Shared fixtures for VRT MAX provider tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Iterable
from typing import Any, TypeVar
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import ProviderType

from music_assistant.providers.vrt_max.provider import VrtMaxProvider
from tests.common import use_real_create_task

_T = TypeVar("_T")


@pytest.fixture
def provider() -> VrtMaxProvider:
    """Return a VrtMaxProvider with mocked client/auth and a neutralized cache."""
    mass = MagicMock()
    # Force every @use_cache lookup to miss so the real method body always runs.
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    mass.cache.set = AsyncMock()
    use_real_create_task(mass)
    manifest = Mock()
    manifest.domain = "vrt_max"
    manifest.type = ProviderType.MUSIC
    config = Mock()
    config.instance_id = "vrt_max--test"
    config.name = "VRT MAX"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {}.get(key, default)
    prov = VrtMaxProvider(mass, manifest, config)
    prov._client = MagicMock()
    prov._auth = Mock()
    prov._auth.enabled = False
    return prov


def async_gen(items: Iterable[_T]) -> Callable[..., AsyncGenerator[_T]]:
    """Return an async-generator function yielding `items`, ignoring call args."""

    async def _gen(*_args: Any, **_kwargs: Any) -> AsyncGenerator[_T]:
        for item in items:
            yield item

    return _gen


def async_gen_then_raise(items: Iterable[_T], exc: Exception) -> Callable[..., AsyncGenerator[_T]]:
    """Return an async-generator function yielding `items` then raising `exc`."""

    async def _gen(*_args: Any, **_kwargs: Any) -> AsyncGenerator[_T]:
        for item in items:
            yield item
        raise exc

    return _gen
