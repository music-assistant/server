"""Tests for external media identifier helpers."""

from music_assistant_models.enums import ExternalID

from music_assistant.helpers.external_ids import (
    external_id_lookup_values,
    normalize_external_id,
    normalize_external_ids,
)


def test_normalize_gtin_variants() -> None:
    """Equivalent UPC/EAN/GTIN representations normalize to GTIN-14."""
    canonical = "00724354283857"

    assert normalize_external_id(ExternalID.BARCODE, "724354283857") == canonical
    assert normalize_external_id(ExternalID.BARCODE, "0724354283857") == canonical
    assert normalize_external_id(ExternalID.BARCODE, canonical) == canonical
    assert normalize_external_id(ExternalID.BARCODE, "000724354283857") == canonical


def test_invalid_gtin_is_preserved() -> None:
    """Invalid provider barcode data remains available for exact matching."""
    assert normalize_external_id(ExternalID.BARCODE, "0724354283858") == "0724354283858"
    assert normalize_external_id(ExternalID.BARCODE, "catalog-123") == "catalog-123"


def test_legacy_gtin_lookup_values() -> None:
    """A canonical barcode lookup includes legacy zero-padded storage forms."""
    values = external_id_lookup_values(ExternalID.BARCODE, "0724354283857")

    assert "724354283857" in values
    assert "0724354283857" in values
    assert "00724354283857" in values
    assert "000724354283857" in values


def test_normalize_isrc_and_mbid() -> None:
    """Formatted ISRCs and MusicBrainz UUIDs use canonical representations."""
    mbid = "B1A9C0E9-D987-4042-AE91-78D6A3267D69"

    assert normalize_external_id(ExternalID.ISRC, "us-rc1-76-07839") == "USRC17607839"
    assert normalize_external_id(ExternalID.ISRC, "invalid-isrc") == "invalid-isrc"
    assert normalize_external_id(ExternalID.MB_RECORDING, f"{{{mbid}}}") == mbid.lower()


def test_normalize_external_ids_deduplicates_values() -> None:
    """Equivalent identifier forms collapse to one canonical pair."""
    result = normalize_external_ids(
        {
            (ExternalID.BARCODE, "0724354283857"),
            (ExternalID.BARCODE, "000724354283857"),
        }
    )

    assert result == {(ExternalID.BARCODE, "00724354283857")}
