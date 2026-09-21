"""Synthetic provider contracts; no NAS connection or server runtime is required."""

import asyncio
import json
import logging
import traceback
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.enums import MediaType, ProviderFeature, StreamType
from music_assistant_models.errors import InvalidDataError, LoginFailed

from music_assistant.constants import UNKNOWN_ARTIST, UNKNOWN_ARTIST_ID_MBID
from music_assistant.helpers.json import json_dumps, json_loads
from music_assistant.providers.feiniu_music import SUPPORTED_FEATURES
from music_assistant.providers.feiniu_music.client import (
    AuthenticationError,
    NetworkError,
    NotFoundError,
    PermissionDeniedError,
    ProtocolError,
    RateLimitError,
    StreamRejectedError,
)
from music_assistant.providers.feiniu_music.parsers import parse_track
from music_assistant.providers.feiniu_music.provider import FeiNiuProvider

from .test_client import Response, client_with


async def test_closing_outer_audio_stream_releases_inner_response(provider: Any) -> None:
    """Closing the provider after one chunk immediately exits the native HTTP context."""
    released = asyncio.Event()

    class RecordingResponse(Response):
        async def __aexit__(self, *_: object) -> None:
            released.set()

    response = RecordingResponse(b"ID3" + b"\x00" * 8192)
    client = client_with(response)
    client._token = "synthetic-token"
    provider._client.audio_stream = client.audio_stream
    provider._client.media_prefix = AsyncMock(side_effect=AssertionError("Unexpected audio probe"))
    details = await provider.get_stream_details("track-test", MediaType.TRACK)
    stream = provider.get_audio_stream(details)
    assert (await anext(stream)).startswith(b"ID3")
    assert not released.is_set()
    await stream.aclose()
    assert released.is_set()
    assert len(client._session.calls) == 1


class MemoryCache:
    """Exercise the actual cache decorator with serialized, instance-scoped entries."""

    def __init__(self) -> None:
        """Start with no saved data."""
        self.entries: dict[tuple[str, str], str] = {}

    async def get_with_freshness(
        self, key: str, *, provider: str, **kwargs: Any
    ) -> tuple[Any, bool, bool]:
        """Read a serialized value."""
        value = self.entries.get((provider, key))
        return (json_loads(value), True, True) if value else (None, False, False)

    async def set(self, key: str, data: Any, *, provider: str, **kwargs: Any) -> None:
        """Save a serialized value."""
        self.entries[provider, key] = json_dumps(data)


def track_data() -> dict[str, Any]:
    """Return a synthetic native track with deliberately private transport fields."""
    return {
        "guid": "track-test",
        "title": "Example",
        "duration": 125000,
        "album": {"guid": "album-test", "name": "Example album"},
        "artists": [{"guid": "artist-test", "name": "Example artist"}],
        "coverId": "cover-test",
        "token": "SECRET-TOKEN",
        "streamUrl": "http://private.invalid/stream?token=SECRET-TOKEN",
        "audioSpec": {
            "path": "/private-nas-home/song.mp3",
            "format": "mp3",
            "codec": "mp3",
            "sampleRate": 44100,
            "channel": 2,
            "bitrate": 320000,
        },
    }


@pytest.fixture
def provider() -> Any:
    """Construct a provider around a synthetic client without booting MA."""
    result: Any = object.__new__(FeiNiuProvider)
    result.config = SimpleNamespace(instance_id="feiniu-test")
    result._client = SimpleNamespace(lyrics=AsyncMock(return_value={"list": []}))
    result._client.page = AsyncMock(return_value={"list": [track_data()], "total": 1})
    result._client.playlists = AsyncMock(
        return_value=[{"guid": "playlist-test"}, {"guid": "empty-playlist"}]
    )
    result._client.detail = AsyncMock(return_value={"track": track_data()})
    result._client.cover = AsyncMock(return_value=b"synthetic-image")
    result._collections = {}
    result._collection_locks = {}
    result._cache_id = "test-load"
    result._image_scope = "test-scope"
    result._closed = False
    result._account_id = None
    result.manifest = SimpleNamespace(domain="feiniu_music")
    result.mass = SimpleNamespace(
        cache=MemoryCache(), create_task=lambda coroutine, **_kwargs: asyncio.create_task(coroutine)
    )
    result.logger = logging.getLogger("feiniu-synthetic-test")
    result._generation = 1
    result._failed_login_generation = None
    result._login_lock = asyncio.Lock()
    return result


