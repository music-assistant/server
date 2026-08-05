"""DIDL-Lite parsing and playback-time helpers."""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from html import unescape

from .models import RendererInstance

LOGGER = logging.getLogger(__name__)

_MAX_DIDL_CHARS = 64 * 1024
_EMPTY_METADATA: dict[str, str | None] = {
    "title": None,
    "artist": None,
    "album": None,
    "image_url": None,
    "duration": None,
}


def parse_didl_metadata(metadata: str | None) -> dict[str, str | None]:
    """Parse the supported fields from DIDL-Lite metadata."""
    result = _EMPTY_METADATA.copy()
    if not metadata:
        return result
    if len(metadata) > _MAX_DIDL_CHARS:
        LOGGER.info("DIDL metadata rejected: document exceeds %d chars", _MAX_DIDL_CHARS)
        return result

    metadata = unescape(metadata)
    lowered = metadata.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        LOGGER.info("DIDL metadata rejected: DOCTYPE/ENTITY declaration present")
        return result

    try:
        root = ET.fromstring(metadata)  # noqa: S314
    except ET.ParseError:
        LOGGER.info("Failed to parse DIDL-Lite metadata: %s", metadata[:300])
        return result

    namespace = {
        "dc": "http://purl.org/dc/elements/1.1/",
        "upnp": "urn:schemas-upnp-org:metadata-1-0/upnp/",
        "didl": "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/",
    }
    item = root.find("didl:item", namespace)
    if item is None:
        item = root

    title = item.find("dc:title", namespace)
    if title is not None and title.text:
        result["title"] = title.text

    artist = item.find("upnp:artist", namespace)
    if artist is None:
        artist = item.find("dc:creator", namespace)
    if artist is not None and artist.text:
        result["artist"] = artist.text

    album = item.find("upnp:album", namespace)
    if album is not None and album.text:
        result["album"] = album.text

    artwork = item.find("upnp:albumArtURI", namespace)
    if artwork is not None and artwork.text:
        result["image_url"] = artwork.text

    resource = item.find("didl:res", namespace)
    if resource is not None and (duration := resource.get("duration")):
        result["duration"] = duration

    return result


def parse_duration(value: str | None) -> int | None:
    """Convert an UPnP duration value to whole seconds."""
    if not value:
        return None
    try:
        parts = value.split(":")
        if len(parts) == 3:
            hours, minutes, seconds = parts
            return int(hours) * 3600 + int(minutes) * 60 + int(float(seconds))
        if len(parts) == 2:
            minutes, seconds = parts
            return int(minutes) * 60 + int(float(seconds))
        return int(float(value))
    except ValueError, TypeError:
        return None


def position_for(instance: RendererInstance, now: float | None = None) -> tuple[int, int]:
    """Return elapsed time and duration for a renderer instance."""
    elapsed = instance.elapsed_offset
    if instance.play_start_time is not None:
        current_time = time.time() if now is None else now
        elapsed += int(current_time - instance.play_start_time)

    duration = instance.stream_metadata.duration if instance.stream_metadata else None
    if duration is None:
        duration = parse_duration((instance.current_metadata or {}).get("duration"))
    duration = duration or 0
    if duration:
        elapsed = min(elapsed, duration)
    return max(0, elapsed), duration


def freeze_elapsed(instance: RendererInstance, now: float | None = None) -> None:
    """Freeze elapsed time at the current playback position."""
    if instance.play_start_time is None:
        return
    current_time = time.time() if now is None else now
    instance.elapsed_offset += int(current_time - instance.play_start_time)
    instance.play_start_time = None


def clear_playback(instance: RendererInstance) -> None:
    """Clear transient playback state for a renderer instance."""
    instance.play_start_time = None
    instance.elapsed_offset = 0
    instance.stream_metadata = None
    instance.metadata_dirty = False
