"""Helpers to read Kodi-style NFO metadata files."""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import TYPE_CHECKING, Any
from xml.parsers.expat import ExpatError

import xmltodict
from music_assistant_models.enums import ArtistEntityType
from music_assistant_models.media_items.metadata import LifeSpan

from music_assistant.constants import MASS_LOGGER_NAME
from music_assistant.helpers.tags import TAG_SPLITTER, clean_mbid, split_items

if TYPE_CHECKING:
    from music_assistant_models.media_items import Artist

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.nfo")
# LifeSpan dates are ISO (YYYY-MM-DD) or partial (YYYY-MM or YYYY)
_ISO_PARTIAL_DATE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")
# other date forms, read with the month as a name or, in a numeric date, as the middle number:
# 15 Dec 1970, December 15, 1970, Dec 1970, 15/12/1970, 1970.12.15, 1970/12, 12-1970
_DATE_FORMS = (
    re.compile(r"^(?P<day>\d{1,2})\s+(?P<month>[a-z]+),?\s+(?P<year>\d{4})$", re.IGNORECASE),
    re.compile(r"^(?P<month>[a-z]+)\s+(?P<day>\d{1,2}),?\s+(?P<year>\d{4})$", re.IGNORECASE),
    re.compile(r"^(?P<month>[a-z]+),?\s+(?P<year>\d{4})$", re.IGNORECASE),
    re.compile(r"^(?P<day>\d{1,2})(?P<sep>[/.-])(?P<month>\d{1,2})(?P=sep)(?P<year>\d{4})$"),
    re.compile(r"^(?P<year>\d{4})(?P<sep>[/.-])(?P<month>\d{1,2})(?P=sep)(?P<day>\d{1,2})$"),
    re.compile(r"^(?P<year>\d{4})[/.-](?P<month>\d{1,2})$"),
    re.compile(r"^(?P<month>\d{1,2})[/.-](?P<year>\d{4})$"),
)
# English month names, full and abbreviated, independent of the process locale
_MONTHS = {
    month_name: number
    for number, name in enumerate(
        (
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ),
        start=1,
    )
    for month_name in (name, name[:3])
}


def parse_nfo_root(data: bytes, root_tag: str) -> dict[str, Any] | None:
    """
    Parse an NFO file's bytes and return its expected root element, or None when malformed.

    :param data: The raw NFO file content.
    :param root_tag: The expected root element name (``album`` or ``artist``).
    """
    try:
        text = data.decode("utf-8")
        # tag names are matched case-insensitively: Kodi writes e.g. <musicBrainzArtistID>
        parsed = xmltodict.parse(text, postprocessor=_lowercase_nfo_key)
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
    title = _single_value(nfo_artist, "title", source) or _single_value(nfo_artist, "name", source)
    if title:
        artist.name = title
    if sort_name := _single_value(nfo_artist, "sortname", source):
        artist.sort_name = sort_name
    if mbid := clean_mbid(_single_value(nfo_artist, "musicbrainzartistid", source), source):
        artist.mbid = mbid
    if description := _single_value(nfo_artist, "biography", source):
        artist.metadata.description = description
    if genre := nfo_artist.get("genre"):
        artist.metadata.genres = set(split_items(genre))
    if style := nfo_artist.get("style"):
        artist.metadata.style = f"{TAG_SPLITTER} ".join(split_items(style))
    if mood := nfo_artist.get("mood"):
        artist.metadata.mood = f"{TAG_SPLITTER} ".join(split_items(mood))
    if (artist_type := _single_value(nfo_artist, "type", source)) and (
        entity_type := ArtistEntityType(artist_type.strip().lower())
    ) != ArtistEntityType.UNKNOWN:
        artist.metadata.artist_entity_type = entity_type
    begin = _iso_partial_date(
        _single_value(nfo_artist, "born", source) or _single_value(nfo_artist, "formed", source)
    )
    end = _iso_partial_date(
        _single_value(nfo_artist, "died", source) or _single_value(nfo_artist, "disbanded", source)
    )
    if begin or end:
        artist.metadata.life_span = LifeSpan(begin=begin, end=end, ended=end is not None)


def _single_value(nfo_artist: dict[Any, Any], key: str, source: str | None) -> str | None:
    """Return the first text value of a single-value NFO element, warning when it is repeated."""
    value = nfo_artist.get(key)
    if isinstance(value, list) and value and isinstance(value[0], str):
        LOGGER.warning(
            "Using the first <%s> value and skipping the repeated ones in %s",
            key,
            source or "NFO data",
        )
        return value[0]
    return value if isinstance(value, str) else None


def _iso_partial_date(value: str | None) -> str | None:
    """Return the value as an ISO (partial) date when it is a date in a supported form, else None."""
    if value is None:
        return None
    text = value.strip()
    if match := next((m for pattern in _DATE_FORMS if (m := pattern.match(text))), None):
        month_text = match["month"]
        month = int(month_text) if month_text.isdigit() else _MONTHS.get(month_text.lower())
        if not month:
            return None
        text = f"{match['year']}-{month:02d}"
        if day := match.groupdict().get("day"):
            text += f"-{int(day):02d}"
    elif not _ISO_PARTIAL_DATE.match(text):
        return None
    year, month, day = (*map(int, text.split("-")), 1, 1)[:3]
    try:
        date(year, month, day)
    except ValueError:
        return None
    return text


def _lowercase_nfo_key(_path: Any, key: str, value: Any) -> tuple[str, Any]:
    """Lowercase an NFO element/attribute name (xmltodict postprocessor)."""
    return key.lower(), value
