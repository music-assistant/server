"""Account-filtered library reads protect details and artwork without core hooks."""

import asyncio
from copy import copy
from typing import Any
from unittest.mock import AsyncMock

import aiohttp
import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import (
    ProviderPermissionDenied,
    ResourceTemporarilyUnavailable,
    UnplayableMediaError,
)

from music_assistant.providers.feiniu_music.client import NetworkError
from music_assistant.providers.feiniu_music.provider import MEMBERSHIP_TTL

from .test_client import Response, client_with
from .test_provider import provider as provider  # noqa: PLC0414
from .test_provider import track_data


@pytest.mark.parametrize("kind", ["track", "album", "artist", "playlist"])
async def test_unlisted_detail_never_calls_unfiltered_endpoint(provider: Any, kind: str) -> None:
    """A successful unfiltered detail response cannot establish account membership."""
    provider._client.page = AsyncMock(return_value={"list": [], "total": 0})
    provider._client.playlists = AsyncMock(return_value=[])
    provider._client.detail = AsyncMock(return_value={"guid": "outside", "title": "Private"})
    with pytest.raises(ProviderPermissionDenied):
        await getattr(provider, "get_" + kind)("outside")
    provider._client.detail.assert_not_awaited()


async def test_page_failure_is_not_empty_or_denied(provider: Any) -> None:
    """A failed second page yields no partial sync and remains retryable."""
    provider._client.page = AsyncMock(
        side_effect=[{"list": [track_data()], "total": 2}, NetworkError("synthetic")]
    )
    yielded = []

    async def collect() -> None:
        async for item in provider.get_library_tracks():
            yielded.append(item)

    with pytest.raises(ResourceTemporarilyUnavailable):
        await collect()
    assert yielded == []
    provider._client.page = AsyncMock(side_effect=NetworkError("synthetic"))
    with pytest.raises(ResourceTemporarilyUnavailable):
        await provider.get_track("track-test")
    provider._client.page = AsyncMock(return_value={"list": [track_data()], "total": 1})
    assert len([item async for item in provider.get_library_tracks()]) == 1


async def test_cover_must_match_account_listed_owner(provider: Any) -> None:
    """Forged and another account's image references never reach the NAS."""
    provider._client.cover = AsyncMock(return_value=b"synthetic-image")
    track = await anext(provider.get_library_tracks())
    path = track.metadata.images[0].path
    assert await provider.resolve_image(path) == b"synthetic-image"
    for invalid in (
        "outside-cover",
        path.replace("cover-test", "outside-cover"),
        path.replace("track-test", "outside"),
        path.replace("test-scope", "other"),
    ):
        with pytest.raises(ProviderPermissionDenied):
            await provider.resolve_image(invalid)
    provider._client.cover.assert_awaited_once_with("cover-test")


async def test_legacy_cover_is_rejected_without_library_reads(provider: Any) -> None:
    """Unsupported development-era references must not trigger full library scans."""
    with pytest.raises(ProviderPermissionDenied):
        await provider.resolve_image("cover-test")
    provider._client.page.assert_not_awaited()
    provider._client.playlists.assert_not_awaited()
    provider._client.cover.assert_not_awaited()


async def test_unknown_cover_lookup_failure_is_not_denial(provider: Any) -> None:
    """A valid reference triggers a normal library read even before any sync."""
    provider._client.page = AsyncMock(side_effect=NetworkError("synthetic"))
    with pytest.raises(ResourceTemporarilyUnavailable):
        await provider.resolve_image("scoped/test-scope/track/track-test/cover-test")


async def test_outside_stream_never_probes_audio(provider: Any) -> None:
    """Known out-of-scope requests stop before detail and playback requests."""
    provider._client.media_prefix = AsyncMock()
    with pytest.raises(ProviderPermissionDenied):
        await provider.get_stream_details("outside", MediaType.TRACK)
    provider._client.media_prefix.assert_not_awaited()


