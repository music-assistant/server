"""Several helper/utils to compare objects."""

from __future__ import annotations

import re
from collections.abc import Sequence
from difflib import SequenceMatcher
from enum import Enum
from functools import lru_cache

from music_assistant_models.enums import ExternalID, MediaType
from music_assistant_models.helpers import create_safe_string
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    ItemMapping,
    MediaItem,
    MediaItemMetadata,
    MediaItemType,
    Playlist,
    Podcast,
    Radio,
    Track,
)

from music_assistant.helpers.external_ids import is_valid_isrc, normalize_external_id

IGNORE_VERSIONS = (
    "explicit",  # explicit is matched separately
    "music from and inspired by the motion picture",
    "original soundtrack",
    "hi-res",  # quality is handled separately
)

_VERSION_IGNORE_WORDS = {
    "album",
    "at",
    "edition",
    "variant",
    "versie",
    "version",
    "versione",
}
_VERSION_WORD_ALIASES = {
    "remastered": "remaster",
}
# phrases stripped from a version before tokenizing: they may contain punctuation
# ("hi-res") that the tokenizer would otherwise split into meaningful-looking tokens
_IGNORE_VERSION_PATTERNS = tuple(
    re.compile(rf"\b{re.escape(phrase)}\b", re.IGNORECASE) for phrase in IGNORE_VERSIONS
)

# version tokens that signal a fundamentally different recording (not just packaging),
# so they must never be treated as an ambiguous/mergeable edition difference
_RECORDING_CONFLICT_VERSION_TOKENS = {
    "acoustic",
    "cover",
    "demo",
    "instrumental",
    "karaoke",
    "live",
    "remix",
    "session",
}

# trailing " - EP" / " - Single" retail suffix (any dash style), as appended by Apple Music
_ALBUM_SUFFIX_PATTERN = re.compile(r"\s+[-\u2013\u2014]\s+(?:EP|Single)\s*$", re.IGNORECASE)

# duration tolerances (seconds) for track comparisons: an external-id corroborated
# match allows more duration drift than a bare title/version fallback
_ISRC_DURATION_TOLERANCE = 8
_FALLBACK_DURATION_TOLERANCE = 2


class AlbumMatchEvidence(Enum):
    """Confidence level for an album identity comparison."""

    MATCH = "match"
    NO_MATCH = "no_match"
    INSUFFICIENT = "insufficient"


def compare_media_item(
    base_item: MediaItemType | ItemMapping,
    compare_item: MediaItemType | ItemMapping,
    strict: bool = True,
) -> bool | None:
    """Compare two media items and return True if they match."""
    if base_item.media_type == MediaType.ARTIST and compare_item.media_type == MediaType.ARTIST:
        assert isinstance(base_item, Artist | ItemMapping)  # for type checking
        assert isinstance(compare_item, Artist | ItemMapping)  # for type checking
        return compare_artist(base_item, compare_item, strict)
    if base_item.media_type == MediaType.ALBUM and compare_item.media_type == MediaType.ALBUM:
        assert isinstance(base_item, Album | ItemMapping)  # for type checking
        assert isinstance(compare_item, Album | ItemMapping)  # for type checking
        return compare_album(base_item, compare_item, strict)
    if base_item.media_type == MediaType.TRACK and compare_item.media_type == MediaType.TRACK:
        assert isinstance(base_item, Track)  # for type checking
        assert isinstance(compare_item, Track)  # for type checking
        return compare_track(base_item, compare_item, strict)
    if base_item.media_type == MediaType.PLAYLIST and compare_item.media_type == MediaType.PLAYLIST:
        assert isinstance(base_item, Playlist | ItemMapping)  # for type checking
        assert isinstance(compare_item, Playlist | ItemMapping)  # for type checking
        return compare_playlist(base_item, compare_item, strict)
    if base_item.media_type == MediaType.RADIO and compare_item.media_type == MediaType.RADIO:
        assert isinstance(base_item, Radio | ItemMapping)  # for type checking
        assert isinstance(compare_item, Radio | ItemMapping)  # for type checking
        return compare_radio(base_item, compare_item, strict)
    if (
        base_item.media_type == MediaType.AUDIOBOOK
        and compare_item.media_type == MediaType.AUDIOBOOK
    ):
        assert isinstance(base_item, Audiobook | ItemMapping)  # for type checking
        assert isinstance(compare_item, Audiobook | ItemMapping)  # for type checking
        return compare_audiobook(base_item, compare_item, strict)
    if base_item.media_type == MediaType.PODCAST and compare_item.media_type == MediaType.PODCAST:
        assert isinstance(base_item, Podcast | ItemMapping)  # for type checking
        assert isinstance(compare_item, Podcast | ItemMapping)  # for type checking
        return compare_podcast(base_item, compare_item, strict)
    assert isinstance(base_item, ItemMapping)  # for type checking
    assert isinstance(compare_item, ItemMapping)  # for type checking
    return compare_item_mapping(base_item, compare_item, strict)


