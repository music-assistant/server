"""Public behavior regressions for bounded lyrics, cached listings and full streams."""

import asyncio
import time
from collections.abc import AsyncGenerator
from copy import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import Album, Artist, ItemMapping, Track

from music_assistant.constants import DB_TABLE_CACHE
from music_assistant.controllers.cache import CacheController
from music_assistant.controllers.cache import controller as cache_module
from music_assistant.providers.feiniu_music import lyrics as lyrics_module
from music_assistant.providers.feiniu_music.client import NetworkError, ProtocolError

from .test_client import Response, client_with
from .test_provider import provider as provider  # noqa: PLC0414
from .test_provider import track_data


@pytest.fixture
async def cached_provider(provider: Any, tmp_path: Path) -> AsyncGenerator[Any]:
    """Use the real MA cache, refresh context and temporary SQLite database."""
    tasks: list[asyncio.Task[Any]] = []

    def create_task(coroutine: Any, **_kwargs: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine)
        tasks.append(task)
        return task

    async def drain() -> None:
        while pending := [task for task in tasks if not task.done()]:
            await asyncio.gather(*pending)

    provider.mass.cache_path = str(tmp_path)
    provider.mass.config = SimpleNamespace(get_raw_core_config_value=lambda *_: "GLOBAL")
    provider.mass.create_task = create_task
    provider.mass.drain_tasks = drain
    cache = provider.mass.cache = CacheController(provider.mass)
    await cache._setup_database()
    try:
        yield provider
    finally:
        await drain()
        await cache.close()


def test_lyrics_reject_expansion_before_calling_expanding_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A small response must be rejected before either expansion allocates output."""
    expand = Mock(side_effect=AssertionError("Expansion was reached"))
    monkeypatch.setattr(lyrics_module, "normalize_lrc_lyrics", expand)
    content = "[00:01.00]" * 1000 + "x" * 10000
    with pytest.raises(InvalidDataError):
        lyrics_module.parse_lyrics({"list": [{"content": content}]})
    expand.assert_not_called()


def test_reasonable_repeated_lyric_line_still_works() -> None:
    """Repeated chorus timestamps preserve text and native offset alignment."""
    content = "[00:02.00]" * 100 + "A reasonable chorus"
    plain, synced = lyrics_module.parse_lyrics({"list": [{"content": content, "offset": 500}]})
    assert plain == "\n".join(["A reasonable chorus"] * 100)
    assert synced == "\n".join(["[00:01.500]A reasonable chorus"] * 100)


@pytest.mark.parametrize("budget", [1149, 1150, 1151])
def test_lyric_expansion_budget_boundary(monkeypatch: pytest.MonkeyPatch, budget: int) -> None:
    """The conservative preallocation estimate accepts equality and rejects overflow."""
    # Ten nine-character timestamps, ten body characters, fifteen reserved per copy.
    content = "[00:01.0]" * 10 + "x" * 10
    monkeypatch.setattr(lyrics_module, "_MAX_EXPANDED_CHARS", budget)
    if budget < 1150:
        with pytest.raises(InvalidDataError):
            lyrics_module.parse_lyrics({"list": [{"content": content}]})
    else:
        plain, synced = lyrics_module.parse_lyrics({"list": [{"content": content}]})
        assert plain == "\n".join(["x" * 10] * 10)
        assert synced == "\n".join(["[00:01.000]" + "x" * 10] * 10)


def test_enhanced_word_timing_cannot_hide_expanding_timestamps() -> None:
    """Removing word timing must not reveal an unchecked second expansion."""
    content = "[00:01.0]" * 100 + "<00:02.0>" + "[00:03.0]" * 100 + "x" * 1000
    with pytest.raises(InvalidDataError):
        lyrics_module.parse_lyrics({"list": [{"content": content}]})


async def test_excessive_lyrics_do_not_break_track_or_stream_details(provider: Any) -> None:
    """Optional lyric rejection keeps the track playable and its metadata intact."""
    provider._client.lyrics.return_value = {
        "list": [{"content": "[00:01.00]" * 1000 + "x" * 10000}]
    }
    track = await provider.get_track("track-test")
    assert track.name == "Example"
    assert track.metadata.lyrics is None
    assert track.metadata.lrc_lyrics is None
    assert (
        await provider.get_stream_details("track-test", MediaType.TRACK)
    ).item_id == track.item_id


@pytest.mark.parametrize("count", [200, 250])
async def test_playlist_refresh_requests_grow_with_native_pages(
    cached_provider: Any, count: int
) -> None:
    """Normal reads cache; forced refresh fetches each native page only once."""
    provider = cached_provider
    rows = [{**track_data(), "guid": f"track-{index % 17}"} for index in range(count)]
    for row in rows[100:200]:
        row["accessStatus"] = 2
    expected = [row["guid"] for row in rows if row["accessStatus"] == 0]

    async def related(_kind: str, _item_id: str, page: int) -> dict[str, Any]:
        return {"list": rows[(page - 1) * 100 : page * 100], "total": count}

    provider._client.related = AsyncMock(side_effect=related)
    for refresh, expected_requests in (
        (False, (count + 99) // 100),
        (False, 0),
        (True, (count + 99) // 100),
    ):
        provider._client.related.reset_mock()
        result = []
        page = 0
        while True:
            async with provider.mass.cache.handle_refresh(refresh):
                tracks = await provider.get_playlist_tracks("playlist", page)
            if not tracks:
                break
            result.extend(tracks)
            page += 1
            assert page <= 3
        await provider.mass.drain_tasks()
        assert [track.item_id for track in result] == expected
        assert [track.position for track in result] == list(range(1, len(expected) + 1))
        assert provider._client.related.await_count == expected_requests


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"accessStatus": None},
        {"accessStatus": False},
        {"accessStatus": "0"},
        {"accessStatus": 3},
        {"accessStatus": 99},
    ],
)
async def test_malformed_playlist_is_not_cached_and_recovers(
    cached_provider: Any, fields: dict[str, Any]
) -> None:
    """Unknown states are failures, never successful empty or partial playlists."""
    provider = cached_provider
    provider._client.related = AsyncMock(
        return_value={"list": [track_data(), {"guid": "broken", **fields}], "total": 2}
    )
    with pytest.raises(InvalidDataError):
        await provider.get_playlist_tracks("playlist")
    await provider.mass.drain_tasks()
    assert await provider.mass.cache.database.get_count(DB_TABLE_CACHE) == 0
    provider._client.related.return_value = {"list": [track_data()], "total": 1}
    assert len(await provider.get_playlist_tracks("playlist")) == 1
    assert provider._client.related.await_count == 2


@pytest.mark.parametrize("kind", ["album", "artist"])
async def test_relation_cache_refresh_expiry_and_identity(
    cached_provider: Any, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Actual MA cache honors refresh, TTL, provider instance and reload identity."""
    provider = cached_provider
    row = track_data() if kind == "album" else {"guid": "album", "name": "Album"}
    provider._client.related = AsyncMock(return_value={"list": [row], "total": 1})
    method = "get_album_tracks" if kind == "album" else "get_artist_albums"
    fetch = getattr(provider, method)
    assert len(await fetch("parent")) == 1
    await provider.mass.drain_tasks()
    assert len(await fetch("parent")) == 1
    assert provider._client.related.await_count == 1
    async with provider.mass.cache.handle_refresh(True):
        assert len(await fetch("parent")) == 1
    await provider.mass.drain_tasks()
    assert provider._client.related.await_count == 2
    later = time.time() + 31
    monkeypatch.setattr(cache_module, "time", SimpleNamespace(time=lambda: later))
    assert len(await fetch("parent")) == 1
    await provider.mass.drain_tasks()
    assert provider._client.related.await_count == 3
    provider._cache_id = "reloaded"
    assert len(await fetch("parent")) == 1
    await provider.mass.drain_tasks()
    assert provider._client.related.await_count == 4
    other = copy(provider)
    other.config = SimpleNamespace(instance_id="other-instance")
    assert len(await getattr(other, method)("parent")) == 1
    assert provider._client.related.await_count == 5


