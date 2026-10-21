"""Parsers for the local filesystem provider."""

import re
from datetime import date
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ArtistEntityType, ExternalID
from music_assistant_models.media_items.metadata import LifeSpan

from music_assistant.helpers.tags import TAG_SPLITTER, clean_mbid, split_items
from music_assistant.helpers.util import parse_title_and_version

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album, Artist

# LifeSpan dates are ISO (YYYY-MM-DD) or partial (YYYY-MM or YYYY)
_ISO_PARTIAL_DATE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")


def parse_album_nfo(album: Album, nfo_album: dict[Any, Any], source: str | None = None) -> None:
    """
    Enrich album metadata from NFO file.

    :param album: The album to enrich.
    :param nfo_album: The parsed 'album' element from the NFO file.
    :param source: Origin of the NFO data (e.g. file path), included in log messages.
    """
    if title := nfo_album.get("title") or nfo_album.get("name"):
        album.name, album.version = parse_title_and_version(title)
    if sort_name := nfo_album.get("sortname"):
        album.sort_name = sort_name
    if releasegroup_id := clean_mbid(nfo_album.get("musicbrainzreleasegroupid"), source):
        album.add_external_id(ExternalID.MB_RELEASEGROUP, releasegroup_id)
    if album_id := clean_mbid(nfo_album.get("musicbrainzalbumid"), source):
        album.add_external_id(ExternalID.MB_ALBUM, album_id)
    if mb_artist_id := clean_mbid(nfo_album.get("musicbrainzalbumartistid"), source):
        if album.artists and not album.artists[0].mbid:
            album.artists[0].mbid = mb_artist_id
    if description := nfo_album.get("review"):
        album.metadata.description = description
    if year := nfo_album.get("year"):
        album.year = int(year)
    if genre := nfo_album.get("genre"):
        album.metadata.genres = set(split_items(genre))


def nfo_album_artist(nfo_album: dict[Any, Any]) -> str | None:
    """
    Return the single album artist named in an album NFO, or None when absent or ambiguous.

    :param nfo_album: The parsed 'album' element from the NFO file.
    """
    value = nfo_album.get("albumartist")
    if not isinstance(value, str) and not (
        isinstance(value, list) and all(isinstance(item, str) for item in value)
    ):
        return None
    # repeated elements (how Jellyfin writes compilations) and semicolon separated values
    # both name more than one artist, so neither is trusted as the album artist
    names = split_items(value)
    return names[0] if len(names) == 1 else None


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
    if style := nfo_artist.get("style"):
        artist.metadata.style = f"{TAG_SPLITTER} ".join(split_items(style))
    if mood := nfo_artist.get("mood"):
        artist.metadata.mood = f"{TAG_SPLITTER} ".join(split_items(mood))
    if (artist_type := nfo_artist.get("type")) and (
        entity_type := ArtistEntityType(artist_type.strip().lower())
    ) != ArtistEntityType.UNKNOWN:
        artist.metadata.artist_entity_type = entity_type
    begin = _iso_partial_date(nfo_artist.get("born") or nfo_artist.get("formed"))
    end = _iso_partial_date(nfo_artist.get("died") or nfo_artist.get("disbanded"))
    if begin or end:
        artist.metadata.life_span = LifeSpan(begin=begin, end=end, ended=end is not None)


def _iso_partial_date(value: str | None) -> str | None:
    """Return the value when it is an ISO (partial) date, otherwise None."""
    if not value or not _ISO_PARTIAL_DATE.match(value := value.strip()):
        return None
    year, month, day = (*map(int, value.split("-")), 1, 1)[:3]
    try:
        date(year, month, day)
    except ValueError:
        return None
    return value