def compare_artist(
    base_item: Artist | ItemMapping,
    compare_item: Artist | ItemMapping,
    strict: bool = True,
) -> bool | None:
    """Compare two artist items and return True if they match."""
    # return early on exact item_id match
    if compare_item_ids(base_item, compare_item):
        return True
    # return early on (un)matched external id
    for ext_id in (ExternalID.MB_ARTIST, ExternalID.DISCOGS, ExternalID.TADB):
        external_id_match = compare_external_ids(
            base_item.external_ids, compare_item.external_ids, ext_id
        )
        if external_id_match is not None:
            return external_id_match
    # return early if artist_types don't match
    if (
        isinstance(base_item, Artist)
        and isinstance(compare_item, Artist)
        and base_item.artist_type != compare_item.artist_type
    ):
        return False
    # finally comparing on (exact) name match
    return compare_strings(base_item.name, compare_item.name, strict=strict)


def compare_album(
    base_item: Album | ItemMapping,
    compare_item: Album | ItemMapping,
    strict: bool = True,
) -> bool | None:
    """Compare two album items and return True if they match."""
    return compare_album_evidence(base_item, compare_item, strict) == AlbumMatchEvidence.MATCH


def compare_album_evidence(
    base_item: Album | ItemMapping,
    compare_item: Album | ItemMapping,
    strict: bool = True,
    base_tracks: Sequence[Track] | None = None,
    compare_tracks: Sequence[Track] | None = None,
) -> AlbumMatchEvidence:
    """
    Return the match evidence for two album items.

    Unlike `compare_album`, this distinguishes a confident non-match from
    insufficient metadata (e.g. an edition difference that cannot be resolved from
    the album's own fields), so a caller that can fetch tracklists knows when doing
    so may still resolve the comparison. If `base_tracks`/`compare_tracks` are
    supplied, an ordered track fingerprint comparison is used to resolve that
    remaining ambiguity, and a conflicting fingerprint overrides an otherwise
    nominally-matching album (e.g. identical title/version/year but a different
    number of tracks).

    :param base_tracks: Ordered tracklist for base_item, if already available to the caller.
    :param compare_tracks: Ordered tracklist for compare_item, if already available.
    """
    # return early on exact item_id match
    if compare_item_ids(base_item, compare_item):
        return AlbumMatchEvidence.MATCH

    # return early on (un)matched authoritative external id
    for ext_id in (
        ExternalID.MB_ALBUM,
        ExternalID.DISCOGS,
        ExternalID.TADB,
    ):
        external_id_match = compare_external_ids(
            base_item.external_ids, compare_item.external_ids, ext_id
        )
        if external_id_match is not None:
            return AlbumMatchEvidence.MATCH if external_id_match else AlbumMatchEvidence.NO_MATCH

    # barcode/ASIN are shared across pressings and are non-unique corroboration only,
    # so they are never used on their own, only to resolve a year or edition ambiguity below
    secondary_external_id_match = any(
        compare_external_ids(base_item.external_ids, compare_item.external_ids, ext_id) is True
        for ext_id in (ExternalID.ASIN, ExternalID.BARCODE)
    )

    # a real edition conflict (e.g. deluxe vs. live) is decisive, an ambiguous
    # subset/superset wording (e.g. "2022 Remaster" vs "Deluxe 2022 Remaster") is not
    version_evidence = _compare_album_version(base_item.version, compare_item.version)
    if version_evidence == AlbumMatchEvidence.NO_MATCH:
        return AlbumMatchEvidence.NO_MATCH
    # compare name
    if not _compare_album_name(base_item.name, compare_item.name):
        return AlbumMatchEvidence.NO_MATCH

    ambiguous = version_evidence == AlbumMatchEvidence.INSUFFICIENT
    if ambiguous and secondary_external_id_match:
        # a shared barcode/ASIN identifies the same retail product, which resolves an
        # ambiguous edition wording; when the caller supplies tracklists, a conflicting
        # fingerprint still overrides
        ambiguous = False
    if not strict and (isinstance(base_item, ItemMapping) or isinstance(compare_item, ItemMapping)):
        return _finalize_album_evidence(ambiguous, base_tracks, compare_tracks)
    # for strict matching we REQUIRE both items to be a real album object
    assert isinstance(base_item, Album)
    assert isinstance(compare_item, Album)
    # compare year: without corroboration this is provider drift, not proof either way
    if (
        base_item.year
        and compare_item.year
        and base_item.year != compare_item.year
        and not secondary_external_id_match
    ):
        ambiguous = True
    # compare explicitness
    if compare_explicit(base_item.metadata, compare_item.metadata) is False:
        return AlbumMatchEvidence.NO_MATCH
    # compare album artist(s)
    if not compare_artists(base_item.artists, compare_item.artists, not strict):
        return AlbumMatchEvidence.NO_MATCH
    return _finalize_album_evidence(ambiguous, base_tracks, compare_tracks)


