"""Data models for AI Radio."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from music_assistant.helpers.datetime import utc


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
    uri: str
    duration: int = 0


@dataclass(slots=True)
class SessionState:
    """State container for an AI Radio run."""

    session_id: str
    station_id: str
    mode: str
    status: str = "running"
    created_at: str = field(default_factory=lambda: utc().isoformat())
    started_at: str | None = None
    ended_at: str | None = None
    progress: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    task: asyncio.Task[Any] | None = field(default=None, repr=False, compare=False)
    queue_id: str | None = field(default=None, repr=False, compare=False)

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
