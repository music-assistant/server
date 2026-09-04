"""Data models for AI Radio."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


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
    # the guard history events this plan claimed, as (section_id, (song, minute)). a caller
    # that drops the plan can drop these too, so a clip that never aired carries no weight
    history_events: list[tuple[str, tuple[int, float]]] = field(default_factory=list)


@dataclass(slots=True)
class DJQueueState:
    """State container for one sticky queue DJ."""

    queue_id: str
    host_id: str
    dj_session_id: str
    # non-empty when this DJ was auto-armed by playing a show's radio item. In-memory only:
    # after a restart it is re-derived from the queue's sources (see queue_dj)
    station_id: str = ""
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
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)
    task: asyncio.Task[Any] | None = field(default=None, repr=False, compare=False)
