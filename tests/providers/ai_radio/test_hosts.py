"""Unit tests for AI Radio host normalization and persistence."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.ai_radio.constants import DEFAULT_LLM_INSTRUCTIONS
from music_assistant.providers.ai_radio.hosts import AIRadioHostsMixin
from music_assistant.providers.ai_radio.storage import AIRadioStorageMixin


class DummyHosts(AIRadioHostsMixin, AIRadioStorageMixin):
    """Minimal harness combining host and storage helpers."""

    def __init__(self) -> None:
        """Initialize dummy mixin state."""
        self.logger = logging.getLogger(__name__)
        self._sections: dict[str, dict[str, Any]] = {}
        self._stations: dict[str, dict[str, Any]] = {}
        self._hosts: dict[str, dict[str, Any]] = {}


def _section(section_id: str, section_type: str = "ai_text") -> dict[str, Any]:
    return {"id": section_id, "name": section_id, "type": section_type, "prompt": "Prompt"}


def _host(section_ids: list[str]) -> dict[str, Any]:
    return {
        "id": "rick",
        "name": "Rick",
        "instructions": "Laid-back evening host.",
        "tts_engine": "",
        "section_ids": section_ids,
        "section_order": [
            {"when": "between_songs", "flow": [{"MUST": section_ids[0]}]},
        ],
    }


def test_normalize_host_returns_known_schema() -> None:
    """Normalize a valid host payload to the known schema."""
    dummy = DummyHosts()
    dummy._sections = {"Song_Transition": _section("Song_Transition")}
    normalized = dummy._normalize_host(_host(["Song_Transition"]))
    assert normalized["id"] == "rick"
    assert normalized["name"] == "Rick"
    assert normalized["instructions"] == "Laid-back evening host."
    assert normalized["tts_engine"] == ""
    assert normalized["section_ids"] == ["Song_Transition"]
    assert normalized["merge_section_id"] == ""


def test_normalize_host_defaults_empty_instructions() -> None:
    """Fall back to the default instructions when the payload's are blank."""
    dummy = DummyHosts()
    dummy._sections = {"Song_Transition": _section("Song_Transition")}
    payload = _host(["Song_Transition"])
    payload["instructions"] = "   "
    normalized = dummy._normalize_host(payload)
    assert normalized["instructions"] == DEFAULT_LLM_INSTRUCTIONS


def test_normalize_host_requires_name() -> None:
    """Reject hosts with a blank name."""
    dummy = DummyHosts()
    dummy._sections = {"Song_Transition": _section("Song_Transition")}
    payload = _host(["Song_Transition"])
    payload["name"] = " "
    with pytest.raises(InvalidDataError):
        dummy._normalize_host(payload)


def test_normalize_host_rejects_unknown_section_reference() -> None:
    """Reject hosts that reference shared sections that do not exist."""
    dummy = DummyHosts()
    dummy._sections = {}
    with pytest.raises(InvalidDataError):
        dummy._normalize_host(_host(["Missing_Section"]))


def test_normalize_host_rejects_non_meta_merge_section() -> None:
    """Reject merge sections that do not point to an ai_meta section."""
    dummy = DummyHosts()
    dummy._sections = {"Song_Transition": _section("Song_Transition")}
    payload = _host(["Song_Transition"])
    payload["merge_section_id"] = "Song_Transition"
    with pytest.raises(InvalidDataError):
        dummy._normalize_host(payload)


