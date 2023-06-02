"""Several helper/utils to compare objects."""
from __future__ import annotations

import re

import unidecode

from music_assistant.common.helpers.util import create_sort_name
from music_assistant.common.models.media_items import (
    Album,
    Artist,
    ItemMapping,
    MediaItem,
    MediaItemMetadata,
    Track,
)


def create_safe_string(input_str: str) -> str:
    """Return clean lowered string for compare actions."""
    input_str = input_str.lower().strip()
    unaccented_string = unidecode.unidecode(input_str)
    return re.sub(r"[^a-zA-Z0-9]", "", unaccented_string)


def loose_compare_strings(base: str, alt: str) -> bool:
    """Compare strings and return True even on partial match."""
    # this is used to display 'versions' of the same track/album
    # where we account for other spelling or some additional wording in the title
    word_count = len(base.split(" "))
    if word_count == 1 and len(base) < 10:
        return compare_strings(base, alt, False)
    base_comp = create_safe_string(base)
    alt_comp = create_safe_string(alt)
    if base_comp in alt_comp:
        return True
    if alt_comp in base_comp:
        return True
    return False


def compare_strings(str1: str, str2: str, strict: bool = True) -> bool:
    """Compare strings and return True if we have an (almost) perfect match."""
    if not str1 or not str2:
        return False
    # return early if total length mismatch
    if abs(len(str1) - len(str2)) > 4:
        return False
    if not strict:
        # handle '&' vs 'And'
        if " & " in str1 and " and " in str2.lower():
            str2 = str2.lower().replace(" and ", " & ")
        elif " and " in str1.lower() and " & " in str2:
            str2 = str2.replace(" & ", " and ")
        return create_safe_string(str1) == create_safe_string(str2)

    return create_sort_name(str1) == create_sort_name(str2)


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
    if left.explicit is None or right.explicit is None:
        # explicitness info is not always present in metadata
        # only strict compare them if both have the info set
        return True
    return left == right


def compare_artist(
    left_artist: Artist | ItemMapping,
    right_artist: Artist | ItemMapping,
) -> bool:
    """Compare two artist items and return True if they match."""
    if left_artist is None or right_artist is None:
        return False
    # return early on exact item_id match
    if compare_item_ids(left_artist, right_artist):
        return True

    # prefer match on musicbrainz_id
    if getattr(left_artist, "musicbrainz_id", None) and getattr(
        right_artist, "musicbrainz_id", None
    ):
        return left_artist.musicbrainz_id == right_artist.musicbrainz_id

    # fallback to comparing
    return compare_strings(left_artist.name, right_artist.name, False)


def compare_artists(
    left_artists: list[Artist | ItemMapping],
    right_artists: list[Artist | ItemMapping],
    any_match: bool = False,
) -> bool:
    """Compare two lists of artist and return True if both lists match (exactly)."""
    matches = 0
    for left_artist in left_artists:
        for right_artist in right_artists:
            if compare_artist(left_artist, right_artist):
                if any_match:
                    return True
                matches += 1
    return len(left_artists) == matches


def compare_item_ids(
    left_item: MediaItem | ItemMapping, right_item: MediaItem | ItemMapping
) -> bool:
    """Compare item_id(s) of two media items."""
    if not left_item.provider or not right_item.provider:
        return False
    if not left_item.item_id or not right_item.item_id:
        return False
    if left_item.provider == right_item.provider and left_item.item_id == right_item.item_id:
        return True

    left_prov_ids = getattr(left_item, "provider_mappings", None)
    right_prov_ids = getattr(right_item, "provider_mappings", None)

    if left_prov_ids is not None:
        for prov_l in left_item.provider_mappings:
            if (
                prov_l.provider_domain == right_item.provider
                and prov_l.item_id == right_item.item_id
            ):
                return True

    if right_prov_ids is not None:
        for prov_r in right_item.provider_mappings:
            if prov_r.provider_domain == left_item.provider and prov_r.item_id == left_item.item_id:
                return True

    if left_prov_ids is not None and right_prov_ids is not None:
        for prov_l in left_item.provider_mappings:
            for prov_r in right_item.provider_mappings:
                if prov_l.provider_domain != prov_r.provider_domain:
                    continue
                if prov_l.item_id == prov_r.item_id:
                    return True
    return False


def compare_albums(
    left_albums: list[Album | ItemMapping],
    right_albums: list[Album | ItemMapping],
):
    """Compare two lists of albums and return True if a match was found."""
    for left_album in left_albums:
        for right_album in right_albums:
            if compare_album(left_album, right_album):
                return True
    return False


