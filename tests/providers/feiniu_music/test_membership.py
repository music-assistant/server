"""Complete library reads belong to sync, not ordinary per-item requests."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError, ProviderPermissionDenied

from .test_provider import MemoryCache, track_data
from .test_provider import provider as provider  # noqa: PLC0414


class ClockCache(MemoryCache):
    """Honor the existing detail/playlist cache's expiry with a controlled clock."""

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


async def test_consecutive_playback_never_scans_library(provider: Any) -> None:
    """Missing membership and both short/long elapsed times need only per-track metadata."""
    clock = [0.0]
    provider.mass.cache = ClockCache(clock)

    async def detail(kind: str, item_id: str) -> dict[str, Any]:
        assert kind == "track"
        return {"track": {**track_data(), "guid": item_id}}

    async def audio(_item_id: str) -> Any:
        yield b"synthetic-audio"

    provider._client.detail = AsyncMock(side_effect=detail)
    provider._client.audio_stream = audio
    for elapsed, item_id in ((0, "track-0"), (31, "track-0"), (901, "track-1"), (3601, "track-2")):
        clock[0] = elapsed
        details = await provider.get_stream_details(item_id, MediaType.TRACK)
        assert [chunk async for chunk in provider.get_audio_stream(details)] == [b"synthetic-audio"]
        track = await provider.get_track(item_id)
        assert await provider.resolve_image(track.metadata.images[0].path) == b"synthetic-image"
        await asyncio.sleep(0)
    assert provider._client.detail.await_count == 4
    provider._client.page.assert_not_awaited()
    provider._client.playlists.assert_not_awaited()
    assert "SECRET-TOKEN" not in repr(provider.mass.cache.entries)
    assert "/private-nas-home" not in repr(provider.mass.cache.entries)


async def test_runtime_uses_native_status_after_detail_expiry(provider: Any) -> None:
    """A prior full sync cannot authorize a track whose native status has changed."""
    clock = [0.0]
    provider.mass.cache = ClockCache(clock)
    assert len([item async for item in provider.get_library_tracks()]) == 1
    assert (await provider.get_track("track-test")).item_id == "track-test"
    await asyncio.sleep(0)
    provider._client.page.reset_mock()
    provider._client.detail = AsyncMock(return_value={"track": {**track_data(), "accessStatus": 2}})
    clock[0] = 31
    with pytest.raises(ProviderPermissionDenied):
        await provider.get_track("track-test")
    provider._client.page.assert_not_awaited()
    provider._client.detail.assert_awaited_once()


async def test_complete_sync_does_not_replace_native_runtime_checks(provider: Any) -> None:
    """Runtime still reads native metadata after a complete library sync."""
    assert len([item async for item in provider.get_library_tracks()]) == 1
    provider._client.page.reset_mock()
    assert (await provider.get_stream_details("track-test", MediaType.TRACK)).duration == 125
    provider._client.detail.assert_awaited_once_with("track", "track-test")
    provider._client.page.assert_not_awaited()


@pytest.mark.parametrize("kind", ["track", "album", "artist", "playlist"])
async def test_detail_without_sync_validates_identity(provider: Any, kind: str) -> None:
    """Native details, not an implicit full list, establish the requested object."""
    row = {
        "guid": "item",
        "name": "Synthetic",
        "title": "Synthetic",
        "coverId": "cover",
        "accessStatus": 0,
    }
    provider._client.detail = AsyncMock(return_value={"track": row} if kind == "track" else row)
    item = await getattr(provider, "get_" + kind)("item")
    assert item.name == "Synthetic"
    provider._client.detail.assert_awaited_once_with(kind, "item")
    assert item.metadata.images[0].path == f"scoped/test-scope/{kind}/item/cover"
    provider._client.page.assert_not_awaited()
    provider._client.playlists.assert_not_awaited()
    provider._cache_id = "new-load"
    provider._client.detail = AsyncMock(return_value={"guid": "wrong", "track": {"guid": "wrong"}})
    with pytest.raises(InvalidDataError):
        await getattr(provider, "get_" + kind)("item")


async def test_relationships_and_playlist_pages_do_not_read_membership(provider: Any) -> None:
    """Native playlist contents are reused across MA pages, without per-member metadata."""
    clock = [0.0]
    provider.mass.cache = ClockCache(clock)

    async def related(kind: str, _item_id: str, page: int, **_kwargs: Any) -> dict[str, Any]:
        if kind == "playlist":
            return {"list": [track_data()] * (100 if page == 1 else 2), "total": 102}
        row = {"guid": "album-test", "name": "Album"} if kind == "artist" else track_data()
        if kind == "album":
            row.pop("accessStatus")
        return {"list": [row], "total": 1}

    provider._client.related = AsyncMock(side_effect=related)
    for _ in range(2):
        assert len(await provider.get_album_tracks("album-test")) == 1
        assert len(await provider.get_artist_albums("artist-test")) == 1
        first = await provider.get_playlist_tracks("playlist-test", 0)
        last = await provider.get_playlist_tracks("playlist-test", 1)
        assert len(first) == 100
        assert len(last) == 2
        assert last[-1].position == 102
        await asyncio.sleep(0)
        clock[0] += 31
    assert provider._client.related.await_count == 8
    provider._client.page.assert_not_awaited()
    provider._client.playlists.assert_not_awaited()
    provider._client.detail.assert_not_awaited()
