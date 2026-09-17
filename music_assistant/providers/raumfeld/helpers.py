"""Helpers for the Teufel Raumfeld player provider."""

from __future__ import annotations

import re
from urllib.parse import unquote

import defusedxml.ElementTree as DefusedET

from .constants import PLAYER_ID_PREFIX

_slug_re = re.compile(r"[^a-z0-9]+")

# DIDL-Lite / metadata XML namespaces used in UPnP TrackMetaData.
_DC = "{http://purl.org/dc/elements/1.1/}"
_UPNP = "{urn:schemas-upnp-org:metadata-1-0/upnp/}"
_DIDL = "{urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/}"


def room_to_player_id(room: str) -> str:
    """
    Derive a stable Music Assistant player_id from a Raumfeld room name.

    :param room: The Raumfeld room name.
    """
    # hassfeld is keyed by room name (kept on the player for commands); only the
    # player_id is slugified, so renaming a room in the app creates a new player
    slug = _slug_re.sub("_", room.strip().lower()).strip("_")
    return f"{PLAYER_ID_PREFIX}_{slug}"


def parse_didl_metadata(didl_xml: str | None) -> dict[str, str | None]:
    """
    Parse a DIDL-Lite ``TrackMetaData`` XML string into title/artist/album/image.

    Returns a dict with keys ``title``, ``artist``, ``album`` and ``image_url``, each
    possibly ``None``. Malformed or empty XML yields all-``None`` (never raises).

    :param didl_xml: The DIDL-Lite metadata XML as returned by GetPositionInfo, or None.
    """
    result: dict[str, str | None] = {
        "title": None,
        "artist": None,
        "album": None,
        "image_url": None,
    }
    if not didl_xml or not didl_xml.strip() or didl_xml.strip().upper() == "NOT_IMPLEMENTED":
        return result
    try:
        root = DefusedET.fromstring(didl_xml)
    except DefusedET.ParseError, ValueError:
        return result
    item = root.find(".//{urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/}item")
    node = item if item is not None else root
    title = node.find(f"{_DC}title")
    artist = node.find(f"{_UPNP}artist")
    if artist is None:
        artist = node.find(f"{_DC}creator")
    album = node.find(f"{_UPNP}album")
    image = node.find(f"{_UPNP}albumArtURI")
    result["title"] = title.text if title is not None else None
    result["artist"] = artist.text if artist is not None else None
    result["album"] = album.text if album is not None else None
    result["image_url"] = image.text if image is not None else None
    return result


def parse_duration(value: str | None) -> int | None:
    """
    Parse a UPnP ``H:MM:SS`` (or ``H:MM:SS.mmm``) duration string into whole seconds.

    :param value: The duration string, or None.
    """
    if not value or value.strip().upper() in ("", "NOT_IMPLEMENTED"):
        return None
    parts = value.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    seconds = 0.0
    for num in nums:
        seconds = seconds * 60 + num
    return int(seconds)


def parse_line_in(didl_xml: str | None) -> dict[str, tuple[str, str]]:
    """
    Parse a Line-In DIDL-Lite listing into ``{renderer_uuid: (stream_url, title)}``.

    :param didl_xml: The DIDL-Lite XML returned by browsing the Line-In container.
    """
    result: dict[str, tuple[str, str]] = {}
    if not didl_xml or not didl_xml.strip():
        return result
    try:
        root = DefusedET.fromstring(didl_xml)
    except DefusedET.ParseError, ValueError:
        return result
    for item in root.findall(f".//{_DIDL}item"):
        res = item.find(f"{_DIDL}res")
        # the item id is like "0/Line In/uuid%3A<renderer-uuid>"; the renderer uuid is
        # the same one the player exposes as its UUID identifier
        item_id = unquote(item.get("id", ""))
        if "uuid:" not in item_id or res is None or not (res.text or "").strip():
            continue
        uuid = item_id.split("uuid:", 1)[1].strip().lower()
        title = item.find(f"{_DC}title")
        name = title.text if title is not None and title.text else "Line-in"
        result[uuid] = (res.text.strip(), name)
    return result