def test_normalize_host_rejects_non_numeric_optional_chance() -> None:
    """Reject OPTIONAL flow entries with non-numeric chance values."""
    dummy = DummyHosts()
    dummy._sections = {"Song_Transition": _section("Song_Transition")}
    payload = _host(["Song_Transition"])
    payload["section_order"] = [
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
        dummy._normalize_host(payload)


def test_normalize_host_rejects_non_numeric_alternative_weight() -> None:
    """Reject ALTERNATIVE choices with non-numeric weight values."""
    dummy = DummyHosts()
    dummy._sections = {"Song_Transition": _section("Song_Transition")}
    payload = _host(["Song_Transition"])
    payload["section_order"] = [
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
        dummy._normalize_host(payload)


def test_default_host_template_is_normalizable() -> None:
    """Ensure the built-in host template passes normalization."""
    dummy = DummyHosts()
    defaults = dummy._default_sections_template()
    dummy._sections = {item["id"]: item for item in defaults}
    template = dummy._default_host_template()
    normalized = dummy._normalize_host(template)
    assert normalized["merge_section_id"] == "Between_Songs_Smoother"


def test_write_and_load_hosts_round_trips_normalized_host(tmp_path: Any) -> None:
    """Round trip a normalized host through write and load."""
    sections = {"Song_Transition": _section("Song_Transition")}
    hosts_file = tmp_path / "hosts.json"

    writer = DummyHosts()
    writer._sections = sections
    writer._hosts_file = hosts_file
    normalized = writer._normalize_host(_host(["Song_Transition"]))
    writer._hosts = {normalized["id"]: normalized}

    asyncio.run(writer._write_hosts())

    reader = DummyHosts()
    reader._sections = sections
    reader._hosts_file = hosts_file

    asyncio.run(reader._load_hosts())

    assert reader._hosts == {normalized["id"]: normalized}


def test_load_hosts_recovers_from_invalid_json(tmp_path: Any) -> None:
    """Continue with no hosts when the hosts file is not valid JSON."""
    hosts_file = tmp_path / "hosts.json"
    hosts_file.write_text("{not valid json")

    dummy = DummyHosts()
    dummy._hosts_file = hosts_file

    asyncio.run(dummy._load_hosts())

    assert dummy._hosts == {}
    assert hosts_file.read_text() == "{not valid json"


def _v2_station(station_id: str, name: str, instructions: str) -> dict[str, Any]:
    return {
        "id": station_id,
        "name": name,
        "source_playlist_id": "playlist-1",
        "source_playlist_provider": "library",
        "general": {"instructions": instructions},
        "section_ids": ["Song_Transition"],
        "sections": [_section("Song_Transition")],
        "section_order": [
            {"when": "between_songs", "flow": [{"MUST": "Song_Transition"}]},
        ],
        "merge_section_id": "",
    }


def test_migrate_v2_extracts_host_and_slims_station() -> None:
    """Extract a host profile out of a v2 station and slim the station in place."""
    dummy = DummyHosts()
    dummy._sections = {"Song_Transition": _section("Song_Transition")}
    stations = [_v2_station("station_a", "Evening Chill", "Laid-back host.")]
    dummy._migrate_stations_v2_to_v3(stations)
    assert len(dummy._hosts) == 1
    host = next(iter(dummy._hosts.values()))
    assert host["instructions"] == "Laid-back host."
    assert host["section_ids"] == ["Song_Transition"]
    assert stations[0]["host_id"] == host["id"]
    assert "section_order" not in stations[0]
    assert "general" not in stations[0]


def test_migrate_v2_dedupes_identical_hosts() -> None:
    """Reuse the same host for stations that share the same persona fingerprint."""
    dummy = DummyHosts()
    dummy._sections = {"Song_Transition": _section("Song_Transition")}
    stations = [
        _v2_station("station_a", "Morning", "Same persona."),
        _v2_station("station_b", "Evening", "Same persona."),
    ]
    dummy._migrate_stations_v2_to_v3(stations)
    assert len(dummy._hosts) == 1
    assert stations[0]["host_id"] == stations[1]["host_id"]


def test_migrate_v2_keeps_distinct_hosts_apart() -> None:
    """Create separate hosts for stations with distinct personas."""
    dummy = DummyHosts()
    dummy._sections = {"Song_Transition": _section("Song_Transition")}
    stations = [
        _v2_station("station_a", "Morning", "Persona A."),
        _v2_station("station_b", "Evening", "Persona B."),
    ]
    dummy._migrate_stations_v2_to_v3(stations)
    assert len(dummy._hosts) == 2
    assert stations[0]["host_id"] != stations[1]["host_id"]
