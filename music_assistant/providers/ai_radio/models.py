"""Data models for AI Radio."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from music_assistant.helpers.datetime import utc

from .constants import RENDER_COOLDOWN_SECONDS, RENDER_FAILURES_BEFORE_COOLDOWN


@dataclass(slots=True)
class RenderHealth:
    """Consecutive render failures of one session and the cool-down they trigger."""

    failures: int = 0
    # loop time the cool-down ends; None while the session renders freely
    cooldown_until: float | None = None

    def cooling_down(self, now: float) -> bool:
        """Return whether the session must not render at the given loop time."""
        return self.cooldown_until is not None and now < self.cooldown_until

    def record_failure(self, now: float) -> bool:
        """Count a failed render and return whether that (re)starts the cool-down."""
        self.failures += 1
        if self.failures < RENDER_FAILURES_BEFORE_COOLDOWN:
            return False
        self.cooldown_until = now + RENDER_COOLDOWN_SECONDS
        return True

    def record_success(self) -> None:
        """Clear the failure count, the session renders freely again."""
        self.failures = 0
        self.cooldown_until = None


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
    clip_id: str
    section_id: str
    section_name: str
    when: str
    insert_at_index: int
    prompt: str
    max_chars: int
    web_search_mode: str
    # when true, a failed weather fetch skips the clip instead of airing it without a forecast
    weather_required: bool = False
    # when true, the section's break may be split so its tail plays over the next record's intro
    allow_post: bool = False
    # the guard history events this plan claimed, as (section_id, (song, minute)). a caller
    # that drops the plan can drop these too, so a clip that never aired carries no weight
    history_events: list[tuple[str, tuple[int, float]]] = field(default_factory=list)


@dataclass(slots=True)
class DJQueueState:
    """State container for one sticky queue DJ."""

    queue_id: str
    host_id: str
    dj_session_id: str
    clip_counter: int = 0
    songs_before_window: int = 0
    minutes_before_window: float = 0.0
    # queue_item_ids of the tracks whose preceding gap this session already settled, by
    # injecting a clip, by leaving it empty on purpose or because it became unusable
    decided_gap_ids: set[str] = field(default_factory=set)
    history: dict[str, list[tuple[int, float]]] = field(default_factory=dict)
    # a freshly armed state may only plan once the previous host's clips are cleared
    ready: bool = False
    replan_pending: bool = False
    render_health: RenderHealth = field(default_factory=RenderHealth, repr=False, compare=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)
    task: asyncio.Task[Any] | None = field(default=None, repr=False, compare=False)


@dataclass(slots=True)
class SessionState:
    """State container for an AI Radio run."""

    session_id: str
    station_id: str
    player_id: str = ""
    status: str = "running"
    created_at: str = field(default_factory=lambda: utc().isoformat())
    started_at: str | None = None
    ended_at: str | None = None
    progress: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    skipped_sections: int = 0
    last_render_error: str | None = None
    render_health: RenderHealth = field(default_factory=RenderHealth, repr=False, compare=False)
    task: asyncio.Task[Any] | None = field(default=None, repr=False, compare=False)
    queue_id: str | None = field(default=None, repr=False, compare=False)

    def as_dict(self) -> dict[str, Any]:
        """Return session as a serializable dictionary."""
        return {
            "session_id": self.session_id,
            "station_id": self.station_id,
            "queue_id": self.queue_id,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
            "skipped_sections": self.skipped_sections,
            "last_render_error": self.last_render_error,
        }
