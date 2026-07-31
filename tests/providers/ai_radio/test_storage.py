"""Unit tests for AI Radio storage normalization helpers."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, cast

import pytest
from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.ai_radio import storage as storage_module
from music_assistant.providers.ai_radio.constants import DEFAULT_LLM_INSTRUCTIONS
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
        "instructions": "Keep it concise and conversational.",
        "weather_provider": "open_meteo",
        "weather_timeout_seconds": "30",
        # timezone/location moved to provider config; a legacy station carrying them
        # must not resurrect them in the normalized output
        "timezone": "Europe/Berlin",
        "location": {"city": "Berlin", "country": "DE"},
        "foo": "bar",
    }

    normalized = storage._normalize_general(raw_general)

    assert normalized["instructions"] == "Keep it concise and conversational."
    assert normalized["weather_timeout_seconds"] == 30
    assert "foo" not in normalized
    assert "timezone" not in normalized
    assert "location" not in normalized

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


def test_load_sections_persists_defaults_when_file_corrupt(tmp_path: Any) -> None:
    """Persist default sections to disk when on-disk payload is empty."""
    sections_file = tmp_path / "sections.json"
    sections_file.write_text(json.dumps({"version": 1, "sections": []}))

    storage = DummyStorage()
    storage._sections_file = sections_file

    asyncio.run(storage._load_sections())

    parsed = json.loads(sections_file.read_text())
    assert len(parsed["sections"]) > 0


def test_load_sections_recovers_from_invalid_json(tmp_path: Any) -> None:
    """Fall back to default sections when the sections file is not valid JSON."""
    sections_file = tmp_path / "sections.json"
    sections_file.write_text("{not valid json")

    storage = DummyStorage()
    storage._sections_file = sections_file

    asyncio.run(storage._load_sections())

    assert len(storage._sections) > 0


def test_load_stations_recovers_from_invalid_json(tmp_path: Any) -> None:
    """Continue with no stations when the stations file is not valid JSON."""
    stations_file = tmp_path / "stations.json"
    stations_file.write_text("{not valid json")

    storage = DummyStorage()
    storage._stations_file = stations_file
    storage._sections_file = tmp_path / "sections.json"

    asyncio.run(storage._load_stations())

    assert storage._stations == {}


def test_write_sections_keeps_existing_file_when_write_fails(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preserve the previous sections file when writing the new content fails."""
    sections_file = tmp_path / "sections.json"
    original_content = json.dumps({"version": 1, "sections": []})
    sections_file.write_text(original_content)

    storage = DummyStorage()
    storage._sections_file = sections_file
    storage._sections = {"s1": {"id": "s1", "name": "S1", "type": "ai_text", "prompt": "P"}}

    def failing_fsync(_fd: int) -> None:
        """Raise to simulate a failed disk write."""
        raise OSError("disk full")

    monkeypatch.setattr(cast("Any", storage_module).os, "fsync", failing_fsync)

    with pytest.raises(OSError, match="disk full"):
        asyncio.run(storage._write_sections())

    assert sections_file.read_text() == original_content


def test_normalize_section_handles_non_numeric_max_chars_cleanly() -> None:
    """Handle non-numeric constraint values without leaking a raw ValueError."""
    storage = DummyStorage()

    with pytest.raises(InvalidDataError):
        storage._normalize_section(
            {
                "id": "s1",
                "name": "s1",
                "type": "ai_text",
                "prompt": "p",
                "constraints": {"max_chars": "abc"},
            }
        )


def test_normalize_station_rejects_non_numeric_alternative_weight() -> None:
    """Reject ALTERNATIVE choices with non-numeric weight values."""
    storage = DummyStorage()
    storage._sections = {"Song_Transition": _section("Song_Transition")}
    station = _station(["Song_Transition"])
    station["section_order"] = [
        {
            "when": "between_songs",
            "flow": [
                {
                    "ALTERNATIVE": {
                        "choices": [{"section": "Song_Transition", "weight": "not-a-number"}]
                    }
                }
            ],
        }
    ]

    with pytest.raises(InvalidDataError, match="weight"):
        storage._normalize_station(station)


def test_load_stations_does_not_persist_invalid_default_station(tmp_path: Any) -> None:
    """Do not persist a default station that fails normalization to disk."""
    storage = DummyStorage()
    storage._stations_file = tmp_path / "stations.json"
    storage._sections_file = tmp_path / "sections.json"
    storage._sections = {item["id"]: item for item in storage._default_sections_template()}

    asyncio.run(storage._load_stations())

    if storage._stations_file.exists():
        parsed = json.loads(storage._stations_file.read_text())
        for station in parsed.get("stations", []):
            assert station.get("source_playlist_id") != ""


def test_normalize_general_replaces_null_instructions_with_default() -> None:
    """Replace null instructions with the default LLM instructions string."""
    storage = DummyStorage()

    result = storage._normalize_general(
        {
            "instructions": None,
            "weather_provider": "open_meteo",
            "weather_timeout_seconds": 20,
        }
    )

    assert result["instructions"] == DEFAULT_LLM_INSTRUCTIONS


@pytest.mark.parametrize(
    "field",
    [
        "max_duration_minutes",
        "dynamic_batch_size",
        "dynamic_poll_seconds",
        "dynamic_prefetch_remaining_tracks",
    ],
)
def test_normalize_station_rejects_non_numeric_numeric_field(field: str) -> None:
    """Reject station numeric fields containing non-numeric values."""
    storage = DummyStorage()
    storage._sections = {"Song_Transition": _section("Song_Transition")}
    station = _station(["Song_Transition"])
    station[field] = "not-a-number"

    with pytest.raises(InvalidDataError):
        storage._normalize_station(station)


def test_write_json_file_round_trips_non_ascii_content_as_utf8(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Write JSON content with an explicit utf-8 encoding and round-trip non-ASCII text."""
    storage = DummyStorage()
    target = tmp_path / "sections.json"
    payload = {"name": "Café Wörld 日本語", "emoji": "🎧"}

    seen_encodings: list[str | None] = []
    real_open = Path.open

    def spy_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        seen_encodings.append(kwargs.get("encoding"))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", spy_open)

    asyncio.run(storage._write_json_file(target, payload))

    assert seen_encodings == ["utf-8"]
    assert json.loads(target.read_bytes().decode("utf-8")) == payload
