"""Several helper/utils to compare objects."""

from __future__ import annotations

import re

import unidecode

from music_assistant.common.helpers.util import create_sort_name
from music_assistant.common.models.enums import ExternalID
from music_assistant.common.models.media_items import (
    Album,
    AlbumTrack,
    Artist,
    ItemMapping,
    MediaItem,
    MediaItemMetadata,
    Track,
)

IGNORE_VERSIONS = (
    "remaster",
    "explicit",
    "music from and inspired by the motion picture",
    "original soundtrack",
    "hi-res",
)


def compare_artist(
    base_item: Artist | ItemMapping,
    compare_item: Artist | ItemMapping,
    allow_name_match: bool = True,
) -> bool | None:
    """Compare two artist items and return True if they match."""
    if base_item is None or compare_item is None:
        return False
    # return early on exact item_id match
    if compare_item_ids(base_item, compare_item):
        return True
    # return early on (un)matched external id
    external_id_match = compare_external_ids(base_item.external_ids, compare_item.external_ids)
    if external_id_match is not None:
        return external_id_match
    ## fallback to comparing on attributes
    name_match = compare_strings(base_item.name, compare_item.name, strict=True)
    if name_match is False:
        return False
    return name_match if allow_name_match else None


def compare_album(
    base_item: Album | ItemMapping,
    compare_item: Album | ItemMapping,
    allow_name_match: bool = True,
) -> bool | None:
    """Compare two album items and return True if they match."""
    if base_item is None or compare_item is None:
        return False
    # return early on exact item_id match
    if compare_item_ids(base_item, compare_item):
        return True
    # return early on (un)matched external id
    external_id_match = compare_external_ids(base_item.external_ids, compare_item.external_ids)
    if external_id_match is not None:
        return external_id_match
    ## fallback to comparing on attributes
    # compare version
    if not compare_version(base_item.version, compare_item.version):
        return False
    # compare name
    name_match = compare_strings(base_item.name, compare_item.name, strict=True)
    if name_match is False:
        return False
    # compare explicitness
    if (
        hasattr(base_item, "metadata")
        and hasattr(compare_item, "metadata")
        and compare_explicit(base_item.metadata, compare_item.metadata) is False
    ):
        return False
    # compare album artist
    # Note: Not present on ItemMapping
    if (
        isinstance(base_item, Album)
        and isinstance(compare_item, Album)
        and not compare_artists(base_item.artists, compare_item.artists, True)
    ):
        return False
    return name_match if allow_name_match else None


def compare_track(
    base_item: Track | AlbumTrack,
    compare_item: Track | AlbumTrack,
    strict: bool = True,
    track_albums: list[Album | ItemMapping] | None = None,
) -> bool:
    """Compare two track items and return True if they match."""
    if base_item is None or compare_item is None:
        return False
    assert isinstance(base_item, Track)
    assert isinstance(compare_item, Track)
    # return early on exact item_id match
    if compare_item_ids(base_item, compare_item):
        return True
    # return early on (un)matched external id
    external_id_match = compare_external_ids(base_item.external_ids, compare_item.external_ids)
    if external_id_match is not None:
        return external_id_match

    ## fallback to comparing on attributes
    # compare name
    if not compare_strings(base_item.name, compare_item.name, strict=True):
        return False
    # track artist(s) must match
    if not compare_artists(base_item.artists, compare_item.artists, any_match=not strict):
        return False
    # track version must match
    if strict and not compare_version(base_item.version, compare_item.version):
        return False
    # check if both tracks are (not) explicit
    if base_item.metadata.explicit is None and isinstance(base_item.album, Album):
        base_item.metadata.explicit = base_item.album.metadata.explicit
    if compare_item.metadata.explicit is None and isinstance(compare_item.album, Album):
        compare_item.metadata.explicit = compare_item.album.metadata.explicit
    if strict and compare_explicit(base_item.metadata, compare_item.metadata) is False:
        return False
    if not strict and not (base_item.album or track_albums):
        # in non-strict mode, the album does not have to match
        return abs(base_item.duration - compare_item.duration) <= 3
    # exact albumtrack match = 100% match
    if (
        isinstance(base_item, AlbumTrack)
        and isinstance(compare_item, AlbumTrack)
        and compare_album(base_item.album, compare_item.album)
        and base_item.track_number == compare_item.track_number
    ):
        return True
    # fallback: exact album match and (near-exact) track duration match
    if (
        base_item.album is not None
        and compare_item.album is not None
        and compare_album(base_item.album, compare_item.album)
        and abs(base_item.duration - compare_item.duration) <= 3
    ):
        return True
    # fallback: additional compare albums provided for base track
    if (
        compare_item.album is not None
        and track_albums
        and abs(base_item.duration - compare_item.duration) <= 3
    ):
        for track_album in track_albums:
            if compare_album(track_album, compare_item.album):
                return True
    # edge case: albumless track
    if (
        base_item.album is None
        and compare_item.album is None
        and abs(base_item.duration - compare_item.duration) <= 1
    ):
        return True

    # all efforts failed, this is NOT a match
    return False


