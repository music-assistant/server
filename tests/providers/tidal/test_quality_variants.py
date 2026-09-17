"""Test collapsing Tidal's per-quality-tier duplicate resources."""

import json
import pathlib
from typing import Any

from music_assistant.providers.tidal.jsonapi import JsonApiDocument
from music_assistant.providers.tidal.quality_variants import (
    album_variant_key,
    collapse_quality_variants,
    track_variant_key,
)

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures" / "v2"


def _load_doc(name: str) -> JsonApiDocument:
    with open(FIXTURES_DIR / name) as f:
        return JsonApiDocument(json.load(f))


def _album_doc(resources: list[dict[str, Any]]) -> JsonApiDocument:
    return JsonApiDocument(
        {"data": [{"type": "albums", "id": r["id"]} for r in resources], "included": resources}
    )


def _album_resource(
    resource_id: str, *, title: str = "Some Album", media_tags: list[str] | None = None
) -> dict[str, Any]:
    return {
        "type": "albums",
        "id": resource_id,
        "attributes": {
            "title": title,
            "version": None,
            "releaseDate": "2024-01-01",
            "numberOfItems": 10,
            "explicit": False,
            "mediaTags": media_tags or [],
        },
        "relationships": {"artists": {"data": [{"type": "artists", "id": "1"}]}},
    }


def test_cheat_code_collapses_to_highest_ranked_tie_winner() -> None:
    """Test the four "Cheat Code" quality variants collapse to the first HIRES_LOSSLESS one."""
    doc = _load_doc("artist_albums.json")

    result = collapse_quality_variants([doc], album_variant_key)
    cheat_code = [r for _, r in result if r["attributes"]["title"] == "Cheat Code"]

    assert len(cheat_code) == 1
    assert cheat_code[0]["id"] == "355219323"


def test_atmos_only_album_is_kept() -> None:
    """Test an album with no other quality variant is kept as-is."""
    resource = _album_resource("900001", title="Atmos Only Album", media_tags=["DOLBY_ATMOS"])
    doc = _album_doc([resource])

    result = collapse_quality_variants([doc], album_variant_key)

    assert len(result) == 1
    assert result[0][1]["id"] == "900001"


def test_variants_split_across_pages_collapse_to_the_winning_page() -> None:
    """Test quality variants of the same album on different pages still collapse."""
    lossless = _album_resource("910001", title="Split Album", media_tags=["LOSSLESS"])
    hi_res = _album_resource("910002", title="Split Album", media_tags=["HIRES_LOSSLESS"])
    page1 = _album_doc([lossless])
    page2 = _album_doc([hi_res])

    result = collapse_quality_variants([page1, page2], album_variant_key)

    assert len(result) == 1
    winning_doc, winning_resource = result[0]
    assert winning_resource["id"] == "910002"
    assert winning_doc is page2


def test_artist_toptracks_isrc_collapse() -> None:
    """Test tracks collapse by isrc, keeping distinct-isrc same-title tracks apart."""
    doc = _load_doc("artist_toptracks.json")

    result = collapse_quality_variants([doc], track_variant_key)
    ids = {r["id"] for _, r in result}

    assert len(result) == 19
    # Same isrc (same recording, two title variants): only the first-seen survives.
    assert "242149003" in ids
    assert "242744824" not in ids
    # Same title, different isrc (different recordings): both are kept.
    assert {"104340964", "13536359"} <= ids
    assert {"46675793", "58503080"} <= ids


def test_track_without_isrc_falls_back_to_metadata_key() -> None:
    """Test a track without an isrc groups by title, version, duration and artists."""
    common_attrs = {
        "title": "Untitled Demo",
        "version": None,
        "duration": "PT3M0S",
        "isrc": None,
    }
    low = {
        "type": "tracks",
        "id": "920001",
        "attributes": {**common_attrs, "mediaTags": ["LOSSLESS"]},
        "relationships": {"artists": {"data": [{"type": "artists", "id": "1"}]}},
    }
    high = {
        "type": "tracks",
        "id": "920002",
        "attributes": {**common_attrs, "mediaTags": ["HIRES_LOSSLESS"]},
        "relationships": {"artists": {"data": [{"type": "artists", "id": "1"}]}},
    }
    doc = JsonApiDocument(
        {
            "data": [{"type": "tracks", "id": "920001"}, {"type": "tracks", "id": "920002"}],
            "included": [low, high],
        }
    )

    result = collapse_quality_variants([doc], track_variant_key)

    assert len(result) == 1
    assert result[0][1]["id"] == "920002"
