"""Helpers for parsing (online and offline) playlists."""

from __future__ import annotations

import configparser
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from aiohttp import client_exceptions

from music_assistant.common.models.errors import InvalidDataError

if TYPE_CHECKING:
    from music_assistant.server import MusicAssistant


LOGGER = logging.getLogger(__name__)
HLS_CONTENT_TYPES = (
    # https://tools.ietf.org/html/draft-pantos-http-live-streaming-19#section-10
    "application/vnd.apple.mpegurl",
    # Additional informal types used by Mozilla gecko not included as they
    # don't reliably indicate HLS streams
)


class IsHLSPlaylist(InvalidDataError):
    """The playlist from an HLS stream and should not be parsed."""


@dataclass
class PlaylistItem:
    """Playlist item."""

    path: str
    length: str | None = None
    title: str | None = None
    stream_info: dict[str, str] | None = None
    key: str | None = None

    @property
    def is_url(self) -> bool:
        """Validate the URL can be parsed and at least has scheme + netloc."""
        result = urlparse(self.path)
        return all([result.scheme, result.netloc])


def parse_m3u(m3u_data: str) -> list[PlaylistItem]:
    """Very simple m3u parser.

    Based on https://github.com/dvndrsn/M3uParser/blob/master/m3uparser.py
    """
    # From Mozilla gecko source: https://github.com/mozilla/gecko-dev/blob/c4c1adbae87bf2d128c39832d72498550ee1b4b8/dom/media/DecoderTraits.cpp#L47-L52

    m3u_lines = m3u_data.splitlines()

    playlist = []

    length = None
    title = None
    stream_info = None
    key = None

    for line in m3u_lines:
        line = line.strip()  # noqa: PLW2901
        if line.startswith("#EXTINF:"):
            # Get length and title from #EXTINF line
            info = line.split("#EXTINF:")[1].split(",", 1)
            if len(info) != 2:
                continue
            length = info[0].strip()[0]
            if length == "-1":
                length = None
            title = info[1].strip()
        elif line.startswith("#EXT-X-STREAM-INF:"):
            # HLS stream properties
            # https://datatracker.ietf.org/doc/html/draft-pantos-http-live-streaming-19#section-10
            stream_info = {}
            for part in line.replace("#EXT-X-STREAM-INF:", "").split(","):
                if "=" not in part:
                    continue
                kev_value_parts = part.strip().split("=")
                stream_info[kev_value_parts[0]] = kev_value_parts[1]
        elif line.startswith("#EXT-X-KEY:"):
            key = line.split(",URI=")[1].strip('"')
        elif line.startswith("#"):
            # Ignore other extensions
            continue
        elif len(line) != 0:
            filepath = line
            if "%20" in filepath:
                # apparently VLC manages to encode spaces in filenames
                filepath = filepath.replace("%20", " ")
            # replace Windows directory separators
            filepath = filepath.replace("\\", "/")
            playlist.append(
                PlaylistItem(
                    path=filepath, length=length, title=title, stream_info=stream_info, key=key
                )
            )
            # reset the song variables so it doesn't use the same EXTINF more than once
            length = None
            title = None
            stream_info = None

    return playlist


def parse_pls(pls_data: str) -> list[PlaylistItem]:
    """Parse (only) filenames/urls from pls playlist file."""
    pls_parser = configparser.ConfigParser()
    try:
        pls_parser.read_string(pls_data, "playlist")
    except configparser.Error as err:
        raise InvalidDataError("Can't parse playlist") from err

    if "playlist" not in pls_parser or pls_parser["playlist"].getint("Version") != 2:
        raise InvalidDataError("Invalid playlist")

    try:
        num_entries = pls_parser.getint("playlist", "NumberOfEntries")
    except (configparser.NoOptionError, ValueError) as err:
        raise InvalidDataError("Invalid NumberOfEntries in playlist") from err

    playlist_section = pls_parser["playlist"]

    playlist = []
    for entry in range(1, num_entries + 1):
        file_option = f"File{entry}"
        if file_option not in playlist_section:
            continue
        itempath = playlist_section[file_option]
        length = playlist_section.get(f"Length{entry}")
        playlist.append(
            PlaylistItem(
                length=length if length and length != "-1" else None,
                title=playlist_section.get(f"Title{entry}"),
                path=itempath,
            )
        )
    return playlist


async def fetch_playlist(mass: MusicAssistant, url: str) -> list[PlaylistItem]:
    """Parse an online m3u or pls playlist."""
    try:
        async with mass.http_session.get(url, allow_redirects=True, timeout=5) as resp:
            charset = resp.charset or "utf-8"
            try:
                playlist_data = (await resp.content.read(64 * 1024)).decode(charset)
            except ValueError as err:
                msg = f"Could not decode playlist {url}"
                raise InvalidDataError(msg) from err
    except TimeoutError as err:
        msg = f"Timeout while fetching playlist {url}"
        raise InvalidDataError(msg) from err
    except client_exceptions.ClientError as err:
        msg = f"Error while fetching playlist {url}"
        raise InvalidDataError(msg) from err

    if "#EXT-X-VERSION:" in playlist_data or "#EXT-X-STREAM-INF:" in playlist_data:
        raise IsHLSPlaylist

    if url.endswith((".m3u", ".m3u8")):
        playlist = parse_m3u(playlist_data)
    else:
        playlist = parse_pls(playlist_data)

    if not playlist:
        msg = f"Empty playlist {url}"
        raise InvalidDataError(msg)

    return playlist
