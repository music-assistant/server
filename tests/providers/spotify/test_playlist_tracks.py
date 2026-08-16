"""Tests for Spotify playlist track pagination."""

import asyncio
from collections import OrderedDict
from typing import Any, NamedTuple
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.spotify.provider import (
    _PLAYLIST_PAGINATION_STATE_LIMIT,
    SpotifyProvider,
)
from tests.common import use_real_create_task

PLAYLIST_ID = "private-playlist"


class SpotifyPlaylistHarness(NamedTuple):
    """Container for a Spotify provider and its mocked API collaborators."""

    provider: SpotifyProvider
    get_playlist: AsyncMock
    requires_global: AsyncMock
    get_metadata: AsyncMock
    get_page: AsyncMock
    set_global: AsyncMock


def _make_provider(instance_id: str = "spotify--test") -> SpotifyPlaylistHarness:
    """Return a Spotify provider with isolated playlist API mocks."""
    provider = object.__new__(SpotifyProvider)
    provider.config = MagicMock(instance_id=instance_id)
    provider.manifest = MagicMock(domain="spotify")
    provider.logger = MagicMock()
    provider._sp_user = {"id": "test-user", "display_name": "Test User"}
    provider._playlist_pagination_states = OrderedDict()
    provider.dev_session_active = True

    mass = MagicMock()
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    mass.cache.set = AsyncMock()

    use_real_create_task(mass)
    provider.mass = mass

    get_playlist = AsyncMock()
    requires_global = AsyncMock(return_value=False)
    get_metadata = AsyncMock()
    get_page = AsyncMock()
    set_global = AsyncMock()
    provider.get_playlist = get_playlist  # type: ignore[method-assign]
    provider._playlist_requires_global_token = requires_global  # type: ignore[method-assign]
    provider._get_paginated_meta = get_metadata  # type: ignore[method-assign]
    provider._get_data_with_caching = get_page  # type: ignore[method-assign]
    provider._set_playlist_requires_global_token = set_global  # type: ignore[method-assign]

    return SpotifyPlaylistHarness(
        provider,
        get_playlist,
        requires_global,
        get_metadata,
        get_page,
        set_global,
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


async def test_second_full_traversal_reuses_etag_page_cache() -> None:
    """A repeated full traversal refreshes metadata but reuses every unchanged page."""
    harness = _make_provider()
    harness.provider.__dict__.pop("_get_paginated_meta")
    harness.provider.__dict__.pop("_get_data_with_caching")
    page_cache: dict[tuple[str, str | None], dict[str, Any]] = {}

    async def cache_get(
        key: str, *, checksum: str | None = None, **_kwargs: Any
    ) -> dict[str, Any] | None:
        return page_cache.get((key, checksum))

    async def cache_set(
        key: str,
        data: dict[str, Any],
        *,
        checksum: str | None = None,
        **_kwargs: Any,
    ) -> None:
        page_cache[(key, checksum)] = data

    async def get_data(_endpoint: str, **kwargs: Any) -> dict[str, Any]:
        if kwargs["limit"] == 1:
            return {"etag": "stable-etag", "total": 120}
        return _playlist_page(120, f"track-{kwargs['offset']}")

    harness.provider.mass.cache.get = AsyncMock(side_effect=cache_get)  # type: ignore[method-assign]
    harness.provider.mass.cache.set = AsyncMock(side_effect=cache_set)  # type: ignore[method-assign]
    get_data_mock = AsyncMock(side_effect=get_data)
    harness.provider._get_data = get_data_mock  # type: ignore[method-assign]
    get_playlist_tracks: Any = SpotifyProvider.get_playlist_tracks.__wrapped__  # type: ignore[attr-defined]

    traversals = [
        [await get_playlist_tracks(harness.provider, PLAYLIST_ID, page=page) for page in range(4)]
        for _ in range(2)
    ]

    metadata_calls = [args for args in get_data_mock.await_args_list if args.kwargs["limit"] == 1]
    page_calls = [args for args in get_data_mock.await_args_list if args.kwargs["limit"] == 50]
    assert len(metadata_calls) == 2
    assert [args.kwargs["offset"] for args in page_calls] == [0, 50, 100]
    assert harness.provider.mass.cache.get.await_count == 6
    assert harness.provider.mass.cache.set.await_count == 3
    assert [[page[0].position for page in traversal[:3]] for traversal in traversals] == [
        [1, 51, 101],
        [1, 51, 101],
    ]
    assert traversals[0][3] == traversals[1][3] == []


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


async def test_distinct_playlists_use_independent_pagination_states() -> None:
    """Different playlists neither block nor replace each other's metadata."""
    harness = _make_provider()
    first_metadata_started = asyncio.Event()
    release_first_metadata = asyncio.Event()

    async def get_metadata(endpoint: str, **_kwargs: Any) -> dict[str, Any]:
        if endpoint == "playlists/private-a/items":
            first_metadata_started.set()
            await release_first_metadata.wait()
            return {"etag": "private-a-etag", "total": 100}
        return {"etag": "private-b-etag", "total": 100}

    async def get_page(_endpoint: str, cache_checksum: str | None, **kwargs: Any) -> dict[str, Any]:
        return _playlist_page(100, f"{cache_checksum}-{kwargs['offset']}")

    harness.get_metadata.side_effect = get_metadata
    harness.get_page.side_effect = get_page

    first_task = asyncio.create_task(harness.provider.get_playlist_tracks("private-a", page=0))
    await first_metadata_started.wait()
    try:
        await asyncio.wait_for(
            harness.provider.get_playlist_tracks("private-b", page=0),
            timeout=1,
        )
    finally:
        release_first_metadata.set()
        await first_task
    await harness.provider.get_playlist_tracks("private-a", page=1)
    await harness.provider.get_playlist_tracks("private-b", page=1)

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
    ]
    assert [args.args[1] for args in harness.get_page.await_args_list] == [
        "private-b-etag",
        "private-a-etag",
        "private-a-etag",
        "private-b-etag",
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


async def test_restricted_playlist_items_metadata_retries_with_global_session() -> None:
    """Playlist items hidden from the developer session remain available globally."""
    harness = _make_provider()
    harness.get_metadata.side_effect = [
        MediaNotFoundError("developer session forbidden"),
        {"etag": "global-etag", "total": 1},
    ]
    harness.get_page.return_value = _playlist_page(1, "global-track")

    tracks = await harness.provider.get_playlist_tracks(PLAYLIST_ID)

    assert [args.kwargs["use_global_session"] for args in harness.get_metadata.await_args_list] == [
        False,
        True,
    ]
    harness.get_page.assert_awaited_once_with(
        f"playlists/{PLAYLIST_ID}/items",
        "global-etag",
        limit=50,
        offset=0,
        use_global_session=True,
    )
    harness.set_global.assert_awaited_once_with(PLAYLIST_ID)
    assert tracks[0].item_id == "global-track"


async def test_restricted_playlist_page_retries_with_global_session() -> None:
    """A rejected developer-session page retries the complete request through the global session."""
    harness = _make_provider()
    harness.get_metadata.side_effect = [
        {"etag": "dev-etag", "total": 1},
        {"etag": "global-etag", "total": 1},
    ]
    harness.get_page.side_effect = [
        MediaNotFoundError("developer session forbidden"),
        _playlist_page(1, "global-track"),
    ]

    tracks = await harness.provider.get_playlist_tracks(PLAYLIST_ID)

    assert [
        (args.args[1], args.kwargs["use_global_session"])
        for args in harness.get_page.await_args_list
    ] == [("dev-etag", False), ("global-etag", True)]
    harness.set_global.assert_awaited_once_with(PLAYLIST_ID)
    assert tracks[0].item_id == "global-track"


@pytest.mark.parametrize(
    ("dev_session_active", "requires_global", "use_global_session"),
    [(False, False, False), (True, True, True)],
)
async def test_playlist_failure_without_available_fallback_propagates(
    dev_session_active: bool,
    requires_global: bool,
    use_global_session: bool,
) -> None:
    """Playlist failures propagate when another Spotify session cannot be tried."""
    harness = _make_provider()
    harness.provider.dev_session_active = dev_session_active
    harness.requires_global.return_value = requires_global
    harness.get_metadata.side_effect = MediaNotFoundError("playlist unavailable")

    with pytest.raises(MediaNotFoundError):
        await harness.provider.get_playlist_tracks(PLAYLIST_ID)

    harness.get_metadata.assert_awaited_once_with(
        f"playlists/{PLAYLIST_ID}/items",
        limit=1,
        offset=0,
        use_global_session=use_global_session,
    )
    harness.set_global.assert_not_awaited()


async def test_failed_global_playlist_fallback_is_not_cached() -> None:
    """An unavailable playlist is not marked as requiring the global session."""
    harness = _make_provider()
    harness.get_metadata.side_effect = [
        MediaNotFoundError("developer session forbidden"),
        MediaNotFoundError("playlist unavailable"),
    ]

    with pytest.raises(MediaNotFoundError):
        await harness.provider.get_playlist_tracks(PLAYLIST_ID)

    assert [args.kwargs["use_global_session"] for args in harness.get_metadata.await_args_list] == [
        False,
        True,
    ]
    harness.set_global.assert_not_awaited()


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


async def test_playlist_pagination_state_is_bounded() -> None:
    """Pagination state retains only the most recently accessed playlists."""
    harness = _make_provider()
    harness.get_metadata.return_value = {"etag": "guard-etag", "total": 50}

    for index in range(_PLAYLIST_PAGINATION_STATE_LIMIT + 1):
        await harness.provider.get_playlist_tracks(f"playlist-{index}", page=1)

    assert len(harness.provider._playlist_pagination_states) == _PLAYLIST_PAGINATION_STATE_LIMIT
