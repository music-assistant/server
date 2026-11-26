"""Tests for radio stream resolution on the streams controller."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import AudioFormat, ProviderMapping, Radio
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.streams.audio import StreamsAudio
from tests.common import set_music_source_access

PLS_BODY = b"[playlist]\nNumberOfEntries=1\nFile1=http://radio.example.com/stream\n"
HLS_BODY = b"#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:10,\nsegment0.aac\n"


class _FakeContent:
    """Minimal stand-in for aiohttp StreamReader content."""

    def __init__(
        self, raw_data: bytes | Exception, chunk_size: int | None = None, delay: float = 0
    ) -> None:
        self._raw_data = raw_data
        self._chunk_size = chunk_size
        self._delay = delay
        self._pos = 0

    async def read(self, n: int) -> bytes:
        if self._delay:
            await asyncio.sleep(self._delay)
        if isinstance(self._raw_data, Exception):
            raise self._raw_data
        # like the real stream, a read hands over what has arrived so far
        available = len(self._raw_data) - self._pos
        size = min(n, self._chunk_size, available) if self._chunk_size else min(n, available)
        chunk = self._raw_data[self._pos : self._pos + size]
        self._pos += size
        return chunk


class _FakeConnCtx:
    """Async context manager yielding a fake radio probe response."""

    def __init__(
        self,
        headers: dict[str, str],
        raw_data: bytes | Exception = b"",
        charset: str | None = None,
        chunk_size: int | None = None,
        delay: float = 0,
    ) -> None:
        self._resp = MagicMock()
        self._resp.headers = headers
        self._resp.charset = charset
        self._resp.content = _FakeContent(raw_data, chunk_size, delay)

    async def __aenter__(self) -> Any:
        return self._resp

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def _streams_audio() -> StreamsAudio:
    """Build a streams audio controller with an empty resolved-radio cache."""
    mass = MagicMock()
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.set = AsyncMock()
    return StreamsAudio(mass)


@pytest.mark.asyncio
async def test_playlist_is_unwrapped_from_the_probe_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The playlist is read from the probe response instead of being fetched again.

    A fetch of its own would go out over another session and user agent than the rest of
    the radio paths use, which a host is free to answer differently.
    """
    audio = _streams_audio()
    responses = {
        "http://radio.example.com/station.pls": _FakeConnCtx(
            {"content-type": "audio/x-scpls"}, PLS_BODY, chunk_size=8
        ),
        "http://radio.example.com/stream": _FakeConnCtx({"icy-metaint": "16000"}),
    }
    requested: list[str] = []

    def _fake_connect(url: str, **_kwargs: Any) -> _FakeConnCtx:
        requested.append(url)
        return responses[url]

    monkeypatch.setattr(audio, "_connect_radio_stream", _fake_connect)

    result = await audio.resolve_radio_stream("http://radio.example.com/station.pls")

    assert result == ("http://radio.example.com/stream", StreamType.ICY)
    assert requested == [
        "http://radio.example.com/station.pls",
        "http://radio.example.com/stream",
    ]
    cast("MagicMock", audio.mass).http_session.get.assert_not_called()


@pytest.mark.asyncio
async def test_hls_playlist_resolves_as_hls(monkeypatch: pytest.MonkeyPatch) -> None:
    """An HLS station without a telling URL is recognised by its content type."""
    audio = _streams_audio()
    response = _FakeConnCtx({"content-type": "application/vnd.apple.mpegurl"}, HLS_BODY)
    monkeypatch.setattr(audio, "_connect_radio_stream", lambda *_args, **_kwargs: response)

    result = await audio.resolve_radio_stream("http://radio.example.com/live")

    assert result == ("http://radio.example.com/live", StreamType.HLS)
    cast("MagicMock", audio.mass).http_session.get.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "content_type"),
    [
        ("http://radio.example.com/live", "audio/x-mpegurl"),
        ("http://radio.example.com/live.m3u8", "application/octet-stream"),
    ],
)
async def test_hls_without_version_tag_still_resolves_as_hls(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    content_type: str,
) -> None:
    """A version-less HLS media playlist is recognised by its required tag."""
    audio = _streams_audio()
    body = b"#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10.0,\nsegment0.aac\n"
    response = _FakeConnCtx({"content-type": content_type}, body)
    monkeypatch.setattr(audio, "_connect_radio_stream", lambda *_args, **_kwargs: response)

    result = await audio.resolve_radio_stream(url)

    assert result == (url, StreamType.HLS)