def compare_artists(
    base_items: list[Artist | ItemMapping],
    compare_items: list[Artist | ItemMapping],
    any_match: bool = True,
) -> bool:
    """Compare two lists of artist and return True if both lists match (exactly)."""
    matches = 0
    for base_item in base_items:
        for compare_item in compare_items:
            if compare_artist(base_item, compare_item):
                if any_match:
                    return True
                matches += 1
    return len(base_items) == matches


def compare_albums(
    base_items: list[Album | ItemMapping],
    compare_items: list[Album | ItemMapping],
    any_match: bool = True,
) -> bool:
    """Compare two lists of albums and return True if a match was found."""
    matches = 0
    for base_item in base_items:
        for compare_item in compare_items:
            if compare_album(base_item, compare_item):
                if any_match:
                    return True
                matches += 1
    return len(base_items) == matches


def compare_item_ids(
    base_item: MediaItem | ItemMapping, compare_item: MediaItem | ItemMapping
) -> bool:
    """Compare item_id(s) of two media items."""
    if not base_item.provider or not compare_item.provider:
        return False
    if not base_item.item_id or not compare_item.item_id:
        return False
    if base_item.provider == compare_item.provider and base_item.item_id == compare_item.item_id:
        return True

    base_prov_ids = getattr(base_item, "provider_mappings", None)
    compare_prov_ids = getattr(compare_item, "provider_mappings", None)

    if base_prov_ids is not None:
        for prov_l in base_item.provider_mappings:
            if (
                prov_l.provider_domain == compare_item.provider
                and prov_l.item_id == compare_item.item_id
            ):
                return True

    if compare_prov_ids is not None:
        for prov_r in compare_item.provider_mappings:
            if prov_r.provider_domain == base_item.provider and prov_r.item_id == base_item.item_id:
                return True

    if base_prov_ids is not None and compare_prov_ids is not None:
        for prov_l in base_item.provider_mappings:
            for prov_r in compare_item.provider_mappings:
                if prov_l.provider_domain != prov_r.provider_domain:
                    continue
                if prov_l.item_id == prov_r.item_id:
                    return True
    return False


def compare_external_ids(
    external_ids_base: set[tuple[ExternalID, str]],
    external_ids_compare: set[tuple[ExternalID, str]],
) -> bool | None:
    """Compare external ids and return True if a match was found."""
    for external_id_base in external_ids_base:
        for external_id_compare in external_ids_compare:
            if external_id_compare[0] != external_id_base[0]:
                continue
            # handle upc stored as EAN-13 barcode
            if external_id_base[0] == ExternalID.BARCODE and len(external_id_base[1]) == 12:
                external_id_base[1] = f"0{external_id_base}"
            if external_id_compare[1] == ExternalID.BARCODE and len(external_id_compare[1]) == 12:
                external_id_compare[1] = f"0{external_id_compare}"
            if external_id_base[0] in (ExternalID.ISRC, ExternalID.BARCODE):
                if external_id_compare[1] == external_id_base[1]:
                    # barcode and isrc can be multiple per media item
                    # so we only return early on match as there might be
                    # another entry for this ExternalID type.
                    return True
                continue
            # other ExternalID types: external id must be exact match.
            return external_id_compare[1] == external_id_base[1]
    # return None to define we did not find the same external id type in both sets
    return None


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


def compare_version(base_version: str, compare_version: str) -> bool:
    """Compare version string."""
    if not base_version and not compare_version:
        return True
    if not base_version and compare_version.lower() in IGNORE_VERSIONS:
        return True
    if not compare_version and base_version.lower() in IGNORE_VERSIONS:
        return True
    if not base_version and compare_version:
        return False
    if base_version and not compare_version:
        return False
    if " " not in base_version:
        return compare_strings(base_version, compare_version)
    # do this the hard way as sometimes the version string is in the wrong order
    base_versions = base_version.lower().split(" ").sort()
    compare_versions = compare_version.lower().split(" ").sort()
    return base_versions == compare_versions


def compare_explicit(base: MediaItemMetadata, compare: MediaItemMetadata) -> bool | None:
    """Compare if explicit is same in metadata."""
    if base.explicit is not None and compare.explicit is not None:
        # explicitness info is not always present in metadata
        # only strict compare them if both have the info set
        return base.explicit == compare.explicit
    return None
