"""Tests for the NetEase provider's scrobble (listening check-in) method."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.neteasecloudmusic import NeteaseCloudMusicProvider
from tests.common import use_real_create_task

_SONG_DETAIL = {"songs": [{"id": 42, "al": {"id": 7, "name": "Album"}}]}


def _stub_client(provider: NeteaseCloudMusicProvider, detail: dict[str, Any]) -> AsyncMock:
    """Attach a client.get stub returning canned payloads keyed by path."""

    async def _fake(path: str, **_kwargs: Any) -> dict[str, Any]:
        return detail if path == "/song/detail" else {"code": 200}

    mock = AsyncMock(side_effect=_fake)
    provider._client = Mock(get=mock)
    return mock


@pytest.fixture
def cached(provider: NeteaseCloudMusicProvider) -> NeteaseCloudMusicProvider:
    """Create a provider with a dict-backed cache and real background task scheduling."""
    store: dict[str, Any] = {}

    async def get_with_freshness(key: str, **_kwargs: Any) -> tuple[Any, bool, bool]:
        return (store.get(key), key in store, key in store)

    async def set_(key: str, data: Any, **_kwargs: Any) -> None:
        store[key] = data

    provider.mass.cache = Mock()
    provider.mass.cache.get_with_freshness = AsyncMock(side_effect=get_with_freshness)
    provider.mass.cache.set = AsyncMock(side_effect=set_)
    use_real_create_task(provider.mass)
    return provider


async def test_scrobble_sends_track_album_and_time(cached: NeteaseCloudMusicProvider) -> None:
    """Scrobble posts the track id, album id (sourceid) and played seconds."""
    client = _stub_client(cached, _SONG_DETAIL)

    await cached.scrobble("42", 180)

    scrobble = next(c for c in client.await_args_list if c.args[0] == "/scrobble")
    assert scrobble.kwargs["params"] == {
        "id": "42",
        "sourceid": "7",
        "time": 180,
        "cookie": "MUSIC_U=test",
    }
    assert scrobble.kwargs["cookie"] == "MUSIC_U=test"


async def test_scrobble_accepts_the_album_field(cached: NeteaseCloudMusicProvider) -> None:
    """Compatible backends return the album under "album" instead of "al"."""
    client = _stub_client(cached, {"songs": [{"id": 42, "album": {"id": 99}}]})

    await cached.scrobble("42", 10)

    scrobble = next(c for c in client.await_args_list if c.args[0] == "/scrobble")
    assert scrobble.kwargs["params"]["sourceid"] == "99"


async def test_scrobble_without_album_raises(cached: NeteaseCloudMusicProvider) -> None:
    """No album means nothing to check in against, so it raises for a later retry."""
    _stub_client(cached, {"songs": [{"id": 42}]})

    with pytest.raises(InvalidDataError):
        await cached.scrobble("42", 10)


async def test_scrobble_propagates_api_errors(cached: NeteaseCloudMusicProvider) -> None:
    """A failing /song/detail propagates instead of silently skipping the play."""
    cached._client = Mock(get=AsyncMock(side_effect=RuntimeError("boom")))

    with pytest.raises(RuntimeError):
        await cached.scrobble("42", 10)


async def test_album_id_is_served_from_cache(provider: NeteaseCloudMusicProvider) -> None:
    """A cached album id is returned without hitting /song/detail again."""
    store: dict[str, Any] = {"_get_track_album_id.42": "7"}

    async def get_with_freshness(key: str, **_kwargs: Any) -> tuple[Any, bool, bool]:
        return (store.get(key), key in store, key in store)

    provider.mass.cache = Mock()
    provider.mass.cache.get_with_freshness = AsyncMock(side_effect=get_with_freshness)
    provider.mass.cache.set = AsyncMock()
    use_real_create_task(provider.mass)
    client = _stub_client(provider, _SONG_DETAIL)

    album_id = await provider._get_track_album_id("42")

    assert album_id == "7"
    assert [c for c in client.await_args_list if c.args[0] == "/song/detail"] == []