def test_media_serialization_never_contains_transport_secrets() -> None:
    """Mappings and image references remain instance scoped and credential free."""
    first = parse_track(track_data(), "instance-a")
    second = parse_track(track_data(), "instance-b")
    serialized = json.dumps(first.to_dict())
    assert "SECRET-TOKEN" not in serialized
    assert "/private-nas-home" not in serialized
    assert "private.invalid" not in serialized
    assert first.provider != second.provider
    assert first.album is not None
    assert first.album.provider == "instance-a"
    assert first.artists[0].provider == "instance-a"
    assert next(iter(first.provider_mappings)).provider_instance == "instance-a"
    assert first.metadata.images is not None
    assert first.metadata.images[0].provider == "instance-a"
    assert not first.metadata.images[0].remotely_accessible
    assert first.duration == 125


@pytest.mark.parametrize("extra", [{}, {"artists": None, "album": None, "audioSpec": None}])
def test_missing_metadata_does_not_fabricate_audio_quality(extra: dict[str, Any]) -> None:
    """Missing artists use MA's explicit placeholder; audio quality stays unknown."""
    item = parse_track({"guid": "missing-fields", **extra}, "instance-a")
    assert item.name == "Untitled track"
    assert len(item.artists) == 1
    assert item.artists[0].name == UNKNOWN_ARTIST
    assert item.artists[0].provider == "instance-a"
    assert item.album is None
    fmt = next(iter(item.provider_mappings)).audio_format
    assert (fmt.sample_rate, fmt.bit_depth, fmt.channels) == (0, 0, 0)
    with pytest.raises(InvalidDataError):
        parse_track({"title": "No stable ID"}, "instance-a")


async def test_unknown_artist_is_local_and_instance_scoped(provider: Any) -> None:
    """The synthetic placeholder must never be sent to a native GUID endpoint."""
    provider._client.detail = AsyncMock(side_effect=AssertionError("Unexpected native request"))
    provider._client.related = AsyncMock(side_effect=AssertionError("Unexpected native request"))
    artist = await provider.get_artist(UNKNOWN_ARTIST)
    assert artist.item_id == UNKNOWN_ARTIST
    assert artist.mbid == UNKNOWN_ARTIST_ID_MBID
    assert next(iter(artist.provider_mappings)).provider_instance == provider.instance_id
    assert await provider.get_artist_albums(UNKNOWN_ARTIST) == []
    provider._client.detail.assert_not_called()
    provider._client.related.assert_not_called()


async def test_concurrent_expiry_performs_one_relogin(provider: Any) -> None:
    """A single generation change services all concurrent expired requests."""
    calls = 0

    async def login() -> None:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        provider._generation += 1

    provider._login = login
    await asyncio.gather(*(provider._reauthenticate(1) for _ in range(5)))
    assert calls == 1


async def test_failed_relogin_does_not_lock_out_user_by_repeated_attempts(
    provider: Any,
) -> None:
    """One failed reauthentication is terminal for that generation."""
    provider._login = AsyncMock(side_effect=AuthenticationError("synthetic failure"))
    for _ in range(3):
        with pytest.raises(LoginFailed):
            await provider._reauthenticate(1)
    assert provider._login.await_count == 1


async def test_reauthentication_retries_operation_only_once(provider: Any) -> None:
    """Repeated 401 responses stop after one retry."""
    provider._reauthenticate = AsyncMock()
    error = AuthenticationError("synthetic failure")
    action = AsyncMock(side_effect=error)
    with pytest.raises(LoginFailed) as raised:
        await provider._call(action)
    assert raised.value is error
    assert action.await_count == 2
    assert provider._reauthenticate.await_count == 1


@pytest.mark.parametrize(
    "error",
    [
        NotFoundError("synthetic"),
        PermissionDeniedError("synthetic"),
        StreamRejectedError("synthetic"),
        ProtocolError("synthetic"),
        NetworkError("synthetic", backoff_time=30),
        RateLimitError("synthetic", backoff_time=60),
    ],
)
async def test_call_preserves_client_error_and_cause(provider: Any, error: Exception) -> None:
    """Non-authentication errors retain their instance, cause and original throw site."""
    cause = ValueError("synthetic origin")
    provider._reauthenticate = AsyncMock()

    async def action() -> None:
        raise error from cause

    with pytest.raises(type(error)) as raised:
        await provider._call(action)
    assert raised.value is error
    assert raised.value.__cause__ is cause
    assert traceback.extract_tb(raised.value.__traceback__)[-1].name == "action"
    provider._reauthenticate.assert_not_awaited()


