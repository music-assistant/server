"""Several helper/utils to compare objects."""
from __future__ import annotations

from typing import List, Union

from music_assistant.helpers.util import create_clean_string
from music_assistant.models.enums import AlbumType
from music_assistant.models.media_items import (
    Album,
    Artist,
    ItemMapping,
    MediaItem,
    MediaItemMetadata,
    Track,
)


def compare_strings(str1, str2, strict=False) -> bool:
    """Compare strings and return True if we have an (almost) perfect match."""
    if str1 is None or str2 is None:
        return False
    if not strict:
        return create_clean_string(str1) == create_clean_string(str2)
    return str1.lower().strip() == str2.lower().strip()


def compare_version(left_version: str, right_version: str) -> bool:
    """Compare version string."""
    if not left_version and not right_version:
        return True
    if not left_version and right_version:
        return False
    if left_version and not right_version:
        return False
    if " " not in left_version:
        return compare_strings(left_version, right_version)
    # do this the hard way as sometimes the version string is in the wrong order
    left_versions = left_version.lower().split(" ").sort()
    right_versions = right_version.lower().split(" ").sort()
    return left_versions == right_versions


def compare_explicit(left: MediaItemMetadata, right: MediaItemMetadata) -> bool:
    """Compare if explicit is same in metadata."""
    if left.explicit is None and right.explicit is None:
        return True
    return left == right


def compare_artist(
    left_artist: Union[Artist, ItemMapping],
    right_artist: Union[Artist, ItemMapping],
) -> bool:
    """Compare two artist items and return True if they match."""
    if left_artist is None or right_artist is None:
        return False
    # return early on exact item_id match
    if compare_item_id(left_artist, right_artist):
        return True

    # prefer match on musicbrainz_id
    if getattr(left_artist, "musicbrainz_id", None) and getattr(
        right_artist, "musicbrainz_id", None
    ):
        return left_artist.musicbrainz_id == right_artist.musicbrainz_id

    # fallback to comparing
    if not left_artist.sort_name:
        left_artist.sort_name = create_clean_string(left_artist.name)
    if not right_artist.sort_name:
        right_artist.sort_name = create_clean_string(right_artist.name)
    return left_artist.sort_name == right_artist.sort_name


def compare_artists(
    left_artists: List[Union[Artist, ItemMapping]],
    right_artists: List[Union[Artist, ItemMapping]],
) -> bool:
    """Compare two lists of artist and return True if both lists match (exactly)."""
    matches = 0
    for left_artist in left_artists:
        for right_artist in right_artists:
            if compare_artist(left_artist, right_artist):
                matches += 1
    return len(left_artists) == matches


def compare_item_id(
    left_item: Union[MediaItem, ItemMapping], right_item: Union[MediaItem, ItemMapping]
) -> bool:
    """Compare two lists of artist and return True if both lists match."""
    if (
        left_item.provider == right_item.provider
        and left_item.item_id == right_item.item_id
    ):
        return True

    if not hasattr(left_item, "provider_ids") or not hasattr(
        right_item, "provider_ids"
    ):
        return False
    for prov_l in left_item.provider_ids:
        for prov_r in right_item.provider_ids:
            if prov_l.prov_type != prov_r.prov_type:
                continue
            if prov_l.item_id == prov_r.item_id:
                return True
    return False


def compare_albums(
    left_albums: List[Union[Album, ItemMapping]],
    right_albums: List[Union[Album, ItemMapping]],
):
    """Compare two lists of albums and return True if a match was found."""
    for left_album in left_albums:
        for right_album in right_albums:
            if compare_album(left_album, right_album):
                return True
    return False


def compare_album(
    left_album: Union[Album, ItemMapping],
    right_album: Union[Album, ItemMapping],
):
    """Compare two album items and return True if they match."""
    if left_album is None or right_album is None:
        return False
    # return early on exact item_id match
    if compare_item_id(left_album, right_album):
        return True

    # prefer match on UPC
    if getattr(left_album, "upc", None) and getattr(right_album, "upc", None):
        if (left_album.upc in right_album.upc) or (right_album.upc in left_album.upc):
            return True
    # prefer match on musicbrainz_id
    # not present on ItemMapping
    if getattr(left_album, "musicbrainz_id", None) and getattr(
        right_album, "musicbrainz_id", None
    ):
        return left_album.musicbrainz_id == right_album.musicbrainz_id

    # fallback to comparing
    if not left_album.sort_name:
        left_album.sort_name = create_clean_string(left_album.name)
    if not right_album.sort_name:
        right_album.sort_name = create_clean_string(right_album.name)
    if left_album.sort_name != right_album.sort_name:
        return False
    if not compare_version(left_album.version, right_album.version):
        return False
    # compare album artist
    # Note: Not present on ItemMapping
    if hasattr(left_album, "artist") and hasattr(right_album, "artist"):
        if not compare_artist(left_album.artist, right_album.artist):
            return False
    return left_album.sort_name == right_album.sort_name


def compare_track(left_track: Track, right_track: Track):
    """Compare two track items and return True if they match."""
    if left_track is None or right_track is None:
        return False
    # album is required for track linking
    if left_track.album is None or right_track.album is None:
        return False
    # return early on exact item_id match
    if compare_item_id(left_track, right_track):
        return True
    if left_track.isrc and left_track.isrc == right_track.isrc:
        # ISRC is always 100% accurate match
        return True
    if left_track.musicbrainz_id and right_track.musicbrainz_id:
        if left_track.musicbrainz_id == right_track.musicbrainz_id:
            # musicbrainz_id is always 100% accurate match
            return True
    # track name and version must match
    if not left_track.sort_name:
        left_track.sort_name = create_clean_string(left_track.name)
    if not right_track.sort_name:
        right_track.sort_name = create_clean_string(right_track.name)
    if left_track.sort_name != right_track.sort_name:
        return False
    if not compare_version(left_track.version, right_track.version):
        return False
    # track artist(s) must match
    if not compare_artists(left_track.artists, right_track.artists):
        return False
    # track if both tracks are (not) explicit
    if not compare_explicit(left_track.metadata, right_track.metadata):
        return False
    # exact album match = 100% match
    if compare_album(left_track.album, right_track.album):
        return True
    if left_track.albums and right_track.albums:
        for left_album in left_track.albums:
            for right_album in right_track.albums:
                if compare_album(left_album, right_album):
                    return True
    # fallback: both albums are compilations and (near-exact) track duration match
    if (
        abs(left_track.duration - right_track.duration) <= 1
        and left_track.album.album_type in (AlbumType.UNKNOWN, AlbumType.COMPILATION)
        and right_track.album.album_type in (AlbumType.UNKNOWN, AlbumType.COMPILATION)
    ):
        return True
    return False
