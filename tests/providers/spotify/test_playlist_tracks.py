"""Tests for Spotify playlist track pagination."""

import asyncio
from collections.abc import Coroutine
from typing import Any, NamedTuple
from unittest.mock import AsyncMock, MagicMock, call

from music_assistant.providers.spotify.provider import SpotifyProvider

PLAYLIST_ID = "private-playlist"


class SpotifyPlaylistHarness(NamedTuple):
    """Container for a Spotify provider and its mocked API collaborators."""

    provider: SpotifyProvider
    get_playlist: AsyncMock
    requires_global: AsyncMock
    get_metadata: AsyncMock
    get_page: AsyncMock


def _make_provider(instance_id: str = "spotify--test") -> SpotifyPlaylistHarness:
    """Return a Spotify provider with isolated playlist API mocks."""
    provider = object.__new__(SpotifyProvider)
    provider.config = MagicMock(instance_id=instance_id)
    provider.manifest = MagicMock(domain="spotify")
    provider.logger = MagicMock()
    provider._sp_user = {"id": "test-user", "display_name": "Test User"}
    provider._playlist_pagination_snapshot = None
    provider._playlist_pagination_lock = asyncio.Lock()

    mass = MagicMock()
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    mass.cache.set = AsyncMock()

    def close_task(coro: Coroutine[Any, Any, Any], **_: Any) -> None:
        coro.close()

    mass.create_task = MagicMock(side_effect=close_task)
    provider.mass = mass

    get_playlist = AsyncMock()
    requires_global = AsyncMock(return_value=False)
    get_metadata = AsyncMock()
    get_page = AsyncMock()
    provider.get_playlist = get_playlist  # type: ignore[method-assign]
    provider._playlist_requires_global_token = requires_global  # type: ignore[method-assign]
    provider._get_paginated_meta = get_metadata  # type: ignore[method-assign]
    provider._get_data_with_caching = get_page  # type: ignore[method-assign]

    return SpotifyPlaylistHarness(
        provider,
        get_playlist,
        requires_global,
        get_metadata,
        get_page,
    )


def _spotify_track(track_id: str) -> dict[str, Any]:
    """Return the minimum Spotify track payload accepted by the parser."""
    return {
        "id": track_id,
        "name": track_id,
        "duration_ms": 180000,
        "external_urls": {"spotify": f"https://open.spotify.com/track/{track_id}"},
        "is_local": False,
        "is_playable": True,
        "explicit": False,
    }


def _playlist_page(total: int, track_id: str) -> dict[str, Any]:
    """Return a Spotify playlist page containing one playable track."""
    return {"total": total, "items": [{"track": _spotify_track(track_id)}]}


async def test_multipage_traversal_reuses_pagination_metadata() -> None:
    """A cold sequential traversal requests metadata once and each valid page once."""
    harness = _make_provider()
    harness.get_metadata.return_value = {"etag": "snapshot-etag", "total": 120}

    async def get_page(
        _endpoint: str, _cache_checksum: str | None, **kwargs: Any
    ) -> dict[str, Any]:
        return _playlist_page(120, f"track-{kwargs['offset']}")

    harness.get_page.side_effect = get_page

    pages = [
        await harness.provider.get_playlist_tracks(PLAYLIST_ID, page=page) for page in range(4)
    ]

    harness.get_metadata.assert_awaited_once_with(
        f"playlists/{PLAYLIST_ID}/items",
        limit=1,
        offset=0,
        use_global_session=False,
    )
    assert harness.get_page.await_args_list == [
        call(
            f"playlists/{PLAYLIST_ID}/items",
            "snapshot-etag",
            limit=50,
            offset=offset,
            use_global_session=False,
        )
        for offset in (0, 50, 100)
    ]
    assert [page[0].position for page in pages[:3]] == [1, 51, 101]
    assert pages[3] == []


