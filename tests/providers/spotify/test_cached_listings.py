"""Tests for the requests Spotify listings make that are cached as a whole."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

from music_assistant.controllers.cache import BYPASS_CACHE
from music_assistant.providers.spotify.provider import SpotifyProvider
from tests.common import use_real_create_task


def _make_provider(total: int) -> tuple[SpotifyProvider, AsyncMock]:
    """Return a Spotify provider with an empty cache whose API lists the given number of items."""
    provider = object.__new__(SpotifyProvider)
    provider.config = MagicMock(instance_id="spotify--test")
    provider.manifest = MagicMock(domain="spotify")
    provider.logger = MagicMock()
    provider.mass = MagicMock()
    provider.mass.cache.get = AsyncMock(return_value=None)
    provider.mass.cache.set = AsyncMock()
    provider.mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    use_real_create_task(provider.mass)

    async def _get_page(_endpoint: str, limit: int, offset: int, **_kwargs: Any) -> dict[str, Any]:
        items = [{"id": f"i{i}"} for i in range(offset, min(offset + limit, total))]
        return {"etag": "etag", "total": total, "items": items}

    get_data = AsyncMock(side_effect=_get_page)
    provider._get_data = get_data  # type: ignore[method-assign]
    return provider, get_data


def _make_album_provider(total: int) -> tuple[SpotifyProvider, AsyncMock]:
    """Return a Spotify provider with a working cache whose API serves an album of the given size."""
    provider, get_data = _make_provider(total)
    store: dict[str, Any] = {}

    async def _get_with_freshness(key: str, **kwargs: Any) -> tuple[Any, bool, bool]:
        if kwargs.get("allow_bypass") and BYPASS_CACHE.get():
            return None, False, False
        return store.get(key), key in store, key in store

    async def _set(key: str, data: Any, **_kwargs: Any) -> None:
        store[key] = data

    provider.mass.cache = MagicMock(
        get_with_freshness=AsyncMock(side_effect=_get_with_freshness),
        set=AsyncMock(side_effect=_set),
    )

    def _track(index: int) -> dict[str, Any]:
        return {
            "id": f"t{index}",
            "name": f"Track {index}",
            "duration_ms": 180000,
            "external_urls": {"spotify": f"https://open.spotify.com/track/t{index}"},
            "is_local": False,
            "is_playable": True,
            "explicit": False,
        }

    def _page(limit: int, offset: int) -> dict[str, Any]:
        end = min(offset + limit, total)
        return {
            "items": [_track(i) for i in range(offset, end)],
            "limit": limit,
            "next": "next-page" if end < total else None,
            "total": total,
        }

    async def _get_album(endpoint: str, **kwargs: Any) -> dict[str, Any]:
        if endpoint == "albums/album1/tracks":
            return _page(kwargs["limit"], kwargs["offset"])
        return {
            "id": "album1",
            "name": "Album",
            "album_type": "album",
            "artists": [],
            "external_urls": {"spotify": "https://open.spotify.com/album/album1"},
            "images": [{"url": "https://i.scdn.co/image/cover", "height": 640, "width": 640}],
            "tracks": _page(50, 0),
        }

    get_data.side_effect = _get_album
    return provider, get_data


async def test_album_tracks_take_one_request() -> None:
    """An album of up to 50 tracks is listed with a single request, each track with the album."""
    provider, get_data = _make_album_provider(total=30)

    tracks = await provider.get_album_tracks("album1")

    assert [track.item_id for track in tracks] == [f"t{i}" for i in range(30)]
    assert all(track.album is not None and track.album.name == "Album" for track in tracks)
    assert all(
        track.image and track.image.path == "https://i.scdn.co/image/cover" for track in tracks
    )
    assert get_data.await_args_list == [call("albums/album1")]


async def test_album_tracks_page_past_the_first_50() -> None:
    """An album of more than 50 tracks requests the tracks after the album's first page."""
    provider, get_data = _make_album_provider(total=60)

    tracks = await provider.get_album_tracks("album1")

    assert [track.item_id for track in tracks] == [f"t{i}" for i in range(60)]
    assert get_data.await_args_list == [
        call("albums/album1"),
        call("albums/album1/tracks", limit=50, offset=50),
    ]


async def test_album_and_its_tracks_share_one_request() -> None:
    """Getting an album and then its tracks costs a single request."""
    provider, get_data = _make_album_provider(total=30)

    await provider.get_album("album1")
    await provider.get_album_tracks("album1")

    assert get_data.await_args_list == [call("albums/album1")]


async def test_album_tracks_follow_a_refreshed_album() -> None:
    """Cached album tracks carry the name of the album after it was refreshed."""
    provider, get_data = _make_album_provider(total=30)
    await provider.get_album_tracks("album1")
    serve_album = get_data.side_effect

    async def _serve_renamed(endpoint: str, **kwargs: Any) -> dict[str, Any]:
        return {**await serve_album(endpoint, **kwargs), "name": "Renamed"}

    get_data.side_effect = _serve_renamed
    token = BYPASS_CACHE.set(True)
    try:
        await provider.get_album("album1")
    finally:
        BYPASS_CACHE.reset(token)

    tracks = await provider.get_album_tracks("album1")

    assert all(track.album is not None and track.album.name == "Renamed" for track in tracks)
    assert get_data.await_count == 2


async def test_audiobook_chapters_take_one_request() -> None:
    """An audiobook of up to 50 chapters is listed with a single request."""
    provider, get_data = _make_provider(total=30)

    chapters = await provider._get_audiobook_chapters_data("book1")

    assert [chapter["id"] for chapter in chapters] == [f"i{i}" for i in range(30)]
    assert get_data.await_args_list == [
        call("audiobooks/book1/chapters", limit=50, offset=0, market="from_token")
    ]
