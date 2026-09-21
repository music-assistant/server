"""Bounded membership caching avoids whole-library reads during consecutive playback."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError, ProviderPermissionDenied

from music_assistant.providers.feiniu_music.provider import MEMBERSHIP_TTL

from .test_provider import MemoryCache, track_data
from .test_provider import provider as provider  # noqa: PLC0414


class ClockCache(MemoryCache):
    """Honor MA's requested expiry using the same controlled clock as membership."""

    def __init__(self, clock: list[float]) -> None:
        """Track expiration separately from serialized values."""
        super().__init__()
        self.clock = clock
        self.expires: dict[tuple[str, str], float] = {}

    async def set(self, key: str, data: Any, *, provider: str, **kwargs: Any) -> None:
        """Store with the real decorator's expiration."""
        await super().set(key, data, provider=provider, **kwargs)
        self.expires[provider, key] = self.clock[0] + kwargs["expiration"]

    async def get_with_freshness(
        self, key: str, *, provider: str, **kwargs: Any
    ) -> tuple[Any, bool, bool]:
        """Return no fresh data after its TTL."""
        if self.expires.get((provider, key), 0) <= self.clock[0]:
            return None, False, False
        return await super().get_with_freshness(key, provider=provider, **kwargs)


@pytest.mark.parametrize("count", [5000, 10000])
async def test_consecutive_playback_does_not_rescan_after_detail_expiry(
    provider: Any, monkeypatch: Any, count: int
) -> None:
    """Fifty/100 initial pages are reused after each 30-second detail expiration."""
    clock = [1000.0]
    monkeypatch.setattr(
        "music_assistant.providers.feiniu_music.provider.monotonic", lambda: clock[0]
    )
    provider.mass.cache = ClockCache(clock)

    def row(index: int) -> dict[str, Any]:
        return {**track_data(), "guid": f"track-{index}"}

    async def page(kind: str, number: int) -> dict[str, Any]:
        assert kind == "track"
        return {
            "list": [row(index) for index in range((number - 1) * 100, min(number * 100, count))],
            "total": count,
        }

    async def detail(kind: str, item_id: str) -> dict[str, Any]:
        assert kind == "track"
        return {"track": row(int(item_id.removeprefix("track-")))}

    async def audio(_item_id: str) -> Any:
        yield b"synthetic-audio"

    provider._client.page = AsyncMock(side_effect=page)
    provider._client.detail = AsyncMock(side_effect=detail)
    provider._client.audio_stream = audio
    for index in (0, 0, 1, 2):
        details = await provider.get_stream_details(f"track-{index}", MediaType.TRACK)
        assert [chunk async for chunk in provider.get_audio_stream(details)] == [b"synthetic-audio"]
        track = await provider.get_track(f"track-{index}")
        assert await provider.resolve_image(track.metadata.images[0].path) == b"synthetic-image"
        assert provider._client.page.await_count == count // 100
        await asyncio.sleep(0)  # Flush MA's asynchronous cache write before advancing time.
        clock[0] += 31
    assert provider._client.detail.await_count == 4
    _, scope = provider._memberships["track"]
    assert len(scope) == count
    assert all(isinstance(paths, set) for paths in scope.values())
    assert "SECRET-TOKEN" not in repr(provider.mass.cache.entries)
    assert "/private-nas-home" not in repr(provider.mass.cache.entries)


async def test_membership_refreshes_on_expiry_and_complete_sync(
    provider: Any, monkeypatch: Any
) -> None:
    """Neither missing IDs nor previous access are remembered forever."""
    clock = [1000.0]
    monkeypatch.setattr(
        "music_assistant.providers.feiniu_music.provider.monotonic", lambda: clock[0]
    )
    assert (await provider.get_track("track-test")).item_id == "track-test"
    new = {**track_data(), "guid": "new-track"}
    provider._client.page = AsyncMock(return_value={"list": [new], "total": 1})
    provider._client.detail = AsyncMock(return_value={"track": new})
    with pytest.raises(ProviderPermissionDenied):
        await provider.get_track("new-track")
    provider._client.page.assert_not_awaited()
    provider._client.detail.assert_not_awaited()
    clock[0] += MEMBERSHIP_TTL
    assert (await provider.get_track("new-track")).item_id == "new-track"
    provider._client.page.assert_awaited_once()
    with pytest.raises(ProviderPermissionDenied):
        await provider.get_track("track-test")
    # Explicit sync publishes a fresh scope even within the membership lifetime.
    provider._client.page = AsyncMock(return_value={"list": [], "total": 0})
    assert [item async for item in provider.get_library_tracks()] == []
    with pytest.raises(ProviderPermissionDenied):
        await provider.get_track("new-track")
    provider._client.detail.assert_awaited_once()