async def test_reload_same_instance_does_not_reuse_previous_account_cache(provider: Any) -> None:
    """Keep MA's real shared cache while replacing credentials and library state."""
    provider._client.lyrics = AsyncMock(return_value={"list": [{"content": "First account"}]})
    first = await provider.get_track("track-test")
    await asyncio.sleep(0)
    other = copy(provider)
    other._memberships = {}
    other._collection_locks = {}
    other._cache_id = "new-load"
    other._image_scope = "new-account"
    other._client = copy(provider._client)
    other._client.page = AsyncMock(return_value={"list": [track_data()], "total": 1})
    other._client.detail = AsyncMock(return_value={"track": track_data()})
    other._client.lyrics = AsyncMock(return_value={"list": [{"content": "Second account"}]})
    second = await other.get_track("track-test")
    assert first.metadata.lyrics == "First account"
    assert second.metadata.lyrics == "Second account"
    assert first.metadata.images[0].path != second.metadata.images[0].path
    other._client.detail.assert_awaited_once()
    with pytest.raises(ProviderPermissionDenied):
        await other.resolve_image(first.metadata.images[0].path)


async def test_expired_collection_failure_does_not_use_stale_membership(
    provider: Any, monkeypatch: Any
) -> None:
    """An expired successful read cannot disguise a failed refresh."""
    clock = [100.0]
    monkeypatch.setattr(
        "music_assistant.providers.feiniu_music.provider.monotonic", lambda: clock[0]
    )
    await provider.get_track("track-test")
    clock[0] += MEMBERSHIP_TTL
    provider._client.page = AsyncMock(side_effect=NetworkError("synthetic"))
    with pytest.raises(ResourceTemporarilyUnavailable):
        await provider.get_track("track-test")
    provider._client.page = AsyncMock(return_value={"list": [], "total": 0})
    with pytest.raises(ProviderPermissionDenied):
        await provider.get_track("track-test")


async def test_stream_business_rejection_is_not_reauthentication(provider: Any) -> None:
    """A stream-context 100004 is unplayable, with no invented global permission meaning."""
    client = client_with(Response(b'{"code":100004}'))
    client._token = "synthetic-token"
    provider._client.audio_stream = client.audio_stream
    provider._reauthenticate = AsyncMock()
    details = await provider.get_stream_details("track-test", MediaType.TRACK)
    with pytest.raises(UnplayableMediaError):
        await anext(provider.get_audio_stream(details))
    provider._reauthenticate.assert_not_awaited()
    assert (await provider.get_track("track-test")).item_id == "track-test"


async def test_playlist_filters_hidden_pages_without_truncating(provider: Any) -> None:
    """An intermediate hidden-only page must not hide later playable entries."""
    provider._client.related = AsyncMock(
        side_effect=[
            {"list": [{"guid": "outside"}] * 100, "total": 102},
            {"list": [track_data(), track_data()], "total": 102},
        ]
    )
    tracks = await provider.get_playlist_tracks("playlist-test")
    assert [track.item_id for track in tracks] == ["track-test", "track-test"]
    assert [track.position for track in tracks] == [1, 2]


async def test_reload_closes_its_owned_http_connector(provider: Any, monkeypatch: Any) -> None:
    """MA's session helper creates a connector; unloading must release that resource."""
    connector = aiohttp.TCPConnector()

    def session_factory(_mass: Any, **kwargs: Any) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(connector=connector, **kwargs)

    values = {
        "url": "http://synthetic.invalid/music/",
        "username": "synthetic",
        "password": "synthetic-secret",
        "device_id": "a" * 32,
    }
    provider.get_setup_value = values.get
    monkeypatch.setattr(
        "music_assistant.providers.feiniu_music.provider.create_clientsession", session_factory
    )
    monkeypatch.setattr(
        "music_assistant.providers.feiniu_music.client.FeiNiuClient.login",
        AsyncMock(return_value={"guid": "synthetic-account"}),
    )
    try:
        await provider.handle_async_init()
        await provider.unload()
        assert connector.closed
    finally:
        await connector.close()
