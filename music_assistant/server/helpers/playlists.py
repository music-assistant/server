"""Helpers for parsing playlists."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import aiohttp

from music_assistant.common.models.errors import InvalidDataError

if TYPE_CHECKING:
    from music_assistant.server import MusicAssistant


LOGGER = logging.getLogger(__name__)


async def parse_m3u(m3u_data: str) -> list[str]:
    """Parse (only) filenames/urls from m3u playlist file."""
    m3u_lines = m3u_data.splitlines()
    lines = []
    for line in m3u_lines:
        line = line.strip()  # noqa: PLW2901
        if line.startswith("#"):
            # ignore metadata
            continue
        if len(line) != 0:
            # Get uri/path from all other, non-blank lines
            lines.append(line)

    return lines


async def parse_pls(pls_data: str) -> list[str]:
    """Parse (only) filenames/urls from pls playlist file."""
    pls_lines = pls_data.splitlines()
    lines = []
    for line in pls_lines:
        line = line.strip()  # noqa: PLW2901
        if not line.startswith("File"):
            # ignore metadata lines
            continue
        if "=" in line:
            # Get uri/path from all other, non-blank lines
            lines.append(line.split("=")[1])

    return lines


async def fetch_playlist(mass: MusicAssistant, url: str) -> list[str]:
    """Parse an online m3u or pls playlist."""
    try:
        async with mass.http_session.get(url, timeout=5) as resp:
            charset = resp.charset or "utf-8"
            try:
                playlist_data = (await resp.content.read(64 * 1024)).decode(charset)
            except ValueError as err:
                msg = f"Could not decode playlist {url}"
                raise InvalidDataError(msg) from err
    except TimeoutError as err:
        msg = f"Timeout while fetching playlist {url}"
        raise InvalidDataError(msg) from err
    except aiohttp.client_exceptions.ClientError as err:
        msg = f"Error while fetching playlist {url}"
        raise InvalidDataError(msg) from err

    if url.endswith((".m3u", ".m3u8")):
        playlist = await parse_m3u(playlist_data)
    else:
        playlist = await parse_pls(playlist_data)

    if not playlist:
        msg = f"Empty playlist {url}"
        raise InvalidDataError(msg)

    return playlist
