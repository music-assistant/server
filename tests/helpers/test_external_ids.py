"""Tests for external media identifier helpers."""

import pytest
from music_assistant_models.enums import ExternalID
from music_assistant_models.errors import InvalidProviderID

from music_assistant.helpers.external_ids import (
    barcode_to_upc,
    external_id_lookup_values,
    external_id_lookup_values_untyped,
    external_item_id,
    is_valid_barcode,
    is_valid_mbid,
    normalize_external_id,
    normalize_external_ids,
    parse_external_item_id,
)
from music_assistant.helpers.uri import parse_uri


def test_normalize_gtin_variants() -> None:
    """Equivalent UPC/EAN/GTIN representations normalize to GTIN-14."""
    canonical = "00724354283857"

    assert normalize_external_id(ExternalID.BARCODE, "724354283857") == canonical
    assert normalize_external_id(ExternalID.BARCODE, "0724354283857") == canonical
    assert normalize_external_id(ExternalID.BARCODE, canonical) == canonical
    assert normalize_external_id(ExternalID.BARCODE, "000724354283857") == canonical


def test_invalid_gtin_is_preserved() -> None:
    """Invalid provider barcode data remains available for exact matching."""
    assert normalize_external_id(ExternalID.BARCODE, "602445790392") == "602445790392"
    assert normalize_external_id(ExternalID.BARCODE, "catalog-123") == "catalog-123"


def test_truncated_gtin14_recovers_check_digit() -> None:
    """A GTIN-14 with its check digit chopped off (Qobuz) recovers the full value."""
    assert normalize_external_id(ExternalID.BARCODE, "0060252758365") == "00602527583655"
    # a structurally valid EAN-13 keeps taking the regular normalization path
    assert normalize_external_id(ExternalID.BARCODE, "0724354283857") == "00724354283857"


def test_legacy_gtin_lookup_values() -> None:
    """A canonical barcode lookup includes legacy zero-padded storage forms."""
    values = external_id_lookup_values(ExternalID.BARCODE, "0724354283857")

    assert "724354283857" in values
    assert "0724354283857" in values
    assert "00724354283857" in values
    assert "000724354283857" in values


def test_untyped_lookup_values_and_itunes_upc() -> None:
    """Untyped lookups normalize formatted IDs and canonical GTINs convert to UPC."""
    assert "USRC17607839" in external_id_lookup_values_untyped("US-RC1-76-07839")
    assert barcode_to_upc("00724354283857") == "724354283857"


def test_normalize_isrc_and_mbid() -> None:
    """Formatted ISRCs and MusicBrainz UUIDs use canonical representations."""
    mbid = "B1A9C0E9-D987-4042-AE91-78D6A3267D69"

    assert normalize_external_id(ExternalID.ISRC, "us-rc1-76-07839") == "USRC17607839"
    assert normalize_external_id(ExternalID.ISRC, "invalid-isrc") == "invalid-isrc"
    assert normalize_external_id(ExternalID.MB_RECORDING, f"{{{mbid}}}") == mbid.lower()


def test_mbid_lookup_values_include_braced_form() -> None:
    """A canonical MBID lookup also covers the legacy braced storage form."""
    mbid = "b1a9c0e9-d987-4042-ae91-78d6a3267d69"
    values = external_id_lookup_values(ExternalID.MB_RECORDING, mbid)

    assert mbid in values
    assert f"{{{mbid}}}" in values


def test_normalize_external_ids_deduplicates_values() -> None:
    """Equivalent identifier forms collapse to one canonical pair."""
    result = normalize_external_ids(
        {
            (ExternalID.BARCODE, "0724354283857"),
            (ExternalID.BARCODE, "000724354283857"),
        }
    )

    assert result == {(ExternalID.BARCODE, "00724354283857")}


def test_is_valid_barcode() -> None:
    """A structurally valid GTIN is recognized regardless of UPC/EAN notation."""
    assert is_valid_barcode("888072439412") is True
    assert is_valid_barcode("0888072439412") is True
    assert is_valid_barcode("00888072439412") is True
    # bad check digit, wrong length or non-numeric data is not a usable barcode
    assert is_valid_barcode("602445790392") is False
    assert is_valid_barcode("12345") is False
    assert is_valid_barcode("catalog-123") is False


def test_is_valid_mbid() -> None:
    """A MusicBrainz id is a UUID, in whatever notation it is written."""
    assert is_valid_mbid("b1a9c0e9-d987-4042-ae91-78d6a3267d69") is True
    assert is_valid_mbid("{B1A9C0E9-D987-4042-AE91-78D6A3267D69}") is True
    assert is_valid_mbid("b1a9c0e9-d987-4042-ae91") is False
    assert is_valid_mbid("../artist/b1a9c0e9-d987-4042-ae91-78d6a3267d69") is False
    assert is_valid_mbid("") is False


@pytest.mark.parametrize(
    ("external_id_type", "value", "item_id"),
    [
        (
            ExternalID.MB_RECORDING,
            "{B1A9C0E9-D987-4042-AE91-78D6A3267D69}",
            "musicbrainz_recordingid:b1a9c0e9-d987-4042-ae91-78d6a3267d69",
        ),
        (
            ExternalID.MB_RELEASEGROUP,
            "b1392450-e666-3926-a536-22c65f834433",
            "musicbrainz_releasegroupid:b1392450-e666-3926-a536-22c65f834433",
        ),
        (ExternalID.ISRC, "us-rc1-76-07839", "isrc:USRC17607839"),
        (ExternalID.BARCODE, "724354283857", "barcode:00724354283857"),
        (ExternalID.DISCOGS, "1157205", "discogs:1157205"),
    ],
)
def test_external_item_id_round_trip(
    external_id_type: ExternalID, value: str, item_id: str
) -> None:
    """An external id becomes one canonical item id, which splits into the same id again."""
    assert external_item_id(external_id_type, value) == item_id
    canonical_value = item_id.split(":", 1)[1]
    assert parse_external_item_id(item_id) == (external_id_type, canonical_value)
    # whatever notation the id is written in, it names the same item
    assert parse_external_item_id(f"{external_id_type.value}:{value}") == (
        external_id_type,
        canonical_value,
    )


@pytest.mark.parametrize(
    "item_id",
    [
        "b1a9c0e9-d987-4042-ae91-78d6a3267d69",
        "musicbrainz_recordingid:",
        ":b1a9c0e9-d987-4042-ae91-78d6a3267d69",
        "spotify:b1a9c0e9-d987-4042-ae91-78d6a3267d69",
        "unknown:b1a9c0e9-d987-4042-ae91-78d6a3267d69",
        "musicbrainz_recordingid:not-a-uuid",
        "musicbrainz_albumid:../artist/b1a9c0e9-d987-4042-ae91-78d6a3267d69",
        "isrc:not-an-isrc",
        "barcode:602445790392",
    ],
)
def test_parse_external_item_id_refuses_a_malformed_id(item_id: str) -> None:
    """An item id without a known id type, or with an id that can not be one, is refused."""
    with pytest.raises(InvalidProviderID):
        parse_external_item_id(item_id)


async def test_external_item_id_fits_in_a_uri() -> None:
    """The uri of an external item parses back into its provider and item id."""
    item_id = external_item_id(ExternalID.MB_RECORDING, "b1a9c0e9-d987-4042-ae91-78d6a3267d69")

    media_type, provider, parsed_item_id = await parse_uri(f"external://track/{item_id}")

    assert (media_type.value, provider, parsed_item_id) == ("track", "external", item_id)