async def test_concurrent_cold_pages_share_inflight_pagination_metadata() -> None:
    """Concurrent cache refreshes share one in-flight metadata request."""
    harness = _make_provider()
    metadata_started = asyncio.Event()
    release_metadata = asyncio.Event()

    async def get_metadata(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        metadata_started.set()
        await release_metadata.wait()
        return {"etag": "shared-etag", "total": 100}

    harness.get_metadata.side_effect = get_metadata
    harness.get_page.side_effect = [
        _playlist_page(100, "page-one-track"),
        _playlist_page(100, "page-zero-track"),
    ]

    page_one_task = asyncio.create_task(harness.provider.get_playlist_tracks(PLAYLIST_ID, page=1))
    await metadata_started.wait()
    page_zero_task = asyncio.create_task(harness.provider.get_playlist_tracks(PLAYLIST_ID, page=0))
    await asyncio.sleep(0)
    release_metadata.set()
    await asyncio.gather(page_one_task, page_zero_task)

    harness.get_metadata.assert_awaited_once()
    assert [args.args[1] for args in harness.get_page.await_args_list] == [
        "shared-etag",
        "shared-etag",
    ]


async def test_page_zero_refresh_replaces_pagination_snapshot() -> None:
    """A new page-zero request replaces both the ETag and total for later pages."""
    harness = _make_provider()
    harness.get_metadata.side_effect = [
        {"etag": "old-etag", "total": 120},
        {"etag": "new-etag", "total": 50},
    ]
    harness.get_page.side_effect = [
        _playlist_page(120, "old-track"),
        _playlist_page(50, "new-track"),
    ]

    await harness.provider.get_playlist_tracks(PLAYLIST_ID, page=0)
    await harness.provider.get_playlist_tracks(PLAYLIST_ID, page=0)
    guarded_page = await harness.provider.get_playlist_tracks(PLAYLIST_ID, page=1)

    assert harness.get_metadata.await_count == 2
    assert [args.args[1] for args in harness.get_page.await_args_list] == [
        "old-etag",
        "new-etag",
    ]
    assert guarded_page == []
    assert harness.get_page.await_count == 2


async def test_cold_nonzero_page_fetches_pagination_metadata() -> None:
    """A direct nonzero page request fetches metadata before requesting its page."""
    harness = _make_provider()
    harness.get_metadata.return_value = {"etag": "cold-etag", "total": 151}
    harness.get_page.return_value = _playlist_page(151, "page-two-track")

    tracks = await harness.provider.get_playlist_tracks(PLAYLIST_ID, page=2)

    harness.get_metadata.assert_awaited_once()
    harness.get_page.assert_awaited_once_with(
        f"playlists/{PLAYLIST_ID}/items",
        "cold-etag",
        limit=50,
        offset=100,
        use_global_session=False,
    )
    assert tracks[0].position == 101


async def test_playlist_identities_do_not_share_pagination_metadata() -> None:
    """Private playlists and liked songs each use their own pagination snapshot."""
    harness = _make_provider()
    liked_songs_id = harness.provider._get_liked_songs_playlist_id()
    harness.get_metadata.side_effect = [
        {"etag": "private-a-etag", "total": 100},
        {"etag": "private-b-etag", "total": 100},
        {"etag": "liked-etag", "total": 100},
    ]
    harness.get_page.side_effect = [
        _playlist_page(100, "private-a-track"),
        _playlist_page(100, "private-b-track"),
        _playlist_page(100, "liked-track"),
    ]

    await harness.provider.get_playlist_tracks("private-a", page=1)
    await harness.provider.get_playlist_tracks("private-b", page=1)
    await harness.provider.get_playlist_tracks(liked_songs_id, page=1)

    assert harness.get_metadata.await_args_list == [
        call(
            "playlists/private-a/items",
            limit=1,
            offset=0,
            use_global_session=False,
        ),
        call(
            "playlists/private-b/items",
            limit=1,
            offset=0,
            use_global_session=False,
        ),
        call("me/tracks", limit=1, offset=0, use_global_session=True),
    ]
    assert [args.args[1] for args in harness.get_page.await_args_list] == [
        "private-a-etag",
        "private-b-etag",
        "liked-etag",
    ]
    assert harness.get_playlist.await_count == 2
    assert harness.requires_global.await_count == 2


async def test_session_paths_do_not_share_pagination_metadata() -> None:
    """The same playlist refetches metadata when its required token path changes."""
    harness = _make_provider()
    harness.requires_global.side_effect = [False, True]
    harness.get_metadata.side_effect = [
        {"etag": "dev-etag", "total": 100},
        {"etag": "global-etag", "total": 100},
    ]
    harness.get_page.side_effect = [
        _playlist_page(100, "dev-track"),
        _playlist_page(100, "global-track"),
    ]

    await harness.provider.get_playlist_tracks(PLAYLIST_ID, page=1)
    await harness.provider.get_playlist_tracks(PLAYLIST_ID, page=1)

    assert harness.get_metadata.await_args_list == [
        call(
            f"playlists/{PLAYLIST_ID}/items",
            limit=1,
            offset=0,
            use_global_session=False,
        ),
        call(
            f"playlists/{PLAYLIST_ID}/items",
            limit=1,
            offset=0,
            use_global_session=True,
        ),
    ]
    assert [
        (args.args[1], args.kwargs["use_global_session"])
        for args in harness.get_page.await_args_list
    ] == [("dev-etag", False), ("global-etag", True)]


async def test_provider_instances_do_not_share_pagination_metadata() -> None:
    """Each Spotify provider instance maintains an independent pagination snapshot."""
    first = _make_provider("spotify--first")
    second = _make_provider("spotify--second")
    first.get_metadata.return_value = {"etag": "first-etag", "total": 100}
    second.get_metadata.return_value = {"etag": "second-etag", "total": 100}
    first.get_page.return_value = _playlist_page(100, "first-track")
    second.get_page.return_value = _playlist_page(100, "second-track")

    await first.provider.get_playlist_tracks(PLAYLIST_ID, page=1)
    await second.provider.get_playlist_tracks(PLAYLIST_ID, page=1)

    first.get_metadata.assert_awaited_once()
    second.get_metadata.assert_awaited_once()
    assert first.get_page.await_args is not None
    assert second.get_page.await_args is not None
    assert first.get_page.await_args.args[1] == "first-etag"
    assert second.get_page.await_args.args[1] == "second-etag"


async def test_offset_guard_skips_invalid_playlist_page_request() -> None:
    """Known totals prevent Spotify requests at or beyond the playlist end."""
    harness = _make_provider()
    harness.get_metadata.return_value = {"etag": "guard-etag", "total": 50}

    tracks = await harness.provider.get_playlist_tracks(PLAYLIST_ID, page=1)

    harness.get_metadata.assert_awaited_once()
    harness.get_page.assert_not_awaited()
    assert tracks == []
