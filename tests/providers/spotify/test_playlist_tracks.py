"""Tests for Spotify playlist track pagination."""

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
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
    get_snapshot: AsyncMock
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
    get_snapshot = AsyncMock()
    get_metadata = AsyncMock()
    get_page = AsyncMock()
    set_global = AsyncMock()
    provider.get_playlist = get_playlist  # type: ignore[method-assign]
    provider._playlist_requires_global_token = requires_global  # type: ignore[method-assign]
    provider._get_playlist_snapshot = get_snapshot  # type: ignore[method-assign]
    provider._get_paginated_meta = get_metadata  # type: ignore[method-assign]
    provider._get_data_with_caching = get_page  # type: ignore[method-assign]
    provider._set_playlist_requires_global_token = set_global  # type: ignore[method-assign]

    return SpotifyPlaylistHarness(
        provider,
        get_playlist,
        requires_global,
        get_snapshot,
        get_metadata,
        get_page,
        set_global,
    )


def _use_real_page_cache(
    harness: SpotifyPlaylistHarness,
    get_data: Callable[..., Awaitable[dict[str, Any]]],
) -> tuple[AsyncMock, AsyncMock, AsyncMock]:
    """
    Serve playlist pages through the real page cache on top of a mocked Spotify API.

    The cache is a dict keyed on cache key and checksum.

    :param harness: The harness whose snapshot and page mocks are replaced by the real methods.
    :param get_data: Stand-in for every Spotify API request.
    :returns: The mocks recording the Spotify API requests, the cache reads and the cache writes.
    """
    harness.provider.__dict__.pop("_get_playlist_snapshot")
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

    cache_get_mock = AsyncMock(side_effect=cache_get)
    cache_set_mock = AsyncMock(side_effect=cache_set)
    harness.provider.mass.cache.get = cache_get_mock  # type: ignore[method-assign]
    harness.provider.mass.cache.set = cache_set_mock  # type: ignore[method-assign]
    get_data_mock = AsyncMock(side_effect=get_data)
    harness.provider._get_data = get_data_mock  # type: ignore[method-assign]
    return get_data_mock, cache_get_mock, cache_set_mock


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
    harness.get_snapshot.return_value = {"checksum": "snapshot-1", "total": 120}

    async def get_page(
        _endpoint: str, _cache_checksum: str | None, **kwargs: Any
    ) -> dict[str, Any]:
        return _playlist_page(120, f"track-{kwargs['offset']}")

    harness.get_page.side_effect = get_page

    pages = [
        await harness.provider.get_playlist_tracks(PLAYLIST_ID, page=page) for page in range(4)
    ]

    harness.get_snapshot.assert_awaited_once_with(PLAYLIST_ID, use_global_session=False)
    harness.get_metadata.assert_not_awaited()
    assert harness.get_page.await_args_list == [
        call(
            f"playlists/{PLAYLIST_ID}/items",
            "snapshot-1",
            limit=50,
            offset=offset,
            use_global_session=False,
        )
        for offset in (0, 50, 100)
    ]
    assert [page[0].position for page in pages[:3]] == [1, 51, 101]
    assert pages[3] == []


async def test_second_full_traversal_reuses_snapshot_page_cache() -> None:
    """A repeated full traversal refreshes the snapshot but reuses every unchanged page."""
    harness = _make_provider()

    async def get_data(endpoint: str, **kwargs: Any) -> dict[str, Any]:
        if endpoint == f"playlists/{PLAYLIST_ID}":
            return {"snapshot_id": "stable-snapshot", "items": {"total": 120}}
        return _playlist_page(120, f"track-{kwargs['offset']}")

    get_data_mock, cache_get, cache_set = _use_real_page_cache(harness, get_data)
    get_playlist_tracks: Any = SpotifyProvider.get_playlist_tracks.__wrapped__  # type: ignore[attr-defined]

    traversals = [
        [await get_playlist_tracks(harness.provider, PLAYLIST_ID, page=page) for page in range(4)]
        for _ in range(2)
    ]

    snapshot_calls = [
        args for args in get_data_mock.await_args_list if args.args[0] == f"playlists/{PLAYLIST_ID}"
    ]
    page_calls = [
        args
        for args in get_data_mock.await_args_list
        if args.args[0] == f"playlists/{PLAYLIST_ID}/items"
    ]
    assert len(snapshot_calls) == 2
    assert [args.kwargs["offset"] for args in page_calls] == [0, 50, 100]
    assert cache_get.await_count == 6
    assert cache_set.await_count == 3
    assert [[page[0].position for page in traversal[:3]] for traversal in traversals] == [
        [1, 51, 101],
        [1, 51, 101],
    ]
    assert traversals[0][3] == traversals[1][3] == []