@pytest.mark.parametrize(
    "error",
    [
        AuthenticationError("synthetic"),
        NotFoundError("synthetic"),
        PermissionDeniedError("synthetic"),
        StreamRejectedError("synthetic"),
        ProtocolError("synthetic"),
        NetworkError("synthetic", backoff_time=30),
        RateLimitError("synthetic", backoff_time=60),
    ],
)
async def test_login_propagates_original_client_error(provider: Any, error: Exception) -> None:
    """Login needs no conversion and cannot advance the generation on failure."""
    provider.get_setup_value = lambda _key: "synthetic"
    provider._client.login = AsyncMock(side_effect=error)
    with pytest.raises(type(error)) as raised:
        await provider._login()
    assert raised.value is error
    assert provider._generation == 1
    provider._client.login.assert_awaited_once()


@pytest.mark.parametrize(
    ("status", "body", "error_type"),
    [(403, b"SECRET", PermissionDeniedError), (200, b'{"code":100004}', StreamRejectedError)],
)
async def test_audio_refusal_does_not_reauthenticate(
    provider: Any,
    status: int,
    body: bytes,
    error_type: Any,
) -> None:
    """Both HTTP permission denial and native stream rejection stop before audio delivery."""
    client = client_with(Response(body, status))
    client._token = "synthetic-token"
    provider._client.audio_stream = client.audio_stream
    provider._reauthenticate = AsyncMock()
    details = SimpleNamespace(provider=provider.instance_id, item_id="track-test", data="test-load")
    with pytest.raises(error_type) as raised:
        await anext(provider.get_audio_stream(details))
    assert "audio_stream" in [
        frame.name for frame in traceback.extract_tb(raised.value.__traceback__)
    ]
    provider._reauthenticate.assert_not_awaited()
    assert len(client._session.calls) == 1


@pytest.mark.parametrize("emit_first", [False, True])
async def test_audio_authentication_retry_is_bounded(provider: Any, emit_first: bool) -> None:
    """An expired stream retries once before any audio, and never after a delivered block."""
    calls = 0
    error = AuthenticationError("synthetic expiry")

    async def audio_stream(_item_id: str) -> AsyncIterator[bytes]:
        nonlocal calls
        calls += 1
        if emit_first:
            yield b"audio"
        raise error

    provider._client.audio_stream = audio_stream
    provider._reauthenticate = AsyncMock()
    details = SimpleNamespace(provider=provider.instance_id, item_id="track-test", data="test-load")
    stream = provider.get_audio_stream(details)
    if emit_first:
        assert await anext(stream) == b"audio"
    with pytest.raises(LoginFailed) as raised:
        await anext(stream)
    assert raised.value is error
    assert calls == (1 if emit_first else 2)
    assert provider._reauthenticate.await_count == (0 if emit_first else 1)


async def test_library_sync_reads_all_pages(provider: Any) -> None:
    """Collection iteration cannot silently truncate the native library."""
    provider._client.page = AsyncMock(
        side_effect=[
            {"list": [{"guid": "one", "title": "One"}], "total": 2},
            {"list": [{"guid": "two", "title": "Two"}], "total": 2},
        ]
    )
    items = [item async for item in provider.get_library_tracks()]
    assert [item.item_id for item in items] == ["one", "two"]
    assert provider._client.page.await_count == 2


async def test_repeated_page_fails_sync(provider: Any) -> None:
    """A broken server cannot be mistaken for a completed sync."""
    provider._client.page = AsyncMock(return_value={"list": [{"guid": "one"}], "total": 2})
    with pytest.raises(InvalidDataError):
        _ = [item async for item in provider.get_library_tracks()]


async def test_stream_details_remain_inside_server(provider: Any) -> None:
    """Stable IDs survive while authentication is absent from stream metadata."""
    data = track_data()
    provider._client.detail = AsyncMock(
        return_value={"track": data, "audioSpec": data["audioSpec"]}
    )
    provider._client.media_prefix = AsyncMock(side_effect=AssertionError("Unexpected audio probe"))
    details = await provider.get_stream_details("track-test", MediaType.TRACK)
    assert details.item_id == "track-test"
    assert details.provider == "feiniu-test"
    assert details.stream_type is StreamType.CUSTOM
    assert details.path is None
    assert details.data == provider._cache_id
    assert not details.extra_input_args
    assert details.allow_seek
    assert not details.can_seek
    provider._client.media_prefix.assert_not_awaited()


