"""Utility helpers for AI Radio."""

from __future__ import annotations

import random
import re
from typing import Any

from music_assistant_models.errors import MusicAssistantError

from music_assistant.helpers.datetime import utc

from .constants import EMPTY_SECTION_ID
from .models import Slot


def utc_now_iso() -> str:
    """Return a UTC ISO timestamp."""
    return utc().isoformat()


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
        raise MusicAssistantError("ALTERNATIVE has no valid section choices")
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
    except TypeError, ValueError:
        return default


def coerce_int(value: Any, default: int) -> int:
    """Convert arbitrary value to int with a safe fallback."""
    try:
        return int(value)
    except TypeError, ValueError:
        return default
