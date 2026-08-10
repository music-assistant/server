"""Unit tests for AI Radio host normalization and persistence."""

from __future__ import annotations

import asyncio
import json
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


def test_migrate_v2_renames_host_on_slug_collision() -> None:
    """Rename the second host's slug when two distinct personas share the same name."""
    dummy = DummyHosts()
    dummy._sections = {"Song_Transition": _section("Song_Transition")}
    stations = [
        _v2_station("station_a", "Same Name", "Persona A."),
        _v2_station("station_b", "Same Name", "Persona B."),
    ]
    dummy._migrate_stations_v2_to_v3(stations)
    assert len(dummy._hosts) == 2
    assert stations[0]["host_id"] != stations[1]["host_id"]
    assert "same_name_host" in dummy._hosts
    renamed_ids = set(dummy._hosts) - {"same_name_host"}
    assert len(renamed_ids) == 1
    assert next(iter(renamed_ids)).startswith("same_name_host_")


def test_load_stations_skips_station_that_fails_host_migration(tmp_path: Any) -> None:
    """Isolate one station's migration failure so the rest of the file still loads."""
    good_station = _v2_station("station_a", "Morning", "Persona A.")
    bad_station = _v2_station("station_b", "Evening", "Persona B.")
    # non-numeric OPTIONAL.chance makes _normalize_host raise during migration
    bad_station["section_order"] = [
        {
            "when": "between_songs",
            "flow": [{"OPTIONAL": {"section": "Song_Transition", "chance": "invalid"}}],
        }
    ]
    stations_file = tmp_path / "stations.json"
    stations_file.write_text(json.dumps({"version": 2, "stations": [good_station, bad_station]}))

    dummy = DummyHosts()
    dummy._stations_file = stations_file
    dummy._sections_file = tmp_path / "sections.json"
    dummy._hosts_file = tmp_path / "hosts.json"
    dummy._sections = {"Song_Transition": _section("Song_Transition")}

    asyncio.run(dummy._load_stations())

    assert len(dummy._hosts) == 1
    assert len(dummy._stations) == 1
    assert next(iter(dummy._stations.values()))["name"] == "Morning"


def test_load_stations_migrates_v2_file_on_disk(tmp_path: Any) -> None:
    """
    Round-trip a v2 stations file through _load_stations.

    Covers: hosts extracted from embedded personas, stations normalized to v3
    in memory, hosts.json and stations.json written, embedded sections
    without a matching section_ids list (fallback derivation) upserted into
    _sections, and stations.json rewritten exactly once.
    """
    v2_station = {
        "id": "station_a",
        "name": "Evening Chill",
        "source_playlist_id": "playlist-1",
        "source_playlist_provider": "library",
        "general": {"instructions": "Laid-back host."},
        # no section_ids: forces the embedded-sections-only fallback
        "sections": [_section("Song_Transition")],
        "section_order": [
            {"when": "between_songs", "flow": [{"MUST": "Song_Transition"}]},
        ],
        "merge_section_id": "",
    }
    stations_file = tmp_path / "stations.json"
    stations_file.write_text(json.dumps({"version": 2, "stations": [v2_station]}))

    dummy = DummyHosts()
    dummy._stations_file = stations_file
    dummy._sections_file = tmp_path / "sections.json"
    dummy._hosts_file = tmp_path / "hosts.json"

    asyncio.run(dummy._load_hosts())

    write_calls = 0
    original_write_stations = dummy._write_stations

    async def _counting_write_stations() -> None:
        nonlocal write_calls
        write_calls += 1
        await original_write_stations()

    dummy._write_stations = _counting_write_stations  # type: ignore[method-assign]

    asyncio.run(dummy._load_stations())

    assert write_calls == 1
    assert len(dummy._hosts) == 1
    assert "Song_Transition" in dummy._sections

    host = next(iter(dummy._hosts.values()))
    assert host["instructions"] == "Laid-back host."
    assert host["section_ids"] == ["Song_Transition"]

    station = next(iter(dummy._stations.values()))
    assert station["host_id"] == host["id"]
    for legacy_key in ("general", "sections", "section_ids", "section_order", "merge_section_id"):
        assert legacy_key not in station

    assert dummy._hosts_file.exists()
    hosts_payload = json.loads(dummy._hosts_file.read_text())
    assert hosts_payload["hosts"][0]["instructions"] == "Laid-back host."

    stations_payload = json.loads(stations_file.read_text())
    assert stations_payload["version"] == 3


def _preset_dummy(tmp_path: Any) -> DummyHosts:
    """Build a host harness pointed at an empty storage dir."""
    dummy = DummyHosts()
    dummy._hosts_file = tmp_path / "hosts.json"
    dummy._stations_file = tmp_path / "stations.json"
    dummy._sections_file = tmp_path / "sections.json"
    return dummy


