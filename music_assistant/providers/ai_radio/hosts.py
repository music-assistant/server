"""Host (personality) storage/normalization mixin for AI Radio."""
# mypy: disable-error-code=attr-defined

from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import aiofiles
from music_assistant_models.errors import InvalidDataError

from music_assistant.helpers.json import async_json_loads, json_dumps

from .constants import DEFAULT_LLM_INSTRUCTIONS, MERGE_SECTION_PROMPT
from .helpers import slugify

if TYPE_CHECKING:
    from pathlib import Path

    from music_assistant.mass import MusicAssistant

# placeholders a segment's prompt needs before it may be scheduled, in the same
# order the frontend's host compiler emits them
GUARD_PLACEHOLDER_TOKENS = ("<weather_hourly>", "<timestamp>")


@dataclass(frozen=True, slots=True)
class _Plays:
    """When a preset segment plays; 'value' carries its songs/minutes/percent, if any."""

    kind: Literal["start", "end", "every_song", "every_n_songs", "every_n_min", "occasionally"]
    value: int = 0


@dataclass(frozen=True, slots=True)
class _PresetSegment:
    """One spoken segment of a preset host."""

    segment_id: str
    name: str
    prompt: str
    web_search: str
    max_chars: int
    plays: _Plays


@dataclass(frozen=True, slots=True)
class _PresetHost:
    """A bundled host persona, kept in sync with the frontend's host editor presets."""

    host_id: str
    name: str
    instructions: str
    segments: tuple[_PresetSegment, ...]


SONG_TRANSITION_PROMPT = (
    "The previous track was <prev_songinfo> and the next track is <next_songinfo>. "
    "Create a natural radio transition that connects both songs, sounds informed "
    "but concise, and avoids filler or repetition."
)

