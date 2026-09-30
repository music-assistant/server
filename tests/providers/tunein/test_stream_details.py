"""Tests for TuneIn stream resolution and its cache."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from music_assistant_models.enums import MediaType

from music_assistant.providers.tunein import TuneInProvider

OLD_URL = "http://icecast2.play.cz/rockzone128.mp3"
NEW_URL = "https://stream.sepia.sk/rockzone128.mp3"


def _tune_response(url: str | None) -> dict[str, Any]:
    body = [] if url is None else [{"element": "audio", "url": url, "media_type": "mp3"}]
    return {"head": {"status": "200"}, "body": body}


def _mock_cache(provider: TuneInProvider) -> None:
    store: dict[tuple[str, int], Any] = {}

    async def _get(key: str, category: int = 0, **_: Any) -> Any:
        return store.get((key, category))

    async def _set(key: str, data: Any, category: int = 0, **_: Any) -> None:
        store[(key, category)] = data

    provider.mass.cache.get = AsyncMock(side_effect=_get)  # type: ignore[method-assign]
    provider.mass.cache.set = AsyncMock(side_effect=_set)  # type: ignore[method-assign]


def _mock_tune(provider: TuneInProvider, url: str | None) -> AsyncMock:
    get_data = AsyncMock(return_value=_tune_response(url))
    provider._TuneInProvider__get_data = get_data  # type: ignore[attr-defined]
    return get_data


async def test_playback_uses_current_stream_url(provider: TuneInProvider) -> None:
    """Test that playback picks up a stream url TuneIn changed after it was cached."""
    _mock_cache(provider)
    get_data = _mock_tune(provider, OLD_URL)
    await provider._get_stream_info("s84091")

    get_data.return_value = _tune_response(NEW_URL)
    details = await provider.get_stream_details("s84091", MediaType.RADIO)

    assert details.path == NEW_URL
    assert await provider._get_stream_info("s84091") == _tune_response(NEW_URL)["body"]


async def test_playback_falls_back_to_cached_streams(provider: TuneInProvider) -> None:
    """Test that playback uses the cached streams when TuneIn returns none."""
    _mock_cache(provider)
    get_data = _mock_tune(provider, OLD_URL)
    await provider._get_stream_info("s84091")

    get_data.return_value = None
    details = await provider.get_stream_details("s84091", MediaType.RADIO)

    assert details.path == OLD_URL


async def test_empty_stream_list_is_not_cached(provider: TuneInProvider) -> None:
    """Test that an empty Tune.ashx answer does not stick in the cache."""
    _mock_cache(provider)
    get_data = _mock_tune(provider, None)
    assert await provider._get_stream_info("s25127") == []

    get_data.return_value = _tune_response(NEW_URL)
    assert await provider._get_stream_info("s25127") == _tune_response(NEW_URL)["body"]