def compare_barcode(
    left_barcodes: set[str],
    right_barcodes: set[str],
):
    """Compare two sets of barcodes and return True if a match was found."""
    for left_barcode in left_barcodes:
        if not left_barcode.strip():
            continue
        for right_barcode in right_barcodes:
            if not right_barcode.strip():
                continue
            # convert EAN-13 to UPC-A by stripping off the leading zero
            left_upc = left_barcode[1:] if left_barcode.startswith("0") else left_barcode
            right_upc = right_barcode[1:] if right_barcode.startswith("0") else right_barcode
            if compare_strings(left_upc, right_upc):
                return True
    return False


def compare_isrc(
    left_isrcs: set[str],
    right_isrcs: set[str],
):
    """Compare two sets of isrc codes and return True if a match was found."""
    for left_isrc in left_isrcs:
        if not left_isrc.strip():
            continue
        for right_isrc in right_isrcs:
            if not right_isrc.strip():
                continue
            if compare_strings(left_isrc, right_isrc):
                return True
    return False


def compare_album(
    left_album: Album | ItemMapping,
    right_album: Album | ItemMapping,
):
    """Compare two album items and return True if they match."""
    if left_album is None or right_album is None:
        return False
    # return early on exact item_id match
    if compare_item_ids(left_album, right_album):
        return True
    # prefer match on barcode/upc
    # not present on ItemMapping
    if (
        getattr(left_album, "barcode", None)
        and getattr(right_album, "barcode", None)
        and compare_barcode(left_album.barcode, right_album.barcode)
    ):
        return True
    # prefer match on musicbrainz_id
    # not present on ItemMapping
    if getattr(left_album, "musicbrainz_id", None) and getattr(right_album, "musicbrainz_id", None):
        return left_album.musicbrainz_id == right_album.musicbrainz_id

    # fallback to comparing
    if not compare_strings(left_album.name, right_album.name, True):
        return False
    if not compare_version(left_album.version, right_album.version):
        return False
    if (
        hasattr(left_album, "metadata")
        and hasattr(right_album, "metadata")
        and not compare_explicit(left_album.metadata, right_album.metadata)
    ):
        return False
    # compare album artist
    # Note: Not present on ItemMapping
    if (
        isinstance(left_album, Album)
        and isinstance(right_album, Album)
        and not compare_artists(left_album.artists, right_album.artists, True)
    ):
        return False
    return left_album.sort_name == right_album.sort_name


def compare_track(left_track: Track, right_track: Track, strict: bool = True):
    """Compare two track items and return True if they match."""
    if left_track is None or right_track is None:
        return False
    assert isinstance(left_track, Track) and isinstance(right_track, Track)
    # return early on exact item_id match
    if compare_item_ids(left_track, right_track):
        return True
    if compare_isrc(left_track.isrc, right_track.isrc):
        return True
    if compare_strings(left_track.musicbrainz_id, right_track.musicbrainz_id):
        return True
    # album is required for track linking
    if strict and left_track.album is None or right_track.album is None:
        return False
    # track name must match
    if not compare_strings(left_track.name, right_track.name, False):
        return False
    # track version must match
    if not compare_version(left_track.version, right_track.version):
        return False
    # track artist(s) must match
    if not compare_artists(left_track.artists, right_track.artists):
        return False
    # check if both tracks are (not) explicit
    if strict and not compare_explicit(left_track.metadata, right_track.metadata):
        return False
    # exact albumtrack match = 100% match
    if (
        compare_album(left_track.album, right_track.album)
        and left_track.track_number
        and right_track.track_number
        and ((left_track.disc_number or 1) == (right_track.disc_number or 1))
        and left_track.track_number == right_track.track_number
    ):
        return True
    # check album match
    if (
        not (album_match_found := compare_album(left_track.album, right_track.album))
        and left_track.albums
        and right_track.albums
    ):
        for left_album in left_track.albums:
            for right_album in right_track.albums:
                if compare_album(left_album, right_album):
                    album_match_found = True
                    if (
                        (left_album.disc_number or 1) == (right_album.disc_number or 1)
                        and left_album.track_number
                        and right_album.track_number
                        and left_album.track_number == right_album.track_number
                    ):
                        # exact albumtrack match = 100% match
                        return True
    # fallback: exact album match and (near-exact) track duration match
    if album_match_found and abs(left_track.duration - right_track.duration) <= 3:
        return True
    return False