PRESET_HOSTS: tuple[_PresetHost, ...] = (
    _PresetHost(
        host_id="morning_show",
        name="Morning show",
        instructions=(
            "Host personality: warm, energetic, upbeat morning-show host who sounds "
            "fully awake and glad to be on air. Program instructions: write for spoken "
            "delivery, keep segments concise, avoid bullet-point phrasing, avoid "
            "cliches, mention concrete details when available, and maintain a "
            "believable radio flow between sections."
        ),
        segments=(
            _PresetSegment(
                segment_id="intro",
                name="Intro",
                prompt=(
                    "The next track is <next_songinfo>. Open the morning show like a "
                    "warm, upbeat host: brief good-morning greeting, one concrete hook "
                    "about the song or artist, and a clean handoff into the music."
                ),
                web_search="disabled",
                max_chars=650,
                plays=_Plays("start"),
            ),
            _PresetSegment(
                segment_id="transition",
                name="Transition",
                prompt=(
                    "The previous track was <prev_songinfo> and the next track is "
                    "<next_songinfo>. Create a natural, energetic morning-show "
                    "transition that connects both songs, sounds informed but concise, "
                    "and avoids filler or repetition."
                ),
                web_search="allow",
                max_chars=650,
                plays=_Plays("every_n_songs", 3),
            ),
            _PresetSegment(
                segment_id="weather",
                name="Weather",
                prompt=(
                    "Using <weather_hourly> and <timestamp>, deliver a short spoken "
                    "weather update with the current outlook, a useful next-hours "
                    "summary, and smooth morning-show phrasing."
                ),
                web_search="disabled",
                max_chars=500,
                plays=_Plays("every_n_min", 60),
            ),
            _PresetSegment(
                segment_id="news",
                name="News",
                prompt=(
                    "Create a short global news bulletin anchored to <timestamp>. Use "
                    "web search. Include two or three current items that are broadly "
                    "relevant, clearly separated, fact-focused, and written for spoken "
                    "delivery."
                ),
                web_search="force",
                max_chars=700,
                plays=_Plays("every_n_min", 60),
            ),
            _PresetSegment(
                segment_id="sign_off",
                name="Sign-off",
                prompt=(
                    "The last track played was <prev_songinfo>. Close the morning show "
                    "with a memorable sign-off: brief reflection, warm farewell, and "
                    "language that sounds like the end of a real radio segment."
                ),
                web_search="disabled",
                max_chars=650,
                plays=_Plays("end"),
            ),
        ),
    ),
    _PresetHost(
        host_id="minimal_dj",
        name="Minimal DJ",
        instructions=(
            "Host personality: minimal, calm, understated DJ who lets the music lead. "
            "Program instructions: keep every segment brief, avoid small talk, avoid "
            "cliches, and never overshadow the songs with unnecessary commentary."
        ),
        segments=(
            _PresetSegment(
                segment_id="transition",
                name="Transition",
                prompt=(
                    "The previous track was <prev_songinfo> and the next track is "
                    "<next_songinfo>. Give a short, minimal DJ transition: one or two "
                    "sentences, calm tone, no filler, just enough to bridge the songs."
                ),
                web_search="disabled",
                max_chars=300,
                plays=_Plays("every_n_songs", 3),
            ),
        ),
    ),
    _PresetHost(
        host_id="music_nerd",
        name="Music nerd",
        instructions=(
            "Host personality: knowledgeable, enthusiastic music nerd who loves sharing "
            "context without lecturing. Program instructions: write for spoken delivery, "
            "keep segments concise, favor concrete facts over generic praise, avoid "
            "cliches, and maintain a believable radio flow between sections."
        ),
        segments=(
            _PresetSegment(
                segment_id="intro",
                name="Intro",
                prompt=(
                    "The next track is <next_songinfo>. Open the program like a "
                    "knowledgeable music host: brief welcome, one genuinely interesting "
                    "detail about the artist or genre, and a clean handoff into the music."
                ),
                web_search="disabled",
                max_chars=650,
                plays=_Plays("start"),
            ),
            _PresetSegment(
                segment_id="artist_fact",
                name="Artist fact",
                prompt=(
                    "The next track is <next_songinfo>. Share one specific, "
                    "well-researched fact about the artist, the recording, or its "
                    "influence. Keep it precise and avoid generic trivia."
                ),
                web_search="allow",
                max_chars=500,
                plays=_Plays("every_n_songs", 2),
            ),
            _PresetSegment(
                segment_id="transition",
                name="Transition",
                prompt=SONG_TRANSITION_PROMPT,
                web_search="allow",
                max_chars=650,
                plays=_Plays("occasionally", 20),
            ),
        ),
    ),
    _PresetHost(
        host_id="party_host",
        name="Party host",
        instructions=(
            "Host personality: high-energy, confident party host who keeps the crowd "
            "hyped. Program instructions: write for spoken delivery, keep segments "
            "concise, avoid bullet-point phrasing, avoid cliches, and maintain a "
            "believable, energetic radio flow between sections."
        ),
        segments=(
            _PresetSegment(
                segment_id="hype_intro",
                name="Hype intro",
                prompt=(
                    "The next track is <next_songinfo>. Open the party like a hype "
                    "radio host: high energy, one confident line about the song or "
                    "artist, and a clean handoff that gets people moving."
                ),
                web_search="disabled",
                max_chars=650,
                plays=_Plays("start"),
            ),
            _PresetSegment(
                segment_id="shout_out",
                name="Shout-out",
                prompt=(
                    "The previous track was <prev_songinfo> and the next track is "
                    "<next_songinfo>. Deliver a high-energy party transition with a "
                    "quick shout-out vibe: keep it fun, confident, and concise, and "
                    "avoid filler or repetition."
                ),
                web_search="allow",
                max_chars=650,
                plays=_Plays("every_n_songs", 3),
            ),
            _PresetSegment(
                segment_id="sign_off",
                name="Sign-off",
                prompt=(
                    "The last track played was <prev_songinfo>. Close the party with a "
                    "memorable, high-energy sign-off: brief hype recap, warm farewell, "
                    "and language that sounds like the end of a real party set."
                ),
                web_search="disabled",
                max_chars=650,
                plays=_Plays("end"),
            ),
        ),
    ),
)


