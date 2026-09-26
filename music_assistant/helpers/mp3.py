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
# room for two of the largest frames (2880 bytes at MPEG 2.5 layer II), past a little padding
FIRST_FRAME_WINDOW: Final[int] = 4096
PROBE_TIMEOUT: Final[float] = 3.0

# kbps per bitrate index 1-14, keyed by (is_mpeg1, layer bits)
_BITRATES: Final[dict[tuple[bool, int], tuple[int, ...]]] = {
    (True, 0b11): (32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    (True, 0b10): (32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    (True, 0b01): (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    (False, 0b11): (32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    (False, 0b10): (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    (False, 0b01): (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
# Hz per sample rate index 0-2, keyed by version bits (MPEG 2.5, MPEG 2, MPEG 1)
_SAMPLE_RATES: Final[dict[int, tuple[int, int, int]]] = {
    0b00: (11025, 12000, 8000),
    0b10: (22050, 24000, 16000),
    0b11: (44100, 48000, 32000),
}


class Mp3SeekHints(NamedTuple):
    """What a remote file allows ffmpeg to skip when it seeks."""

    fastseek: bool
    skip_bytes: int


NO_SEEK_HINTS: Final[Mp3SeekHints] = Mp3SeekHints(fastseek=False, skip_bytes=0)


async def probe_mp3_seek_hints(
    http_session: ClientSession,
    url: str,
    headers: dict[str, str],
    timeout: float = PROBE_TIMEOUT,
) -> Mp3SeekHints | None:
    """
    Read the head of a remote MP3 to find out how ffmpeg can seek in it quickly.

    Returns NO_SEEK_HINTS when the file offers no shortcut (no MPEG audio where it should
    start, or no range support), and None when the file could not be read (timeout or
    network error), in which case asking again later may still succeed.

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
            if not has_mp3_frame(window):
                return NO_SEEK_HINTS
            return Mp3SeekHints(fastseek=True, skip_bytes=skip_bytes)
    # ValueError: aiohttp refuses provider header values holding control characters
    except (ClientError, OSError, TimeoutError, ValueError) as err:
        # the url may carry credentials, so it stays out of the log
        LOGGER.debug("Unable to probe MP3 for seek hints: %s", err.__class__.__name__)
        return None


def ffmpeg_http_headers(extra_input_args: Sequence[str]) -> dict[str, str]:
    """
    Return the HTTP headers ffmpeg sends for the given input arguments.

    Without a User-Agent in the arguments, ffmpeg only sends the returned one when it is
    passed along as `-user_agent`.

    :param extra_input_args: The ffmpeg input arguments of the stream.
    """
    headers = dict(HTTP_HEADERS)
    header_user_agent: str | None = None
    for option, value in pairwise(extra_input_args):
        if option == "-user_agent":
            headers["User-Agent"] = value
        elif option == "-headers":
            for line in value.splitlines():
                name, sep, header_value = line.partition(":")
                name = name.strip()
                if not sep or not name:
                    continue
                if name.lower() == "user-agent":
                    header_user_agent = header_value.strip()
                else:
                    headers[name] = header_value.strip()
    # ffmpeg prefers a User-Agent from -headers over -user_agent, whatever their order
    if header_user_agent is not None:
        headers["User-Agent"] = header_user_agent
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


def has_mp3_frame(data: bytes) -> bool:
    """
    Return whether the data holds MPEG audio: a valid frame directly followed by a matching one.

    :param data: Bytes starting where the audio is expected to begin.
    """
    for offset in range(len(data) - 3):
        if (frame := _parse_frame_header(data[offset : offset + 4])) is None:
            continue
        length, stream_format = frame
        next_offset = offset + length
        if (
            next_frame := _parse_frame_header(data[next_offset : next_offset + 4])
        ) is not None and next_frame[1] == stream_format:
            return True
    return False


def _parse_frame_header(header: bytes) -> tuple[int, tuple[int, int]] | None:
    """
    Parse a 4-byte MPEG audio frame header.

    Returns the frame length in bytes plus a key for the stream format (version, layer and
    sample rate), or None when the bytes are not a valid header.

    :param header: The four candidate header bytes.
    """
    if len(header) < 4 or header[0] != 0xFF or header[1] & 0xE0 != 0xE0:
        return None
    version = (header[1] >> 3) & 0x03
    layer = (header[1] >> 1) & 0x03
    bitrate_index = header[2] >> 4
    sample_rate_index = (header[2] >> 2) & 0x03
    # reserved values, and free-format bitrate which nothing we care about uses;
    # the reserved layer also rules out AAC ADTS, which shares the sync word
    if version == 0b01 or layer == 0b00 or bitrate_index in (0, 0x0F) or sample_rate_index == 3:
        return None
    is_mpeg1 = version == 0b11
    bitrate = _BITRATES[(is_mpeg1, layer)][bitrate_index - 1] * 1000
    sample_rate = _SAMPLE_RATES[version][sample_rate_index]
    padding = (header[2] >> 1) & 0x01
    if layer == 0b11:
        length = (12 * bitrate // sample_rate + padding) * 4
    else:
        # layer III outside MPEG 1 carries half the samples per frame
        slots = 72 if layer == 0b01 and not is_mpeg1 else 144
        length = slots * bitrate // sample_rate + padding
    return length, (version << 2 | layer, sample_rate_index)


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
    :raises ClientResponseError: When the server answers with an error status.
    """
    range_headers = {**headers, "Range": f"bytes={start}-{start + length - 1}"}
    async with http_session.get(url, headers=range_headers) as resp:
        if resp.status != 206:
            # a server ignoring the range would send the whole file, drop the connection
            resp.close()
            # an error status may be temporary, so it fails the probe instead of answering it
            resp.raise_for_status()
            return None
        data = b""
        while len(data) < length and (chunk := await resp.content.read(length - len(data))):
            data += chunk
        return data
