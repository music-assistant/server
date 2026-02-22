"""Unit tests for AI Radio storage normalization helpers."""

from __future__ import annotations

import logging
from typing import Any

from music_assistant.providers.ai_radio.storage import AIRadioStorageMixin


class DummyStorage(AIRadioStorageMixin):
    """Minimal storage harness for unit testing helper methods."""

    def __init__(self) -> None:
        """Initialize dummy mixin state."""
        self.logger = logging.getLogger(__name__)
        self._sections: dict[str, dict[str, Any]] = {}
        self._stations: dict[str, dict[str, Any]] = {}


def test_normalize_general_uses_known_schema_only() -> None:
    """Normalize general config to known keys and converted value types."""
    storage = DummyStorage()
    raw_general = {
        "timezone": "Europe/Berlin",
        "location": {"city": "Berlin", "country": "DE"},
        "model": "gpt-4o-mini",
        "temperature": "0.5",
        "max_tokens": "1200",
        "weather_provider": "open_meteo",
        "weather_timeout_seconds": "30",
        "foo": "bar",
    }

    normalized = storage._normalize_general(raw_general)

    assert normalized["timezone"] == "Europe/Berlin"
    assert normalized["location"] == {"city": "Berlin", "country": "DE"}
    assert normalized["temperature"] == 0.5
    assert normalized["max_tokens"] == 1200
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
