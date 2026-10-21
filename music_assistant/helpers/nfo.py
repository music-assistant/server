"""Helpers to read Kodi-style NFO metadata files."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from xml.parsers.expat import ExpatError

import xmltodict

from music_assistant.helpers.tags import clean_mbid, split_items

if TYPE_CHECKING:
    from music_assistant_models.media_items import Artist


def parse_nfo_root(data: bytes, root_tag: str) -> dict[str, Any] | None:
    """
    Parse an NFO file's bytes and return its expected root element, or None when malformed.

    :param data: The raw NFO file content.
    :param root_tag: The expected root element name (``album`` or ``artist``).
    """
    try:
        text = data.decode("utf-8")
        parsed = xmltodict.parse(text)
    except UnicodeDecodeError, ExpatError, ValueError:
        return None
    root = parsed.get(root_tag)
    return root if isinstance(root, dict) else None


def parse_artist_nfo(artist: Artist, nfo_artist: dict[Any, Any], source: str | None = None) -> None:
    """
    Enrich artist metadata from NFO file.

    :param artist: The artist to enrich.
    :param nfo_artist: The parsed 'artist' element from the NFO file.
    :param source: Origin of the NFO data (e.g. file path), included in log messages.
    """
    if title := nfo_artist.get("title") or nfo_artist.get("name"):
        artist.name = title
    if sort_name := nfo_artist.get("sortname"):
        artist.sort_name = sort_name
    if mbid := clean_mbid(nfo_artist.get("musicbrainzartistid"), source):
        artist.mbid = mbid
    if description := nfo_artist.get("biography"):
        artist.metadata.description = description
    if genre := nfo_artist.get("genre"):
        artist.metadata.genres = set(split_items(genre))
