"""Data models and utility helpers for AI Radio."""

from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import mutagen.id3 as mutagen_id3

from .constants import EMPTY_SECTION_ID


class AIRadioError(RuntimeError):
    """Raised when AI Radio encounters an unrecoverable error."""


def utc_now_iso() -> str:
    """Return a UTC ISO timestamp."""
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class Slot:
    """Insertion slot between source tracks."""

    when: str
    at_index: int
    prev_index: int | None
    next_index: int | None
    very_next_index: int | None
    minute_mark: float


@dataclass(slots=True)
class PlannedSection:
    """A section that should be generated for a run."""

    order: int
    section_id: str
    section_name: str
    when: str
    insert_at_index: int
    prompt: str
    max_chars: int
    web_search_mode: str


@dataclass(slots=True)
class GeneratedSection:
    """A generated section with final text."""

    order: int
    section_id: str
    section_name: str
    when: str
    insert_at_index: int
    text: str


@dataclass(slots=True)
class AudioSection:
    """Audio artifact for a generated section."""

    order: int
    section_id: str
    section_name: str
    insert_at_index: int
    file_path: str
    uri: str


@dataclass(slots=True)
class SessionState:
    """State container for an AI Radio run."""

    session_id: str
    station_id: str
    mode: str
    status: str = "running"
    created_at: str = field(default_factory=utc_now_iso)
    started_at: str | None = None
    ended_at: str | None = None
    progress: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    task: asyncio.Task[Any] | None = field(default=None, repr=False, compare=False)

    def as_dict(self) -> dict[str, Any]:
        """Return session as a serializable dictionary."""
        return {
            "session_id": self.session_id,
            "station_id": self.station_id,
            "mode": self.mode,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
        }


def slugify(value: str) -> str:
    """Create a slug from arbitrary text."""
    text = value.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = text.strip("_")
    return text or "station"


def is_empty_section(section_id: str) -> bool:
    """Return True when this section acts as a no-op marker."""
    return section_id.strip().upper() == EMPTY_SECTION_ID


def track_songinfo(track: dict[str, Any] | None) -> str:
    """Return a display string for a track dictionary."""
    if not track:
        return ""
    value = str(track.get("songinfo") or "").strip()
    if value:
        return value
    artist = str(track.get("artist") or "").strip()
    name = str(track.get("name") or "").strip()
    return f"{artist} - {name}".strip(" -")


def pick_weighted_choice(choices: list[dict[str, Any]], rng: random.Random) -> str:
    """Pick one ALTERNATIVE section using weighted randomness."""
    valid: list[tuple[str, float]] = []
    for choice in choices:
        section_id = str(choice.get("section", "")).strip()
        weight = float(choice.get("weight", 1))
        if section_id and weight > 0:
            valid.append((section_id, weight))
    if not valid:
        raise AIRadioError("ALTERNATIVE has no valid section choices")
    total = sum(weight for _, weight in valid)
    target = rng.random() * total
    cursor = 0.0
    for section_id, weight in valid:
        cursor += weight
        if target <= cursor:
            return section_id
    return valid[-1][0]


def build_slots(tracks: list[dict[str, Any]]) -> list[Slot]:
    """Build insertion slots from a source track list."""
    if not tracks:
        return []

    cumulative_minutes = [0.0]
    total = 0.0
    for track in tracks:
        duration = track.get("duration")
        seconds = float(duration) if isinstance(duration, (int, float)) and duration > 0 else 210.0
        total += seconds / 60.0
        cumulative_minutes.append(total)

    slots: list[Slot] = []
    slots.append(
        Slot(
            when="start_of_playlist",
            at_index=0,
            prev_index=None,
            next_index=0,
            very_next_index=1 if len(tracks) > 1 else None,
            minute_mark=0.0,
        )
    )
    for index in range(len(tracks) - 1):
        slots.append(
            Slot(
                when="between_songs",
                at_index=index + 1,
                prev_index=index,
                next_index=index + 1,
                very_next_index=index + 2 if index + 2 < len(tracks) else None,
                minute_mark=cumulative_minutes[index + 1],
            )
        )
    slots.append(
        Slot(
            when="end_of_playlist",
            at_index=len(tracks),
            prev_index=len(tracks) - 1,
            next_index=None,
            very_next_index=None,
            minute_mark=cumulative_minutes[-1],
        )
    )
    return slots


def soft_limit_text(text: str, max_chars: int, tolerance_ratio: float = 0.15) -> str:
    """Trim generated text softly near sentence boundaries."""
    if max_chars <= 0:
        return text.strip()
    slack = max(30, int(max_chars * tolerance_ratio))
    hard_limit = max_chars + slack
    cleaned = text.strip()
    if len(cleaned) <= hard_limit:
        return cleaned

    candidate = cleaned[:hard_limit].rstrip()
    sentence_ends = [match.end() for match in re.finditer(r"[.!?](?:\s|$)", candidate)]
    if sentence_ends:
        after_target = [index for index in sentence_ends if index >= max_chars]
        if after_target:
            return candidate[: after_target[0]].strip()
        return candidate[: sentence_ends[-1]].strip()

    last_space = candidate.rfind(" ")
    if last_space > 0:
        return candidate[:last_space].rstrip()
    return candidate


def coerce_float(value: Any, default: float) -> float:
    """Convert arbitrary value to float with a safe fallback."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def coerce_int(value: Any, default: int) -> int:
    """Convert arbitrary value to int with a safe fallback."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def write_id3_tags(mp3_path: str, title: str, artist: str) -> None:
    """Write simple ID3 metadata to an MP3 file."""
    id3: Any = mutagen_id3
    try:
        tags = id3.ID3(mp3_path)
    except id3.ID3NoHeaderError:
        tags = id3.ID3()
    tags.delall("TIT2")
    tags.add(id3.TIT2(encoding=1, text=title))
    tags.delall("TPE1")
    tags.add(id3.TPE1(encoding=1, text=artist))
    tags.delall("TPE2")
    tags.add(id3.TPE2(encoding=1, text=artist))
    tags.delall("TALB")
    tags.add(id3.TALB(encoding=1, text="AI Radio Sections"))
    tags.save(mp3_path, v2_version=3)
