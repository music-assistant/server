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
from music_assistant.providers.ai_radio.storage import AIRadioStorageMixin


class DummyStorage(AIRadioStorageMixin):
    """Minimal storage harness for unit testing helper methods."""

    def __init__(self) -> None:
        """Initialize dummy mixin state."""
        self.logger = logging.getLogger(__name__)
        self._sections: dict[str, dict[str, Any]] = {}
        self._stations: dict[str, dict[str, Any]] = {}
        self._hosts: dict[str, dict[str, Any]] = {}


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


def test_normalize_station_rejects_unknown_host_reference() -> None:
    """Reject stations that reference a host id that does not exist."""
    storage = DummyStorage()

    with pytest.raises(InvalidDataError, match="unknown host"):
        storage._normalize_station(
            {
                "id": "station_a",
                "name": "Station A",
                "source_playlist_id": "playlist-1",
                "host_id": "missing_host",
            }
        )


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


@pytest.mark.parametrize("field", ["max_duration_minutes"])
def test_normalize_station_rejects_non_numeric_numeric_field(field: str) -> None:
    """Reject station numeric fields containing non-numeric values."""
    storage = DummyStorage()
    storage._sections = {"Song_Transition": _section("Song_Transition")}
    storage._hosts = {"host_a": {"id": "host_a", "name": "Host A"}}
    station = _station(["Song_Transition"])
    station["host_id"] = "host_a"
    station[field] = "not-a-number"

    with pytest.raises(InvalidDataError, match="must be numeric"):
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


def _record_write(writes: list[str], name: str) -> Any:
    """Return a persistence stub that records that it was called."""

    async def _write() -> None:
        writes.append(name)

    return _write


def test_load_stations_v3_skips_the_migration_and_rewrites_nothing(tmp_path: Any) -> None:
    """A file already at v3 loads as-is, inventing no hosts and rewriting no file."""
    stations_file = tmp_path / "stations.json"
    payload = {
        "version": 3,
        "stations": [
            {
                "id": "station_a",
                "name": "Station A",
                "source_playlist_id": "playlist-1",
                "host_id": "rick",
            }
        ],
    }
    stations_file.write_text(json.dumps(payload))

    storage = DummyStorage()
    storage._stations_file = stations_file
    storage._sections_file = tmp_path / "sections.json"
    storage._hosts = {"rick": {"id": "rick", "name": "Rick"}}
    writes: list[str] = []
    for name in ("_write_stations", "_write_sections", "_write_hosts"):
        setattr(storage, name, _record_write(writes, name))

    asyncio.run(storage._load_stations())

    assert list(storage._stations) == ["station_a"]
    assert storage._stations["station_a"]["host_id"] == "rick"
    assert list(storage._hosts) == ["rick"]
    assert writes == []
    assert json.loads(stations_file.read_text()) == payload


def test_normalize_station_v3_requires_known_host() -> None:
    """Reject stations that reference a host that does not exist."""
    dummy = DummyStorage()
    dummy._hosts = {}
    station = {
        "id": "station_a",
        "name": "Station A",
        "source_playlist_id": "playlist-1",
        "host_id": "rick",
    }
    with pytest.raises(InvalidDataError):
        dummy._normalize_station(station)


def test_normalize_station_v3_returns_slim_schema() -> None:
    """Normalize a v3 station payload without any legacy embedded fields."""
    dummy = DummyStorage()
    dummy._hosts = {"rick": {"id": "rick", "name": "Rick"}}
    station = {
        "id": "station_a",
        "name": "Station A",
        "source_playlist_id": "playlist-1",
        "host_id": "rick",
    }
    normalized = dummy._normalize_station(station)
    assert normalized["host_id"] == "rick"
    for legacy_key in ("general", "sections", "section_ids", "section_order", "merge_section_id"):
        assert legacy_key not in normalized
