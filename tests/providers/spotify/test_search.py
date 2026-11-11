"""Unit tests for the Spotify provider's search implementation."""

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import RetriesExhausted

from music_assistant.providers.spotify import provider as provider_module
from music_assistant.providers.spotify.provider import SpotifyProvider
from tests.common import use_real_create_task


def _make_track_obj(track_id: str, name: str) -> dict[str, Any]:
    return {
        "id": track_id,
        "name": name,
        "duration_ms": 1000,
        "disc_number": 1,
        "track_number": 1,
        "explicit": False,
        "is_local": False,
        "is_playable": True,
        "external_ids": {},
        "external_urls": {"spotify": f"https://open.spotify.com/track/{track_id}"},
        "artists": [
            {
                "id": "artist1",
                "name": "Test Artist",
                "external_urls": {"spotify": "https://open.spotify.com/artist/artist1"},
            }
        ],
    }


def _make_playlist_obj(playlist_id: str, name: str) -> dict[str, Any]:
    return {
        "id": playlist_id,
        "name": name,
        "collaborative": False,
        "owner": {"id": "spotify", "display_name": "Spotify"},
        "external_urls": {"spotify": f"https://open.spotify.com/playlist/{playlist_id}"},
        "images": [],
    }


@pytest.fixture
def get_data() -> AsyncMock:
    """Return an AsyncMock standing in for SpotifyProvider._get_data."""
    return AsyncMock()


@pytest.fixture
def provider(get_data: AsyncMock, monkeypatch: pytest.MonkeyPatch) -> SpotifyProvider:
    """Return a SpotifyProvider with a mocked _get_data, bypassing __init__."""
    prov = object.__new__(SpotifyProvider)
    # instance_id and domain are read-only properties backed by config/manifest
    prov.config = MagicMock(instance_id="spotify--test")
    prov.manifest = MagicMock(domain="spotify")
    prov.logger = MagicMock()
    prov._sp_user = None
    prov.dev_session_active = False

    mass = MagicMock()
    # bypass the use_cache decorator: always miss
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    mass.cache.set = AsyncMock()
    use_real_create_task(mass)
    prov.mass = mass

    monkeypatch.setattr(prov, "_get_data", get_data)
    return prov


def _respond(_endpoint: str, **kwargs: Any) -> dict[str, Any]:
    """Answer a search request with one item per requested type."""
    result: dict[str, Any] = {}
    types = kwargs["type"].split(",")
    if "track" in types:
        result["tracks"] = {"items": [_make_track_obj("t1", "Track")]}
    if "playlist" in types:
        result["playlists"] = {"items": [_make_playlist_obj("p1", "Playlist")]}
    return result


@pytest.mark.asyncio
async def test_search_without_dev_session_is_one_request(
    provider: SpotifyProvider, get_data: AsyncMock
) -> None:
    """Without a custom client ID all types go in one request, without forcing a session."""
    get_data.side_effect = _respond

    result = await provider.search("chill", [MediaType.TRACK, MediaType.PLAYLIST], limit=10)

    get_data.assert_awaited_once_with(
        "search",
        q="chill",
        type="track,playlist",
        limit=10,
        offset=0,
        use_global_session=False,
    )
    assert len(result.tracks) == 1
    assert len(result.playlists) == 1


@pytest.mark.asyncio
async def test_search_playlists_use_global_session_with_dev_session(
    provider: SpotifyProvider, get_data: AsyncMock
) -> None:
    """With a custom client ID playlists are searched on the global session, the rest is not."""
    provider.dev_session_active = True
    get_data.side_effect = _respond

    result = await provider.search("chill", [MediaType.TRACK, MediaType.PLAYLIST], limit=10)

    assert get_data.await_count == 2
    calls = {call.kwargs["type"]: call.kwargs for call in get_data.await_args_list}
    assert calls["track"]["use_global_session"] is False
    assert calls["playlist"]["use_global_session"] is True
    assert len(result.tracks) == 1
    assert len(result.playlists) == 1


@pytest.mark.asyncio
async def test_search_only_playlists_with_dev_session(
    provider: SpotifyProvider, get_data: AsyncMock
) -> None:
    """A playlist-only search makes a single request, on the global session."""
    provider.dev_session_active = True
    get_data.side_effect = _respond

    result = await provider.search("chill", [MediaType.PLAYLIST], limit=10)

    get_data.assert_awaited_once_with(
        "search",
        q="chill",
        type="playlist",
        limit=10,
        offset=0,
        use_global_session=True,
    )
    assert len(result.playlists) == 1


