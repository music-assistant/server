"""Native per-item boundaries protect details, playlist members and owned artwork."""

import asyncio
from copy import copy
from typing import Any
from unittest.mock import AsyncMock, Mock, call

import aiohttp
import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    ProviderPermissionDenied,
    ResourceTemporarilyUnavailable,
    UnplayableMediaError,
)

from music_assistant.providers.feiniu_music.client import NetworkError, NotFoundError

from .test_client import Response, client_with
from .test_provider import provider as provider  # noqa: PLC0414
from .test_provider import track_data


@pytest.mark.parametrize("kind", ["track", "album", "artist", "playlist"])
async def test_native_detail_rejection_does_not_scan_library(provider: Any, kind: str) -> None:
    """The native missing/hidden response is preserved without global permission mapping."""
    error = NotFoundError("API code 100005")
    provider._client.detail = AsyncMock(side_effect=error)
    with pytest.raises(MediaNotFoundError) as raised:
        await getattr(provider, "get_" + kind)("outside")
    assert raised.value is error
    provider._client.page.assert_not_awaited()
    provider._client.playlists.assert_not_awaited()


@pytest.mark.parametrize(
    ("fields", "error"),
    [
        ({"accessStatus": 2}, ProviderPermissionDenied),
        ({"accessStatus": 3}, MediaNotFoundError),
        ({}, InvalidDataError),
        ({"accessStatus": 99}, InvalidDataError),
        ({"accessStatus": False}, InvalidDataError),
        ({"accessStatus": "0"}, InvalidDataError),
        ({"accessStatus": None}, InvalidDataError),
    ],
)
async def test_track_status_denied_before_parsing_or_cache(
    provider: Any, fields: dict[str, Any], error: type[Exception]
) -> None:
    """Only an explicit integer zero may authorize native metadata."""
    row = track_data()
    row.pop("accessStatus")
    row.update(fields)
    provider._client.detail = AsyncMock(return_value={"track": row})
    provider._parse_item = Mock(side_effect=AssertionError("Unauthorized metadata parsed"))
    with pytest.raises(error):
        await provider.get_track("track-test")
    await asyncio.sleep(0)
    provider._parse_item.assert_not_called()
    assert provider.mass.cache.entries == {}
    provider._client.lyrics.assert_not_awaited()
    provider._client.page.assert_not_awaited()


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
    # A failed sync must not block independently valid per-item metadata.
    assert (await provider.get_track("track-test")).item_id == "track-test"
    provider._client.page = AsyncMock(return_value={"list": [track_data()], "total": 1})
    assert len([item async for item in provider.get_library_tracks()]) == 1


@pytest.mark.parametrize("kind", ["track", "album", "artist", "playlist"])
async def test_cover_must_match_native_owner(provider: Any, kind: str) -> None:
    """A visible owner permits its exact image, never a substituted cover or other instance."""
    row = {**track_data(), "name": "Synthetic", "guid": "owner"}
    provider._client.detail = AsyncMock(return_value={"track": row} if kind == "track" else row)
    path = f"scoped/test-scope/{kind}/owner/cover-test"
    assert await provider.resolve_image(path) == b"synthetic-image"
    for invalid in (
        "outside-cover",
        path.replace("cover-test", "outside-cover"),
        path.replace("test-scope", "other"),
    ):
        with pytest.raises(ProviderPermissionDenied):
            await provider.resolve_image(invalid)
    provider._client.cover.assert_awaited_once_with("cover-test")
    provider._client.page.assert_not_awaited()
    provider._client.playlists.assert_not_awaited()


@pytest.mark.parametrize("kind", ["track", "album", "artist", "playlist"])
async def test_inaccessible_artwork_owner_never_fetches_cover(provider: Any, kind: str) -> None:
    """Even a valid leaked cover ID needs a currently accessible native owner."""
    if kind == "track":
        provider._client.detail = AsyncMock(
            return_value={"track": {**track_data(), "accessStatus": 2}}
        )
    else:
        provider._client.detail = AsyncMock(side_effect=NotFoundError("API code 100005"))
    with pytest.raises((ProviderPermissionDenied, MediaNotFoundError)):
        await provider.resolve_image(f"scoped/test-scope/{kind}/track-test/cover-test")
    provider._client.cover.assert_not_awaited()
    provider._client.page.assert_not_awaited()
    provider._client.playlists.assert_not_awaited()


