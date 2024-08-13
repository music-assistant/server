"""Several helper/utils to compare objects."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

import unidecode

from music_assistant.common.models.enums import ExternalID, MediaType
from music_assistant.common.models.media_items import (
    Album,
    Artist,
    ItemMapping,
    MediaItem,
    MediaItemMetadata,
    MediaItemType,
    Playlist,
    Radio,
    Track,
)

IGNORE_VERSIONS = (
    "explicit",  # explicit is matched separately
    "music from and inspired by the motion picture",
    "original soundtrack",
    "hi-res",  # quality is handled separately
)


def compare_media_item(
    base_item: MediaItemType | ItemMapping,
    compare_item: MediaItemType | ItemMapping,
    strict: bool = True,
) -> bool | None:
    """Compare two media items and return True if they match."""
    if base_item.media_type == MediaType.ARTIST and compare_item.media_type == MediaType.ARTIST:
        return compare_artist(base_item, compare_item, strict)
    if base_item.media_type == MediaType.ALBUM and compare_item.media_type == MediaType.ALBUM:
        return compare_album(base_item, compare_item, strict)
    if base_item.media_type == MediaType.TRACK and compare_item.media_type == MediaType.TRACK:
        return compare_track(base_item, compare_item, strict)
    if base_item.media_type == MediaType.PLAYLIST and compare_item.media_type == MediaType.PLAYLIST:
        return compare_playlist(base_item, compare_item, strict)
    if base_item.media_type == MediaType.RADIO and compare_item.media_type == MediaType.RADIO:
        return compare_radio(base_item, compare_item, strict)
    return compare_item_mapping(base_item, compare_item, strict)


def compare_artist(
    base_item: Artist | ItemMapping,
    compare_item: Artist | ItemMapping,
    strict: bool = True,
) -> bool | None:
    """Compare two artist items and return True if they match."""
    if base_item is None or compare_item is None:
        return False
    # return early on exact item_id match
    if compare_item_ids(base_item, compare_item):
        return True
    # return early on (un)matched external id
    for ext_id in (ExternalID.DISCOGS, ExternalID.MB_ARTIST, ExternalID.TADB):
        external_id_match = compare_external_ids(
            base_item.external_ids, compare_item.external_ids, ext_id
        )
        if external_id_match is not None:
            return external_id_match
    # finally comparing on (exact) name match
    return compare_strings(base_item.name, compare_item.name, strict=strict)


def compare_album(
    base_item: Album | ItemMapping | None,
    compare_item: Album | ItemMapping | None,
    strict: bool = True,
) -> bool | None:
    """Compare two album items and return True if they match."""
    if base_item is None or compare_item is None:
        return False
    # return early on exact item_id match
    if compare_item_ids(base_item, compare_item):
        return True

    # return early on (un)matched external id
    for ext_id in (
        ExternalID.DISCOGS,
        ExternalID.MB_ALBUM,
        ExternalID.TADB,
        ExternalID.ASIN,
        ExternalID.BARCODE,
    ):
        external_id_match = compare_external_ids(
            base_item.external_ids, compare_item.external_ids, ext_id
        )
        if external_id_match is not None:
            return external_id_match

    # compare version
    if not compare_version(base_item.version, compare_item.version):
        return False
    # compare name
    if not compare_strings(base_item.name, compare_item.name, strict=True):
        return False
    if not strict and (isinstance(base_item, ItemMapping) or isinstance(compare_item, ItemMapping)):
        return True
    # for strict matching we REQUIRE both items to be a real album object
    assert isinstance(base_item, Album)
    assert isinstance(compare_item, Album)
    # compare year
    if base_item.year and compare_item.year and base_item.year != compare_item.year:
        return False
    # compare explicitness
    if compare_explicit(base_item.metadata, compare_item.metadata) is False:
        return False
    # compare album artist(s)
    return compare_artists(base_item.artists, compare_item.artists, not strict)


def compare_track(
    base_item: Track | None,
    compare_item: Track | None,
    strict: bool = True,
    track_albums: list[Album] | None = None,
) -> bool:
    """Compare two track items and return True if they match."""
    if base_item is None or compare_item is None:
        return False
    # return early on exact item_id match
    if compare_item_ids(base_item, compare_item):
        return True
    # return early on (un)matched external id
    for ext_id in (
        ExternalID.MB_RECORDING,
        ExternalID.DISCOGS,
        ExternalID.ACOUSTID,
        ExternalID.TADB,
        # make sure to check musicbrainz before isrc
        # https://github.com/music-assistant/hass-music-assistant/issues/2316
        ExternalID.ISRC,
        ExternalID.ASIN,
    ):
        external_id_match = compare_external_ids(
            base_item.external_ids, compare_item.external_ids, ext_id
        )
        if external_id_match is not None:
            return external_id_match

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

    # exact albumtrack match = 100% match
    if (
        base_item.album
        and compare_item.album
        and compare_album(base_item.album, compare_item.album, False)
        and base_item.disc_number
        and compare_item.disc_number
        and base_item.track_number
        and compare_item.track_number
        and base_item.disc_number == compare_item.disc_number
        and base_item.track_number == compare_item.track_number
    ):
        return True

    # fallback: exact album match and (near-exact) track duration match
    if (
        base_item.album is not None
        and compare_item.album is not None
        and (base_item.track_number == 0 or compare_item.track_number == 0)
        and compare_album(base_item.album, compare_item.album, False)
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
            if compare_album(track_album, compare_item.album, False):
                return True

    # fallback edge case: albumless track with same duration
    if (
        base_item.album is None
        and compare_item.album is None
        and base_item.disc_number == 0
        and compare_item.disc_number == 0
        and base_item.track_number == 0
        and compare_item.track_number == 0
        and base_item.duration == compare_item.duration
    ):
        return True

    if strict:
        # in strict mode, we require an exact album match so return False here
        return False

    # Accept last resort (in non strict mode): (near) exact duration,
    # otherwise fail all other cases.
    # Note that as this stage, all other info already matches,
    # such as title, artist etc.
    return abs(base_item.duration - compare_item.duration) <= 2


def compare_playlist(
    base_item: Playlist | ItemMapping,
    compare_item: Playlist | ItemMapping,
    strict: bool = True,
) -> bool | None:
    """Compare two Playlist items and return True if they match."""
    if base_item is None or compare_item is None:
        return False
    # return early on exact item_id match
    if compare_item_ids(base_item, compare_item):
        return True
    # compare owner (if not ItemMapping)
    if isinstance(base_item, Playlist) and isinstance(compare_item, Playlist):
        if not compare_strings(base_item.owner, compare_item.owner):
            return False
    # compare version
    if not compare_version(base_item.version, compare_item.version):
        return False
    # finally comparing on (exact) name match
    return compare_strings(base_item.name, compare_item.name, strict=strict)


def compare_radio(
    base_item: Radio | ItemMapping,
    compare_item: Radio | ItemMapping,
    strict: bool = True,
) -> bool | None:
    """Compare two Radio items and return True if they match."""
    if base_item is None or compare_item is None:
        return False
    # return early on exact item_id match
    if compare_item_ids(base_item, compare_item):
        return True
    # compare version
    if not compare_version(base_item.version, compare_item.version):
        return False
    # finally comparing on (exact) name match
    return compare_strings(base_item.name, compare_item.name, strict=strict)


def compare_item_mapping(
    base_item: ItemMapping,
    compare_item: ItemMapping,
    strict: bool = True,
) -> bool | None:
    """Compare two ItemMapping items and return True if they match."""
    if base_item is None or compare_item is None:
        return False
    # return early on exact item_id match
    if compare_item_ids(base_item, compare_item):
        return True
    # return early on (un)matched external id
    external_id_match = compare_external_ids(base_item.external_ids, compare_item.external_ids)
    if external_id_match is not None:
        return external_id_match
    # compare version
    if not compare_version(base_item.version, compare_item.version):
        return False
    # finally comparing on (exact) name match
    return compare_strings(base_item.name, compare_item.name, strict=strict)


def compare_artists(
    base_items: list[Artist | ItemMapping],
    compare_items: list[Artist | ItemMapping],
    any_match: bool = True,
) -> bool:
    """Compare two lists of artist and return True if both lists match (exactly)."""
    if not base_items or not compare_items:
        return False
    # match if first artist matches in both lists
    if compare_artist(base_items[0], compare_items[0]):
        return True
    # compare the artist lists
    matches = 0
    for base_item in base_items:
        for compare_item in compare_items:
            if compare_artist(base_item, compare_item):
                if any_match:
                    return True
                matches += 1
    return len(base_items) == len(compare_items) == matches


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
    external_id_type: ExternalID,
) -> bool | None:
    """Compare external ids and return True if a match was found."""
    base_ids = {x[1] for x in external_ids_base if x[0] == external_id_type}
    if not base_ids:
        # return early if the requested external id type is not present in the base set
        return None
    compare_ids = {x[1] for x in external_ids_compare if x[0] == external_id_type}
    if not compare_ids:
        # return early if the requested external id type is not present in the compare set
        return None
    for base_id in base_ids:
        if base_id in compare_ids:
            return True
        # handle upc stored as EAN-13 barcode
        if external_id_type == ExternalID.BARCODE and len(base_id) == 12:
            if f"0{base_id}" in compare_ids:
                return True
        # handle EAN-13 stored as UPC barcode
        if external_id_type == ExternalID.BARCODE and len(base_id) == 13:
            if base_id[1:] in compare_ids:
                return True
        # return false if the identifier is unique (e.g. musicbrainz id)
        if external_id_type.is_unique:
            return False
    return None


def create_safe_string(input_str: str, lowercase: bool = True, replace_space: bool = False) -> str:
    """Return clean lowered string for compare actions."""
    input_str = input_str.lower().strip() if lowercase else input_str.strip()
    unaccented_string = unidecode.unidecode(input_str)
    regex = r"[^a-zA-Z0-9]" if replace_space else r"[^a-zA-Z0-9 ]"
    return re.sub(regex, "", unaccented_string)


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
    return alt_comp in base_comp


def compare_strings(str1: str, str2: str, strict: bool = True) -> bool:
    """Compare strings and return True if we have an (almost) perfect match."""
    if not str1 or not str2:
        return False
    str1_lower = str1.lower()
    str2_lower = str2.lower()
    if strict:
        return str1_lower == str2_lower
    # return early if total length mismatch
    if abs(len(str1) - len(str2)) > 4:
        return False
    # handle '&' vs 'And'
    if " & " in str1_lower and " and " in str2_lower:
        str2 = str2_lower.replace(" and ", " & ")
    elif " and " in str1_lower and " & " in str2:
        str2 = str2.replace(" & ", " and ")
    if create_safe_string(str1) == create_safe_string(str2):
        return True
    # last resort: use difflib to compare strings
    required_accuracy = 0.9 if (len(str1) + len(str2)) > 18 else 0.8
    return SequenceMatcher(a=str1_lower, b=str2).ratio() > required_accuracy


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

    if " " not in base_version and " " not in compare_version:
        return compare_strings(base_version, compare_version, False)

    # do this the hard way as sometimes the version string is in the wrong order
    base_versions = sorted(base_version.lower().split(" "))
    compare_versions = sorted(compare_version.lower().split(" "))
    # filter out words we can ignore (such as 'version')
    ignore_words = [*IGNORE_VERSIONS, "version", "edition", "variant", "versie", "versione"]
    base_versions = [x for x in base_versions if x not in ignore_words]
    compare_versions = [x for x in compare_versions if x not in ignore_words]

    return base_versions == compare_versions


def compare_explicit(base: MediaItemMetadata, compare: MediaItemMetadata) -> bool | None:
    """Compare if explicit is same in metadata."""
    if base.explicit is not None and compare.explicit is not None:
        # explicitness info is not always present in metadata
        # only strict compare them if both have the info set
        return base.explicit == compare.explicit
    return None
