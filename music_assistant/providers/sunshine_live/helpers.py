"""Helpers for the Sunshine Live provider."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from music_assistant_models.enums import ContentType

from .constants import FALLBACK_IMAGE_URL, NAME_WORDS, STATION_NAME

# host, channel and format of a stream; what follows names the player to the ad platform
_STREAM_URL = re.compile(r"^(?:https?:)?//(stream\.[^/\s]+)/([^/\s]+)/([^/\s]+)")
_STREAM_FORMAT = re.compile(r"^(mp3|aac)-(\d+)$")
_SONG_PUNCTUATION = re.compile(r"[\W_]+")
_TITLE_VERSION = re.compile(r"\s*[(\[][^)\]]*[)\]]\s*$")


@dataclass(frozen=True, slots=True)
class Channel:
    """One channel of the station."""

    name: str
    stream_url: str
    image: str
    content_type: ContentType
    bit_rate: int | None
    artist: str = ""
    title: str = ""
    cover: str | None = None


def parse_channels(data: Any) -> dict[str, Channel]:
    """
    Return the usable channels of an answer of the station, by the channel id of the station.

    :param data: The decoded answer, a mapping that holds one entry for each channel.
    """
    if not isinstance(data, dict):
        return {}
    channels: dict[str, Channel] = {}
    for entry in data.values():
        if isinstance(entry, dict) and (channel := _parse_channel(entry)):
            channels[str(entry.get("station_id"))] = channel
    return channels


def is_station_title(stream_title: str) -> bool:
    """
    Return whether a title from the stream only names the station, not what plays.

    :param stream_title: The title from the stream, like "SUNSHINE LIVE - Simulcast".
    """
    return stream_title.casefold().startswith(STATION_NAME.casefold())


def is_same_song(stream_title: str, channel: Channel) -> bool:
    """
    Return whether the title from the stream is the song the list of the station names.

    :param stream_title: The title from the stream.
    :param channel: The channel with the song from the list of the station.
    """
    # the two write a song differently, like "Timmo, Stoked - Back to the Oldschool
    # (Extended Mix)" and "Timmo & Stoked" with "Back to the Oldschool"
    artist, separator, title = stream_title.partition(" - ")
    if not separator or not channel.title:
        return False
    return _song_key(artist, title) == _song_key(channel.artist, channel.title)


def _parse_channel(entry: dict[str, Any]) -> Channel | None:
    """Return a channel, None when it has no id or no stream to play."""
    match = _STREAM_URL.match(str(entry.get("url_high") or entry.get("url_low") or "").strip())
    if entry.get("station_id") is None or not match:
        return None
    host, path, stream_format = match.groups()
    known_format = _STREAM_FORMAT.match(stream_format)
    return Channel(
        name=_channel_name(str(entry.get("stream") or ""), fallback=path),
        stream_url=f"https://{host}/{path}/{stream_format}/",
        image=str(entry.get("stream_logo") or "").strip() or FALLBACK_IMAGE_URL,
        content_type=(
            ContentType.try_parse(known_format.group(1)) if known_format else ContentType.UNKNOWN
        ),
        bit_rate=int(known_format.group(2)) if known_format else None,
        artist=str(entry.get("artist_name") or "").strip(),
        title=str(entry.get("song_title") or "").strip(),
        cover=_cover(entry.get("covers")),
    )


def _song_key(artist: str, title: str) -> tuple[str, str]:
    """Return an artist and title that only hold what both sources write the same."""
    bare_title = _TITLE_VERSION.sub("", title)
    return _SONG_PUNCTUATION.sub("", artist.casefold()), _SONG_PUNCTUATION.sub(
        "", bare_title.casefold()
    )


def _cover(covers: Any) -> str | None:
    """Return the largest cover of a channel, which some channels answer without."""
    if not isinstance(covers, dict):
        return None
    for size in ("cover_art_url_xxl", "cover_art_url_xl", "cover_art_url_l"):
        if url := str(covers.get(size) or "").strip():
            return url
    return None


def _channel_name(name: str, fallback: str) -> str:
    """Return the name to show for a channel, from its name at the station or its stream."""
    words = (name.strip() or fallback).split()
    readable = " ".join(NAME_WORDS.get(word.upper(), word.capitalize()) for word in words)
    if readable.lower().startswith(STATION_NAME.lower()):
        return readable
    return f"{STATION_NAME} - {readable}"
