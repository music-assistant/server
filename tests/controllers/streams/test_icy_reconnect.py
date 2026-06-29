"""Tests for ICY radio stream reconnection on disconnect."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    ProviderPermissionDenied,
    RetriesExhausted,
)
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import MultiPartPath, StreamDetails

from music_assistant.controllers.streams.audio import StreamsAudio

_META_INT = 4


class _FakeContent:
    """Minimal stand-in for aiohttp StreamReader content, driven by a script of frames."""

    def __init__(self, frames: list[bytes | Exception]) -> None:
        self._frames = list(frames)

    async def readexactly(self, _n: int) -> bytes:
        if not self._frames:
            raise asyncio.IncompleteReadError(b"", _n)
        item = self._frames.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeConnCtx:
    """Async context manager yielding a fake ICY response."""

    def __init__(
        self, frames: list[bytes | Exception], status: int = 200, meta_int: str = str(_META_INT)
    ) -> None:
        self._resp = MagicMock()
        self._resp.headers = {"icy-metaint": meta_int}
        self._resp.content = _FakeContent(frames)
        if status >= 400:
            self._resp.raise_for_status.side_effect = aiohttp.ClientResponseError(
                MagicMock(), (), status=status
            )

    async def __aenter__(self) -> Any:
        return self._resp

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def _radio_streamdetails() -> StreamDetails:
    return StreamDetails(
        provider="test_provider",
        item_id="radio1",
        audio_format=AudioFormat(content_type=ContentType.MP3),
        media_type=MediaType.RADIO,
        stream_type=StreamType.ICY,
        path="http://example.test/radio.mp3",
    )


@pytest.mark.asyncio
async def test_icy_stream_reconnects_after_disconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mid-stream disconnect transparently reconnects and keeps yielding audio."""
    audio = StreamsAudio(MagicMock())

    # connection #1 yields one audio frame then drops; connection #2 yields another.
    connections = [
        _FakeConnCtx([b"AAAA", b"\x00", asyncio.IncompleteReadError(b"", _META_INT)]),
        _FakeConnCtx([b"BBBB", b"\x00", asyncio.IncompleteReadError(b"", _META_INT)]),
    ]
    connect_calls = 0

    def _fake_connect(*_args: Any, **_kwargs: Any) -> _FakeConnCtx:
        nonlocal connect_calls
        ctx = connections[connect_calls]
        connect_calls += 1
        return ctx

    monkeypatch.setattr(audio, "_connect_radio_stream", _fake_connect)

    chunks: list[bytes] = []
    async for chunk in audio.get_icy_radio_stream(
        "http://example.test/radio.mp3", _radio_streamdetails()
    ):
        chunks.append(chunk)
        if len(chunks) == 2:
            # got one frame from each connection - reconnection proven
            break

    assert chunks == [b"AAAA", b"BBBB"]
    assert connect_calls == 2


@pytest.mark.asyncio
async def test_icy_stream_raises_on_http_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 404 response is terminal and surfaces as MediaNotFoundError, not a reconnect."""
    audio = StreamsAudio(MagicMock())

    monkeypatch.setattr(
        audio, "_connect_radio_stream", lambda *_a, **_k: _FakeConnCtx([], status=404)
    )

    with pytest.raises(MediaNotFoundError):
        async for _chunk in audio.get_icy_radio_stream(
            "http://example.test/radio.mp3", _radio_streamdetails()
        ):
            pass


@pytest.mark.asyncio
async def test_icy_stream_gives_up_when_no_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connection that never delivers audio bails out instead of reconnecting forever."""
    audio = StreamsAudio(MagicMock())
    connect_calls = 0

    def _fake_connect(*_args: Any, **_kwargs: Any) -> _FakeConnCtx:
        nonlocal connect_calls
        connect_calls += 1
        # empty frames -> readexactly immediately raises IncompleteReadError (no data)
        return _FakeConnCtx([])

    monkeypatch.setattr(audio, "_connect_radio_stream", _fake_connect)
    # don't actually wait out the backoff between reconnect attempts
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    with pytest.raises(RetriesExhausted):
        async for _chunk in audio.get_icy_radio_stream(
            "http://example.test/radio.mp3", _radio_streamdetails()
        ):
            pass

    # bails after the budget is exhausted rather than spinning indefinitely
    assert connect_calls < 50