async def test_track_owned_nested_images_preserve_exact_association(provider: Any) -> None:
    """The root track may authorize its returned album/artist covers, not arbitrary IDs."""
    row = track_data()
    row["album"]["coverId"] = "album-cover"
    row["artists"][0]["coverId"] = "artist-cover"
    provider._client.detail = AsyncMock(return_value={"track": row})
    for cover in ("cover-test", "album-cover", "artist-cover"):
        assert (
            await provider.resolve_image(f"scoped/test-scope/track/track-test/{cover}")
            == b"synthetic-image"
        )
    with pytest.raises(ProviderPermissionDenied):
        await provider.resolve_image("scoped/test-scope/track/track-test/unrelated-cover")
    assert provider._client.cover.await_count == 3
    provider._client.page.assert_not_awaited()


async def test_legacy_cover_is_rejected_without_library_reads(provider: Any) -> None:
    """Unsupported development-era references must not trigger full library scans."""
    with pytest.raises(ProviderPermissionDenied):
        await provider.resolve_image("cover-test")
    provider._client.page.assert_not_awaited()
    provider._client.playlists.assert_not_awaited()
    provider._client.cover.assert_not_awaited()


async def test_unknown_cover_lookup_failure_is_not_denial(provider: Any) -> None:
    """A failed per-owner lookup is a network failure, not a permission decision."""
    provider._client.detail = AsyncMock(side_effect=NetworkError("synthetic"))
    with pytest.raises(ResourceTemporarilyUnavailable):
        await provider.resolve_image("scoped/test-scope/track/track-test/cover-test")


async def test_outside_stream_never_probes_audio(provider: Any) -> None:
    """A denied native track status stops before any audio request."""
    provider._client.detail = AsyncMock(
        return_value={"track": {**track_data(), "guid": "outside", "accessStatus": 2}}
    )
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


async def test_failed_sync_propagates_without_partial_items(provider: Any) -> None:
    """Network failure publishes no partial result, and a subsequent sync can succeed."""
    assert len([item async for item in provider.get_library_tracks()]) == 1
    provider._client.page = AsyncMock(
        side_effect=[
            {"list": [{**track_data(), "guid": "new"}], "total": 2},
            NetworkError("synthetic"),
        ]
    )
    delivered = []

    async def collect() -> None:
        async for item in provider.get_library_tracks():
            delivered.append(item)

    with pytest.raises(ResourceTemporarilyUnavailable):
        await collect()
    assert delivered == []
    provider._client.page = AsyncMock(return_value={"list": [], "total": 0})
    assert [item async for item in provider.get_library_tracks()] == []


@pytest.mark.parametrize(
    ("code", "error"), [(100004, UnplayableMediaError), (100005, MediaNotFoundError)]
)
async def test_stream_business_rejection_is_not_reauthentication(
    provider: Any, code: int, error: type[Exception]
) -> None:
    """Audio business refusals never reach the decoder or trigger login."""
    client = client_with(Response(f'{{"code":{code}}}'.encode()))
    client._token = "synthetic-token"
    provider._client.audio_stream = client.audio_stream
    provider._reauthenticate = AsyncMock()
    details = await provider.get_stream_details("track-test", MediaType.TRACK)
    with pytest.raises(error):
        await anext(provider.get_audio_stream(details))
    provider._reauthenticate.assert_not_awaited()
    assert (await provider.get_track("track-test")).item_id == "track-test"


