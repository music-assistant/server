"""Unit tests for AI Radio storage normalization helpers."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.ai_radio.storage import AIRadioStorageMixin


class DummyStorage(AIRadioStorageMixin):
    """Minimal storage harness for unit testing helper methods."""

    def __init__(self) -> None:
        """Initialize dummy mixin state."""
        self.logger = logging.getLogger(__name__)
        self._sections: dict[str, dict[str, Any]] = {}
        self._stations: dict[str, dict[str, Any]] = {}


def _section(
    section_id: str,
    *,
    name: str | None = None,
    section_type: str = "ai_text",
    prompt: str = "Prompt",
) -> dict[str, Any]:
    """Build a minimal shared section payload."""
    return {
        "id": section_id,
        "name": name or section_id,
        "type": section_type,
        "prompt": prompt,
    }


def _station(section_ids: list[str]) -> dict[str, Any]:
    """Build a minimal valid station payload."""
    return {
        "id": "station_a",
        "name": "Station A",
        "source_playlist_id": "playlist-1",
        "source_playlist_provider": "library",
        "section_ids": section_ids,
        "section_order": [
            {
                "when": "between_songs",
                "flow": [{"MUST": section_ids[0]}],
            }
        ],
    }


def test_normalize_general_uses_known_schema_only() -> None:
    """Normalize general config to known keys and converted value types."""
    storage = DummyStorage()
    raw_general = {
        "timezone": "Europe/Berlin",
        "location": {"city": "Berlin", "country": "DE"},
        "instructions": "Keep it concise and conversational.",
        "weather_provider": "open_meteo",
        "weather_timeout_seconds": "30",
        "foo": "bar",
    }

    normalized = storage._normalize_general(raw_general)

    assert normalized["timezone"] == "Europe/Berlin"
    assert normalized["location"] == {"city": "Berlin", "country": "DE"}
    assert normalized["instructions"] == "Keep it concise and conversational."
    assert normalized["weather_timeout_seconds"] == 30
    assert "foo" not in normalized

    default_keys = set(storage._default_station_template()["general"].keys())
    assert set(normalized.keys()) == default_keys


def test_materialize_sections_reports_missing_and_returns_copies() -> None:
    """Resolve sections and report unknown ids without mutating source map."""
    storage = DummyStorage()
    storage._sections = {
        "Song_Transition": {
            "id": "Song_Transition",
            "name": "Song Transition",
            "type": "ai_text",
            "prompt": "Transition",
        }
    }

    sections, missing = storage._materialize_sections(["Song_Transition", "Unknown_Section"])

    assert missing == ["Unknown_Section"]
    assert sections[0]["id"] == "Song_Transition"
    sections[0]["name"] = "Changed"
    assert storage._sections["Song_Transition"]["name"] == "Song Transition"


def test_refresh_station_sections_rebuilds_embedded_sections() -> None:
    """Rebuild embedded station sections from selected shared ids."""
    storage = DummyStorage()
    storage._sections = {
        "Song_Transition": {
            "id": "Song_Transition",
            "name": "Song Transition",
            "type": "ai_text",
            "prompt": "Transition",
        }
    }
    storage._stations = {
        "station_a": {
            "id": "station_a",
            "section_ids": ["Song_Transition", "Missing_Section"],
            "sections": [],
        }
    }

    storage._refresh_station_sections()

    sections = storage._stations["station_a"]["sections"]
    assert len(sections) == 1
    assert sections[0]["id"] == "Song_Transition"


def test_normalize_section_rejects_invalid_type() -> None:
    """Reject shared sections with unsupported types."""
    storage = DummyStorage()

    with pytest.raises(InvalidDataError, match="invalid type"):
        storage._normalize_section(
            {
                "id": "Bad_Section",
                "name": "Bad Section",
                "type": "unsupported",
                "prompt": "Prompt",
            }
        )


def test_normalize_section_normalizes_invalid_web_search_and_constraints() -> None:
    """Clamp section fields to the supported ai_text schema."""
    storage = DummyStorage()

    normalized = storage._normalize_section(
        {
            "id": "Global_News",
            "name": "Global News",
            "type": "ai_text",
            "prompt": "News prompt",
            "web_search": "sometimes",
            "constraints": {"max_chars": "450"},
        }
    )

    assert normalized["web_search"] == "disabled"
    assert normalized["constraints"] == {"max_chars": 450}


def test_normalize_station_rejects_missing_source_playlist_id() -> None:
    """Reject stations without a source playlist reference."""
    storage = DummyStorage()
    storage._sections = {"Song_Transition": _section("Song_Transition")}
    station = _station(["Song_Transition"])
    station["source_playlist_id"] = ""

    with pytest.raises(InvalidDataError, match="source_playlist_id is required"):
        storage._normalize_station(station)


def test_normalize_station_rejects_unknown_section_reference() -> None:
    """Reject stations that reference shared sections that do not exist."""
    storage = DummyStorage()

    with pytest.raises(InvalidDataError, match="unknown sections: Song_Transition"):
        storage._normalize_station(_station(["Song_Transition"]))


def test_normalize_station_rejects_merge_section_outside_selected_ids() -> None:
    """Reject merge sections that are not part of the station section list."""
    storage = DummyStorage()
    storage._sections = {
        "Song_Transition": _section("Song_Transition"),
        "Between_Songs_Mix": _section(
            "Between_Songs_Mix",
            name="Between Songs Mix",
            section_type="ai_meta",
        ),
    }
    station = _station(["Song_Transition"])
    station["merge_section_id"] = "Between_Songs_Mix"

    with pytest.raises(InvalidDataError, match="must be selected in station section_ids"):
        storage._normalize_station(station)


def test_normalize_station_rejects_non_meta_merge_section() -> None:
    """Reject merge sections that do not point to an ai_meta section."""
    storage = DummyStorage()
    storage._sections = {
        "Song_Transition": _section("Song_Transition"),
        "Weather_Short": _section("Weather_Short", name="Weather Short"),
    }
    station = _station(["Song_Transition", "Weather_Short"])
    station["merge_section_id"] = "Weather_Short"

    with pytest.raises(InvalidDataError, match="must reference an ai_meta section"):
        storage._normalize_station(station)


def test_normalize_station_rejects_non_numeric_optional_chance() -> None:
    """Reject OPTIONAL flow entries with non-numeric chance values."""
    storage = DummyStorage()
    storage._sections = {"Song_Transition": _section("Song_Transition")}
    station = _station(["Song_Transition"])
    station["section_order"] = [
        {
            "when": "between_songs",
            "flow": [
                {
                    "OPTIONAL": {
                        "section": "Song_Transition",
                        "chance": "invalid",
                    }
                }
            ],
        }
    ]

    with pytest.raises(InvalidDataError, match="OPTIONAL chance must be numeric"):
        storage._normalize_station(station)