class AIRadioHostsMixin:
    """Mixin with host persistence and normalization helpers."""

    if TYPE_CHECKING:
        mass: MusicAssistant
        logger: logging.Logger
        _hosts_file: Path
        _stations_file: Path
        _hosts: dict[str, dict[str, Any]]
        _sections: dict[str, dict[str, Any]]

    async def _load_hosts(self) -> None:
        """Load host profiles from disk."""
        hosts_file_exists = await asyncio.to_thread(self._hosts_file.exists)
        if not hosts_file_exists:
            self._hosts = {}
            return
        async with aiofiles.open(self._hosts_file) as file_handle:
            content = await file_handle.read()
        try:
            payload = await async_json_loads(content)
        except ValueError as err:
            # keep the corrupt file on disk for inspection; it is only
            # overwritten again once a host is saved
            self.logger.error("Hosts file is corrupt, starting without hosts: %s", err)
            payload = {}
        items = payload.get("hosts", []) if isinstance(payload, dict) else []
        parsed: dict[str, dict[str, Any]] = {}
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                try:
                    normalized = self._normalize_host(item)
                except Exception as err:
                    self.logger.warning("Skipping invalid host profile: %s", err)
                    continue
                parsed[normalized["id"]] = normalized
        self._hosts = parsed

    async def _write_hosts(self) -> None:
        """Persist host profiles to disk."""
        payload = {
            "version": 1,
            "hosts": sorted(self._hosts.values(), key=lambda item: item["name"]),
        }
        await self._write_json_file(self._hosts_file, payload)

    async def _seed_preset_hosts(self) -> None:
        """Seed the bundled preset hosts, but only on a fresh (never configured) install."""
        for storage_file in (self._hosts_file, self._stations_file):
            if await asyncio.to_thread(storage_file.exists):
                return
        for host, sections in self._default_preset_hosts():
            for section in sections:
                self._sections[section["id"]] = section
            self._hosts[host["id"]] = host
        await self._write_sections()
        await self._write_hosts()
        self.logger.info("Seeded %d preset hosts on this fresh install", len(PRESET_HOSTS))

    def _normalize_host(
        self,
        host: dict[str, Any],
        sections_map: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Validate and normalize a host profile."""
        known_sections = self._sections if sections_map is None else sections_map
        host_id = slugify(str(host.get("id", "")).strip() or str(host.get("name", "")).strip())
        name = str(host.get("name", "")).strip()
        if not name:
            raise InvalidDataError("Host name is required")

        instructions = str(host.get("instructions") or "").strip() or DEFAULT_LLM_INSTRUCTIONS
        tts_engine = str(host.get("tts_engine") or "").strip()

        section_ids: list[str] = []
        seen: set[str] = set()
        for item in host.get("section_ids", []):
            if not isinstance(item, (str, int)):
                continue
            section_id = str(item).strip()
            if not section_id or section_id in seen:
                continue
            seen.add(section_id)
            section_ids.append(section_id)
        if not section_ids:
            raise InvalidDataError("Host requires at least one section id")
        _sections, missing = self._materialize_sections(section_ids, known_sections)
        if missing:
            raise InvalidDataError(
                f"Host references unknown sections: {', '.join(sorted(set(missing)))}"
            )

        raw_section_order = host.get("section_order")
        if not isinstance(raw_section_order, list) or not raw_section_order:
            raise InvalidDataError("Host requires a non-empty 'section_order' list")
        self._validate_section_order(raw_section_order, set(section_ids))

        merge_section_id = str(host.get("merge_section_id", "")).strip()
        if merge_section_id:
            if merge_section_id not in section_ids:
                raise InvalidDataError("merge_section_id must be selected in host section_ids")
            merge_section = known_sections.get(merge_section_id)
            if not merge_section or str(merge_section.get("type", "")).strip().lower() != "ai_meta":
                raise InvalidDataError("merge_section_id must reference an ai_meta section")

        return {
            "id": host_id,
            "name": name,
            "instructions": instructions,
            "tts_engine": tts_engine,
            "section_ids": section_ids,
            "section_order": deepcopy(raw_section_order),
            "merge_section_id": merge_section_id,
        }

    def _default_host_template(self) -> dict[str, Any]:
        """Return the built-in host template."""
        default_sections = self._default_sections_template()
        default_section_ids = [item["id"] for item in default_sections]
        return {
            "id": "default_host",
            "name": "Default Host",
            "instructions": DEFAULT_LLM_INSTRUCTIONS,
            "tts_engine": "",
            "section_ids": default_section_ids,
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
            "merge_section_id": "Between_Songs_Smoother",
        }

    def _default_preset_hosts(self) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
        """Return the bundled preset hosts, each paired with the sections it references."""
        return [_compile_preset_host(preset) for preset in PRESET_HOSTS]

    def _migrate_stations_v2_to_v3(self, stations: list[dict[str, Any]]) -> None:
        """
        Extract host profiles out of v2 stations and slim the stations in place.

        :param stations: v2 station dicts, mutated in place to the v3 shape.
        """
        legacy_keys = ("general", "sections", "section_ids", "section_order", "merge_section_id")
        seen: dict[str, str] = {}
        for station in stations:
            try:
                for item in station.get("sections", []):
                    if not isinstance(item, dict):
                        continue
                    normalized_section = self._normalize_section(item)
                    if normalized_section["id"] not in self._sections:
                        self._sections[normalized_section["id"]] = normalized_section

                general_raw = station.get("general")
                general = general_raw if isinstance(general_raw, dict) else {}
                instructions = (
                    str(general.get("instructions") or "").strip() or DEFAULT_LLM_INSTRUCTIONS
                )
                section_ids = [
                    str(item).strip()
                    for item in station.get("section_ids", [])
                    if str(item).strip()
                ]
                if not section_ids:
                    section_ids = [
                        str(item.get("id", "")).strip()
                        for item in station.get("sections", [])
                        if isinstance(item, dict) and str(item.get("id", "")).strip()
                    ]
                section_order = station.get("section_order") or []
                merge_section_id = str(station.get("merge_section_id", "")).strip()
                fingerprint = json_dumps(
                    {
                        "instructions": instructions,
                        "section_ids": section_ids,
                        "section_order": section_order,
                        "merge_section_id": merge_section_id,
                    }
                )
                if fingerprint in seen:
                    host_id = seen[fingerprint]
                else:
                    host = self._normalize_host(
                        {
                            "id": f"{station.get('name', 'host')}_host",
                            "name": f"{str(station.get('name', 'Host')).strip()} Host",
                            "instructions": instructions,
                            "section_ids": section_ids,
                            "section_order": section_order,
                            "merge_section_id": merge_section_id,
                        }
                    )
                    # a second distinct persona landing on the same slug must not overwrite
                    # the first
                    while host["id"] in self._hosts:
                        host["id"] = f"{host['id']}_{len(self._hosts)}"
                    self._hosts[host["id"]] = host
                    host_id = host["id"]
                    seen[fingerprint] = host_id
                station["host_id"] = host_id
                for key in legacy_keys:
                    station.pop(key, None)
            except Exception as err:
                self.logger.warning(
                    "Skipping station %s during host migration: %s",
                    station.get("id") or station.get("name") or "<unknown>",
                    err,
                )


def _compile_preset_host(preset: _PresetHost) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build a host profile plus its sections from a preset, exactly as the frontend does."""
    sections: list[dict[str, Any]] = []
    start_flow: list[dict[str, Any]] = []
    between_flow: list[dict[str, Any]] = []
    end_flow: list[dict[str, Any]] = []
    for segment in preset.segments:
        # host-scoped id: sections live in one flat library, so two hosts must not
        # collide on e.g. both having an "intro"
        section_id = slugify(f"{preset.host_id}_{segment.segment_id}")
        sections.append(
            {
                "id": section_id,
                "name": segment.name,
                "type": "ai_text",
                "prompt": segment.prompt,
                "web_search": segment.web_search,
                "constraints": {"max_chars": segment.max_chars},
            }
        )
        match segment.plays.kind:
            case "start":
                start_flow.append({"MUST": section_id})
            case "end":
                end_flow.append({"MUST": section_id})
            case "every_song":
                between_flow.append({"MUST": section_id})
            case _:
                between_flow.append(_optional_flow_item(section_id, segment))

    section_order: list[dict[str, Any]] = []
    if start_flow:
        section_order.append({"when": "start_of_playlist", "flow": start_flow})
    if between_flow:
        section_order.append({"when": "between_songs", "flow": between_flow})
    if end_flow:
        section_order.append({"when": "end_of_playlist", "flow": end_flow})

    merge_section_id = f"{preset.host_id}_smoother"
    sections.append(
        {
            "id": merge_section_id,
            "name": "Between Songs Mix",
            "type": "ai_meta",
            "prompt": MERGE_SECTION_PROMPT,
        }
    )
    host = {
        "id": preset.host_id,
        "name": preset.name,
        "instructions": preset.instructions,
        "tts_engine": "",
        "section_ids": [section["id"] for section in sections],
        "section_order": section_order,
        "merge_section_id": merge_section_id,
    }
    return host, sections


def _optional_flow_item(section_id: str, segment: _PresetSegment) -> dict[str, Any]:
    """Translate a recurring segment's cadence into an OPTIONAL flow item with guards."""
    plays = segment.plays
    max_per_60min = 0
    if plays.kind == "every_n_songs":
        songs = max(1, plays.value)
        chance = min(1.0, 2 / songs)
        # the guard allows a section once song_global - last_event >= min_gap_songs, and
        # consecutive gaps are one song apart, so the cadence is the gap itself
        min_gap_songs = songs
    elif plays.kind == "every_n_min":
        minutes = max(1, plays.value)
        chance = 1.0
        min_gap_songs = 0
        max_per_60min = round(60 / minutes)
    else:
        chance = min(100, max(0, plays.value)) / 100
        min_gap_songs = 1
    return {
        "OPTIONAL": {
            "section": section_id,
            "chance": chance,
            "guards": {
                "min_gap_songs": min_gap_songs,
                "max_per_60min": max_per_60min,
                "require_placeholders_present": [
                    token for token in GUARD_PLACEHOLDER_TOKENS if token in segment.prompt
                ],
            },
        }
    }