async def test_changed_snapshot_refetches_cached_pages() -> None:
    """A page stored under one snapshot id is not served once the playlist has changed."""
    harness = _make_provider()
    snapshots = iter(["snapshot-1", "snapshot-2"])
    track_prefixes = iter(["old", "old", "old", "reordered", "reordered", "reordered"])

    async def get_data(endpoint: str, **kwargs: Any) -> dict[str, Any]:
        if endpoint == f"playlists/{PLAYLIST_ID}":
            # an edit that keeps the item count, like a reorder
            return {"snapshot_id": next(snapshots), "items": {"total": 120}}
        return _playlist_page(120, f"{next(track_prefixes)}-{kwargs['offset']}")

    get_data_mock, _cache_get, _cache_set = _use_real_page_cache(harness, get_data)
    get_playlist_tracks: Any = SpotifyProvider.get_playlist_tracks.__wrapped__  # type: ignore[attr-defined]

    traversals = [
        [await get_playlist_tracks(harness.provider, PLAYLIST_ID, page=page) for page in range(3)]
        for _ in range(2)
    ]

    page_calls = [
        args
        for args in get_data_mock.await_args_list
        if args.args[0] == f"playlists/{PLAYLIST_ID}/items"
    ]
    assert [args.kwargs["offset"] for args in page_calls] == [0, 50, 100, 0, 50, 100]
    assert [[page[0].item_id for page in traversal] for traversal in traversals] == [
        ["old-0", "old-50", "old-100"],
        ["reordered-0", "reordered-50", "reordered-100"],
    ]


@pytest.mark.parametrize(
    ("dev_session_active", "use_global_session", "playlist"),
    [
        (True, False, {"snapshot_id": "snapshot-1", "items": {"total": 51}}),
        (True, True, {"snapshot_id": "snapshot-1"}),
        (True, True, {"snapshot_id": "snapshot-1", "items": None}),
        (False, False, {"snapshot_id": "snapshot-1"}),
    ],
    ids=[
        "developer-session",
        "global-session",
        "global-session-null-items",
        "no-developer-session",
    ],
)
async def test_playlist_snapshot_request(
    dev_session_active: bool, use_global_session: bool, playlist: dict[str, Any]
) -> None:
    """
    The snapshot is read from the playlist itself, and a missing item total does not block pages.

    A developer session that omits the items is covered by its own test.
    """
    harness = _make_provider()
    harness.provider.dev_session_active = dev_session_active
    harness.requires_global.return_value = use_global_session
    harness.provider.__dict__.pop("_get_playlist_snapshot")
    get_data = AsyncMock(return_value={**playlist, "etag": "playlist-etag"})
    harness.provider._get_data = get_data  # type: ignore[method-assign]
    harness.get_page.return_value = _playlist_page(51, "page-one-track")

    tracks = await harness.provider.get_playlist_tracks(PLAYLIST_ID, page=1)

    get_data.assert_awaited_once_with(
        f"playlists/{PLAYLIST_ID}",
        fields="snapshot_id,items(total)",
        use_global_session=use_global_session,
    )
    harness.get_page.assert_awaited_once_with(
        f"playlists/{PLAYLIST_ID}/items",
        "snapshot-1",
        limit=50,
        offset=50,
        use_global_session=use_global_session,
    )
    harness.set_global.assert_not_awaited()
    assert tracks[0].position == 51