@pytest.mark.asyncio
async def test_search_playlists_fall_back_to_dev_session_when_global_is_limited(
    provider: SpotifyProvider, get_data: AsyncMock
) -> None:
    """A rate limited global session neither fails the search nor drops its playlists."""
    provider.dev_session_active = True

    async def side_effect(endpoint: str, **kwargs: Any) -> dict[str, Any]:
        if kwargs["use_global_session"]:
            raise RetriesExhausted("Not retrying, asked to wait 600 seconds")
        return _respond(endpoint, **kwargs)

    get_data.side_effect = side_effect

    result = await provider.search("chill", [MediaType.TRACK, MediaType.PLAYLIST], limit=10)

    assert len(result.tracks) == 1
    assert len(result.playlists) == 1


@pytest.mark.asyncio
async def test_search_playlists_fall_back_when_global_session_is_backing_off(
    provider: SpotifyProvider, get_data: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A global session that keeps retrying a rate limit is not waited out."""
    provider.dev_session_active = True
    monkeypatch.setattr(provider_module, "GLOBAL_SEARCH_TIMEOUT", 0.05)

    async def side_effect(endpoint: str, **kwargs: Any) -> dict[str, Any]:
        if kwargs["use_global_session"]:
            await asyncio.sleep(160)
        return _respond(endpoint, **kwargs)

    get_data.side_effect = side_effect

    async with asyncio.timeout(5):
        result = await provider.search("chill", [MediaType.TRACK, MediaType.PLAYLIST], limit=10)

    assert len(result.tracks) == 1
    assert len(result.playlists) == 1


def _respond_global_pages(failing_offset: int, **kwargs: Any) -> dict[str, Any] | None:
    """Answer a dev request, or a full global playlist page before the failing offset."""
    if not kwargs["use_global_session"]:
        playlists = [_make_playlist_obj("g0", "Playlist"), _make_playlist_obj("p1", "Playlist")]
        return {"playlists": {"items": playlists}}
    if kwargs["offset"] >= failing_offset:
        return None
    offset = kwargs["offset"]
    items = [_make_playlist_obj(f"g{offset + i}", "Playlist") for i in range(10)]
    return {"playlists": {"items": items}}


@pytest.mark.asyncio
async def test_search_playlists_fallback_keeps_earlier_global_pages(
    provider: SpotifyProvider, get_data: AsyncMock
) -> None:
    """A later global page that fails keeps the earlier ones, topped up without duplicates."""
    provider.dev_session_active = True

    async def side_effect(_endpoint: str, **kwargs: Any) -> dict[str, Any]:
        if (result := _respond_global_pages(10, **kwargs)) is None:
            raise RetriesExhausted("Not retrying, asked to wait 600 seconds")
        return result

    get_data.side_effect = side_effect

    result = await provider.search("chill", [MediaType.PLAYLIST], limit=20)

    assert [p.item_id for p in result.playlists] == [*(f"g{i}" for i in range(10)), "p1"]


@pytest.mark.asyncio
async def test_search_playlists_keep_throttled_global_pages_past_the_deadline(
    provider: SpotifyProvider, get_data: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Global pages fetched before the deadline are kept and topped up by the dev session."""
    provider.dev_session_active = True
    monkeypatch.setattr(provider_module, "GLOBAL_SEARCH_TIMEOUT", 0.05)

    async def side_effect(_endpoint: str, **kwargs: Any) -> dict[str, Any]:
        if (result := _respond_global_pages(20, **kwargs)) is None:
            # the global session serves one request per 2 seconds
            await asyncio.sleep(2)
            return {"playlists": {"items": []}}
        return result

    get_data.side_effect = side_effect

    result = await provider.search("chill", [MediaType.PLAYLIST], limit=25)

    assert [p.item_id for p in result.playlists] == [*(f"g{i}" for i in range(20)), "p1"]
    dev_calls = [c.kwargs for c in get_data.await_args_list if not c.kwargs["use_global_session"]]
    assert [(c["offset"], c["limit"]) for c in dev_calls] == [(0, 5)]


@pytest.mark.asyncio
async def test_search_skips_null_playlist_items(
    provider: SpotifyProvider, get_data: AsyncMock
) -> None:
    """Null items Spotify returns for inaccessible playlists are dropped."""
    get_data.return_value = {
        "playlists": {"items": [None, _make_playlist_obj("p1", "Playlist"), None]}
    }

    result = await provider.search("chill", [MediaType.PLAYLIST], limit=10)

    assert [p.item_id for p in result.playlists] == ["p1"]


@pytest.mark.asyncio
async def test_search_cache_uses_global_session_checksum(
    provider: SpotifyProvider, get_data: AsyncMock
) -> None:
    """Search only reuses cache entries written since playlists moved to the global session."""
    get_data.return_value = {}
    cache_get = cast("AsyncMock", provider.mass.cache.get_with_freshness)

    await provider.search("chill", [MediaType.PLAYLIST], limit=10)

    cache_get.assert_awaited_once()
    assert cache_get.await_args is not None
    assert cache_get.await_args.kwargs["checksum"] == "global_session_v1"