@pytest.mark.asyncio
async def test_icy_stream_invalid_metaint_is_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed icy-metaint header raises a controlled InvalidDataError, not a raw ValueError."""
    audio = StreamsAudio(MagicMock())
    monkeypatch.setattr(
        audio, "_connect_radio_stream", lambda *_a, **_k: _FakeConnCtx([], meta_int="not-a-number")
    )

    with pytest.raises(InvalidDataError):
        async for _chunk in audio.get_icy_radio_stream(
            "http://example.test/radio.mp3", _radio_streamdetails()
        ):
            pass


@pytest.mark.asyncio
async def test_icy_failover_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mirror returning an HTTP error fails over to the next mirror URL."""
    audio = StreamsAudio(MagicMock())
    seen: list[str] = []

    def _connect(url: str, *_a: Any, **_k: Any) -> _FakeConnCtx:
        seen.append(url)
        if url == "http://primary.test/stream":
            return _FakeConnCtx([], status=403)
        return _FakeConnCtx([b"BBBB"])

    monkeypatch.setattr(audio, "_connect_radio_stream", _connect)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    mirrors = [
        MultiPartPath(path="http://primary.test/stream"),
        MultiPartPath(path="http://backup.test/stream"),
    ]
    chunks: list[bytes] = []
    async for chunk in audio.get_reconnecting_icy_radio_stream(mirrors, _radio_streamdetails()):
        chunks.append(chunk)
        break

    assert chunks == [b"BBBB"]
    assert seen == ["http://primary.test/stream", "http://backup.test/stream"]


@pytest.mark.asyncio
async def test_icy_failover_raises_when_all_mirrors_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """When every mirror is unreachable the last terminal error is raised, not spun on forever."""
    audio = StreamsAudio(MagicMock())
    connect_calls = 0

    def _connect(_url: str, *_a: Any, **_k: Any) -> _FakeConnCtx:
        nonlocal connect_calls
        connect_calls += 1
        return _FakeConnCtx([], status=403)

    monkeypatch.setattr(audio, "_connect_radio_stream", _connect)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    mirrors = [
        MultiPartPath(path="http://a.test/stream"),
        MultiPartPath(path="http://b.test/stream"),
    ]
    with pytest.raises(ProviderPermissionDenied):
        async for _chunk in audio.get_reconnecting_icy_radio_stream(
            mirrors, _radio_streamdetails()
        ):
            pass

    # bounded failover - does not retry indefinitely
    assert connect_calls <= len(mirrors) * 2 + 1


@pytest.mark.asyncio
async def test_icy_single_mirror_propagates_terminal_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single mirror URL delegates to get_icy_radio_stream and surfaces its terminal error."""
    audio = StreamsAudio(MagicMock())
    monkeypatch.setattr(
        audio, "_connect_radio_stream", lambda *_a, **_k: _FakeConnCtx([], status=404)
    )

    with pytest.raises(MediaNotFoundError):
        async for _chunk in audio.get_reconnecting_icy_radio_stream(
            [MultiPartPath(path="http://only.test/stream")], _radio_streamdetails()
        ):
            pass


@pytest.mark.asyncio
async def test_icy_failover_requires_at_least_one_url() -> None:
    """An empty mirror list is rejected with InvalidDataError."""
    audio = StreamsAudio(MagicMock())
    with pytest.raises(InvalidDataError):
        async for _chunk in audio.get_reconnecting_icy_radio_stream([], _radio_streamdetails()):
            pass
