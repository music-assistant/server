"""Helpers for seeking in remote MP3 streams."""

from __future__ import annotations

import asyncio
import logging
from itertools import pairwise
from typing import TYPE_CHECKING, Final, NamedTuple

from aiohttp import ClientError

from music_assistant.constants import MASS_LOGGER_NAME

from .audio import HTTP_HEADERS

if TYPE_CHECKING:
    from collections.abc import Sequence

    from aiohttp import ClientSession

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.helpers.mp3")

ID3V2_HEADER_SIZE: Final[int] = 10
# large enough for the frame header plus the Xing/Info or VBRI header behind it
FIRST_FRAME_WINDOW: Final[int] = 512
PROBE_TIMEOUT: Final[float] = 3.0

# offset of the Xing/Info header behind the side info, keyed by (is_mpeg1, is_mono),
# the same table ffmpeg's mp3 demuxer reads it from
_XING_OFFSETS: Final[dict[tuple[bool, bool], int]] = {
    (True, False): 4 + 32,
    (True, True): 4 + 17,
    (False, False): 4 + 17,
    (False, True): 4 + 9,
}
_VBRI_OFFSET: Final[int] = 4 + 32


class Mp3SeekHints(NamedTuple):
    """What a remote MP3 allows ffmpeg to skip when it seeks."""

    skip_bytes: int
    is_cbr: bool


NO_SEEK_HINTS: Final[Mp3SeekHints] = Mp3SeekHints(0, False)


async def probe_mp3_seek_hints(
    http_session: ClientSession,
    url: str,
    headers: dict[str, str],
    timeout: float = PROBE_TIMEOUT,
) -> Mp3SeekHints | None:
    """
    Read the head of a remote MP3 to find out how ffmpeg can seek in it quickly.

    Returns NO_SEEK_HINTS when the file offers no shortcut, and None when it could not be
    read (timeout or network error), in which case asking again later may still succeed.

    :param http_session: The HTTP session to fetch the byte ranges with.
    :param url: URL of the MP3 file.
    :param headers: HTTP headers ffmpeg sends for this URL.
    :param timeout: Maximum seconds the whole probe may take.
    """
    try:
        async with asyncio.timeout(timeout):
            head = await _fetch_range(http_session, url, headers, 0, FIRST_FRAME_WINDOW)
            if head is None:
                return NO_SEEK_HINTS
            skip_bytes = parse_id3v2_tag_size(head)
            if skip_bytes:
                window = await _fetch_range(
                    http_session, url, headers, skip_bytes, FIRST_FRAME_WINDOW
                )
                if window is None:
                    return NO_SEEK_HINTS
            else:
                window = head
            is_cbr = parse_first_mp3_frame(window)
            if is_cbr is None:
                return NO_SEEK_HINTS
            return Mp3SeekHints(skip_bytes, is_cbr)
    # ValueError: aiohttp refuses provider header values holding control characters
    except (ClientError, OSError, TimeoutError, ValueError) as err:
        # the url may carry credentials, so it stays out of the log
        LOGGER.debug("Unable to probe MP3 for seek hints: %s", err.__class__.__name__)
        return None


def ffmpeg_http_headers(extra_input_args: Sequence[str]) -> dict[str, str]:
    """
    Return the HTTP headers ffmpeg sends for the given input arguments.

    :param extra_input_args: The ffmpeg input arguments of the stream.
    """
    headers = dict(HTTP_HEADERS)
    for option, value in pairwise(extra_input_args):
        if option == "-user_agent":
            headers["User-Agent"] = value
        elif option == "-headers":
            for line in value.splitlines():
                name, sep, header_value = line.partition(":")
                if sep and name.strip():
                    headers[name.strip()] = header_value.strip()
    return headers


def parse_id3v2_tag_size(data: bytes) -> int:
    """
    Return the full size of the ID3v2 tag at the start of the data, or 0 without one.

    :param data: The first bytes of the file (at least 10).
    """
    if len(data) < ID3V2_HEADER_SIZE or not data.startswith(b"ID3"):
        return 0
    major_version, flags = data[3], data[5]
    size_bytes = data[6:10]
    if major_version not in (2, 3, 4) or any(byte & 0x80 for byte in size_bytes):
        return 0
    size = 0
    for byte in size_bytes:
        size = (size << 7) | byte
    size += ID3V2_HEADER_SIZE
    # only v2.4 defines the footer flag
    if major_version == 4 and flags & 0x10:
        size += ID3V2_HEADER_SIZE
    return size


def parse_first_mp3_frame(data: bytes) -> bool | None:
    """
    Find the first MPEG audio frame in the data and tell whether it marks a CBR file.

    Returns True when the frame carries a LAME/Xing `Info` header (CBR), False for any
    other valid frame and None when the data holds no valid MPEG audio frame header.

    :param data: Bytes starting where the audio is expected to begin.
    """
    for offset in range(len(data) - 3):
        if (header := _parse_frame_header(data[offset : offset + 4])) is None:
            continue
        is_mpeg1, is_layer3, is_mono = header
        if not is_layer3:
            return False
        frame = data[offset:]
        xing_offset = _XING_OFFSETS[(is_mpeg1, is_mono)]
        tag = frame[xing_offset : xing_offset + 4]
        if tag == b"Info":
            return True
        if tag == b"Xing" or frame[_VBRI_OFFSET : _VBRI_OFFSET + 4] == b"VBRI":
            return False
        if len(frame) < _VBRI_OFFSET + 4:
            # too little data to rule out a VBR header
            return None
        return False
    return None


def _parse_frame_header(header: bytes) -> tuple[bool, bool, bool] | None:
    """
    Parse a 4-byte MPEG audio frame header.

    Returns (is_mpeg1, is_layer3, is_mono), or None when the bytes are not a valid header.

    :param header: The four candidate header bytes.
    """
    if header[0] != 0xFF or header[1] & 0xE0 != 0xE0:
        return None
    version = (header[1] >> 3) & 0x03
    layer = (header[1] >> 1) & 0x03
    bitrate_index = header[2] >> 4
    sample_rate_index = (header[2] >> 2) & 0x03
    # reserved values, and free-format bitrate which nothing we care about uses;
    # the reserved layer also rules out AAC ADTS, which shares the sync word
    if version == 0b01 or layer == 0b00 or bitrate_index in (0, 0x0F) or sample_rate_index == 3:
        return None
    return version == 0b11, layer == 0b01, (header[3] >> 6) == 0b11


async def _fetch_range(
    http_session: ClientSession,
    url: str,
    headers: dict[str, str],
    start: int,
    length: int,
) -> bytes | None:
    """
    Fetch a byte range, or return None when the server does not serve it as a range.

    :param http_session: The HTTP session to fetch with.
    :param url: URL of the file.
    :param headers: HTTP headers to send along.
    :param start: Offset of the first byte.
    :param length: Number of bytes to fetch.
    """
    range_headers = {**headers, "Range": f"bytes={start}-{start + length - 1}"}
    async with http_session.get(url, headers=range_headers) as resp:
        if resp.status != 206:
            # a server ignoring the range would send the whole file, drop the connection
            resp.close()
            return None
        data = b""
        while len(data) < length and (chunk := await resp.content.read(length - len(data))):
            data += chunk
        return data
