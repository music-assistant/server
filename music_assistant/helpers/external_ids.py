"""Helpers for normalizing and looking up external media identifiers."""

from __future__ import annotations

import re
from collections.abc import Iterable
from uuid import UUID

from music_assistant_models.enums import ExternalID

_GTIN_LENGTHS = (8, 12, 13, 14, 15)
_ISRC_PATTERN = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{7}$")
_MUSICBRAINZ_PREFIX = "musicbrainz_"

_EXTERNAL_ID_PRIORITY = {
    ExternalID.MB_RECORDING: 0,
    ExternalID.MB_TRACK: 1,
    ExternalID.MB_ALBUM: 2,
    ExternalID.MB_ARTIST: 3,
    ExternalID.DISCOGS: 4,
    ExternalID.TADB: 5,
    ExternalID.ACOUSTID: 6,
    ExternalID.MB_RELEASEGROUP: 7,
    ExternalID.ASIN: 20,
    ExternalID.BARCODE: 21,
    ExternalID.ISRC: 22,
}


def normalize_external_id(external_id_type: ExternalID, value: str) -> str:
    """
    Return the canonical representation of an external media identifier.

    Invalid identifiers are returned stripped but otherwise unchanged so provider data
    remains available for exact matching.

    :param external_id_type: Type of the external identifier.
    :param value: Identifier value supplied by a provider or file tag.
    """
    value = value.strip()
    if external_id_type == ExternalID.BARCODE:
        return _normalize_barcode(value)
    if external_id_type == ExternalID.ISRC:
        return _normalize_isrc(value)
    if external_id_type.value.startswith(_MUSICBRAINZ_PREFIX):
        return _normalize_mbid(value)
    return value


def normalize_external_ids(
    external_ids: Iterable[tuple[ExternalID, str]],
) -> set[tuple[ExternalID, str]]:
    """
    Return external identifiers with canonical values.

    :param external_ids: External identifier type/value pairs.
    """
    return {
        (external_id_type, normalize_external_id(external_id_type, value))
        for external_id_type, value in external_ids
        if value.strip()
    }


def external_id_lookup_values(external_id_type: ExternalID, value: str) -> tuple[str, ...]:
    """
    Return indexed lookup values compatible with current and legacy storage formats.

    :param external_id_type: Type of the external identifier.
    :param value: Identifier value to look up.
    """
    raw_value = value.strip()
    normalized_value = normalize_external_id(external_id_type, raw_value)
    values = {raw_value, normalized_value}

    if external_id_type == ExternalID.BARCODE and _is_valid_gtin(normalized_value):
        payload = normalized_value.lstrip("0")
        for length in _GTIN_LENGTHS:
            if len(payload) <= length:
                values.add(payload.zfill(length))
    elif external_id_type == ExternalID.ISRC and _ISRC_PATTERN.fullmatch(normalized_value):
        values.add(
            f"{normalized_value[:2]}-{normalized_value[2:5]}-"
            f"{normalized_value[5:7]}-{normalized_value[7:]}"
        )
    elif external_id_type.value.startswith(_MUSICBRAINZ_PREFIX) and normalized_value != raw_value:
        values.add(f"{{{normalized_value}}}")

    return tuple(sorted(values))


def is_valid_isrc(value: str) -> bool:
    """
    Return whether a value is a structurally valid (canonical) ISRC.

    An invalid ISRC is still preserved as-is for storage/exact-match lookups, but
    should be treated as absent by callers that rely on the ISRC being a genuine,
    globally unique recording identifier (e.g. album/track fingerprint comparisons).

    :param value: Identifier value to validate.
    """
    return bool(_ISRC_PATTERN.fullmatch(normalize_external_id(ExternalID.ISRC, value)))


def external_id_lookup_values_untyped(value: str) -> tuple[str, ...]:
    """
    Return lookup values for an identifier whose type is not known.

    :param value: Identifier value to look up.
    """
    values = {value.strip()}
    for external_id_type in ExternalID:
        values.update(external_id_lookup_values(external_id_type, value))
    return tuple(sorted(values))


def barcode_to_upc(value: str) -> str:
    """
    Return the shortest UPC/EAN representation of a valid barcode.

    :param value: Barcode in a provider or canonical storage format.
    """
    normalized_value = normalize_external_id(ExternalID.BARCODE, value)
    if not _is_valid_gtin(normalized_value):
        return value
    payload = normalized_value.lstrip("0")
    return payload.zfill(12)


def external_id_sort_key(external_id: tuple[ExternalID, str]) -> tuple[int, str, str]:
    """
    Return a deterministic preference key for an external identifier.

    :param external_id: External identifier type/value pair.
    """
    external_id_type, value = external_id
    default_priority = 10 if external_id_type.is_unique else 30
    return (
        _EXTERNAL_ID_PRIORITY.get(external_id_type, default_priority),
        external_id_type.value,
        normalize_external_id(external_id_type, value),
    )


def _normalize_barcode(value: str) -> str:
    """Return a valid UPC/EAN/GTIN as GTIN-14."""
    compact_value = value.replace("-", "").replace(" ", "")
    if len(compact_value) not in _GTIN_LENGTHS or not compact_value.isdigit():
        return value
    payload = compact_value.lstrip("0")
    if not payload or len(payload) > 14:
        return value
    normalized_value = payload.zfill(14)
    return normalized_value if _is_valid_gtin(normalized_value) else value


def _normalize_isrc(value: str) -> str:
    """Return a valid ISRC without separators."""
    normalized_value = value.replace("-", "").replace(" ", "").upper()
    return normalized_value if _ISRC_PATTERN.fullmatch(normalized_value) else value


def _normalize_mbid(value: str) -> str:
    """Return a MusicBrainz identifier as a lowercase UUID."""
    try:
        return str(UUID(value.strip("{}")))
    except ValueError, AttributeError:
        return value


def _is_valid_gtin(value: str) -> bool:
    """Return whether a value is a canonical GTIN-14 with a valid check digit."""
    if len(value) != 14 or not value.isdigit():
        return False
    body = value[:-1]
    checksum = sum(
        int(char) * (3 if index % 2 == 0 else 1) for index, char in enumerate(reversed(body))
    )
    return (10 - checksum % 10) % 10 == int(value[-1])