async def test_complete_sync_seeds_scope_without_retaining_full_details(provider: Any) -> None:
    """Playback and cover resolution reuse sync's IDs and cover associations."""
    assert len([item async for item in provider.get_library_tracks()]) == 1
    provider._client.page.reset_mock()
    details = await provider.get_stream_details("track-test", MediaType.TRACK)
    assert details.duration == 125
    await provider.resolve_image("scoped/test-scope/track/track-test/cover-test")
    provider._client.page.assert_not_awaited()
    assert provider._memberships["track"][1] == {
        "track-test": {"scoped/test-scope/track/track-test/cover-test"}
    }


@pytest.mark.parametrize("kind", ["track", "album", "artist", "playlist"])
async def test_detail_is_fetched_only_after_scope_and_validates_identity(
    provider: Any, kind: str
) -> None:
    """Native detail shapes are mapped without accepting a different returned ID."""
    row = {"guid": "item", "name": "Synthetic", "title": "Synthetic", "coverId": "cover"}
    provider._client.page = AsyncMock(return_value={"list": [row], "total": 1})
    provider._client.playlists = AsyncMock(return_value=[row])
    provider._client.detail = AsyncMock(return_value={"track": row} if kind == "track" else row)
    item = await getattr(provider, "get_" + kind)("item")
    assert item.name == "Synthetic"
    provider._client.detail.assert_awaited_once_with(kind, "item")
    assert item.metadata.images[0].path == f"scoped/test-scope/{kind}/item/cover"
    # Use another load key to force an uncached detail, while retaining proven membership.
    provider._cache_id = "new-load"
    provider._client.detail = AsyncMock(return_value={"guid": "wrong", "track": {"guid": "wrong"}})
    with pytest.raises(InvalidDataError):
        await getattr(provider, "get_" + kind)("item")


async def test_relationships_and_playlist_pages_reuse_membership(
    provider: Any, monkeypatch: Any
) -> None:
    """All relationship paths share scope; paging a playlist reuses native contents."""
    clock = [1000.0]
    monkeypatch.setattr(
        "music_assistant.providers.feiniu_music.provider.monotonic", lambda: clock[0]
    )
    provider.mass.cache = ClockCache(clock)
    rows = {
        "track": track_data(),
        "album": {"guid": "album-test", "name": "Album"},
        "artist": {"guid": "artist-test", "name": "Artist"},
    }

    async def page(kind: str, _page: int) -> dict[str, Any]:
        return {"list": [rows[kind]], "total": 1}

    async def related(kind: str, _item_id: str, page: int, **_kwargs: Any) -> dict[str, Any]:
        if kind == "playlist":
            return {"list": [track_data()] * (100 if page == 1 else 2), "total": 102}
        return {"list": [rows["album" if kind == "artist" else "track"]], "total": 1}

    provider._client.page = AsyncMock(side_effect=page)
    provider._client.related = AsyncMock(side_effect=related)
    for _ in range(2):
        assert len(await provider.get_album_tracks("album-test")) == 1
        assert len(await provider.get_artist_albums("artist-test")) == 1
        first = await provider.get_playlist_tracks("playlist-test", 0)
        last = await provider.get_playlist_tracks("playlist-test", 1)
        assert len(first) == 100
        assert len(last) == 2
        assert last[-1].position == 102
        assert provider._client.page.await_count == 3
        await asyncio.sleep(0)
        clock[0] += 31
    playlist_calls = [
        call for call in provider._client.related.await_args_list if call.args[0] == "playlist"
    ]
    assert len(playlist_calls) == 4  # Two native pages per content refresh, not per MA page.
    provider._client.playlists.assert_awaited_once()
    provider._client.detail.assert_not_awaited()