def compare_album_track_fingerprint(
    base_tracks: Sequence[Track] | None,
    compare_tracks: Sequence[Track] | None,
) -> AlbumMatchEvidence:
    """
    Compare two album tracklists position-by-position and return match evidence.

    Requires an identical disc/track shape to consider two tracklists the same
    edition; a tracklist that never reports a disc number is treated as insufficient
    (not assumed disc 1) when compared against a genuinely multi-disc tracklist. At
    each position, a shared (normalized) ISRC with a compatible duration is preferred
    as identity evidence; conflicting ISRCs indicate a different recording/remaster.
    Positions without a usable ISRC on either side fall back to a normalized
    title/version match with a tight duration tolerance.

    :param base_tracks: Ordered tracklist for the base album.
    :param compare_tracks: Ordered tracklist for the album being compared.
    """
    if not base_tracks or not compare_tracks:
        return AlbumMatchEvidence.INSUFFICIENT
    base_positions = _track_positions(base_tracks)
    compare_positions = _track_positions(compare_tracks)
    if not base_positions or not compare_positions:
        return AlbumMatchEvidence.INSUFFICIENT
    base_is_multi_disc = any(disc_number > 1 for disc_number, _ in base_positions)
    compare_is_multi_disc = any(disc_number > 1 for disc_number, _ in compare_positions)
    if (base_is_multi_disc and _has_unknown_disc_layout(compare_tracks)) or (
        compare_is_multi_disc and _has_unknown_disc_layout(base_tracks)
    ):
        # one side never reports a disc number while the other is genuinely multi-disc:
        # assuming disc 1 for the unknown side would produce a false shape conflict
        return AlbumMatchEvidence.INSUFFICIENT
    if base_positions.keys() != compare_positions.keys():
        # different disc/track shape (e.g. a bonus disc or missing tracks): different edition
        return AlbumMatchEvidence.NO_MATCH

    evidence = AlbumMatchEvidence.MATCH
    for position, base_track in base_positions.items():
        position_evidence = _compare_track_fingerprint(base_track, compare_positions[position])
        if position_evidence == AlbumMatchEvidence.NO_MATCH:
            return AlbumMatchEvidence.NO_MATCH
        if position_evidence == AlbumMatchEvidence.INSUFFICIENT:
            evidence = AlbumMatchEvidence.INSUFFICIENT
    return evidence


