"""Parsers for Apple Music API response objects."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse

from music_assistant_models.enums import AlbumType, ContentType, ExternalID, ImageType, MediaType
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    Playlist,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.helpers.util import (
    infer_album_type,
    normalize_unicode,
    parse_title_and_version,
)

from .constants import BLOBSTORE_DOMAIN, MAX_ARTWORK_DIMENSION, UNKNOWN_PLAYLIST_NAME
from .helpers.utils import is_library_id

if TYPE_CHECKING:
    from .provider import AppleMusicProvider


def is_remotely_accessible_artwork_url(url: str) -> bool:
    """
    Check if artwork URL is remotely accessible without caching.

    Blobstore URLs have AWS signatures that expire after 24h, so they must be cached immediately.
    mzstatic.com URLs are permanent CDN URLs that can be used directly.

    :param url: The artwork URL to check.
    :return: True if the URL is remotely accessible (permanent), False if it needs caching.
    """
    hostname = urlparse(url).hostname or ""
    return BLOBSTORE_DOMAIN not in hostname


def parse_artist(provider: AppleMusicProvider, artist_obj: dict[str, Any]) -> Artist | ItemMapping:
    """Parse artist object to generic layout."""
    relationships = artist_obj.get("relationships", {})
    if (
        artist_obj.get("type") == "library-artists"
        and relationships.get("catalog", {}).get("data", []) != []
    ):
        artist_id = relationships["catalog"]["data"][0]["id"]
        attributes = relationships["catalog"]["data"][0]["attributes"]
    elif "attributes" in artist_obj:
        artist_id = artist_obj["id"]
        attributes = artist_obj["attributes"]
    else:
        artist_id = artist_obj["id"]
        provider.logger.debug("No attributes found for artist %s", artist_obj)
        return ItemMapping(
            media_type=MediaType.ARTIST,
            provider=provider.instance_id,
            item_id=artist_id,
            name=artist_id,
        )
    artist = Artist(
        item_id=artist_id,
        name=cast("str", normalize_unicode(attributes.get("name"))),
        provider=provider.domain,
        provider_mappings={
            ProviderMapping(
                item_id=artist_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                url=attributes.get("url"),
            )
        },
    )
    if artwork := attributes.get("artwork"):
        url = artwork["url"]
        if artwork["width"] and artwork["height"]:
            url = url.format(
                w=min(artwork["width"], MAX_ARTWORK_DIMENSION),
                h=min(artwork["height"], MAX_ARTWORK_DIMENSION),
            )
        artist.metadata.add_image(
            MediaItemImage(
                provider=provider.instance_id,
                type=ImageType.THUMB,
                path=url,
                remotely_accessible=is_remotely_accessible_artwork_url(url),
            )
        )
    if genres := attributes.get("genreNames"):
        artist.metadata.genres = set(genres)
    if notes := attributes.get("editorialNotes"):
        artist.metadata.description = notes.get("standard") or notes.get("short")
    return artist


def parse_album(
    provider: AppleMusicProvider,
    album_obj: dict[str, Any],
    is_favourite: bool | None = None,
) -> Album | ItemMapping | None:
    """Parse album object to generic layout."""
    relationships = album_obj.get("relationships", {})
    catalog_data = relationships.get("catalog", {}).get("data", [])
    response_type = album_obj.get("type")
    if response_type == "library-albums" and catalog_data != [] and "attributes" in catalog_data[0]:
        album_id = catalog_data[0]["id"]
        attributes = catalog_data[0]["attributes"]
    elif "attributes" in album_obj:
        album_id = album_obj["id"]
        attributes = album_obj["attributes"]
    else:
        album_id = album_obj["id"]
        return ItemMapping(
            media_type=MediaType.ALBUM,
            provider=provider.instance_id,
            item_id=album_id,
            name=album_id,
        )
    name, version = parse_title_and_version(attributes["name"])
    # Check availability: library albums owned by user OR catalog items with playParams
    is_library_album = is_library_id(album_id) and album_obj.get("type") == "library-albums"
    has_play_params = attributes.get("playParams", {}).get("id") is not None
    album = Album(
        item_id=album_id,
        provider=provider.domain,
        name=cast("str", normalize_unicode(name)),
        version=version,
        provider_mappings={
            ProviderMapping(
                item_id=album_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                url=attributes.get("url"),
                available=is_library_album or has_play_params,
            )
        },
    )
    album_artists = _parse_album_artists(provider, attributes, relationships)
    if album_artists:
        album.artists = album_artists
    if release_date := attributes.get("releaseDate"):
        album.year = int(release_date.split("-")[0])
    if genres := attributes.get("genreNames"):
        album.metadata.genres = set(genres)
    if artwork := attributes.get("artwork"):
        url = artwork["url"]
        if artwork["width"] and artwork["height"]:
            url = url.format(
                w=min(artwork["width"], MAX_ARTWORK_DIMENSION),
                h=min(artwork["height"], MAX_ARTWORK_DIMENSION),
            )
        album.metadata.add_image(
            MediaItemImage(
                provider=provider.instance_id,
                type=ImageType.THUMB,
                path=url,
                remotely_accessible=is_remotely_accessible_artwork_url(url),
            )
        )
    if album_copyright := attributes.get("copyright"):
        album.metadata.copyright = album_copyright
    if record_label := attributes.get("recordLabel"):
        album.metadata.label = record_label
    if upc := attributes.get("upc"):
        album.external_ids.add((ExternalID.BARCODE, "0" + upc))
    if notes := attributes.get("editorialNotes"):
        album.metadata.description = notes.get("standard") or notes.get("short")
    if content_rating := attributes.get("contentRating"):
        album.metadata.explicit = content_rating == "explicit"
    album_type = AlbumType.ALBUM
    if attributes.get("isSingle"):
        album_type = AlbumType.SINGLE
    elif attributes.get("isCompilation"):
        album_type = AlbumType.COMPILATION
    album.album_type = album_type
    # Try inference — override if it finds something more specific
    inferred_type = infer_album_type(album.name, "")
    if inferred_type in (AlbumType.SOUNDTRACK, AlbumType.LIVE):
        album.album_type = inferred_type
    album.favorite = is_favourite or False
    return album


def parse_track(
    provider: AppleMusicProvider,
    track_obj: dict[str, Any],
    is_favourite: bool | None = None,
) -> Track:
    """Parse track object to generic layout."""
    relationships = track_obj.get("relationships", {})
    raw_attributes = track_obj.get("attributes", {})
    if (
        track_obj.get("type") == "library-songs"
        and relationships.get("catalog", {}).get("data", []) != []
    ):
        track_id = relationships.get("catalog", {})["data"][0]["id"]
        attributes = relationships.get("catalog", {})["data"][0]["attributes"]
    elif "attributes" in track_obj:
        track_id = track_obj["id"]
        attributes = track_obj["attributes"]
    else:
        track_id = track_obj["id"]
        attributes = {}
    name, version = parse_title_and_version(attributes.get("name", ""))
    # Check availability: library tracks owned by user OR catalog items with playParams
    is_library_track = is_library_id(track_id) and track_obj.get("type") == "library-songs"
    has_play_params = attributes.get("playParams", {}).get("id") is not None
    track = Track(
        item_id=track_id,
        provider=provider.domain,
        name=cast("str", normalize_unicode(name)),
        version=version,
        duration=attributes.get("durationInMillis", 0) / 1000,
        provider_mappings={
            ProviderMapping(
                item_id=track_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                audio_format=AudioFormat(content_type=ContentType.AAC),
                url=attributes.get("url"),
                available=is_library_track or has_play_params,
            )
        },
    )
    if disc_number := attributes.get("discNumber"):
        track.disc_number = disc_number
    if track_number := attributes.get("trackNumber"):
        track.track_number = track_number
    # Prefer catalog information over library information for artists.
    if "artists" in relationships:
        artists = relationships["artists"]
        track.artists = UniqueList([parse_artist(provider, artist) for artist in artists["data"]])
    elif artist_name := normalize_unicode(
        attributes.get("artistName") or raw_attributes.get("artistName")
    ):
        track.artists = UniqueList(
            [
                ItemMapping(
                    media_type=MediaType.ARTIST,
                    item_id=artist_name,
                    provider=provider.instance_id,
                    name=artist_name,
                )
            ]
        )
    if albums := relationships.get("albums"):
        if "data" in albums and len(albums["data"]) > 0:
            parsed_album = parse_album(provider, albums["data"][0])
            if parsed_album:
                track.album = parsed_album
    elif album_name := normalize_unicode(
        attributes.get("albumName") or raw_attributes.get("albumName")
    ):
        track.album = ItemMapping(
            media_type=MediaType.ALBUM,
            item_id=album_name,
            provider=provider.instance_id,
            name=album_name,
        )
    if artwork := attributes.get("artwork"):
        url = artwork["url"]
        if artwork["width"] and artwork["height"]:
            url = url.format(
                w=min(artwork["width"], MAX_ARTWORK_DIMENSION),
                h=min(artwork["height"], MAX_ARTWORK_DIMENSION),
            )
        track.metadata.add_image(
            MediaItemImage(
                provider=provider.instance_id,
                type=ImageType.THUMB,
                path=url,
                remotely_accessible=is_remotely_accessible_artwork_url(url),
            )
        )
    if genres := attributes.get("genreNames"):
        track.metadata.genres = set(genres)
    if composers := attributes.get("composerName"):
        track.metadata.performers = set(composers.split(", "))
    if content_rating := attributes.get("contentRating"):
        track.metadata.explicit = content_rating == "explicit"
    if isrc := attributes.get("isrc"):
        track.external_ids.add((ExternalID.ISRC, isrc))
    track.favorite = is_favourite or False
    return track


def parse_playlist(
    provider: AppleMusicProvider,
    playlist_obj: dict[str, Any],
    is_favourite: bool | None = None,
    can_edit_hint: bool | None = None,
    library_id_override: str | None = None,
) -> Playlist:
    """Parse Apple Music playlist object to generic layout."""
    attributes = playlist_obj["attributes"]
    raw_playlist_id = playlist_obj["id"]
    play_params = attributes.get("playParams", {})
    # Prefer write-safe library IDs when available.
    playlist_id = (
        library_id_override
        or (raw_playlist_id if is_library_id(raw_playlist_id) else play_params.get("globalId"))
        or raw_playlist_id
    )
    is_editable = can_edit_hint if can_edit_hint is not None else attributes.get("canEdit", False)
    playlist = Playlist(
        item_id=playlist_id,
        provider=provider.instance_id,
        name=attributes.get("name", UNKNOWN_PLAYLIST_NAME),
        owner=attributes.get("curatorName", "me"),
        provider_mappings={
            ProviderMapping(
                item_id=playlist_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                url=attributes.get("url"),
                is_unique=is_editable,
            )
        },
        is_editable=is_editable,
    )
    if artwork := attributes.get("artwork"):
        url = artwork["url"]
        if artwork["width"] and artwork["height"]:
            url = url.format(
                w=min(artwork["width"], MAX_ARTWORK_DIMENSION),
                h=min(artwork["height"], MAX_ARTWORK_DIMENSION),
            )
        playlist.metadata.add_image(
            MediaItemImage(
                provider=provider.instance_id,
                type=ImageType.THUMB,
                path=url,
                remotely_accessible=True,
            )
        )
    if description := attributes.get("description"):
        playlist.metadata.description = description.get("standard")
    playlist.favorite = is_favourite or False
    return playlist


def parse_station_as_playlist(
    provider: AppleMusicProvider,
    station_obj: dict[str, Any],
) -> Playlist:
    """Parse a station object into a dynamic Playlist."""
    station_id = station_obj["id"]
    attributes = station_obj.get("attributes", {})
    name = attributes.get("name", station_id)
    playlist = Playlist(
        item_id=station_id,
        provider=provider.instance_id,
        name=name,
        is_dynamic=True,
        provider_mappings={
            ProviderMapping(
                item_id=station_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
            )
        },
    )
    if artwork := attributes.get("artwork"):
        url = artwork["url"]
        if artwork.get("width") and artwork.get("height"):
            url = url.format(
                w=min(artwork["width"], MAX_ARTWORK_DIMENSION),
                h=min(artwork["height"], MAX_ARTWORK_DIMENSION),
            )
        playlist.metadata.add_image(
            MediaItemImage(
                provider=provider.instance_id,
                type=ImageType.THUMB,
                path=url,
                remotely_accessible=True,
            )
        )
    return playlist


def _parse_album_artists(
    provider: AppleMusicProvider,
    attributes: dict[str, Any],
    relationships: dict[str, Any],
) -> UniqueList[Artist | ItemMapping] | None:
    """Parse the album artists from an album's attributes and relationships."""
    album_artist_name = normalize_unicode(attributes.get("artistName"))
    # Skip relationships that cannot produce a named artist.
    artist_objs = [
        artist
        for artist in relationships.get("artists", {}).get("data", [])
        if _has_artist_details(artist)
    ]
    artists = UniqueList([parse_artist(provider, artist) for artist in artist_objs])
    if album_artist_name and attributes.get("isCompilation"):
        # A lone related artist can be a contributor rather than the album artist.
        if len(artists) == 1 and artists[0].name != album_artist_name:
            artists = UniqueList()
    if artists:
        return artists
    if album_artist_name:
        return UniqueList(
            [
                ItemMapping(
                    media_type=MediaType.ARTIST,
                    provider=provider.instance_id,
                    item_id=album_artist_name,
                    name=album_artist_name,
                )
            ]
        )
    return None


def _has_artist_details(artist_obj: dict[str, Any]) -> bool:
    """Check if an artist object holds enough details to parse it."""
    relationships = artist_obj.get("relationships", {})
    catalog_data = relationships.get("catalog", {}).get("data", [])
    if artist_obj.get("type") == "library-artists" and catalog_data:
        attributes = catalog_data[0].get("attributes", {})
    else:
        attributes = artist_obj.get("attributes", {})
    return bool(normalize_unicode(attributes.get("name")))