async def test_stream_opens_audio_once_and_yields_all_chunks(provider: Any) -> None:
    """Metadata lookup does not open audio; the streaming path consumes one response."""
    payload = b"ID3" + b"x" * 8192
    client = client_with(Response(payload))
    client._token = "synthetic-token"
    provider._client.audio_stream = client.audio_stream
    provider._client.media_prefix = AsyncMock(side_effect=AssertionError("Unexpected audio probe"))
    details = await provider.get_stream_details("track-test", MediaType.TRACK)
    assert not client._session.calls
    chunks = [chunk async for chunk in provider.get_audio_stream(details)]
    assert len(chunks) > 1
    assert b"".join(chunks) == payload
    assert len(client._session.calls) == 1


@pytest.mark.parametrize("payload", [b'{"code":100004}', b"<html>login</html>"])
async def test_stream_details_do_not_bypass_stream_validation(
    provider: Any, payload: bytes
) -> None:
    """Error JSON and login HTML are rejected in the actual audio path."""
    client = client_with(Response(payload))
    client._token = "synthetic-token"
    provider._client.audio_stream = client.audio_stream
    details = await provider.get_stream_details("track-test", MediaType.TRACK)
    with pytest.raises((StreamRejectedError, ProtocolError)):
        await anext(provider.get_audio_stream(details))


async def test_search_limit_closes_pages_immediately(provider: Any) -> None:
    """A limit stops fetching and closes the suspended paging generator before returning."""
    closed = asyncio.Event()

    async def pages(_fetch: Any) -> AsyncIterator[dict[str, Any]]:
        try:
            yield track_data()
            pytest.fail("Search fetched past its limit")
        finally:
            closed.set()

    provider._pages = pages
    provider._client.search = AsyncMock()
    result = await provider.search("Example", [MediaType.TRACK], limit=1)
    assert len(result.tracks) == 1
    assert closed.is_set()


def test_only_verified_read_features_are_declared() -> None:
    """Verified read features are enabled while remote mutations stay disabled."""
    assert ProviderFeature.SEARCH in SUPPORTED_FEATURES
    assert ProviderFeature.LIBRARY_PLAYLISTS in SUPPORTED_FEATURES
    assert not any(feature.name.endswith("_EDIT") for feature in SUPPORTED_FEATURES)


async def test_playlist_page_preserves_duplicates_and_absolute_positions(provider: Any) -> None:
    """Repeated tracks remain separate entries across native page boundaries."""
    provider._client.related = AsyncMock(
        side_effect=[
            {"list": [track_data()] * 100, "total": 102},
            {"list": [track_data(), track_data()], "total": 102},
        ]
    )
    tracks = await provider.get_playlist_tracks("playlist-test", page=1)
    assert [track.item_id for track in tracks] == ["track-test", "track-test"]
    assert [track.position for track in tracks] == [101, 102]
    assert provider._client.related.await_count == 2
    provider._client.related.assert_any_await("playlist", "playlist-test", 2)


async def test_empty_playlist_returns_no_tracks(provider: Any) -> None:
    """A genuine empty playlist is distinct from a request failure."""
    provider._client.related = AsyncMock(return_value={"list": [], "total": 0})
    assert await provider.get_playlist_tracks("empty-playlist") == []


async def test_playlist_sync_reads_complete_collection(provider: Any) -> None:
    """Playlist sync uses the collection endpoint independently of track pagination."""
    provider._client.playlists = AsyncMock(return_value=[{"guid": "one"}, {"guid": "two"}])
    playlists = [item async for item in provider.get_library_playlists()]
    assert len(playlists) == 2
    assert all(not item.is_editable for item in playlists)
    provider._client.playlists.assert_awaited_once()


async def test_playlist_search_reads_pages_and_keeps_instance_ownership(provider: Any) -> None:
    """Native search results reach MA without dropping pages or enabling edits."""
    provider._client.search = AsyncMock(
        side_effect=[
            {"list": [{"guid": "one", "name": "First"}], "total": 3},
            {"list": [{"guid": "two", "name": "Second"}], "total": 3},
        ]
    )
    results = await provider.search("Example", [MediaType.PLAYLIST], limit=2)
    assert [item.item_id for item in results.playlists] == ["one", "two"]
    assert all(
        item.provider == "feiniu-test" and not item.is_editable for item in results.playlists
    )
    assert not results.tracks
    assert provider._client.search.await_count == 2
    provider._client.search.assert_any_await("playlist", "Example", 1, size=2)
    provider._client.search.assert_any_await("playlist", "Example", 2, size=2)


