"""Sticky per-queue AI DJ for AI Radio."""
# mypy: disable-error-code=attr-defined

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

import aiofiles
from music_assistant_models.enums import EventType
from music_assistant_models.errors import InvalidDataError

from music_assistant.helpers.json import async_json_loads

from .constants import ATTR_GAP_NEXT_ID, ATTR_QUEUE_DJ, ATTR_SESSION_ID
from .models import DJQueueState, PlannedSection, SessionState

if TYPE_CHECKING:
    from pathlib import Path

    from music_assistant_models.event import MassEvent
    from music_assistant_models.player_queue import PlayerQueue
    from music_assistant_models.queue_item import QueueItem

    from music_assistant.mass import MusicAssistant

# a track without a known duration still has to count for something in the minute
# bookkeeping, so it is billed as an average length song
FALLBACK_TRACK_SECONDS = 210

QUEUE_PAGE_SIZE = 500

# per section history cap, generous for the widest guard window (60 minutes)
HISTORY_EVENTS_PER_SECTION = 50

# result of one injection attempt. only "gap_gone" leaves its gap unserved: a gap that is
# too close to the player or already holds a clip is deliberately served or forever gone
DJInjectOutcome = Literal["injected", "gap_gone", "too_close", "occupied"]


class AIRadioQueueDJMixin:
    """Mixin managing sticky queue DJ state and clip injection."""

    if TYPE_CHECKING:
        mass: MusicAssistant
        logger: logging.Logger
        _hosts: dict[str, dict[str, Any]]
        _dj_queues: dict[str, DJQueueState]
        _sessions: dict[str, SessionState]
        _dj_file: Path
        _dj_lock: asyncio.Lock
        _unloading: bool

    async def set_queue_dj(self, queue_id: str, host_id: str | None) -> dict[str, str]:
        """
        Enable, switch or disable the sticky AI DJ on a queue.

        :param queue_id: The queue to change.
        :param host_id: The host to enable, or None to disable.
        :return: The full queue-to-host mapping after the change.
        """
        queue_id = str(queue_id).strip()
        if not queue_id:
            raise InvalidDataError("queue_id is required")
        armed: DJQueueState | None = None
        async with self._dj_lock:
            if host_id is None:
                self._dj_queues.pop(queue_id, None)
            else:
                host_id = str(host_id).strip()
                if host_id not in self._hosts:
                    raise InvalidDataError(f"Unknown host id: {host_id}")
                armed = self._arm_dj_state(queue_id, host_id)
            await self._write_queue_dj()
        # any pending clip other than the armed session's own carries a persona and voice
        # that is no longer the queue's, so it has to go before the replan can plant this
        # host's clips into the gaps it frees up
        await self._remove_pending_dj_clips(queue_id)
        if armed is not None:
            # only now may this state plan: a pass racing the cleanup above would see the
            # gaps the old clips still occupy, mark them served and leave them empty.
            # a newer switch may already have replaced us, and marks its own state
            if self._dj_queues.get(queue_id) is armed:
                armed.ready = True
            self._schedule_replan(queue_id)
        return await self.get_queue_dj_status()

    async def get_queue_dj_status(self) -> dict[str, str]:
        """Return the queue-to-host mapping of all active queue DJs."""
        return {queue_id: state.host_id for queue_id, state in self._dj_queues.items()}

    async def _load_queue_dj(self) -> None:
        """Load persisted queue DJ assignments and arm their states."""
        file_exists = await asyncio.to_thread(self._dj_file.exists)
        if not file_exists:
            self._dj_queues = {}
            return
        async with aiofiles.open(self._dj_file) as file_handle:
            content = await file_handle.read()
        try:
            payload = await async_json_loads(content)
        except ValueError as err:
            self.logger.error("Queue DJ file is corrupt, starting without queue DJs: %s", err)
            payload = {}
        queues = payload.get("queues", {}) if isinstance(payload, dict) else {}
        self._dj_queues = {}
        if isinstance(queues, dict):
            for queue_id, entry in queues.items():
                host_id = str(entry.get("host_id", "")).strip() if isinstance(entry, dict) else ""
                if host_id not in self._hosts:
                    self.logger.warning(
                        "Dropping queue DJ for %s: host %s no longer exists", queue_id, host_id
                    )
                    continue
                # no clip cleanup precedes a boot arm, so this state may plan right away
                self._arm_dj_state(str(queue_id), host_id).ready = True

    async def _write_queue_dj(self) -> None:
        """Persist queue DJ assignments to disk."""
        payload = {
            "version": 1,
            "queues": {
                queue_id: {"host_id": state.host_id}
                for queue_id, state in sorted(self._dj_queues.items())
            },
        }
        await self._write_json_file(self._dj_file, payload)

    def _arm_dj_state(self, queue_id: str, host_id: str) -> DJQueueState:
        """Create fresh in-memory DJ state for a queue."""
        # a fresh session id per arm keeps clip ids from colliding with clips
        # persisted in the queue by a previous run of this provider
        state = DJQueueState(
            queue_id=queue_id,
            host_id=host_id,
            dj_session_id=f"dj{uuid4().hex[:12]}",
        )
        self._dj_queues[queue_id] = state
        return state

    async def _on_dj_queue_event(self, event: MassEvent) -> None:
        """Handle queue and player events for the queues that run a DJ."""
        queue_id = str(event.object_id or "")
        if queue_id not in self._dj_queues:
            return
        if event.event == EventType.PLAYER_REMOVED:
            async with self._dj_lock:
                self._dj_queues.pop(queue_id, None)
                await self._write_queue_dj()
            self.logger.debug("Dropped queue DJ for removed player %s", queue_id)
            return
        self._schedule_replan(queue_id)

    def _schedule_replan(self, queue_id: str) -> None:
        """Request a replan pass for the given queue."""
        if self._unloading:
            return
        state = self._dj_queues.get(queue_id)
        if state is None or state.replan_pending:
            return
        state.replan_pending = True
        state.task = self.mass.create_task(
            self._drain_replans(queue_id), task_id=f"ai_radio_dj_replan_{queue_id}"
        )

    async def _drain_replans(self, queue_id: str) -> None:
        """Run replan passes until no further request landed during the last one."""
        # the inserts of a pass re-fire QUEUE_ITEMS_UPDATED while this task still holds the
        # replan task id, so its follow-up request is served here instead of by a new task
        while (state := self._dj_queues.get(queue_id)) is not None and state.replan_pending:
            if self._unloading:
                return
            try:
                await self._replan_queue(queue_id)
            except Exception:
                # clearing the request keeps a later event able to schedule a fresh task,
                # and returning keeps a permanently failing host from hot-looping here.
                # a re-arm mid-pass swapped in a new state, so clear the live one
                self.logger.exception("Queue DJ replan failed for %s", queue_id)
                if (live_state := self._dj_queues.get(queue_id)) is not None:
                    live_state.replan_pending = False
                return

    async def _replan_queue(self, queue_id: str) -> None:
        """Run one planning, injection and repair pass over a queue."""
        state = self._dj_queues.get(queue_id)
        if state is None:
            return
        async with state.lock:
            # cleared up front so an event landing mid-pass requests a fresh pass
            state.replan_pending = False
            if not state.ready:
                # a switch armed this state and is still clearing the previous host's clips.
                # planning now would mark the gaps those clips still hold as served, so this
                # request is dropped: the switch schedules its own pass once it is done
                self.logger.debug("Queue %s is waiting for its DJ switch cleanup", queue_id)
                return
            if any(
                session.status == "running" and session.queue_id == queue_id
                for session in self._sessions.values()
            ):
                # a show plans its own breaks into this queue and its clips carry no DJ
                # attribute, so injecting here would stack talk on top of talk
                self.logger.debug("Queue %s is running a show, skipping replan", queue_id)
                return
            queue = self.mass.player_queues.get(queue_id)
            if queue is None:
                # an unknown queue is usually one that has not registered yet (players
                # appear seconds after this provider loads), so the state is kept and the
                # QUEUE_ADDED event resumes injection. PLAYER_REMOVED is what drops it.
                self.logger.debug("Queue %s is not registered (yet), skipping replan", queue_id)
                return
            items = self._dj_queue_items(queue_id)
            guard_index = self._dj_guard_index(queue)
            if self._repair_dj_clips(queue_id, items, guard_index):
                items = self._dj_queue_items(queue_id)

            window = self._dj_window(items, guard_index, state.last_planned_item_id)
            if len(window) < 2:
                state.last_planned_item_id = window[-1].queue_item_id if window else None
                self.logger.debug(
                    "Queue %s has no plannable gap ahead of the player, skipping replan", queue_id
                )
                return
            # the planner counts songs and minutes from the first window track, so the
            # offsets have to be measured to that same point or the axis shifts between
            # passes and the OPTIONAL guards compare positions that never line up.
            # recomputed absolutely every pass, so a cleared or replaced queue self-corrects
            state.songs_before_window, state.minutes_before_window = self._dj_window_offsets(
                items, window[0].queue_item_id
            )
            host = self._hosts.get(state.host_id)
            if host is None:
                self.logger.warning(
                    "Disabling queue DJ on %s: host %s no longer exists", queue_id, state.host_id
                )
                async with self._dj_lock:
                    self._dj_queues.pop(queue_id, None)
                    await self._write_queue_dj()
                return

            window_tracks = [
                self._queue_item_to_track(index, item) for index, item in enumerate(window)
            ]
            program = self._build_program({"id": "", "name": f"AI DJ {host['name']}"}, host)
            runtime_tokens = await self._prepare_runtime_tokens(program)
            if self._dj_queues.get(queue_id) is not state:
                # a switch or disable replaced this queue's state while the fetch above
                # was in flight, so the session this pass planned for is gone
                return
            planned, history = self._plan_sections(
                session_id=state.dj_session_id,
                tracks=window_tracks,
                program=program,
                track_index_offset=state.songs_before_window,
                minute_offset=state.minutes_before_window,
                history_state=state.history,
                allowed_slot_when=["between_songs"],
                runtime_tokens=runtime_tokens,
            )
            # insert order is not load bearing: every clip resolves its own target index
            # by item id right before inserting. descending walks back from the queue tail
            injected = 0
            skipped: dict[str, int] = {}
            unserved_indexes: list[int] = []
            for section in sorted(planned, key=lambda item: item.insert_at_index, reverse=True):
                outcome = await self._inject_dj_clip(
                    queue_id=queue_id,
                    state=state,
                    program=program,
                    target=window_tracks[section.insert_at_index],
                    section=section,
                )
                if outcome == "injected":
                    injected += 1
                else:
                    skipped[outcome] = skipped.get(outcome, 0) + 1
                    if outcome == "gap_gone":
                        unserved_indexes.append(section.insert_at_index)
                if self._dj_queues.get(queue_id) is not state:
                    # same race caught mid-loop: one clip may already have landed for the
                    # replaced session, the rest must not follow it in
                    return
            # only the newest events matter to the guards (the last one for min_gap_songs,
            # a 60 minute window for max_per_60min), so the tail is dropped
            state.history = {
                section_id: events[-HISTORY_EVENTS_PER_SECTION:]
                for section_id, events in history.items()
            }
            if not unserved_indexes:
                state.last_planned_item_id = window_tracks[-1]["item_id"]
            elif (resume_index := min(unserved_indexes)) > 1:
                # anchor the next window on the track in front of the earliest unserved gap,
                # or the DJ goes silent for good on everything this pass left behind. an
                # index of 0 or 1 means the window already starts there, so the marker stays
                state.last_planned_item_id = window_tracks[resume_index - 1]["item_id"]
            self.logger.debug(
                "Replanned queue %s: window %s tracks, planned %s, injected %s, skipped %s",
                queue_id,
                len(window_tracks),
                len(planned),
                injected,
                skipped,
            )

    async def _inject_dj_clip(
        self,
        queue_id: str,
        state: DJQueueState,
        program: dict[str, Any],
        target: dict[str, Any],
        section: PlannedSection,
    ) -> DJInjectOutcome:
        """Insert one planned clip in front of its target track and report the outcome."""
        # both the target position and the guard are re-read here: planning awaits can take
        # seconds, in which the queue may have been reordered and the player buffered ahead
        queue = self.mass.player_queues.get(queue_id)
        if queue is None:
            return "gap_gone"
        guard_index = self._dj_guard_index(queue)
        target_index = self.mass.player_queues.index_by_id(queue_id, target["item_id"])
        if target_index is None:
            return "gap_gone"
        if target_index <= guard_index + 1:
            return "too_close"
        preceding = self.mass.player_queues.items(queue_id, limit=1, offset=target_index - 1)
        if preceding and preceding[0].extra_attributes.get(ATTR_QUEUE_DJ):
            return "occupied"
        # the planner numbers its clips from zero every pass, so the id comes from the
        # state counter instead to stay unique for the lifetime of the session
        section.clip_id = f"{state.dj_session_id}_{state.clip_counter:03d}"
        state.clip_counter += 1
        clip = self._section_to_clip_item(queue_id, state.dj_session_id, program, section)
        clip.extra_attributes[ATTR_QUEUE_DJ] = True
        clip.extra_attributes[ATTR_GAP_NEXT_ID] = target["item_id"]
        await self.mass.player_queues.load(
            queue_id,
            [clip],
            insert_at_index=target_index,
            keep_remaining=True,
            keep_played=True,
        )
        return "injected"

    async def _remove_pending_dj_clips(self, queue_id: str) -> None:
        """Remove not-yet-played DJ clips from the queue, except the armed session's own."""
        queue = self.mass.player_queues.get(queue_id)
        if queue is None:
            return
        # a queue runs exactly one DJ, so the live session is the only one whose clips may
        # stay: that keeps the clips a re-enable racing this cleanup already injected, while
        # clips of a session nothing remembers anymore (ids are re-rolled on every provider
        # load) are cleared instead of lingering in the gaps forever
        live_state = self._dj_queues.get(queue_id)
        keep_session_id = live_state.dj_session_id if live_state is not None else None
        guard_index = self._dj_guard_index(queue)
        removed = 0
        for item in self._dj_queue_items(queue_id)[guard_index + 1 :]:
            if not item.extra_attributes.get(ATTR_QUEUE_DJ):
                continue
            if (
                keep_session_id is not None
                and item.extra_attributes.get(ATTR_SESSION_ID) == keep_session_id
            ):
                continue
            self.mass.player_queues.delete_item(queue_id, item.queue_item_id)
            removed += 1
        if removed:
            self.logger.debug("Removed %s pending DJ clip(s) from queue %s", removed, queue_id)

    def _dj_guard_index(self, queue: PlayerQueue) -> int:
        """Return the highest queue index the player already owns."""
        # the player owns the current and the already buffered item, and the slot right
        # after the buffered one may be handed to the player at any moment
        current_index = queue.current_index if queue.current_index is not None else -1
        index_in_buffer = queue.index_in_buffer if queue.index_in_buffer is not None else -1
        return max(current_index, index_in_buffer)

    def _repair_dj_clips(self, queue_id: str, items: list[QueueItem], guard_index: int) -> bool:
        """Delete DJ clips that no longer sit in front of the track they announce."""
        deleted = 0
        for index in range(guard_index + 2, len(items)):
            item = items[index]
            if not item.extra_attributes.get(ATTR_QUEUE_DJ):
                continue
            successor = items[index + 1] if index + 1 < len(items) else None
            if successor is not None and successor.queue_item_id == item.extra_attributes.get(
                ATTR_GAP_NEXT_ID
            ):
                continue
            self.mass.player_queues.delete_item(queue_id, item.queue_item_id)
            deleted += 1
        if deleted:
            self.logger.debug("Repaired queue %s: deleted %s stale DJ clip(s)", queue_id, deleted)
        return bool(deleted)

    def _dj_window(
        self, items: list[QueueItem], guard_index: int, marker_item_id: str | None
    ) -> list[QueueItem]:
        """Return the upcoming music items that this pass may plan against."""
        upcoming = [
            item
            for item in items[guard_index + 1 :]
            if not item.extra_attributes.get(ATTR_QUEUE_DJ)
        ]
        if marker_item_id is not None:
            for position, item in enumerate(upcoming):
                if item.queue_item_id == marker_item_id:
                    # the marker itself is kept as the anchor of the gap behind it
                    return upcoming[position:]
        return upcoming

    def _dj_window_offsets(self, items: list[QueueItem], window_start_id: str) -> tuple[int, float]:
        """Return the songs and minutes of music playing before the first window track."""
        behind = []
        for item in items:
            if item.queue_item_id == window_start_id:
                break
            if not item.extra_attributes.get(ATTR_QUEUE_DJ):
                behind.append(item)
        minutes = sum(item.duration or FALLBACK_TRACK_SECONDS for item in behind) / 60.0
        return len(behind), minutes

    def _dj_queue_items(self, queue_id: str) -> list[QueueItem]:
        """Return all items of a queue."""
        items: list[QueueItem] = []
        offset = 0
        while True:
            page = self.mass.player_queues.items(queue_id, limit=QUEUE_PAGE_SIZE, offset=offset)
            items.extend(page)
            if len(page) < QUEUE_PAGE_SIZE:
                return items
            offset += QUEUE_PAGE_SIZE

    def _queue_item_to_track(self, index: int, item: QueueItem) -> dict[str, Any]:
        """Convert a music queue item into the track dict the planner consumes."""
        name = ""
        artist = ""
        if (media_item := item.media_item) is not None:
            name = str(media_item.name or "")
            artists = getattr(media_item, "artists", None)
            if artists:
                artist = str(artists[0].name)
        if not name:
            raw_name = str(item.name or "")
            artist, separator, name = raw_name.partition(" - ")
            if not separator:
                artist, name = "", raw_name
        return {
            "index": index,
            "item_id": item.queue_item_id,
            "name": name,
            "artist": artist,
            "songinfo": f"{artist} - {name}".strip(" -"),
            "duration": item.duration,
            "media_item": None,
        }
