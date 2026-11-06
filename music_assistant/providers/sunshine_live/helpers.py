"""Helpers for the Sunshine Live provider."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from music_assistant_models.enums import ContentType

from .constants import FALLBACK_IMAGE_URL, NAME_WORDS, STATION_NAME

_STREAM_URL = re.compile(r"^(?:https?:)?//(stream\.[^/\s]+/([^/\s]+)/\S*)$")
_STREAM_FORMAT = re.compile(r"/(mp3|aac)-(\d+)(?:/|$)")


@dataclass(frozen=True, slots=True)
class Channel:
    """One channel of the station."""

    name: str
    stream_url: str
    image: str
    content_type: ContentType
    bit_rate: int | None


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


def _parse_channel(entry: dict[str, Any]) -> Channel | None:
    """Return a channel, None when it has no id or no stream to play."""
    match = _STREAM_URL.match(str(entry.get("url_high") or entry.get("url_low") or "").strip())
    if entry.get("station_id") is None or not match:
        return None
    stream_format = _STREAM_FORMAT.search(match.group(1))
    return Channel(
        name=_channel_name(str(entry.get("stream") or ""), fallback=match.group(2)),
        stream_url=f"https://{match.group(1)}",
        image=str(entry.get("stream_logo") or "").strip() or FALLBACK_IMAGE_URL,
        content_type=(
            ContentType.try_parse(stream_format.group(1)) if stream_format else ContentType.UNKNOWN
        ),
        bit_rate=int(stream_format.group(2)) if stream_format else None,
    )


def _channel_name(name: str, fallback: str) -> str:
    """Return the name to show for a channel, from its name at the station or its stream."""
    words = (name.strip() or fallback).split()
    readable = " ".join(NAME_WORDS.get(word.upper(), word.capitalize()) for word in words)
    if readable.lower().startswith(STATION_NAME.lower()):
        return readable
    return f"{STATION_NAME} - {readable}"
