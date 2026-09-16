"""Tests for the remote MP3 seek probe."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from aiohttp import ClientConnectionError, ClientSession

from music_assistant.helpers.audio import HTTP_HEADERS
from music_assistant.helpers.mp3 import (
    NO_SEEK_HINTS,
    Mp3SeekHints,
    ffmpeg_http_headers,
    parse_first_mp3_frame,
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


def _frame(version: int = _MPEG1, mode: int = _STEREO, tag: bytes = b"", offset: int = 36) -> bytes:
    """Build a 128 kbps frame with an optional VBR/CBR header tag at the given offset."""
    header = bytes([0xFF, version, 0x90, mode])
    body = bytearray(400)
    body[offset - 4 : offset - 4 + len(tag)] = tag
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


class _FakeSession:
    """HTTP session double serving a byte blob, with switches for misbehaving servers."""

    def __init__(self, blob: bytes, *, honour_range: bool = True, delay: float = 0) -> None:
        self.blob = blob
        self.honour_range = honour_range
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
                if not session.honour_range:
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
        (_frame(_MPEG1, _STEREO, b"Info", 36), True),
        (_frame(_MPEG1, _MONO, b"Info", 21), True),
        (_frame(_MPEG2, _STEREO, b"Info", 21), True),
        (_frame(_MPEG2, _MONO, b"Info", 13), True),
        (_frame(_MPEG25, _MONO, b"Info", 13), True),
        (_frame(_MPEG1, _STEREO, b"Xing", 36), False),
        (_frame(_MPEG2, _MONO, b"Xing", 13), False),
        (_frame(_MPEG1, _STEREO, b"VBRI", 36), False),
        (_frame(_MPEG1, _STEREO), False),
        # an Info tag at the stereo offset does not count for a mono frame
        (_frame(_MPEG1, _MONO, b"Info", 36), False),
        (_frame(_MPEG1_LAYER2, _STEREO, b"Info", 36), False),
        # leading junk before the frame is scanned past
        (b"\x00\x00\x00" + _frame(_MPEG1, _STEREO, b"Info", 36), True),
        # AAC ADTS shares the sync word but uses the reserved layer
        (b"\xff\xf1\x50\x80" + bytes(400), None),
        (b"\xff\xfb\xf0\x00" + bytes(400), None),
        (b"\xff\xfb\x9c\x00" + bytes(400), None),
        (b"\xff\xeb\x90\x00" + bytes(400), None),
        (b"ID3\x03" + bytes(400), None),
        (_frame(_MPEG1, _STEREO)[:20], None),
        (b"", None),
    ],
    ids=[
        "mpeg1-stereo-info",
        "mpeg1-mono-info",
        "mpeg2-stereo-info",
        "mpeg2-mono-info",
        "mpeg25-mono-info",
        "mpeg1-xing",
        "mpeg2-mono-xing",
        "vbri",
        "no-header",
        "info-at-wrong-offset",
        "layer2",
        "leading-junk",
        "adts",
        "bad-bitrate",
        "bad-samplerate",
        "reserved-version",
        "stacked-tag",
        "truncated-frame",
        "empty",
    ],
)
def test_parse_first_mp3_frame(data: bytes, expected: bool | None) -> None:
    """Only a Layer III frame with an Info header at its side-info offset is CBR."""
    assert parse_first_mp3_frame(data) is expected


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("blob", "expected"),
    [
        (_id3_tag(2000) + _frame(tag=b"Info") * 3, Mp3SeekHints(2010, True)),
        (_id3_tag(2000, version=4, flags=0x10) + _frame(tag=b"Xing"), Mp3SeekHints(2020, False)),
        (_frame(tag=b"Info") * 3, Mp3SeekHints(0, True)),
        (_frame() * 3, Mp3SeekHints(0, False)),
        (_id3_tag(2000) + _id3_tag(2000) + _frame(tag=b"Info"), NO_SEEK_HINTS),
        (b"fLaC" + bytes(2000), NO_SEEK_HINTS),
        (bytes(range(256)) * 8, NO_SEEK_HINTS),
        (_id3_tag(2000), NO_SEEK_HINTS),
    ],
    ids=["cbr-tag", "vbr-tag-footer", "cbr-no-tag", "vbr-no-tag", "stacked", "flac", "junk", "eof"],
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