async def test_playlist_items_hidden_from_developer_session_are_read_globally() -> None:
    """
    A playlist the developer session returns without items is probed and read globally.

    The global item total keeps a read past the end from reaching Spotify.
    """
    harness = _make_provider()
    harness.provider.__dict__.pop("_get_playlist_snapshot")
    global_playlists: set[str] = set()
    harness.requires_global.side_effect = lambda playlist_id: playlist_id in global_playlists
    harness.set_global.side_effect = global_playlists.add

    async def get_data(_endpoint: str, **kwargs: Any) -> dict[str, Any]:
        if kwargs["use_global_session"]:
            return {"snapshot_id": "s1", "items": {"total": 100}}
        # Development Mode returns only the metadata of a playlist the user does not own
        return {"snapshot_id": "s1"}

    async def get_page(
        _endpoint: str, _cache_checksum: str | None, **kwargs: Any
    ) -> dict[str, Any]:
        if not kwargs["use_global_session"]:
            raise MediaNotFoundError("developer session forbidden")
        return _playlist_page(100, f"track-{kwargs['offset']}")

    get_data_mock = AsyncMock(side_effect=get_data)
    harness.provider._get_data = get_data_mock  # type: ignore[method-assign]
    harness.get_page.side_effect = get_page

    past_end = await harness.provider.get_playlist_tracks(PLAYLIST_ID, page=2)
    tracks = await harness.provider.get_playlist_tracks(PLAYLIST_ID, page=1)

    harness.get_page.assert_awaited_once_with(
        f"playlists/{PLAYLIST_ID}/items",
        "s1",
        limit=50,
        offset=50,
        use_global_session=True,
    )
    assert get_data_mock.await_args_list == [
        call(
            f"playlists/{PLAYLIST_ID}",
            fields="snapshot_id,items(total)",
            use_global_session=use_global_session,
        )
        for use_global_session in (False, True)
    ]
    harness.set_global.assert_awaited_once_with(PLAYLIST_ID)
    assert past_end == []
    assert tracks[0].item_id == "track-50"