async def test_playlist_search_empty_and_unrequested_types(provider: Any) -> None:
    """A no-match result is empty and unsupported media types cause no request."""
    provider._client.search = AsyncMock(return_value={"list": [], "total": 0})
    assert not (await provider.search("Absent", [MediaType.PLAYLIST])).playlists
    provider._client.search.assert_awaited_once()
    provider._client.search.reset_mock()
    await provider.search("Example", [MediaType.RADIO])
    await provider.search("Example", [MediaType.PLAYLIST], limit=0)
    provider._client.search.assert_not_awaited()


async def test_real_cache_decorator_roundtrip_and_instance_isolation(
    provider: Any,
) -> None:
    """Exercise MA's decorator with its actual JSON serialization and reconstruction."""

    class Cache:
        def __init__(self) -> None:
            self.entries: dict[tuple[str, str], str] = {}

        async def get_with_freshness(
            self, key: str, *, provider: str, **kwargs: Any
        ) -> tuple[Any, bool, bool]:
            value = self.entries.get((provider, key))
            return (json_loads(value), True, True) if value else (None, False, False)

        async def set(self, key: str, data: Any, *, provider: str, **kwargs: Any) -> None:
            self.entries[provider, key] = json_dumps(data)

    mass = SimpleNamespace(
        cache=Cache(), create_task=lambda coroutine, **_kwargs: asyncio.create_task(coroutine)
    )
    provider.mass = mass
    provider.manifest = SimpleNamespace(domain="feiniu_music")
    provider._client.detail = AsyncMock(return_value={"track": track_data()})
    other: Any = object.__new__(FeiNiuProvider)
    other.mass = mass
    other.manifest = provider.manifest
    other.config = SimpleNamespace(instance_id="another-account")
    other._generation = 1
    other._client = SimpleNamespace(
        detail=AsyncMock(return_value={"track": {**track_data(), "title": "Other account"}}),
        lyrics=AsyncMock(return_value={"list": []}),
    )
    other._closed = False
    other._collections = {}
    other._collection_locks = {}
    other._cache_id = "another-load"
    other._image_scope = "another-scope"
    other._client.page = AsyncMock(
        return_value={"list": [{**track_data(), "title": "Other account"}], "total": 1}
    )
    other.logger = provider.logger
    await provider.get_track("track-test")
    await asyncio.sleep(0)
    cached = await provider.get_track("track-test")
    separate = await other.get_track("track-test")
    await asyncio.sleep(0)
    assert cached.name == "Example"
    assert cached.provider == "feiniu-test"
    assert separate.name == "Other account"
    assert separate.provider == "another-account"
    assert provider._client.detail.await_count == 1
    assert other._client.detail.await_count == 1


async def test_track_detail_attaches_lyrics_without_affecting_library_scan(provider: Any) -> None:
    """Fetch source lyrics on detail lookup, not once per row during library sync."""
    provider._client.detail = AsyncMock(return_value={"track": track_data()})
    provider._client.lyrics = AsyncMock(
        return_value={"list": [{"content": "[00:01.00]Synthetic", "offset": 0}]}
    )
    track = await provider.get_track("track-test")
    assert track.metadata.lyrics == "Synthetic"
    assert track.metadata.lrc_lyrics == "[00:01.000]Synthetic"
    provider._client.lyrics.assert_awaited_once_with("track-test")
    provider._client.lyrics.reset_mock()
    provider._client.page = AsyncMock(return_value={"list": [track_data()], "total": 1})
    assert len([item async for item in provider.get_library_tracks()]) == 1
    provider._client.lyrics.assert_not_awaited()


async def test_optional_lyric_failure_keeps_track_and_reports_failure(
    provider: Any, caplog: Any
) -> None:
    """An optional request failure is visible without making the track unplayable."""
    provider._client.detail = AsyncMock(return_value={"track": track_data()})
    provider._client.lyrics = AsyncMock(side_effect=NetworkError("synthetic private response"))
    track = await provider.get_track("track-test")
    assert track.item_id == "track-test"
    assert track.metadata.lyrics is None
    assert "Optional FeiNiu lyrics unavailable" in caplog.text
    assert "synthetic private response" not in caplog.text
