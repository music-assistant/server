"""Parsers for the local filesystem provider."""

from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ExternalID

from music_assistant.helpers.tags import clean_mbid, split_items
from music_assistant.helpers.util import parse_title_and_version

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album


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