async def test_concurrent_cold_pages_share_inflight_pagination_metadata() -> None:
    """Concurrent cache refreshes share one in-flight metadata request."""
    harness = _make_provider()
    metadata_started = asyncio.Event()
    release_metadata = asyncio.Event()

    async def get_snapshot(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        metadata_started.set()
        await release_metadata.wait()
        return {"checksum": "shared-snapshot", "total": 100}

    harness.get_snapshot.side_effect = get_snapshot
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

    harness.get_snapshot.assert_awaited_once()
    assert [args.args[1] for args in harness.get_page.await_args_list] == [
        "shared-snapshot",
        "shared-snapshot",
    ]


async def test_distinct_playlists_use_independent_pagination_states() -> None:
    """Different playlists neither block nor replace each other's metadata."""
    harness = _make_provider()
    first_metadata_started = asyncio.Event()
    release_first_metadata = asyncio.Event()

    async def get_snapshot(prov_playlist_id: str, **_kwargs: Any) -> dict[str, Any]:
        if prov_playlist_id == "private-a":
            first_metadata_started.set()
            await release_first_metadata.wait()
            return {"checksum": "private-a-snapshot", "total": 100}
        return {"checksum": "private-b-snapshot", "total": 100}

    async def get_page(_endpoint: str, cache_checksum: str | None, **kwargs: Any) -> dict[str, Any]:
        return _playlist_page(100, f"{cache_checksum}-{kwargs['offset']}")

    harness.get_snapshot.side_effect = get_snapshot
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

    assert harness.get_snapshot.await_args_list == [
        call("private-a", use_global_session=False),
        call("private-b", use_global_session=False),
    ]
    assert [args.args[1] for args in harness.get_page.await_args_list] == [
        "private-b-snapshot",
        "private-a-snapshot",
        "private-a-snapshot",
        "private-b-snapshot",
    ]


async def test_page_zero_refresh_replaces_pagination_snapshot() -> None:
    """A new page-zero request replaces both the checksum and total for later pages."""
    harness = _make_provider()
    harness.get_snapshot.side_effect = [
        {"checksum": "old-snapshot", "total": 120},
        {"checksum": "new-snapshot", "total": 50},
    ]
    harness.get_page.side_effect = [
        _playlist_page(120, "old-track"),
        _playlist_page(50, "new-track"),
    ]

    await harness.provider.get_playlist_tracks(PLAYLIST_ID, page=0)
    await harness.provider.get_playlist_tracks(PLAYLIST_ID, page=0)
    guarded_page = await harness.provider.get_playlist_tracks(PLAYLIST_ID, page=1)

    assert harness.get_snapshot.await_count == 2
    assert [args.args[1] for args in harness.get_page.await_args_list] == [
        "old-snapshot",
        "new-snapshot",
    ]
    assert guarded_page == []
    assert harness.get_page.await_count == 2


async def test_cold_nonzero_page_fetches_pagination_metadata() -> None:
    """A direct nonzero page request fetches metadata before requesting its page."""
    harness = _make_provider()
    harness.get_snapshot.return_value = {"checksum": "cold-snapshot", "total": 151}
    harness.get_page.return_value = _playlist_page(151, "page-two-track")

    tracks = await harness.provider.get_playlist_tracks(PLAYLIST_ID, page=2)

    harness.get_snapshot.assert_awaited_once()
    harness.get_page.assert_awaited_once_with(
        f"playlists/{PLAYLIST_ID}/items",
        "cold-snapshot",
        limit=50,
        offset=100,
        use_global_session=False,
    )
    assert tracks[0].position == 101


async def test_liked_songs_use_default_session() -> None:
    """Liked Songs reads me/tracks, checked by its ETag, with the session of the library sync."""
    harness = _make_provider()
    harness.get_metadata.return_value = {"etag": "liked-etag", "total": 1}
    harness.get_page.return_value = _playlist_page(1, "liked-track")

    tracks = await harness.provider.get_playlist_tracks(
        harness.provider._get_liked_songs_playlist_id()
    )

    harness.get_metadata.assert_awaited_once_with(
        "me/tracks", limit=1, offset=0, use_global_session=False
    )
    harness.get_page.assert_awaited_once_with(
        "me/tracks", "liked-etag", limit=50, offset=0, use_global_session=False
    )
    harness.get_snapshot.assert_not_awaited()
    harness.get_playlist.assert_not_awaited()
    assert tracks[0].item_id == "liked-track"


async def test_playlist_identities_do_not_share_pagination_metadata() -> None:
    """Private playlists and liked songs each use their own pagination snapshot."""
    harness = _make_provider()
    liked_songs_id = harness.provider._get_liked_songs_playlist_id()
    harness.get_snapshot.side_effect = [
        {"checksum": "private-a-snapshot", "total": 100},
        {"checksum": "private-b-snapshot", "total": 100},
    ]
    harness.get_metadata.return_value = {"etag": "liked-etag", "total": 100}
    harness.get_page.side_effect = [
        _playlist_page(100, "private-a-track"),
        _playlist_page(100, "private-b-track"),
        _playlist_page(100, "liked-track"),
    ]

    await harness.provider.get_playlist_tracks("private-a", page=1)
    await harness.provider.get_playlist_tracks("private-b", page=1)
    await harness.provider.get_playlist_tracks(liked_songs_id, page=1)

    assert harness.get_snapshot.await_args_list == [
        call("private-a", use_global_session=False),
        call("private-b", use_global_session=False),
    ]
    harness.get_metadata.assert_awaited_once_with(
        "me/tracks", limit=1, offset=0, use_global_session=False
    )
    assert [args.args[1] for args in harness.get_page.await_args_list] == [
        "private-a-snapshot",
        "private-b-snapshot",
        "liked-etag",
    ]
    assert harness.get_playlist.await_count == 2
    assert harness.requires_global.await_count == 2


async def test_session_paths_share_pagination_metadata() -> None:
    """The same playlist reuses its snapshot when its required token path changes."""
    harness = _make_provider()
    harness.requires_global.side_effect = [False, True]
    harness.get_snapshot.return_value = {"checksum": "snapshot-1", "total": 100}
    harness.get_page.side_effect = [
        _playlist_page(100, "dev-track"),
        _playlist_page(100, "global-track"),
    ]

    await harness.provider.get_playlist_tracks(PLAYLIST_ID, page=1)
    await harness.provider.get_playlist_tracks(PLAYLIST_ID, page=1)

    harness.get_snapshot.assert_awaited_once_with(PLAYLIST_ID, use_global_session=False)
    assert [
        (args.args[1], args.kwargs["use_global_session"])
        for args in harness.get_page.await_args_list
    ] == [("snapshot-1", False), ("snapshot-1", True)]


async def test_restricted_playlist_metadata_retries_with_global_session() -> None:
    """Playlist metadata hidden from the developer session remains available globally."""
    harness = _make_provider()
    harness.get_snapshot.side_effect = [
        MediaNotFoundError("developer session forbidden"),
        {"checksum": "snapshot-1", "total": 1},
    ]
    harness.get_page.return_value = _playlist_page(1, "global-track")

    tracks = await harness.provider.get_playlist_tracks(PLAYLIST_ID)

    assert harness.get_snapshot.await_args_list == [
        call(PLAYLIST_ID, use_global_session=False),
        call(PLAYLIST_ID, use_global_session=True),
    ]
    harness.get_page.assert_awaited_once_with(
        f"playlists/{PLAYLIST_ID}/items",
        "snapshot-1",
        limit=50,
        offset=0,
        use_global_session=True,
    )
    harness.set_global.assert_awaited_once_with(PLAYLIST_ID)
    assert tracks[0].item_id == "global-track"


async def test_restricted_playlist_page_retries_with_global_session() -> None:
    """A rejected developer-session page retries on the global session and marks the playlist."""
    harness = _make_provider()
    harness.get_snapshot.return_value = {"checksum": "snapshot-1", "total": 1}
    harness.get_page.side_effect = [
        MediaNotFoundError("developer session forbidden"),
        _playlist_page(1, "global-track"),
    ]

    tracks = await harness.provider.get_playlist_tracks(PLAYLIST_ID)

    assert harness.get_snapshot.await_args_list == [
        call(PLAYLIST_ID, use_global_session=False),
        call(PLAYLIST_ID, use_global_session=True),
    ]
    assert [
        (args.args[1], args.kwargs["use_global_session"])
        for args in harness.get_page.await_args_list
    ] == [("snapshot-1", False), ("snapshot-1", True)]
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
    harness.get_snapshot.side_effect = MediaNotFoundError("playlist unavailable")

    with pytest.raises(MediaNotFoundError):
        await harness.provider.get_playlist_tracks(PLAYLIST_ID)

    harness.get_snapshot.assert_awaited_once_with(
        PLAYLIST_ID, use_global_session=use_global_session
    )
    harness.set_global.assert_not_awaited()


async def test_failed_global_playlist_fallback_is_not_cached() -> None:
    """An unavailable playlist is not marked as requiring the global session."""
    harness = _make_provider()
    harness.get_snapshot.side_effect = [
        MediaNotFoundError("developer session forbidden"),
        MediaNotFoundError("playlist unavailable"),
    ]

    with pytest.raises(MediaNotFoundError):
        await harness.provider.get_playlist_tracks(PLAYLIST_ID)

    assert [args.kwargs["use_global_session"] for args in harness.get_snapshot.await_args_list] == [
        False,
        True,
    ]
    harness.set_global.assert_not_awaited()


async def test_failed_global_page_fallback_is_not_cached() -> None:
    """A page unavailable on both sessions does not mark the playlist as requiring the global one."""
    harness = _make_provider()
    harness.get_snapshot.return_value = {"checksum": "snapshot-1", "total": 1}
    harness.get_page.side_effect = [
        MediaNotFoundError("developer session forbidden"),
        MediaNotFoundError("playlist unavailable"),
    ]

    with pytest.raises(MediaNotFoundError):
        await harness.provider.get_playlist_tracks(PLAYLIST_ID)

    assert [args.kwargs["use_global_session"] for args in harness.get_snapshot.await_args_list] == [
        False,
        True,
    ]
    assert [args.kwargs["use_global_session"] for args in harness.get_page.await_args_list] == [
        False,
        True,
    ]
    harness.set_global.assert_not_awaited()


async def test_provider_instances_do_not_share_pagination_metadata() -> None:
    """Each Spotify provider instance maintains an independent pagination snapshot."""
    first = _make_provider("spotify--first")
    second = _make_provider("spotify--second")
    first.get_snapshot.return_value = {"checksum": "first-snapshot", "total": 100}
    second.get_snapshot.return_value = {"checksum": "second-snapshot", "total": 100}
    first.get_page.return_value = _playlist_page(100, "first-track")
    second.get_page.return_value = _playlist_page(100, "second-track")

    await first.provider.get_playlist_tracks(PLAYLIST_ID, page=1)
    await second.provider.get_playlist_tracks(PLAYLIST_ID, page=1)

    first.get_snapshot.assert_awaited_once()
    second.get_snapshot.assert_awaited_once()
    assert first.get_page.await_args is not None
    assert second.get_page.await_args is not None
    assert first.get_page.await_args.args[1] == "first-snapshot"
    assert second.get_page.await_args.args[1] == "second-snapshot"


async def test_offset_guard_skips_invalid_playlist_page_request() -> None:
    """Known totals prevent Spotify requests at or beyond the playlist end."""
    harness = _make_provider()
    harness.get_snapshot.return_value = {"checksum": "guard-snapshot", "total": 50}

    tracks = await harness.provider.get_playlist_tracks(PLAYLIST_ID, page=1)

    harness.get_snapshot.assert_awaited_once()
    harness.get_page.assert_not_awaited()
    assert tracks == []


async def test_playlist_pagination_state_is_bounded() -> None:
    """Pagination state retains only the most recently accessed playlists."""
    harness = _make_provider()
    harness.get_snapshot.return_value = {"checksum": "guard-snapshot", "total": 50}

    for index in range(_PLAYLIST_PAGINATION_STATE_LIMIT + 1):
        await harness.provider.get_playlist_tracks(f"playlist-{index}", page=1)

    assert len(harness.provider._playlist_pagination_states) == _PLAYLIST_PAGINATION_STATE_LIMIT
