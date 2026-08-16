"""Tests for radio stream resolution on the streams controller."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from music_assistant_models.enums import StreamType
from music_assistant_models.errors import InvalidDataError

from music_assistant.controllers.streams.audio import StreamsAudio

PLS_BODY = b"[playlist]\nNumberOfEntries=1\nFile1=http://radio.example.com/stream\n"
HLS_BODY = b"#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:10,\nsegment0.aac\n"


class _FakeContent:
    """Minimal stand-in for aiohttp StreamReader content."""

    def __init__(self, raw_data: bytes | Exception, chunk_size: int | None = None) -> None:
        self._raw_data = raw_data
        self._chunk_size = chunk_size
        self._pos = 0

    async def read(self, n: int) -> bytes:
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
    ) -> None:
        self._resp = MagicMock()
        self._resp.headers = headers
        self._resp.charset = charset
        self._resp.content = _FakeContent(raw_data, chunk_size)

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