@pytest.mark.parametrize("kind", ["album", "artist"])
async def test_failed_relation_lookup_can_recover(cached_provider: Any, kind: str) -> None:
    """A transient failure never becomes a cached empty relationship."""
    provider = cached_provider
    row = track_data() if kind == "album" else {"guid": "album", "name": "Album"}
    provider._client.related = AsyncMock(
        side_effect=[NetworkError("temporary"), {"list": [row], "total": 1}]
    )
    fetch = provider.get_album_tracks if kind == "album" else provider.get_artist_albums
    with pytest.raises(NetworkError):
        await fetch("parent")
    assert len(await fetch("parent")) == 1
    assert provider._client.related.await_count == 2


@pytest.mark.parametrize("source", ["library", "search", "album", "playlist"])
async def test_track_listings_compact_only_nested_items_and_keep_artwork(
    provider: Any, source: str
) -> None:
    """Compact relations retain scoped images without any extra detail lookup."""
    row = track_data()
    row["album"]["coverId"] = "album-cover"
    row["artists"][0]["coverId"] = "artist-cover"
    data = {"list": [row], "total": 1}
    provider._client.page = AsyncMock(return_value=data)
    provider._client.search = AsyncMock(return_value=data)
    provider._client.related = AsyncMock(return_value=data)
    provider._client.detail = AsyncMock(return_value={"track": row})
    if source == "library":
        track = await anext(provider.get_library_tracks())
    elif source == "search":
        track = (await provider.search("Example", [MediaType.TRACK])).tracks[0]
    elif source == "album":
        track = (await provider.get_album_tracks("album"))[0]
    else:
        track = (await provider.get_playlist_tracks("playlist"))[0]
    assert isinstance(track, Track)
    assert isinstance(track.album, ItemMapping)
    assert isinstance(track.artists[0], ItemMapping)
    assert (track.album.item_id, track.album.name) == ("album-test", "Example album")
    assert track.album.provider == track.artists[0].provider == provider.instance_id
    provider._client.detail.assert_not_awaited()
    for nested in (track.album, track.artists[0]):
        assert "metadata" not in nested.to_dict()
        assert "provider_mappings" not in nested.to_dict()
        assert nested.image is not None
        assert not nested.image.remotely_accessible
        assert nested.image.path.startswith("scoped/test-scope/track/track-test/")
        assert await provider.resolve_image(nested.image.path) == b"synthetic-image"
    detail = await provider.get_track("track-test")
    assert isinstance(detail.album, Album)
    assert isinstance(detail.artists[0], Artist)


