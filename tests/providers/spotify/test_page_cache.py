"""Tests for the Spotify page cache."""

from collections import OrderedDict
from functools import partial
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

from music_assistant.providers.spotify.provider import SpotifyProvider
from tests.common import use_real_create_task

ETAG = "tracks-etag"


class _PageCache:
    """Dict-backed stand-in for the cache controller, keyed on key, provider and checksum."""

    def __init__(self) -> None:
        """Initialize an empty cache."""
        self.entries: dict[tuple[str, str, str | None], Any] = {}
        self.get = AsyncMock(side_effect=self._get)
        self.set = AsyncMock(side_effect=self._set)
        # @use_cache never gets a hit, so every decorated method runs its body
        self.get_with_freshness = AsyncMock(return_value=(None, False, False))

    async def _get(
        self, key: str, *, provider: str, checksum: str | None = None, **_kwargs: Any
    ) -> Any:
        return self.entries.get((key, provider, checksum))

    async def _set(
        self, key: str, data: Any, *, provider: str, checksum: str | None = None, **_kwargs: Any
    ) -> None:
        self.entries[(key, provider, checksum)] = data


def _make_provider() -> tuple[SpotifyProvider, _PageCache, AsyncMock]:
    """
    Return a Spotify provider with a dict-backed cache and a mocked Spotify API.

    Every API request answers with one saved track, an ETag and a total of one.
    """
    provider = object.__new__(SpotifyProvider)
    provider.config = MagicMock(instance_id="spotify--test")
    provider.manifest = MagicMock(domain="spotify")
    provider.logger = MagicMock()
    provider._playlist_pagination_states = OrderedDict()
    provider.dev_session_active = True

    cache = _PageCache()
    mass = MagicMock()
    mass.cache = cache
    use_real_create_task(mass)
    provider.mass = mass

    get_data = AsyncMock(
        return_value={"etag": ETAG, "total": 1, "items": [{"track": _spotify_track("track1")}]}
    )
    provider._get_data = get_data  # type: ignore[method-assign]
    return provider, cache, get_data


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


async def test_library_sync_and_liked_songs_share_pages() -> None:
    """The library track sync and the Liked Songs playlist fetch a me/tracks page only once."""
    provider, _cache, get_data = _make_provider()

    library_tracks = [track async for track in provider.get_library_tracks()]
    liked_tracks = await provider.get_playlist_tracks(provider._get_liked_songs_playlist_id())

    page_requests = [args for args in get_data.await_args_list if args.kwargs["limit"] == 50]
    assert page_requests == [call("me/tracks", limit=50, offset=0)]
    assert [track.item_id for track in library_tracks] == ["track1"]
    assert [track.item_id for track in liked_tracks] == ["track1"]


async def test_page_cache_key_separates_only_a_forced_global_session() -> None:
    """
    A page fetched with a forced global session is not served to the default session.

    Passing use_global_session=False is the same request as leaving it out, so both share a page.
    """
    provider, cache, get_data = _make_provider()
    get_page = partial(provider._get_data_with_caching, "me/tracks", ETAG, limit=50, offset=0)

    await get_page(use_global_session=True)
    await get_page(use_global_session=False)
    await get_page()

    assert [args.args[0] for args in cache.get.await_args_list] == [
        "me/tracks.limit50.offset0.use_global_sessionTrue",
        "me/tracks.limit50.offset0",
        "me/tracks.limit50.offset0",
    ]
    assert get_data.await_args_list == [
        call("me/tracks", limit=50, offset=0, use_global_session=True),
        call("me/tracks", limit=50, offset=0, use_global_session=False),
    ]
