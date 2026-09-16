"""Tests for the remote MP3 seek probe."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from aiohttp import ClientConnectionError, ClientResponseError, ClientSession

from music_assistant.helpers.audio import HTTP_HEADERS
from music_assistant.helpers.mp3 import (
    NO_SEEK_HINTS,
    Mp3SeekHints,
    ffmpeg_http_headers,
    has_mp3_frame,
    parse_id3v2_tag_size,
    probe_mp3_seek_hints,
)

# second header byte per MPEG version, all Layer III without CRC
_MPEG1 = 0xFB
_MPEG2 = 0xF3
_MPEG25 = 0xE3
_MPEG1_LAYER2 = 0xFD
_STEREO = 0x00
_MONO = 0xC0


# frame length for bitrate index 9 at the first sample rate of each version
_FRAME_LENGTHS = {_MPEG1: 417, _MPEG2: 261, _MPEG25: 522, _MPEG1_LAYER2: 522}


def _frame(version: int = _MPEG1, mode: int = _STEREO, tag: bytes = b"") -> bytes:
    """Build a full-length frame, with an optional Info/Xing tag where MPEG1 stereo keeps it."""
    header = bytes([0xFF, version, 0x90, mode])
    body = bytearray(_FRAME_LENGTHS[version] - 4)
    body[32 : 32 + len(tag)] = tag
    return header + bytes(body)


def _id3_header(body_size: int, version: int = 3, flags: int = 0) -> bytes:
    synchsafe = bytes((body_size >> shift) & 0x7F for shift in (21, 14, 7, 0))
    return b"ID3" + bytes([version, 0, flags]) + synchsafe


def _id3_tag(body_size: int, version: int = 3, flags: int = 0) -> bytes:
    footer = b"3DI" + bytes(7) if flags & 0x10 else b""
    return _id3_header(body_size, version, flags) + bytes(body_size) + footer


class _FakeContent:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self, n: int) -> bytes:
        # hand out at most 100 bytes per read, like a real socket might
        chunk, self._data = self._data[: min(n, 100)], self._data[min(n, 100) :]
        return chunk


class _FakeResponse:
    def __init__(self, status: int, data: bytes) -> None:
        self.status = status
        self.content = _FakeContent(data)
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise ClientResponseError(MagicMock(), (), status=self.status)


class _FakeSession:
    """HTTP session double serving a byte blob, with switches for misbehaving servers."""

    def __init__(
        self, blob: bytes, *, honour_range: bool = True, delay: float = 0, status: int = 206
    ) -> None:
        self.blob = blob
        self.honour_range = honour_range
        self.status = status
        self.delay = delay
        self.requests: list[dict[str, str]] = []
        self.responses: list[_FakeResponse] = []

    def get(self, _url: str, headers: dict[str, str]) -> Any:
        self.requests.append(headers)
        return self._respond(headers)

    def _respond(self, headers: dict[str, str]) -> Any:
        session = self

        class _Ctx:
            async def __aenter__(self) -> _FakeResponse:
                if session.delay:
                    await asyncio.sleep(session.delay)
                if session.status != 206:
                    resp = _FakeResponse(session.status, b"")
                elif not session.honour_range:
                    resp = _FakeResponse(200, session.blob)
                else:
                    start, end = headers["Range"].removeprefix("bytes=").split("-")
                    resp = _FakeResponse(206, session.blob[int(start) : int(end) + 1])
                session.responses.append(resp)
                return resp

            async def __aexit__(self, *_args: object) -> None:
                return None

        return _Ctx()


def _session(fake: object) -> ClientSession:
    return cast("ClientSession", fake)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (_id3_header(257), 267),
        (_id3_header(31911853), 31911863),
        (_id3_header(100, version=4), 110),
        (_id3_header(100, version=4, flags=0x10), 120),
        # the footer flag means nothing before v2.4
        (_id3_header(100, version=3, flags=0x10), 110),
        (b"\xff\xfb\x90\x00" + bytes(20), 0),
        (b"ID3\x03\x00\x00\x00\x00\x80\x00", 0),
        (b"ID3\x09\x00\x00\x00\x00\x00\x01", 0),
        (b"ID3\x03\x00", 0),
        (b"", 0),
    ],
    ids=[
        "v2.3",
        "v2.3-large",
        "v2.4",
        "v2.4-footer",
        "v2.3-ignores-footer-flag",
        "no-tag",
        "invalid-synchsafe",
        "unknown-version",
        "truncated",
        "empty",
    ],
)
def test_parse_id3v2_tag_size(data: bytes, expected: int) -> None:
    """The tag size covers the header, the synchsafe body size and a v2.4 footer."""
    assert parse_id3v2_tag_size(data) == expected


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (_frame(_MPEG1, _STEREO) * 2, True),
        (_frame(_MPEG1, _MONO) * 2, True),
        (_frame(_MPEG2, _STEREO) * 2, True),
        (_frame(_MPEG25, _MONO) * 2, True),
        (_frame(_MPEG1_LAYER2, _STEREO) * 2, True),
        (_frame(tag=b"Xing") + _frame(), True),
        # a padded frame is one byte longer
        (b"\xff\xfb\x92\x00" + bytes(414) + _frame(), True),
        # leading junk before the frames is scanned past
        (b"\x00\x00\x00" + _frame() * 2, True),
        # a lone header proves nothing: it has to be followed by a matching frame
        (_frame() + bytes(500), False),
        (_frame(), False),
        (_frame() + b"\x00" + _frame(), False),
        (_frame(_MPEG1) + _frame(_MPEG2), False),
        # AAC ADTS shares the sync word but uses the reserved layer
        (b"\xff\xf1\x50\x80" + bytes(400), False),
        (b"\xff\xfb\xf0\x00" + bytes(400), False),
        (b"\xff\xfb\x00\x00" + bytes(400), False),
        (b"\xff\xfb\x9c\x00" + bytes(400), False),
        (b"\xff\xeb\x90\x00" + bytes(400), False),
        (b"ID3\x03" + bytes(400), False),
        (b"\xff\xfb\x90", False),
        (b"", False),
    ],
    ids=[
        "mpeg1-stereo",
        "mpeg1-mono",
        "mpeg2-stereo",
        "mpeg25-mono",
        "layer2",
        "vbr-header",
        "padded",
        "leading-junk",
        "lone-header",
        "single-frame",
        "misaligned",
        "format-change",
        "adts",
        "bad-bitrate",
        "free-format",
        "bad-samplerate",
        "reserved-version",
        "stacked-tag",
        "truncated-header",
        "empty",
    ],
)
def test_has_mp3_frame(data: bytes, *, expected: bool) -> None:
    """Only a valid frame followed by a matching one counts, wherever it sits in the window."""
    assert has_mp3_frame(data) is expected


def test_ffmpeg_http_headers() -> None:
    """Headers follow what the ffmpeg input args make ffmpeg send."""
    assert ffmpeg_http_headers([]) == HTTP_HEADERS
    assert ffmpeg_http_headers(["-user_agent", "Test/1.0", "-ss", "30"]) == {
        "User-Agent": "Test/1.0"
    }
    assert ffmpeg_http_headers(["-headers", "Authorization: Bearer x\r\nX-Empty:\r\n"]) == {
        **HTTP_HEADERS,
        "Authorization": "Bearer x",
        "X-Empty": "",
    }
    # valueless flags in between do not shift which value belongs to which option
    assert ffmpeg_http_headers(["-re", "-user_agent", "Test/1.0", "-y", "-headers", "A: b"]) == {
        "User-Agent": "Test/1.0",
        "A": "b",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("blob", "expected"),
    [
        (_id3_tag(2000) + _frame(tag=b"Info") * 3, Mp3SeekHints(True, 2010)),
        (
            _id3_tag(2000, version=4, flags=0x10) + _frame(tag=b"Xing") * 2,
            Mp3SeekHints(True, 2020),
        ),
        (_frame(tag=b"Info") * 3, Mp3SeekHints(True, 0)),
        (_frame() * 3, Mp3SeekHints(True, 0)),
        (_id3_tag(2000) + _id3_tag(8000) + _frame(tag=b"Info") * 3, NO_SEEK_HINTS),
        (_frame(tag=b"Info") + bytes(5000), NO_SEEK_HINTS),
        (b"fLaC" + bytes(2000), NO_SEEK_HINTS),
        (bytes(range(256)) * 8, NO_SEEK_HINTS),
        (_id3_tag(2000), NO_SEEK_HINTS),
    ],
    ids=[
        "cbr-tag",
        "vbr-tag-footer",
        "cbr-no-tag",
        "no-header-no-tag",
        "stacked",
        "lone-frame",
        "flac",
        "junk",
        "eof",
    ],
)
async def test_probe_mp3_seek_hints(blob: bytes, expected: Mp3SeekHints) -> None:
    """The probe reads the tag header and the first frame behind it, with the given headers."""
    session = _FakeSession(blob)
    headers = {"User-Agent": "Test/1.0"}

    assert await probe_mp3_seek_hints(_session(session), "http://x/a.mp3", headers) == expected
    assert all(request["User-Agent"] == "Test/1.0" for request in session.requests)


@pytest.mark.asyncio
async def test_probe_mp3_seek_hints_range_ignored() -> None:
    """A server that ignores the range gets its connection dropped and no hints."""
    session = _FakeSession(_id3_tag(2000) + _frame(tag=b"Info"), honour_range=False)

    assert await probe_mp3_seek_hints(_session(session), "http://x/a.mp3", {}) == NO_SEEK_HINTS
    assert len(session.responses) == 1
    assert session.responses[0].closed


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 429, 503])
async def test_probe_mp3_seek_hints_error_status(status: int) -> None:
    """An error status may pass, so it fails the probe rather than ruling the file out."""
    session = _FakeSession(_frame() * 3, status=status)

    assert await probe_mp3_seek_hints(_session(session), "http://x/a.mp3", {}) is None


@pytest.mark.asyncio
async def test_probe_mp3_seek_hints_timeout() -> None:
    """A slow server runs into the probe timeout instead of holding up the seek."""
    session = _FakeSession(_frame(tag=b"Info"), delay=1)

    hints = await probe_mp3_seek_hints(_session(session), "http://x/a.mp3", {}, timeout=0.01)

    assert hints is None


@pytest.mark.asyncio
async def test_probe_mp3_seek_hints_http_error() -> None:
    """A failing request reports that the file could not be probed, without raising."""

    class _BrokenSession:
        def get(self, *_args: object, **_kwargs: object) -> Any:
            raise ClientConnectionError("boom")

    hints = await probe_mp3_seek_hints(_session(_BrokenSession()), "http://x/a.mp3", {})

    assert hints is None


@pytest.mark.asyncio
async def test_probe_mp3_seek_hints_rejected_header() -> None:
    """A header value aiohttp refuses to send does not break the seek."""
    async with ClientSession() as session:
        hints = await probe_mp3_seek_hints(
            session, "http://127.0.0.1:9/a.mp3", {"X-Bad": "a\r\nb"}, timeout=1
        )

    assert hints is None