@pytest.mark.parametrize(
    "content_range", [None, "broken", "bytes 1-4096/4097", "bytes 0-4095/1000000", "bytes 0-4095/*"]
)
async def test_full_stream_rejects_partial_or_malformed_206_and_cleans_up(
    content_range: str | None,
) -> None:
    """A successful status cannot turn an unsolicited fragment into a full track."""
    exited = asyncio.Event()

    class RecordedResponse(Response):
        async def __aexit__(self, *_: object) -> None:
            exited.set()

    headers = {"Content-Length": "4096"}
    if content_range is not None:
        headers["Content-Range"] = content_range
    client = client_with(RecordedResponse(b"fLaC" + bytes(4092), 206, headers))
    client._token = "synthetic-token"
    with pytest.raises(ProtocolError):
        _ = [chunk async for chunk in client.audio_stream("track")]
    assert exited.is_set()
    assert not client._responses
    assert "Range" not in client._session.calls[0][2].get("headers", {})


async def test_full_covering_206_and_explicit_range_probe_remain_supported() -> None:
    """A complete 206 is distinguishable from an intentional partial range probe."""
    payload = b"fLaC" + bytes(96)
    client = client_with(
        Response(payload, 206, {"Content-Range": "bytes 0-99/100", "Content-Length": "100"})
    )
    client._token = "synthetic-token"
    assert b"".join([chunk async for chunk in client.audio_stream("track")]) == payload
    probe = client_with(Response(payload, 206, {"Content-Range": "bytes 50-149/1000"}))
    info, data = await probe.media_prefix("audio", "track", start=50, limit=100, validate=True)
    assert info["status"] == 206
    assert data == payload
    assert probe._session.calls[0][2]["headers"]["Range"] == "bytes=50-149"


@pytest.mark.parametrize("length", ["99", "101", "invalid", "-100"])
async def test_full_stream_rejects_inconsistent_206_content_length(length: str) -> None:
    """Even a full Content-Range cannot excuse contradictory framing."""
    client = client_with(
        Response(
            b"fLaC" + bytes(96), 206, {"Content-Range": "bytes 0-99/100", "Content-Length": length}
        )
    )
    client._token = "synthetic-token"
    with pytest.raises(ProtocolError):
        await anext(client.audio_stream("track"))
    assert not client._responses


@pytest.mark.parametrize("actual_size", [100, 8999, 9000, 9001])
async def test_206_stream_checks_actual_bytes_without_content_length(actual_size: int) -> None:
    """Range totals are enforced across prefix and subsequent chunks, including EOF."""
    payload = b"fLaC" + bytes(actual_size - 4)
    client = client_with(Response(payload, 206, {"Content-Range": "bytes 0-8999/9000"}))
    client._token = "synthetic-token"
    chunks = []
    if actual_size == 9000:
        chunks = [chunk async for chunk in client.audio_stream("track")]
        assert b"".join(chunks) == payload
    else:

        async def collect() -> None:
            async for chunk in client.audio_stream("track"):
                chunks.append(chunk)

        with pytest.raises(ProtocolError):
            await collect()
        assert sum(map(len, chunks)) <= 9000
    assert not client._responses


@pytest.mark.parametrize("source", ["library", "search", "artist"])
async def test_album_listings_compact_artists_with_owned_artwork(
    provider: Any, source: str
) -> None:
    """Full albums retain only compact artist references without metadata requests."""
    row = {
        "guid": "album-test",
        "name": "Album",
        "artists": [{"guid": "artist-test", "name": "Artist", "coverId": "artist-cover"}],
    }
    data = {"list": [row], "total": 1}
    provider._client.page = AsyncMock(return_value=data)
    provider._client.search = AsyncMock(return_value=data)
    provider._client.related = AsyncMock(return_value=data)
    provider._client.detail = AsyncMock(return_value=row)
    if source == "library":
        album = await anext(provider.get_library_albums())
    elif source == "search":
        album = (await provider.search("Album", [MediaType.ALBUM])).albums[0]
    else:
        album = (await provider.get_artist_albums("artist-test"))[0]
    assert isinstance(album, Album)
    artist = album.artists[0]
    assert isinstance(artist, ItemMapping)
    assert (artist.item_id, artist.name, artist.provider) == (
        "artist-test",
        "Artist",
        provider.instance_id,
    )
    provider._client.detail.assert_not_awaited()
    assert artist.image is not None
    assert artist.image.path.startswith("scoped/test-scope/album/album-test/")
    assert "metadata" not in artist.to_dict()
    assert await provider.resolve_image(artist.image.path) == b"synthetic-image"
