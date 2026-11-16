"""Public playlists on an artist page use one bounded, cached metadata request."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.errors import (
    AuthenticationFailed,
    InvalidDataError,
    MediaNotFoundError,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import Playlist

from music_assistant.providers.soundcloud import (
    ARTIST_PLAYLISTS_LIMIT,
    SoundcloudMusicProvider,
)
from tests.common import use_real_create_task


@pytest.fixture
def public_provider(provider: SoundcloudMusicProvider) -> SoundcloudMusicProvider:
    """Exercise the real cache with a mocked metadata endpoint."""
    entries: dict[str, Any] = {}

    async def get(key: str, **_kwargs: Any) -> tuple[Any, bool, bool]:
        return deepcopy(entries.get(key)), key in entries, key in entries

    async def set_entry(key: str, data: Any, **_kwargs: Any) -> None:
        entries[key] = deepcopy(data)

    provider.mass.cache.get_with_freshness = AsyncMock(side_effect=get)  # type: ignore[method-assign]
    provider.mass.cache.set = AsyncMock(side_effect=set_entry)  # type: ignore[method-assign]
    use_real_create_task(provider.mass)
    provider._soundcloud.get_playlists_from_user.return_value = {"collection": [_playlist()]}
    return provider


def _playlist(**extra: Any) -> dict[str, Any]:
    return {
        "kind": "playlist",
        "id": 10,
        "title": "Burning Man 2025",
        "sharing": "public",
        "user": {"id": 123, "username": "Muloka"},
        "permalink_url": "https://soundcloud.com/muloka/sets/burning-man-2025",
        "artwork_url": "https://i1.sndcdn.com/artworks-test-large.jpg",
        "tracks": [{"id": 1}],
        **extra,
    }


async def test_artist_playlists(public_provider: SoundcloudMusicProvider) -> None:
    """The artist ID is sufficient, with no profile setting or track hydration."""
    result = await public_provider.get_artist_playlists("123")
    assert len(result) == 1
    playlist = result[0]
    assert isinstance(playlist, Playlist)
    assert playlist.item_id == "10"
    assert playlist.owner == "Muloka"
    assert next(iter(playlist.provider_mappings)).provider_instance == public_provider.instance_id
    assert next(iter(playlist.provider_mappings)).url == _playlist()["permalink_url"]
    public_provider._soundcloud.get_playlists_from_user.assert_awaited_once_with(
        "123", ARTIST_PLAYLISTS_LIMIT
    )
    public_provider._soundcloud.get_playlist_details.assert_not_awaited()
    public_provider._soundcloud.get_track_details.assert_not_awaited()


@pytest.mark.parametrize("bad_id", ["", "../me", "123/likes", "\uff11\uff12\uff13", "-1"])
async def test_invalid_id(public_provider: SoundcloudMusicProvider, bad_id: str) -> None:
    """Reject user IDs that could change the endpoint path."""
    with pytest.raises(InvalidDataError):
        await public_provider.get_artist_playlists(bad_id)
    public_provider._soundcloud.get_playlists_from_user.assert_not_awaited()


async def test_filter_and_deduplicate(public_provider: SoundcloudMusicProvider) -> None:
    """Only public playlists owned by this user appear, excluding albums."""
    public_provider._soundcloud.get_playlists_from_user.return_value = {
        "collection": [
            None,
            {},
            _playlist(sharing="private"),
            _playlist(is_album=True),
            _playlist(user={"id": 999}),
            _playlist(user=None),
            _playlist(title=""),
            _playlist(),
            _playlist(),
        ]
    }
    assert len(await public_provider.get_artist_playlists("123")) == 1


async def test_bounded_request(public_provider: SoundcloudMusicProvider) -> None:
    """Never walk next_href or hydrate every playlist to render the artist page."""
    public_provider._soundcloud.get_playlists_from_user.return_value = {
        "collection": [_playlist(id=index + 1) for index in range(120)],
        "next_href": "https://api-v2.soundcloud.com/users/123/playlists_without_albums?offset=100",
    }
    assert len(await public_provider.get_artist_playlists("123")) == ARTIST_PLAYLISTS_LIMIT
    public_provider._soundcloud.get_playlists_from_user.assert_awaited_once_with(
        "123", ARTIST_PLAYLISTS_LIMIT
    )


async def test_cached(public_provider: SoundcloudMusicProvider) -> None:
    """Repeated artist-page loads reuse cached metadata."""
    first = await public_provider.get_artist_playlists("123")
    await asyncio.sleep(0)
    assert await public_provider.get_artist_playlists("123") == first
    public_provider._soundcloud.get_playlists_from_user.assert_awaited_once_with(
        "123", ARTIST_PLAYLISTS_LIMIT
    )


async def test_bad_response(public_provider: SoundcloudMusicProvider) -> None:
    """An API error is not cached as an empty playlist collection."""
    public_provider._soundcloud.get_playlists_from_user.return_value = {"error": "failed"}
    with pytest.raises(MediaNotFoundError):
        await public_provider.get_artist_playlists("123")


@pytest.mark.parametrize(
    "response",
    [
        {"errors": [{"error_message": "401 - Unauthorized"}]},
        {"errors": [{"error_message": "403 - Forbidden"}]},
        {"errors": [{"error_message": "503 - Service Unavailable"}]},
        {"collection": None},
        [],
        None,
    ],
)
async def test_failed_listing_recovers_without_cached_empty_result(
    public_provider: SoundcloudMusicProvider, response: Any
) -> None:
    """An invalid response never poisons the next successful artist-page request."""
    endpoint = public_provider._soundcloud.get_playlists_from_user
    endpoint.side_effect = [response, {"collection": [_playlist()]}]

    with pytest.raises(MediaNotFoundError):
        await public_provider.get_artist_playlists("123")
    cast("AsyncMock", public_provider.mass.cache.set).assert_not_awaited()
    assert len(await public_provider.get_artist_playlists("123")) == 1
    await asyncio.sleep(0)
    assert len(await public_provider.get_artist_playlists("123")) == 1
    assert endpoint.await_count == 2


@pytest.mark.parametrize("error", [AuthenticationFailed, ResourceTemporarilyUnavailable])
async def test_endpoint_errors_propagate_and_can_recover(
    public_provider: SoundcloudMusicProvider, error: type[Exception]
) -> None:
    """Authentication and service exceptions retain their type and are never cached."""
    endpoint = public_provider._soundcloud.get_playlists_from_user
    endpoint.side_effect = [error("Request failed"), {"collection": [_playlist()]}]

    with pytest.raises(error):
        await public_provider.get_artist_playlists("123")
    cast("AsyncMock", public_provider.mass.cache.set).assert_not_awaited()
    assert len(await public_provider.get_artist_playlists("123")) == 1
    assert endpoint.await_count == 2


async def test_valid_empty_listing_is_cached(public_provider: SoundcloudMusicProvider) -> None:
    """A successful empty collection is distinguishable from an API error."""
    public_provider._soundcloud.get_playlists_from_user.return_value = {"collection": []}
    assert await public_provider.get_artist_playlists("123") == []
    await asyncio.sleep(0)
    assert await public_provider.get_artist_playlists("123") == []
    public_provider._soundcloud.get_playlists_from_user.assert_awaited_once()


async def test_artist_page_does_not_require_following(
    public_provider: SoundcloudMusicProvider,
) -> None:
    """Looking up an artist does not traverse or depend on the account's follow list."""
    assert len(await public_provider.get_artist_playlists("123")) == 1
    public_provider._soundcloud.get_following.assert_not_awaited()
    public_provider._soundcloud.get_album_from_user.assert_not_awaited()


async def test_cache_keeps_artist_results_separate(
    public_provider: SoundcloudMusicProvider,
) -> None:
    """Opening a second artist cannot reuse the first artist's playlist results."""
    public_provider._soundcloud.get_playlists_from_user.side_effect = [
        {"collection": [_playlist()]},
        {"collection": [_playlist(id=20, user={"id": 456, "username": "Other"})]},
    ]
    assert [item.item_id for item in await public_provider.get_artist_playlists("123")] == ["10"]
    await asyncio.sleep(0)
    assert [item.item_id for item in await public_provider.get_artist_playlists("456")] == ["20"]
    await asyncio.sleep(0)
    assert [item.item_id for item in await public_provider.get_artist_playlists("123")] == ["10"]
    assert public_provider._soundcloud.get_playlists_from_user.await_count == 2
