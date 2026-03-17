"""Station and section storage/normalization mixin for AI Radio."""
# mypy: disable-error-code=attr-defined

from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import aiofiles
from music_assistant_models.errors import InvalidDataError

from music_assistant.helpers.json import async_json_dumps, async_json_loads

from .constants import (
    DEFAULT_ELEVENLABS_MODEL,
    DEFAULT_LLM_MODEL,
    DEFAULT_MAX_TOKENS,
    DEFAULT_OPENAI_TTS_MODEL,
    DEFAULT_OPENAI_TTS_VOICE,
    DEFAULT_SECTION_STORE_PATH,
    DEFAULT_TEMPERATURE,
    DEFAULT_TTS_PROVIDER,
    DEFAULT_WEATHER_PROVIDER,
    DEFAULT_WEATHER_TIMEOUT_SECONDS,
    EMPTY_SECTION_ID,
    VALID_WEB_SEARCH_MODES,
)
from .models import slugify

_slugify = slugify

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig

    from music_assistant.mass import MusicAssistant


class AIRadioStorageMixin:
    """Mixin with station/section persistence and normalization helpers."""

    if TYPE_CHECKING:
        mass: MusicAssistant
        config: ProviderConfig
        logger: logging.Logger
        _sections_file: Path
        _stations_file: Path
        _sections: dict[str, dict[str, Any]]
        _stations: dict[str, dict[str, Any]]

    async def _load_sections(self) -> None:
        """Load shared section definitions from disk."""
        sections_file_exists = await asyncio.to_thread(self._sections_file.exists)
        if not sections_file_exists:
            defaults = self._default_sections_template()
            self._sections = {item["id"]: item for item in defaults}
            await self._write_sections()
            return
        async with aiofiles.open(self._sections_file) as file_handle:
            content = await file_handle.read()
        payload = await async_json_loads(content)
        items = payload.get("sections", []) if isinstance(payload, dict) else []
        parsed: dict[str, dict[str, Any]] = {}
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                try:
                    normalized = self._normalize_section(item)
                except Exception as err:
                    self.logger.warning("Skipping invalid section profile: %s", err)
                    continue
                parsed[normalized["id"]] = normalized
        if not parsed:
            defaults = self._default_sections_template()
            parsed = {item["id"]: item for item in defaults}
            await self._write_sections()
        self._sections = parsed

    async def _write_sections(self) -> None:
        """Persist shared section definitions to disk."""
        payload = {
            "version": 1,
            "sections": sorted(self._sections.values(), key=lambda item: item["id"].lower()),
        }
        content = await async_json_dumps(payload, indent=True)
        async with aiofiles.open(self._sections_file, "w") as file_handle:
            await file_handle.write(content)

    async def _load_stations(self) -> None:
        """Load station profiles from disk."""
        stations_file_exists = await asyncio.to_thread(self._stations_file.exists)
        if not stations_file_exists:
            default_station = self._default_station_template()
            self._stations = {default_station["id"]: default_station}
            await self._write_stations()
            return
        async with aiofiles.open(self._stations_file) as file_handle:
            content = await file_handle.read()
        payload = await async_json_loads(content)
        stations = payload.get("stations", []) if isinstance(payload, dict) else []
        parsed: dict[str, dict[str, Any]] = {}
        sections_changed = False
        if isinstance(stations, list):
            for station in stations:
                if not isinstance(station, dict):
                    continue
                try:
                    if self._upsert_embedded_sections_from_station(station):
                        sections_changed = True
                    normalized = self._normalize_station(station)
                except Exception as err:
                    self.logger.warning("Skipping invalid station profile: %s", err)
                    continue
                parsed[normalized["id"]] = normalized
        self._stations = parsed
        if not self._stations:
            default_station = self._default_station_template()
            self._stations = {default_station["id"]: default_station}
            await self._write_stations()
        if sections_changed:
            await self._write_sections()

    async def _write_stations(self) -> None:
        """Persist station profiles to disk."""
        payload = {
            "version": 2,
            "stations": sorted(self._stations.values(), key=lambda item: item["name"]),
        }
        content = await async_json_dumps(payload, indent=True)
        async with aiofiles.open(self._stations_file, "w") as file_handle:
            await file_handle.write(content)

    def _materialize_sections(
        self, section_ids: list[str]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Resolve section ids to shared section objects."""
        sections: list[dict[str, Any]] = []
        missing: list[str] = []
        for section_id in section_ids:
            section = self._sections.get(section_id)
            if section is None:
                missing.append(section_id)
                continue
            sections.append(deepcopy(section))
        return sections, missing

    def _refresh_station_sections(self) -> None:
        """Rebuild embedded station sections from selected shared ids."""
        for station in self._stations.values():
            section_ids = list(station.get("section_ids", []))
            sections, _missing = self._materialize_sections(section_ids)
            station["sections"] = sections

    def _normalize_section(self, section: dict[str, Any]) -> dict[str, Any]:
        """Validate and normalize a shared section definition."""
        section_id = str(section.get("id", "")).strip()
        if not section_id:
            raise InvalidDataError("Section id is required")

        section_type = str(section.get("type", "ai_text")).strip().lower()
        if section_type not in {"ai_text", "ai_meta"}:
            raise InvalidDataError(f"Section '{section_id}' has invalid type '{section_type}'")

        name = str(section.get("name", section_id)).strip() or section_id
        prompt = str(section.get("prompt", "")).strip()
        if not prompt:
            raise InvalidDataError(f"Section '{section_id}' prompt is required")

        normalized: dict[str, Any] = {
            "id": section_id,
            "name": name,
            "type": section_type,
            "prompt": prompt,
        }

        if section_type == "ai_text":
            web_search = str(section.get("web_search", "disabled")).strip().lower()
            if web_search not in VALID_WEB_SEARCH_MODES:
                web_search = "disabled"
            normalized["web_search"] = web_search
            raw_constraints = section.get("constraints")
            max_chars = 0
            if isinstance(raw_constraints, dict):
                max_chars = int(raw_constraints.get("max_chars", 0) or 0)
            if max_chars > 0:
                normalized["constraints"] = {"max_chars": max_chars}
        for passthrough_key in ("cover_image",):
            if passthrough_key in section:
                normalized[passthrough_key] = section[passthrough_key]
        return normalized

    def _normalize_general(self, source_general: Any) -> dict[str, Any]:
        """Normalize station general settings to a known schema."""
        defaults = cast("dict[str, Any]", deepcopy(self._default_station_template()["general"]))
        if not isinstance(source_general, dict):
            return defaults

        location = source_general.get("location")
        if not isinstance(location, dict):
            location = {}

        def _text(key: str) -> str:
            value = source_general.get(key, defaults[key])
            return str(value or defaults[key]).strip() or str(defaults[key])

        def _number(key: str, cast_type: type[int | float]) -> int | float:
            value = source_general.get(key, defaults[key])
            try:
                return cast_type(value)
            except (TypeError, ValueError):
                return cast_type(defaults[key])

        return {
            "timezone": _text("timezone"),
            "location": {
                "city": str(location.get("city", "")).strip(),
                "country": str(location.get("country", "")).strip(),
            },
            "model": _text("model"),
            "temperature": _number("temperature", float),
            "max_tokens": _number("max_tokens", int),
            "instructions": str(source_general.get("instructions", defaults["instructions"])),
            "section_store_path": _text("section_store_path"),
            "weather_provider": _text("weather_provider"),
            "weather_timeout_seconds": _number("weather_timeout_seconds", int),
            "tts_provider": _text("tts_provider"),
            "openai_tts_model": _text("openai_tts_model"),
            "openai_tts_voice": _text("openai_tts_voice"),
            "openai_tts_instructions": str(
                source_general.get(
                    "openai_tts_instructions",
                    defaults["openai_tts_instructions"],
                )
            ),
            "elevenlabs_model": _text("elevenlabs_model"),
            "elevenlabs_voice_id": str(
                source_general.get("elevenlabs_voice_id", defaults["elevenlabs_voice_id"])
            ).strip(),
        }

    def _upsert_embedded_sections_from_station(self, station: dict[str, Any]) -> bool:
        """Upsert embedded station sections and return whether shared sections changed."""
        changed = False
        raw_sections = station.get("sections")
        if not isinstance(raw_sections, list):
            return changed

        ids_from_embedded: list[str] = []
        for item in raw_sections:
            if not isinstance(item, dict):
                continue
            normalized = self._normalize_section(item)
            ids_from_embedded.append(normalized["id"])
            existing = self._sections.get(normalized["id"])
            if existing != normalized:
                self._sections[normalized["id"]] = normalized
                changed = True

        raw_ids = station.get("section_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            station["section_ids"] = ids_from_embedded
        return changed

    def _normalize_station(self, station: dict[str, Any]) -> dict[str, Any]:
        """Validate and normalize a station profile."""
        station_id = str(station.get("id", "")).strip() or uuid4().hex[:8]
        station_id = _slugify(station_id)
        name = str(station.get("name", "")).strip()
        if not name:
            raise InvalidDataError("Station name is required")

        source_playlist_id = str(station.get("source_playlist_id", "")).strip()
        if not source_playlist_id:
            raise InvalidDataError("Station source_playlist_id is required")
        source_playlist_provider = str(station.get("source_playlist_provider", "library")).strip()
        source_playlist_provider = source_playlist_provider or "library"

        raw_section_order = station.get("section_order")
        if not isinstance(raw_section_order, list) or not raw_section_order:
            raise InvalidDataError("Station requires a non-empty 'section_order' list")

        section_ids = self._collect_station_section_ids(station)
        if not section_ids:
            raise InvalidDataError("Station requires at least one section id")

        sections, missing = self._materialize_sections(section_ids)
        if missing:
            raise InvalidDataError(
                f"Station references unknown sections: {', '.join(sorted(set(missing)))}"
            )

        self._validate_section_order(raw_section_order, set(section_ids))

        general = self._normalize_general(station.get("general"))
        merge_section_id = str(station.get("merge_section_id", "")).strip()
        if merge_section_id:
            if merge_section_id not in section_ids:
                raise InvalidDataError("merge_section_id must be selected in station section_ids")
            merge_section = self._sections.get(merge_section_id)
            if not merge_section or str(merge_section.get("type", "")).strip().lower() != "ai_meta":
                raise InvalidDataError("merge_section_id must reference an ai_meta section")

        return {
            "id": station_id,
            "name": name,
            "source_playlist_id": source_playlist_id,
            "source_playlist_provider": source_playlist_provider,
            "target_playlist_provider": str(station.get("target_playlist_provider", "builtin")),
            "default_player_id": str(station.get("default_player_id", "")),
            "max_duration_minutes": float(station.get("max_duration_minutes", 0) or 0),
            "dynamic_batch_size": max(1, int(station.get("dynamic_batch_size", 1) or 1)),
            "dynamic_poll_seconds": max(1, int(station.get("dynamic_poll_seconds", 5) or 5)),
            "dynamic_prefetch_remaining_tracks": max(
                1, int(station.get("dynamic_prefetch_remaining_tracks", 2) or 2)
            ),
            "clear_queue_on_start": bool(station.get("clear_queue_on_start", True)),
            "merge_section_id": merge_section_id,
            "general": general,
            "section_ids": section_ids,
            "sections": sections,
            "section_order": deepcopy(raw_section_order),
        }

    def _collect_station_section_ids(self, station: dict[str, Any]) -> list[str]:
        """Collect station section ids from section_ids or legacy embedded sections."""
        ids: list[str] = []
        raw_ids = station.get("section_ids")
        if isinstance(raw_ids, list):
            ids.extend(str(item).strip() for item in raw_ids if str(item).strip())

        if not ids:
            raw_sections = station.get("sections")
            if isinstance(raw_sections, list):
                for section in raw_sections:
                    if not isinstance(section, dict):
                        continue
                    section_id = str(section.get("id", "")).strip()
                    if section_id:
                        ids.append(section_id)

        deduped: list[str] = []
        seen: set[str] = set()
        for section_id in ids:
            if section_id in seen:
                continue
            seen.add(section_id)
            deduped.append(section_id)
        return deduped

    def _validate_section_order(
        self,
        section_order: list[Any],
        valid_section_ids: set[str],
    ) -> None:
        """Validate section order references known shared section ids."""

        def _ensure_known(section_id: str) -> None:
            if section_id == EMPTY_SECTION_ID:
                return
            if section_id not in valid_section_ids:
                raise InvalidDataError(
                    f"section_order references unknown section id '{section_id}'"
                )

        for rule in section_order:
            if not isinstance(rule, dict):
                raise InvalidDataError("Each section_order rule must be an object")
            flow = rule.get("flow")
            if not isinstance(flow, list) or not flow:
                raise InvalidDataError("Each section_order rule requires non-empty flow")
            for item in flow:
                if not isinstance(item, dict):
                    raise InvalidDataError("Flow item must be an object")
                if "MUST" in item:
                    _ensure_known(str(item.get("MUST", "")).strip())
                    continue
                if "OPTIONAL" in item:
                    optional = item.get("OPTIONAL")
                    if not isinstance(optional, dict):
                        raise InvalidDataError("OPTIONAL flow must be an object")
                    _ensure_known(str(optional.get("section", "")).strip())
                    continue
                if "ALTERNATIVE" in item:
                    alternative = item.get("ALTERNATIVE")
                    if not isinstance(alternative, dict):
                        raise InvalidDataError("ALTERNATIVE flow must be an object")
                    choices = alternative.get("choices")
                    if not isinstance(choices, list) or not choices:
                        raise InvalidDataError("ALTERNATIVE flow requires non-empty choices")
                    for choice in choices:
                        if not isinstance(choice, dict):
                            raise InvalidDataError("ALTERNATIVE choice must be an object")
                        _ensure_known(str(choice.get("section", "")).strip())

    def _default_sections_template(self) -> list[dict[str, Any]]:
        """Return built-in shared section templates."""
        return [
            {
                "id": "Song_Introduction_Start",
                "name": "Song Introduction Start",
                "type": "ai_text",
                "web_search": "disabled",
                "prompt": (
                    "The next track is <next_songinfo>. Open the program like a "
                    "polished radio host: brief welcome, confident energy, one "
                    "concrete hook about the song or artist, and a clean handoff "
                    "into the music."
                ),
                "constraints": {"max_chars": 650},
            },
            {
                "id": "Song_Transition",
                "name": "Song Transition",
                "type": "ai_text",
                "web_search": "allow",
                "prompt": (
                    "The previous track was <prev_songinfo> and the next track is "
                    "<next_songinfo>. Create a natural radio transition that "
                    "connects both songs, sounds informed but concise, and avoids "
                    "filler or repetition."
                ),
                "constraints": {"max_chars": 650},
            },
            {
                "id": "Global_News",
                "name": "Global News",
                "type": "ai_text",
                "web_search": "force",
                "prompt": (
                    "Create a short global news bulletin anchored to <timestamp>. "
                    "Use web search. Include two or three current items that are "
                    "broadly relevant, clearly separated, fact-focused, and "
                    "written for spoken delivery."
                ),
                "constraints": {"max_chars": 700},
            },
            {
                "id": "Weather_Short",
                "name": "Weather Short",
                "type": "ai_text",
                "web_search": "disabled",
                "prompt": (
                    "Using <weather_hourly> and <timestamp>, deliver a short "
                    "spoken weather update with the current outlook, a useful "
                    "next-hours summary, and smooth radio phrasing."
                ),
                "constraints": {"max_chars": 500},
            },
            {
                "id": "Song_Introduction_End",
                "name": "Song Introduction End",
                "type": "ai_text",
                "web_search": "disabled",
                "prompt": (
                    "The last track played was <prev_songinfo>. Close the program "
                    "with a memorable sign-off: brief reflection, warm farewell, "
                    "and language that sounds like the end of a real radio segment."
                ),
                "constraints": {"max_chars": 650},
            },
            {
                "id": "Between_Songs_Smoother",
                "name": "Between Songs Mix",
                "type": "ai_meta",
                "prompt": (
                    "Merge the drafts below into one coherent radio break. "
                    "Preserve factual content, remove duplication, and make the "
                    "final segment sound like one host speaking naturally.\n"
                    "<section_drafts>"
                ),
            },
        ]

    def _default_station_template(self) -> dict[str, Any]:
        """Return the built-in station template."""
        default_sections = self._default_sections_template()
        default_section_ids = [item["id"] for item in default_sections]
        return {
            "id": "example_station",
            "name": "Example AI Radio Station",
            "source_playlist_id": "",
            "source_playlist_provider": "library",
            "target_playlist_provider": "builtin",
            "default_player_id": "",
            "max_duration_minutes": 0,
            "dynamic_batch_size": 1,
            "dynamic_poll_seconds": 5,
            "dynamic_prefetch_remaining_tracks": 2,
            "clear_queue_on_start": True,
            "merge_section_id": "Between_Songs_Smoother",
            "general": {
                "timezone": "UTC",
                "location": {
                    "city": "",
                    "country": "",
                },
                "model": DEFAULT_LLM_MODEL,
                "temperature": DEFAULT_TEMPERATURE,
                "max_tokens": DEFAULT_MAX_TOKENS,
                "instructions": (
                    "Host personality: warm, sharp, music-literate, and slightly "
                    "premium without sounding formal. Program instructions: "
                    "write for spoken delivery, keep segments concise, avoid "
                    "bullet-point phrasing, avoid clichés, mention concrete "
                    "details when available, and maintain a believable radio flow "
                    "between sections."
                ),
                "section_store_path": DEFAULT_SECTION_STORE_PATH,
                "weather_provider": DEFAULT_WEATHER_PROVIDER,
                "weather_timeout_seconds": DEFAULT_WEATHER_TIMEOUT_SECONDS,
                "tts_provider": DEFAULT_TTS_PROVIDER,
                "openai_tts_model": DEFAULT_OPENAI_TTS_MODEL,
                "openai_tts_voice": DEFAULT_OPENAI_TTS_VOICE,
                "openai_tts_instructions": (
                    "Delivery instructions: confident radio host, natural pacing, "
                    "clear sentence endings, subtle energy lift on intros and "
                    "transitions, and a warm close without exaggerated theatrics."
                ),
                "elevenlabs_model": DEFAULT_ELEVENLABS_MODEL,
                "elevenlabs_voice_id": "",
            },
            "section_ids": default_section_ids,
            "sections": deepcopy(default_sections),
            "section_order": [
                {"when": "start_of_playlist", "flow": [{"MUST": "Song_Introduction_Start"}]},
                {
                    "when": "between_songs",
                    "flow": [
                        {
                            "ALTERNATIVE": {
                                "choices": [
                                    {"section": "Song_Transition", "weight": 100},
                                ]
                            }
                        },
                        {
                            "OPTIONAL": {
                                "section": "Weather_Short",
                                "chance": 0.2,
                                "guards": {
                                    "min_gap_songs": 3,
                                    "max_per_60min": 1,
                                    "require_placeholders_present": ["<weather_hourly>"],
                                },
                            }
                        },
                        {
                            "OPTIONAL": {
                                "section": "Global_News",
                                "chance": 0.12,
                                "guards": {
                                    "min_gap_songs": 4,
                                    "max_per_60min": 1,
                                    "require_placeholders_present": ["<timestamp>"],
                                },
                            }
                        },
                    ],
                },
                {"when": "end_of_playlist", "flow": [{"MUST": "Song_Introduction_End"}]},
            ],
        }