def album_tracks_have_positions(tracks: Sequence[Track] | None) -> bool:
    """
    Return True if a tracklist has a trustworthy, unambiguous disc/track layout.

    A caller choosing a base tracklist for album-track fingerprinting can use this to
    reject a tracklist whose positions cannot be trusted (a missing disc or track number,
    or a duplicate position) and fall back to another source instead.

    :param tracks: Tracklist to inspect.
    """
    if not tracks:
        return False
    # a missing disc or track number is treated as unknown rather than silently assumed,
    # so such a tracklist is not trusted as a shape reference
    if any(not track.disc_number or not track.track_number for track in tracks):
        return False
    return bool(_track_positions(tracks))


def compare_track(
    base_item: Track,
    compare_item: Track,
    strict: bool = True,
    track_albums: list[Album] | None = None,
) -> bool:
    """Compare two track items and return True if they match."""
    # return early on exact item_id match
    if compare_item_ids(base_item, compare_item):
        return True
    # tracks on the same album but different discs are always distinct,
    # even if they share external IDs (e.g. same recording on multiple discs)
    if (
        base_item.album
        and compare_item.album
        and base_item.disc_number
        and compare_item.disc_number
        and base_item.disc_number != compare_item.disc_number
        and compare_album(base_item.album, compare_item.album, False)
    ):
        return False
    # return early on (un)matched primary/unique external id
    for ext_id in (
        ExternalID.MB_RECORDING,
        ExternalID.MB_TRACK,
        ExternalID.ACOUSTID,
    ):
        external_id_match = compare_external_ids(
            base_item.external_ids, compare_item.external_ids, ext_id
        )
        if external_id_match is not None:
            return external_id_match
    # check secondary external id matches
    for ext_id in (
        ExternalID.DISCOGS,
        ExternalID.TADB,
        ExternalID.ISRC,
        ExternalID.ASIN,
    ):
        external_id_match = compare_external_ids(
            base_item.external_ids, compare_item.external_ids, ext_id
        )
        if external_id_match is True:
            # we got a 'soft-match' on a secondary external id (like ISRC)
            # but we do a double check on duration
            if abs(base_item.duration - compare_item.duration) <= _ISRC_DURATION_TOLERANCE:
                return True

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
    # a missing disc number means unknown: assume disc 1 (local files often omit the tag)
    if (
        base_item.album
        and compare_item.album
        and compare_album(base_item.album, compare_item.album, False)
        and base_item.track_number
        and compare_item.track_number
        and (base_item.disc_number or 1) == (compare_item.disc_number or 1)
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
    # require (exact) name match
    if not compare_strings(base_item.name, compare_item.name, strict=strict):
        return False
    # require exact owner match (if not ItemMapping)
    if isinstance(base_item, Playlist) and isinstance(compare_item, Playlist):
        if not compare_strings(base_item.owner, compare_item.owner):
            return False
    # a playlist is always unique - so do a strict compare on item id(s)
    return compare_item_ids(base_item, compare_item)


def compare_radio(
    base_item: Radio | ItemMapping,
    compare_item: Radio | ItemMapping,
    strict: bool = True,
) -> bool | None:
    """Compare two Radio items and return True if they match."""
    # return early on exact item_id match
    if compare_item_ids(base_item, compare_item):
        return True
    # a dynamic station is its provider's own, so a same-named station is a different one
    if _is_dynamic_radio(base_item) or _is_dynamic_radio(compare_item):
        return False
    # compare version
    if not compare_version(base_item.version, compare_item.version):
        return False
    # finally comparing on (exact) name match
    return compare_strings(base_item.name, compare_item.name, strict=strict)


def compare_audiobook(
    base_item: Audiobook | ItemMapping,
    compare_item: Audiobook | ItemMapping,
    strict: bool = True,
) -> bool | None:
    """Compare two Audiobook items and return True if they match."""
    # return early on exact item_id match
    if compare_item_ids(base_item, compare_item):
        return True

    # return early on (un)matched external id
    for ext_id in (
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
    # for strict matching we REQUIRE both items to be a real Audiobook object
    assert isinstance(base_item, Audiobook)
    assert isinstance(compare_item, Audiobook)
    # compare publisher
    if (
        base_item.publisher
        and compare_item.publisher
        and not compare_strings(base_item.publisher, compare_item.publisher, strict=True)
    ):
        return False

    def _audiobook_artist_name(value: str | Artist | ItemMapping) -> str:
        return value.name if isinstance(value, Artist | ItemMapping) else value

    # compare narrator(s) — different narrators indicate different recordings and must not be merged
    if base_item.narrators and compare_item.narrators:
        base_narrators = {
            create_safe_string(_audiobook_artist_name(n)) for n in base_item.narrators
        }
        compare_narrators = {
            create_safe_string(_audiobook_artist_name(n)) for n in compare_item.narrators
        }
        if base_narrators.isdisjoint(compare_narrators):
            return False
    # compare author(s)
    for author in base_item.authors:
        author_safe = create_safe_string(_audiobook_artist_name(author))
        if author_safe in [
            create_safe_string(_audiobook_artist_name(x)) for x in compare_item.authors
        ]:
            return True
    return False


def compare_podcast(
    base_item: Podcast | ItemMapping,
    compare_item: Podcast | ItemMapping,
    strict: bool = True,
) -> bool | None:
    """Compare two Podcast items and return True if they match."""
    # return early on exact item_id match
    if compare_item_ids(base_item, compare_item):
        return True

    # return early on (un)matched external id
    for ext_id in (
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
    # for strict matching we REQUIRE both items to be a real Podcast object
    assert isinstance(base_item, Podcast)
    assert isinstance(compare_item, Podcast)
    # compare publisher
    return not (
        base_item.publisher
        and compare_item.publisher
        and not compare_strings(base_item.publisher, compare_item.publisher, strict=True)
    )


def compare_item_mapping(
    base_item: ItemMapping,
    compare_item: ItemMapping,
    strict: bool = True,
) -> bool | None:
    """Compare two ItemMapping items and return True if they match."""
    # return early on exact item_id match
    if compare_item_ids(base_item, compare_item):
        return True
    # return early on (un)matched external id
    # check all ExternalID, as ItemMapping is a minimized obj for all MediaItems
    for ext_id in ExternalID:
        external_id_match = compare_external_ids(
            base_item.external_ids, compare_item.external_ids, ext_id
        )
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
        assert isinstance(base_item, MediaItem)  # for type checking
        for prov_l in base_item.provider_mappings:
            if (
                prov_l.provider_instance == compare_item.provider
                and prov_l.item_id == compare_item.item_id
            ):
                return True

    if compare_prov_ids is not None:
        assert isinstance(compare_item, MediaItem)  # for type checking
        for prov_r in compare_item.provider_mappings:
            if (
                prov_r.provider_instance == base_item.provider
                and prov_r.item_id == base_item.item_id
            ):
                return True

    if base_prov_ids is not None and compare_prov_ids is not None:
        assert isinstance(base_item, MediaItem)  # for type checking
        assert isinstance(compare_item, MediaItem)  # for type checking
        for prov_l in base_item.provider_mappings:
            for prov_r in compare_item.provider_mappings:
                if prov_l.provider_domain != prov_r.provider_domain:
                    continue
                if (
                    prov_l.is_unique or prov_r.is_unique
                ) and prov_l.provider_instance != prov_r.provider_instance:
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
    base_ids = {
        normalize_external_id(external_id_type, value)
        for current_type, value in external_ids_base
        if current_type == external_id_type
    }
    if not base_ids:
        # return early if the requested external id type is not present in the base set
        return None
    compare_ids = {
        normalize_external_id(external_id_type, value)
        for current_type, value in external_ids_compare
        if current_type == external_id_type
    }
    if not compare_ids:
        # return early if the requested external id type is not present in the compare set
        return None
    if base_ids.intersection(compare_ids):
        return True
    if external_id_type.is_unique:
        return False
    return None


def loose_compare_strings(base: str, alt: str) -> bool:
    """Compare strings and return True even on partial match."""
    # this is used to display 'versions' of the same track/album
    # where we account for other spelling or some additional wording in the title
    if len(base) <= 3 or len(alt) <= 3:
        return compare_strings(base, alt, True)
    word_count = len(base.strip().split(" "))
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
        # fall back to the same normalization the (search_name) candidate lookup uses,
        # so an item that selection surfaces is never rejected here on formatting alone
        return str1_lower == str2_lower or _compare_safe_strings(str1, str2)
    # return early if total length mismatch
    if abs(len(str1) - len(str2)) > 4:
        return False
    # handle '&' vs 'And'
    if " & " in str1_lower and " and " in str2_lower:
        str2 = str2_lower.replace(" and ", " & ")
    elif " and " in str1_lower and " & " in str2:
        str2 = str2_lower.replace(" & ", " and ")
    if create_safe_string(str1) == create_safe_string(str2):
        return True
    # last resort: use difflib to compare strings
    required_accuracy = 0.9 if (len(str1) + len(str2)) > 18 else 0.8
    return SequenceMatcher(a=str1_lower, b=str2_lower).ratio() > required_accuracy


def compare_version(base_version: str, compare_version: str) -> bool:
    """Compare version string."""
    return _normalize_version_tokens(base_version) == _normalize_version_tokens(compare_version)


def compare_explicit(base: MediaItemMetadata, compare: MediaItemMetadata) -> bool | None:
    """Compare if explicit is same in metadata."""
    if base.explicit is not None and compare.explicit is not None:
        # explicitness info is not always present in metadata
        # only strict compare them if both have the info set
        return base.explicit == compare.explicit
    return None


@lru_cache(maxsize=1024)
def _normalize_version_tokens(value: str) -> tuple[str, ...]:
    """Return meaningful, deduplicated version tokens in stable order."""
    if not value:
        return ()
    stripped_value = value.casefold()
    for pattern in _IGNORE_VERSION_PATTERNS:
        stripped_value = pattern.sub(" ", stripped_value)
    tokens = (
        _VERSION_WORD_ALIASES.get(token, token) for token in re.findall(r"[^\W_]+", stripped_value)
    )
    return tuple(sorted({token for token in tokens if token not in _VERSION_IGNORE_WORDS}))


def _is_dynamic_radio(item: Radio | ItemMapping) -> bool:
    """Return True if the item is a dynamic radio station."""
    return isinstance(item, Radio) and item.is_dynamic


def _compare_album_version(base_version: str, compare_version: str) -> AlbumMatchEvidence:
    """Return match evidence for an album version/edition comparison."""
    base_tokens = set(_normalize_version_tokens(base_version))
    compare_tokens = set(_normalize_version_tokens(compare_version))
    if base_tokens == compare_tokens:
        return AlbumMatchEvidence.MATCH
    # a recording-changing qualifier (live, karaoke, remix, ...) makes an otherwise
    # unequal pair of editions unsafe to merge, wherever it appears in either wording,
    # not only when it is the token that happens to differ between the two, and even
    # when the other side omits version metadata entirely
    if (base_tokens | compare_tokens) & _RECORDING_CONFLICT_VERSION_TOKENS:
        return AlbumMatchEvidence.NO_MATCH
    if not base_tokens or not compare_tokens:
        # a provider commonly omits edition metadata entirely (e.g. a remaster tagged
        # without a version string), so a blank version next to a real one is
        # undecided rather than a proven conflict: let a tracklist resolve it
        return AlbumMatchEvidence.INSUFFICIENT
    if base_tokens < compare_tokens or compare_tokens < base_tokens:
        # one version's wording is a strict subset of the other's (e.g. "2022 Remaster"
        # vs. "Deluxe 2022 Remaster"): an ambiguous packaging difference a tracklist can resolve
        return AlbumMatchEvidence.INSUFFICIENT
    return AlbumMatchEvidence.NO_MATCH


def _compare_album_name(base_name: str, compare_name: str) -> bool:
    """Return True if two album titles are the same identity, ignoring formatting drift."""
    # the retail suffix carries no identity information: Apple Music appends it to
    # EP/single titles while already setting album_type
    return compare_strings(
        _ALBUM_SUFFIX_PATTERN.sub("", base_name),
        _ALBUM_SUFFIX_PATTERN.sub("", compare_name),
    )


def _compare_safe_strings(base: str, compare: str) -> bool:
    """Return True if two names are equal ignoring case, diacritics, punctuation and spacing."""
    base_safe = _normalize_name(base)
    compare_safe = _normalize_name(compare)
    if base_safe and compare_safe:
        return base_safe == compare_safe
    if base_safe or compare_safe:
        return False
    # both names collapse to nothing under normalization (e.g. the band "!!!"): fall back
    # to a raw comparison with all whitespace removed, so spacing drift ("( )" vs "()")
    # still matches while unrelated symbol-only names don't
    return "".join(base.split()).casefold() == "".join(compare.split()).casefold()


@lru_cache(maxsize=1024)
def _normalize_name(name: str) -> str:
    """Return a punctuation/diacritic/whitespace-insensitive name for identity checks."""
    return create_safe_string(name, True, True)


def _track_positions(tracks: Sequence[Track]) -> dict[tuple[int, int], Track]:
    """Return tracks keyed by their (disc_number, track_number) position."""
    if len({bool(track.disc_number) for track in tracks}) > 1:
        # some tracks report a disc number and others don't: the shape can't be trusted
        return {}
    positions: dict[tuple[int, int], Track] = {}
    for track in tracks:
        if not track.track_number:
            return {}
        key = (track.disc_number or 1, track.track_number)
        if key in positions:
            # duplicate position: the tracklist shape cannot be trusted
            return {}
        positions[key] = track
    return positions


def _has_unknown_disc_layout(tracks: Sequence[Track]) -> bool:
    """Return True if a tracklist reports no disc number at all (an assumed single disc)."""
    return all(not track.disc_number for track in tracks)


def _compare_track_fingerprint(base_track: Track, compare_track: Track) -> AlbumMatchEvidence:
    """Return match evidence for a single album-track position."""
    base_isrcs = _track_isrcs(base_track)
    compare_isrcs = _track_isrcs(compare_track)
    if base_isrcs and compare_isrcs:
        if base_isrcs.isdisjoint(compare_isrcs):
            # both sides tagged an ISRC and they disagree: a different recording/remaster
            return AlbumMatchEvidence.NO_MATCH
        if not base_track.duration or not compare_track.duration:
            return AlbumMatchEvidence.INSUFFICIENT
        if _duration_close(base_track.duration, compare_track.duration, _ISRC_DURATION_TOLERANCE):
            return AlbumMatchEvidence.MATCH
        return AlbumMatchEvidence.INSUFFICIENT

    # no usable ISRC on (at least) one side: fall back to title/version + duration
    if not base_track.name or not compare_track.name:
        return AlbumMatchEvidence.INSUFFICIENT
    if not compare_strings(base_track.name, compare_track.name, strict=True):
        return AlbumMatchEvidence.NO_MATCH
    if not compare_version(base_track.version, compare_track.version):
        return AlbumMatchEvidence.NO_MATCH
    if not base_track.duration or not compare_track.duration:
        return AlbumMatchEvidence.INSUFFICIENT
    if _duration_close(base_track.duration, compare_track.duration, _FALLBACK_DURATION_TOLERANCE):
        return AlbumMatchEvidence.MATCH
    return AlbumMatchEvidence.NO_MATCH


def _track_isrcs(track: Track) -> set[str]:
    """Return the structurally valid, normalized ISRCs tagged on a track."""
    return {
        normalize_external_id(ExternalID.ISRC, value)
        for current_type, value in track.external_ids
        if current_type == ExternalID.ISRC and is_valid_isrc(value)
    }


def _duration_close(base_duration: int, compare_duration: int, tolerance: int) -> bool:
    """Return True if two track durations (in seconds) are within tolerance."""
    return abs(base_duration - compare_duration) <= tolerance


def _finalize_album_evidence(
    ambiguous: bool,
    base_tracks: Sequence[Track] | None,
    compare_tracks: Sequence[Track] | None,
) -> AlbumMatchEvidence:
    """Combine an album's metadata ambiguity with an optional track fingerprint override."""
    fingerprint_evidence = compare_album_track_fingerprint(base_tracks, compare_tracks)
    if fingerprint_evidence == AlbumMatchEvidence.NO_MATCH:
        # a conflicting tracklist is decisive even if the album's own metadata looked fine
        return AlbumMatchEvidence.NO_MATCH
    if not ambiguous:
        return AlbumMatchEvidence.MATCH
    return fingerprint_evidence