async def test_album_tracks_without_access_status_preserve_pagination(provider: Any) -> None:
    """Album rows without access flags retain all pages in server order."""
    rows = [{**track_data(), "guid": f"track-{index}"} for index in range(102)]
    for row in rows:
        row.pop("accessStatus")
    provider._client.related = AsyncMock(
        side_effect=[
            {"list": rows[:100], "total": 102},
            {"list": rows[100:], "total": 102},
        ]
    )
    tracks = await provider.get_album_tracks("album-test")
    assert [track.item_id for track in tracks] == [row["guid"] for row in rows]
    assert provider._client.related.await_args_list == [
        call("album", "album-test", 1),
        call("album", "album-test", 2),
    ]
    provider._client.detail.assert_not_awaited()
    provider._client.page.assert_not_awaited()
    provider._client.playlists.assert_not_awaited()


async def test_artist_albums_without_access_status_preserve_filtered_pages(provider: Any) -> None:
    """Parse the server's filtered album pages without extra lookups or reordering."""
    # The synthetic server response omits album-50 and has no per-row access flag.
    rows = [
        {"guid": f"album-{index}", "name": f"Album {index}"}
        for index in reversed(range(103))
        if index != 50
    ]
    provider._client.related = AsyncMock(
        side_effect=[
            {"list": rows[:100], "total": 102},
            {"list": rows[100:], "total": 102},
        ]
    )
    albums = await provider.get_artist_albums("artist-test")
    assert [album.item_id for album in albums] == [row["guid"] for row in rows]
    assert [album.name for album in albums] == [row["name"] for row in rows]
    assert "album-50" not in {album.item_id for album in albums}
    assert provider._client.related.await_args_list == [
        call("artist", "artist-test", 1, albums=True),
        call("artist", "artist-test", 2, albums=True),
    ]
    provider._client.detail.assert_not_awaited()
    provider._client.page.assert_not_awaited()
    provider._client.playlists.assert_not_awaited()


async def test_playlist_filters_hidden_pages_without_truncating(provider: Any) -> None:
    """An intermediate hidden-only page must not hide later playable entries."""
    provider._client.related = AsyncMock(
        side_effect=[
            {"list": [{"guid": "outside", "accessStatus": 2}] * 100, "total": 102},
            {"list": [track_data(), track_data()], "total": 102},
        ]
    )
    tracks = await provider.get_playlist_tracks("playlist-test")
    assert [track.item_id for track in tracks] == ["track-test", "track-test"]
    assert [track.position for track in tracks] == [1, 2]
    provider._client.detail.assert_not_awaited()
    provider._client.page.assert_not_awaited()
    provider._client.playlists.assert_not_awaited()


@pytest.mark.parametrize(
    "fields", [{"accessStatus": value} for value in (2, 3, 4, 99, None, False, "0")] + [{}]
)
async def test_playlist_member_status_is_checked_without_metadata(
    provider: Any, fields: dict[str, Any]
) -> None:
    """Only explicit allowed rows survive; unknown/missing states cannot leak into the cache."""
    denied = {"guid": "outside", "title": "Hidden metadata", **fields}
    provider._client.related = AsyncMock(return_value={"list": [denied, track_data()], "total": 2})
    tracks = await provider.get_playlist_tracks("playlist-test")
    assert [track.item_id for track in tracks] == ["track-test"]
    await asyncio.sleep(0)
    assert "Hidden metadata" not in repr(provider.mass.cache.entries)
    provider._client.detail.assert_not_awaited()
    provider._client.page.assert_not_awaited()
    provider._client.playlists.assert_not_awaited()


@pytest.mark.parametrize("kind", ["album", "artist", "playlist"])
async def test_native_relationship_filtering_does_not_scan_collection(
    provider: Any, kind: str
) -> None:
    """Empty native album/artist relations and hidden playlist errors retain their semantics."""
    if kind == "playlist":
        provider._client.related = AsyncMock(side_effect=NotFoundError("API code 100005"))
        with pytest.raises(MediaNotFoundError):
            await provider.get_playlist_tracks("outside")
    else:
        provider._client.related = AsyncMock(return_value={"list": [], "total": 0})
        fetch = provider.get_album_tracks if kind == "album" else provider.get_artist_albums
        assert await fetch("outside") == []
    provider._client.page.assert_not_awaited()
    provider._client.playlists.assert_not_awaited()
    provider._client.detail.assert_not_awaited()


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
