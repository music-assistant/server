"""Parsers for the local filesystem provider."""

from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ExternalID

from music_assistant.helpers.tags import clean_mbid, split_items
from music_assistant.helpers.util import parse_title_and_version

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album, Artist


def parse_album_nfo(album: Album, nfo_album: dict[Any, Any], source: str | None = None) -> None:
    """
    Enrich album metadata from NFO file.

    :param album: The album to enrich.
    :param nfo_album: The parsed 'album' element from the NFO file.
    :param source: Origin of the NFO data (e.g. file path), included in log messages.
    """
    if title := nfo_album.get("title") or nfo_album.get("name"):
        album.name, album.version = parse_title_and_version(_nfo_text(title, "title", source))
    if sort_name := nfo_album.get("sortname"):
        album.sort_name = _nfo_text(sort_name, "sortname", source)
    if releasegroup_id := clean_mbid(nfo_album.get("musicbrainzreleasegroupid"), source):
        album.add_external_id(ExternalID.MB_RELEASEGROUP, releasegroup_id)
    if album_id := clean_mbid(nfo_album.get("musicbrainzalbumid"), source):
        album.add_external_id(ExternalID.MB_ALBUM, album_id)
    if mb_artist_id := clean_mbid(nfo_album.get("musicbrainzalbumartistid"), source):
        if album.artists and not album.artists[0].mbid:
            album.artists[0].mbid = mb_artist_id
    if description := nfo_album.get("review"):
        album.metadata.description = _nfo_text(description, "review", source)
    if year := nfo_album.get("year"):
        album.year = int(year)
    if genre := nfo_album.get("genre"):
        album.metadata.genres = set(split_items(genre))


def parse_artist_nfo(artist: Artist, nfo_artist: dict[Any, Any], source: str | None = None) -> None:
    """
    Enrich artist metadata from NFO file.

    :param artist: The artist to enrich.
    :param nfo_artist: The parsed 'artist' element from the NFO file.
    :param source: Origin of the NFO data (e.g. file path), included in log messages.
    """
    if title := nfo_artist.get("title") or nfo_artist.get("name"):
        artist.name = _nfo_text(title, "title", source)
    if sort_name := nfo_artist.get("sortname"):
        artist.sort_name = _nfo_text(sort_name, "sortname", source)
    if mbid := clean_mbid(nfo_artist.get("musicbrainzartistid"), source):
        artist.mbid = mbid
    if description := nfo_artist.get("biography"):
        artist.metadata.description = _nfo_text(description, "biography", source)
    if genre := nfo_artist.get("genre"):
        artist.metadata.genres = set(split_items(genre))


def _nfo_text(value: Any, field: str, source: str | None) -> str:
    """
    Return a scalar NFO text value, rejecting the list/mapping shapes xmltodict yields.

    Repeated or nested elements (e.g. two ``<sortname>`` tags) parse to a list or dict; assigning
    those to a scalar field would corrupt the item and only fail later at persistence, so reject
    them here as a malformed NFO.

    :param value: The raw value read from the parsed NFO element.
    :param field: The NFO field name, included in the error message.
    :param source: Origin of the NFO data (e.g. file path), included in the error message.
    :raises ValueError: When the value is not a scalar string.
    """
    if isinstance(value, str):
        return value
    raise ValueError(f"non-scalar {field} in {source}: {value!r}")