def test_seed_preset_hosts_seeds_four_hosts_on_fresh_install(tmp_path: Any) -> None:
    """Seed the four preset hosts with host-scoped sections and persist both files."""
    dummy = _preset_dummy(tmp_path)

    asyncio.run(dummy._seed_preset_hosts())

    assert list(dummy._hosts) == ["morning_show", "minimal_dj", "music_nerd", "party_host"]
    host = dummy._hosts["morning_show"]
    assert host["name"] == "Morning show"
    assert host["section_ids"] == [
        "morning_show_intro",
        "morning_show_transition",
        "morning_show_weather",
        "morning_show_news",
        "morning_show_sign_off",
        "morning_show_smoother",
    ]
    assert host["merge_section_id"] == "morning_show_smoother"
    assert all(section_id in dummy._sections for section_id in host["section_ids"])
    # the same segment name on two hosts must not share a section
    assert dummy._sections["minimal_dj_transition"]["constraints"] == {"max_chars": 300}
    assert dummy._sections["morning_show_transition"]["constraints"] == {"max_chars": 650}

    hosts_payload = json.loads(dummy._hosts_file.read_text())
    assert [item["id"] for item in hosts_payload["hosts"]] == [
        "minimal_dj",
        "morning_show",
        "music_nerd",
        "party_host",
    ]
    section_ids = {item["id"] for item in json.loads(dummy._sections_file.read_text())["sections"]}
    assert section_ids == set(dummy._sections)
    assert not dummy._stations_file.exists()


def test_seed_preset_hosts_skipped_for_migrated_install(tmp_path: Any) -> None:
    """Leave a pre-v3 install alone: its hosts come from the station migration."""
    dummy = _preset_dummy(tmp_path)
    dummy._stations_file.write_text(json.dumps({"version": 2, "stations": []}))

    asyncio.run(dummy._seed_preset_hosts())

    assert dummy._hosts == {}
    assert dummy._sections == {}
    assert not dummy._hosts_file.exists()


def test_seed_preset_hosts_skipped_when_hosts_file_exists(tmp_path: Any) -> None:
    """Never re-seed once a hosts file exists, even when the user emptied it."""
    dummy = _preset_dummy(tmp_path)
    dummy._hosts_file.write_text(json.dumps({"version": 1, "hosts": []}))

    asyncio.run(dummy._seed_preset_hosts())

    assert dummy._hosts == {}
    assert dummy._sections == {}


def test_preset_hosts_and_sections_are_already_normalized() -> None:
    """Ensure every seeded preset passes host and section validation untouched."""
    dummy = DummyHosts()
    for host, sections in dummy._default_preset_hosts():
        for section in sections:
            assert dummy._normalize_section(section) == section
            dummy._sections[section["id"]] = section
        assert dummy._normalize_host(host) == host


def test_morning_show_preset_section_order_matches_frontend_compiler() -> None:
    """Pin the compiled cadence of the morning show preset to the frontend's compiler output."""
    dummy = DummyHosts()
    hosts = {host["id"]: host for host, _ in dummy._default_preset_hosts()}

    assert hosts["morning_show"]["section_order"] == [
        {"when": "start_of_playlist", "flow": [{"MUST": "morning_show_intro"}]},
        {
            "when": "between_songs",
            "flow": [
                {
                    "OPTIONAL": {
                        "section": "morning_show_transition",
                        "chance": 2 / 3,
                        "guards": {
                            "min_gap_songs": 3,
                            "max_per_60min": 0,
                            "require_placeholders_present": [],
                        },
                    }
                },
                {
                    "OPTIONAL": {
                        "section": "morning_show_weather",
                        "chance": 1.0,
                        "guards": {
                            "min_gap_songs": 0,
                            "max_per_60min": 1,
                            "require_placeholders_present": ["<weather_hourly>", "<timestamp>"],
                        },
                    }
                },
                {
                    "OPTIONAL": {
                        "section": "morning_show_news",
                        "chance": 1.0,
                        "guards": {
                            "min_gap_songs": 0,
                            "max_per_60min": 1,
                            "require_placeholders_present": ["<timestamp>"],
                        },
                    }
                },
            ],
        },
        {"when": "end_of_playlist", "flow": [{"MUST": "morning_show_sign_off"}]},
    ]


def test_music_nerd_preset_compiles_its_recurring_segments_as_optional() -> None:
    """Compile an every-2-songs segment into a certain OPTIONAL and an occasional one by chance."""
    dummy = DummyHosts()
    hosts = {host["id"]: host for host, _ in dummy._default_preset_hosts()}

    assert hosts["music_nerd"]["section_order"] == [
        {"when": "start_of_playlist", "flow": [{"MUST": "music_nerd_intro"}]},
        {
            "when": "between_songs",
            "flow": [
                {
                    "OPTIONAL": {
                        "section": "music_nerd_artist_fact",
                        "chance": 1.0,
                        "guards": {
                            "min_gap_songs": 2,
                            "max_per_60min": 0,
                            "require_placeholders_present": [],
                        },
                    }
                },
                {
                    "OPTIONAL": {
                        "section": "music_nerd_transition",
                        "chance": 0.2,
                        "guards": {
                            "min_gap_songs": 1,
                            "max_per_60min": 0,
                            "require_placeholders_present": [],
                        },
                    }
                },
            ],
        },
    ]
