"""Tests for PIRA.AT catalog normalization."""

from __future__ import annotations

import pytest

from music_assistant.providers.pira_at.catalog import parse_catalog


def test_parse_catalog_normalizes_playable_stations() -> None:
    """Playable stations receive stable, source-qualified IDs."""
    catalog = parse_catalog(
        {
            "gzc": {
                "42": {
                    "id": "42",
                    "bron": "GZC",
                    "station": "Pira FM",
                    "mp3link": "https://radio.example/stream",
                    "freq": "97.2",
                    "locatie": "Overijssel",
                    "luisteraars": "12",
                    "nowPlaying": "Artist - Song",
                    "timestamp": "1700000000",
                }
            }
        }
    )

    station = catalog["GZC:42"]
    assert station.name == "Pira FM"
    assert station.stream_url == "https://radio.example/stream"
    assert station.region == "Overijssel"
    assert station.listeners == 12
    assert station.timestamp == 1700000000


def test_parse_catalog_skips_unplayable_entries() -> None:
    """Entries without a public HTTP stream do not appear in the catalog."""
    catalog = parse_catalog(
        {
            "pfm": {
                "invalid": {"station": "Offline", "mp3link": "ftp://radio.example/stream"},
                "valid": {"station": "Online", "mp3link": "http://radio.example/stream"},
            }
        }
    )

    assert list(catalog) == ["pfm:valid"]


def test_parse_catalog_rejects_invalid_root_payload() -> None:
    """The API root must be a JSON object."""
    with pytest.raises(TypeError, match="not a JSON object"):
        parse_catalog([])


def test_parse_catalog_allows_catalog_without_playable_stations() -> None:
    """A temporarily empty source remains available as an empty catalog."""
    assert parse_catalog({"epc": {"offline": {"station": "Offline"}}}) == {}


def test_parse_catalog_uses_english_fallback_labels() -> None:
    """Incomplete station details have English display fallbacks."""
    catalog = parse_catalog({"epc": {"1": {"id": "1", "mp3link": "https://radio.example/stream"}}})

    station = catalog["epc:1"]
    assert station.name == "Unknown station"
    assert station.region == "Unknown"