@pytest.mark.asyncio
async def test_content_type_is_matched_case_insensitively(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server shouting its media type in mixed case is understood all the same."""
    audio = _streams_audio()
    response = _FakeConnCtx({"content-type": "Application/Vnd.Apple.Mpegurl"}, HLS_BODY)
    monkeypatch.setattr(audio, "_connect_radio_stream", lambda *_args, **_kwargs: response)

    result = await audio.resolve_radio_stream("http://radio.example.com/live")

    assert result == ("http://radio.example.com/live", StreamType.HLS)


@pytest.mark.asyncio
async def test_trickling_playlist_body_gives_up_instead_of_stalling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server feeding the playlist a byte at a time cannot hold up resolving."""
    audio = _streams_audio()
    response = _FakeConnCtx({"content-type": "audio/x-scpls"}, PLS_BODY, chunk_size=1, delay=0.02)
    monkeypatch.setattr(audio, "_connect_radio_stream", lambda *_args, **_kwargs: response)
    # a byte at a time at that pace runs well past the budget
    monkeypatch.setattr("music_assistant.controllers.streams.audio.PLAYLIST_READ_TIMEOUT", 0.05)

    with pytest.raises(InvalidDataError, match="Timeout"):
        await audio.resolve_radio_stream("http://radio.example.com/station.pls")

    cast("MagicMock", audio.mass).cache.set.assert_not_called()


@pytest.mark.asyncio
async def test_truncated_playlist_is_not_cached_as_a_direct_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A playlist body that dies mid-transfer fails instead of being streamed as audio."""
    audio = _streams_audio()
    response = _FakeConnCtx(
        {"content-type": "audio/x-scpls"}, aiohttp.ClientPayloadError("connection closed")
    )
    monkeypatch.setattr(audio, "_connect_radio_stream", lambda *_args, **_kwargs: response)

    with pytest.raises(InvalidDataError):
        await audio.resolve_radio_stream("http://radio.example.com/station.pls")

    cast("MagicMock", audio.mass).cache.set.assert_not_called()


class _FailingConnCtx:
    """Async context manager whose radio probe fails before any response."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def __aenter__(self) -> Any:
        raise self._error

    async def __aexit__(self, *_exc: object) -> bool:
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "cached"),
    [
        (
            aiohttp.ClientConnectorDNSError(MagicMock(), OSError("Timeout while contacting DNS")),
            False,
        ),
        (aiohttp.ServerDisconnectedError(), False),
        (aiohttp.ClientPayloadError("bad response"), True),
    ],
)
async def test_failed_connection_streams_directly_without_caching(
    monkeypatch: pytest.MonkeyPatch, error: Exception, cached: bool
) -> None:
    """
    A connection that fails streams the URL directly but is probed again on the next start.

    A server that answered in a way that cannot be read is still remembered as a direct stream.
    """
    audio = _streams_audio()
    monkeypatch.setattr(
        audio, "_connect_radio_stream", lambda *_args, **_kwargs: _FailingConnCtx(error)
    )
    monkeypatch.setattr(audio, "_validate_shoutcast_stream", AsyncMock(return_value=False))

    result = await audio.resolve_radio_stream("http://radio.example.com/stream")

    assert result == ("http://radio.example.com/stream", StreamType.HTTP)
    assert cast("MagicMock", audio.mass).cache.set.called is cached


_RADIO_URL = "http://radio.example.com/stream"


def _resumable_radio_item(audio: StreamsAudio) -> QueueItem:
    """Return a radio queue item holding unexpired HTTP stream details for _RADIO_URL."""
    mass = cast("MagicMock", audio.mass)
    mass.player_queues.queue_data_or_none.return_value = MagicMock(userid=None)
    mass.webserver.auth.get_user = AsyncMock(return_value=None)
    set_music_source_access(mass, {"radiobrowser": None})
    station = Radio(
        item_id="station",
        provider="radiobrowser",
        name="Station",
        provider_mappings={
            ProviderMapping(
                item_id="station", provider_domain="radiobrowser", provider_instance="radiobrowser"
            )
        },
    )
    queue_item = QueueItem(
        queue_id="q1", queue_item_id="qi1", name="Station", duration=0, media_item=station
    )
    queue_item.streamdetails = StreamDetails(
        provider="radiobrowser",
        item_id="station",
        audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
        media_type=MediaType.RADIO,
        stream_type=StreamType.HTTP,
        path=_RADIO_URL,
    )
    return queue_item


@pytest.mark.asyncio
async def test_resumed_station_is_checked_again_after_a_failed_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Stopping and starting the same radio item checks the station again.

    Its stream details are reused for a while, so a check that failed on a connection error
    would otherwise keep the station playing without track information until they expire.
    """
    audio = _streams_audio()
    queue_item = _resumable_radio_item(audio)
    responses: list[Any] = [
        _FailingConnCtx(aiohttp.ClientConnectorDNSError(MagicMock(), OSError("DNS timeout"))),
        _FakeConnCtx({"icy-metaint": "16000"}),
    ]
    monkeypatch.setattr(audio, "_connect_radio_stream", lambda *_args, **_kwargs: responses.pop(0))
    monkeypatch.setattr(audio, "_validate_shoutcast_stream", AsyncMock(return_value=False))

    # the first start falls back to the plain stream, the start after DNS is back finds ICY
    first = await audio.get_stream_details(queue_item=queue_item)
    assert first.stream_type == StreamType.HTTP
    second = await audio.get_stream_details(queue_item=queue_item)

    assert second is first
    assert second.stream_type == StreamType.ICY


@pytest.mark.asyncio
async def test_resumed_station_keeps_its_stored_details_when_the_check_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resumed station whose re-check fails on a timeout still plays from its stored details."""
    audio = _streams_audio()
    queue_item = _resumable_radio_item(audio)
    stored = queue_item.streamdetails
    monkeypatch.setattr(
        audio, "_connect_radio_stream", lambda *_args, **_kwargs: _FailingConnCtx(TimeoutError())
    )

    result = await audio.get_stream_details(queue_item=queue_item)

    assert result is stored
    assert result.stream_type == StreamType.HTTP
    assert result.path == _RADIO_URL
