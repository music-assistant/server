"""Several helper/utils to compare objects."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, List

import unidecode

if TYPE_CHECKING:
    from music_assistant.models.media_items import Album, Artist, Track


def get_compare_string(input_str):
    """Return clean lowered string for compare actions."""
    unaccented_string = unidecode.unidecode(input_str)
    return re.sub(r"[^a-zA-Z0-9]", "", unaccented_string).lower()


def compare_strings(str1, str2, strict=False):
    """Compare strings and return True if we have an (almost) perfect match."""
    match = str1.lower() == str2.lower()
    if not match and not strict:
        match = get_compare_string(str1) == get_compare_string(str2)
    return match


def compare_version(left_version: str, right_version: str):
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


def compare_artists(left_artists: List["Artist"], right_artists: List["Artist"]):
    """Compare two lists of artist and return True if both lists match."""
    matches = 0
    for left_artist in left_artists:
        for right_artist in right_artists:
            if compare_strings(left_artist.name, right_artist.name):
                matches += 1
    return len(left_artists) == matches


def compare_albums(left_albums: List["Album"], right_albums: List["Album"]):
    """Compare two lists of albums and return True if a match was found."""
    for left_album in left_albums:
        for right_album in right_albums:
            if compare_album(left_album, right_album):
                return True
    return False


def compare_album(left_album: "Album", right_album: "Album"):
    """Compare two album items and return True if they match."""
    if left_album is None or right_album is None:
        return False
    # do not match on year and albumtype as this info is often inaccurate on providers
    if (
        left_album.provider == right_album.provider
        and left_album.item_id == right_album.item_id
    ):
        return True
    if left_album.upc and right_album.upc:
        if (left_album.upc in right_album.upc) or (right_album.upc in left_album.upc):
            # UPC is always 100% accurate match
            return True
    if not compare_strings(left_album.name, right_album.name):
        return False
    if not compare_version(left_album.version, right_album.version):
        return False
    if not compare_strings(left_album.artist.name, right_album.artist.name):
        return False
    # 100% match, all criteria passed
    return True


def compare_track(left_track: "Track", right_track: "Track"):
    """Compare two track items and return True if they match."""
    if (
        left_track.provider == right_track.provider
        and left_track.item_id == right_track.item_id
    ):
        return True
    if left_track.isrc and left_track.isrc == right_track.isrc:
        # ISRC is always 100% accurate match
        return True
    # track name and version must match
    if not compare_strings(left_track.name, right_track.name):
        return False
    if not compare_version(left_track.version, right_track.version):
        return False
    # track artist(s) must match
    if not compare_artists(left_track.artists, right_track.artists):
        return False
    # album match OR near exact duration match
    if (
        compare_album(left_track.album, right_track.album)
        and abs(left_track.duration - right_track.duration) < 5
    ) or abs(left_track.duration - right_track.duration) < 1:
        # 100% match, all criteria passed
        return True
    return False
