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

# result of one splice attempt. only "gap_gone" leaves its gap unserved: a gap that already
# holds a clip is served, and one that slipped behind the player can never be served again
DJSpliceOutcome = Literal["injected", "gap_gone", "too_close", "occupied"]


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
        try:
            async with self._dj_lock:
                if host_id is None:
                    self._dj_queues.pop(queue_id, None)
                else:
                    host_id = str(host_id).strip()
                    if host_id not in self._hosts:
                        raise InvalidDataError(f"Unknown host id: {host_id}")
                    armed = self._arm_dj_state(queue_id, host_id)
                await self._write_queue_dj()
            # stale clips carry the old host's persona, so they must go before a replan
            # can reuse the gaps they occupy
            self._remove_pending_dj_clips(queue_id)
        except Exception:
            # an armed state that never reached its cleanup stays unready forever, which
            # reads as an armed DJ that never speaks. dropping it lets a retry arm cleanly
            if armed is not None:
                async with self._dj_lock:
                    if self._dj_queues.get(queue_id) is armed:
                        del self._dj_queues[queue_id]
            raise
        if armed is not None:
            if self._dj_queues.get(queue_id) is armed:
                # only if we're still the live state: a newer switch may have replaced us.
                # flipped after cleanup so a racing pass doesn't mark old clips' gaps served
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
                # cleared (not left pending) so a later event can retry without hot-looping
                # here; re-fetched since a re-arm mid-pass may have swapped in a new state
                self.logger.exception("Queue DJ replan failed for %s", queue_id)
                if (live_state := self._dj_queues.get(queue_id)) is not None:
                    live_state.replan_pending = False
                return

    async def _replan_queue(self, queue_id: str) -> None:  # noqa: PLR0915
        """Run one planning, injection and repair pass over a queue."""
        state = self._dj_queues.get(queue_id)
        if state is None:
            return
        async with state.lock:
            # cleared up front so an event landing mid-pass requests a fresh pass
            state.replan_pending = False
            if not state.ready:
                # a switch armed this state but hasn't finished clearing the old clips yet;
                # planning now would mark their gaps served. the switch replans once ready
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
                # usually the queue just hasn't registered yet (players appear seconds after
                # load); state is kept, QUEUE_ADDED resumes it, PLAYER_REMOVED is what drops it
                self.logger.debug("Queue %s is not registered (yet), skipping replan", queue_id)
                return
            items = self._dj_queue_items(queue_id)
            guard_index = self._dj_guard_index(queue)
            if self._repair_dj_clips(queue_id, state, items, guard_index):
                items = self._dj_queue_items(queue_id)
            # tracks that left the queue keep no decision, so the set cannot grow unbounded
            state.decided_gap_ids &= {item.queue_item_id for item in items}

            window = self._dj_window(items, guard_index)
            if len(window) < 2:
                self.logger.debug(
                    "Queue %s has no plannable gap ahead of the player, skipping replan", queue_id
                )
                return
            # measured from the same point the planner counts from, or OPTIONAL guard
            # positions drift between passes. recomputed every pass so state self-corrects
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
            # every gap the planner is about to evaluate, so gaps where chance or a guard
            # picks nothing count as decided too instead of being rolled again next pass
            evaluated_gap_ids = {
                str(track["item_id"])
                for track in window_tracks[1:]
                if track["item_id"] not in state.decided_gap_ids
            }
            planned, history = self._plan_sections(
                session_id=state.dj_session_id,
                tracks=window_tracks,
                program=program,
                track_index_offset=state.songs_before_window,
                minute_offset=state.minutes_before_window,
                history_state=state.history,
                allowed_slot_when=["between_songs"],
                runtime_tokens=runtime_tokens,
                decided_next_item_ids=state.decided_gap_ids,
            )
            # spliced into a working copy and applied as one update; a call per clip would
            # flood every client with queue events
            working = self._dj_queue_items(queue_id)
            # re-read: the planning awaits above can take seconds, letting the player buffer
            # further ahead
            guard_index = self._dj_guard_index(queue)
            # insert order is not load bearing: every clip resolves its own target position
            # in the working copy. descending walks back from the queue tail
            injected = 0
            skipped: dict[str, int] = {}
            rejected: list[PlannedSection] = []
            for section in sorted(planned, key=lambda item: item.insert_at_index, reverse=True):
                target = window_tracks[section.insert_at_index]
                outcome = self._splice_dj_clip(
                    queue_id=queue_id,
                    items=working,
                    guard_index=guard_index,
                    state=state,
                    program=program,
                    target=target,
                    section=section,
                )
                if outcome == "injected":
                    injected += 1
                else:
                    rejected.append(section)
                    skipped[outcome] = skipped.get(outcome, 0) + 1
                    if outcome == "gap_gone":
                        # the target moved or left the queue, so nothing was decided about
                        # its gap and a later pass has to look at it again
                        evaluated_gap_ids.discard(str(target["item_id"]))
            if injected:
                self.mass.player_queues.update_items(queue_id, working)
            # a rejected clip never airs, so its guard event must be removed: left in, it
            # would block its own successor for a full guard window and double-count later
            for section in rejected:
                for section_id, event in section.history_events:
                    if (events := history.get(section_id)) and event in events:
                        events.remove(event)
            # only the newest events matter to the guards (the last one for min_gap_songs,
            # a 60 minute window for max_per_60min), so the tail is dropped
            state.history = {
                section_id: events[-HISTORY_EVENTS_PER_SECTION:]
                for section_id, events in history.items()
            }
            state.decided_gap_ids |= evaluated_gap_ids
            self.logger.debug(
                "Replanned queue %s: window %s tracks, %s open gaps, planned %s, injected %s, "
                "skipped %s, decided %s",
                queue_id,
                len(window_tracks),
                len(evaluated_gap_ids),
                len(planned),
                injected,
                skipped,
                len(state.decided_gap_ids),
            )

    def _splice_dj_clip(
        self,
        queue_id: str,
        items: list[QueueItem],
        guard_index: int,
        state: DJQueueState,
        program: dict[str, Any],
        target: dict[str, Any],
        section: PlannedSection,
    ) -> DJSpliceOutcome:
        """Insert one planned clip in front of its target track and report the outcome."""
        target_index = next(
            (index for index, item in enumerate(items) if item.queue_item_id == target["item_id"]),
            None,
        )
        if target_index is None:
            return "gap_gone"
        if target_index <= guard_index + 1:
            return "too_close"
        if items[target_index - 1].extra_attributes.get(ATTR_QUEUE_DJ):
            return "occupied"
        # the planner numbers its clips from zero every pass, so the id comes from the
        # state counter instead to stay unique for the lifetime of the session
        section.clip_id = f"{state.dj_session_id}_{state.clip_counter:03d}"
        state.clip_counter += 1
        clip = self._section_to_clip_item(queue_id, state.dj_session_id, program, section)
        clip.extra_attributes[ATTR_QUEUE_DJ] = True
        clip.extra_attributes[ATTR_GAP_NEXT_ID] = target["item_id"]
        # sharing the target's sort index keeps the clip in front of the track it announces
        # when the queue is un-shuffled, without renumbering everything behind it
        clip.sort_index = items[target_index].sort_index
        items.insert(target_index, clip)
        return "injected"

    def _remove_pending_dj_clips(self, queue_id: str) -> None:
        """Remove not-yet-played DJ clips from the queue, except the armed session's own."""
        # await-free on purpose: nothing can mutate the queue between the snapshot below and
        # the update that applies the filtered list
        queue = self.mass.player_queues.get(queue_id)
        if queue is None:
            return
        # only the live session's clips survive, so a re-enable racing this cleanup keeps
        # its own; clips from a session nothing remembers (ids re-roll each load) are cleared
        live_state = self._dj_queues.get(queue_id)
        keep_session_id = live_state.dj_session_id if live_state is not None else None
        guard_index = self._dj_guard_index(queue)
        items = self._dj_queue_items(queue_id)
        # one update for the whole cleanup: a delete per clip floods every connected client
        # with queue events. items up to the guard are what the player already owns
        kept = [
            item
            for index, item in enumerate(items)
            if index <= guard_index
            or not item.extra_attributes.get(ATTR_QUEUE_DJ)
            or (
                keep_session_id is not None
                and item.extra_attributes.get(ATTR_SESSION_ID) == keep_session_id
            )
        ]
        if (removed := len(items) - len(kept)) == 0:
            return
        self.mass.player_queues.update_items(queue_id, kept)
        self.logger.debug("Removed %s pending DJ clip(s) from queue %s", removed, queue_id)

    def _dj_guard_index(self, queue: PlayerQueue) -> int:
        """Return the highest queue index the player already owns."""
        # the player owns the current and the already buffered item, and the slot right
        # after the buffered one may be handed to the player at any moment
        # not the shared committed_index: this guard needs the furthest index reached in list
        # order, where that helper follows playback order and wraps back to the front on repeat
        current_index = queue.current_index if queue.current_index is not None else -1
        index_in_buffer = queue.index_in_buffer if queue.index_in_buffer is not None else -1
        return max(current_index, index_in_buffer)

    def _repair_dj_clips(
        self, queue_id: str, state: DJQueueState, items: list[QueueItem], guard_index: int
    ) -> bool:
        """Delete DJ clips that no longer sit in front of the track they announce."""
        stale_ids: set[str] = set()
        for index in range(guard_index + 2, len(items)):
            item = items[index]
            if not item.extra_attributes.get(ATTR_QUEUE_DJ):
                continue
            successor = items[index + 1] if index + 1 < len(items) else None
            if successor is not None and successor.queue_item_id == item.extra_attributes.get(
                ATTR_GAP_NEXT_ID
            ):
                continue
            stale_ids.add(item.queue_item_id)
            # the gap this clip was serving is open again, so let a later pass decide it anew
            if (gap_next_id := item.extra_attributes.get(ATTR_GAP_NEXT_ID)) is not None:
                state.decided_gap_ids.discard(str(gap_next_id))
        if not stale_ids:
            return False
        # one update for all of them, so the clients see a single queue change
        self.mass.player_queues.update_items(
            queue_id, [item for item in items if item.queue_item_id not in stale_ids]
        )
        self.logger.debug(
            "Repaired queue %s: deleted %s stale DJ clip(s)", queue_id, len(stale_ids)
        )
        return True

    def _dj_window(self, items: list[QueueItem], guard_index: int) -> list[QueueItem]:
        """Return the upcoming music items that this pass may plan against."""
        # every upcoming track, decided or not: the planner counts songs and minutes over a
        # contiguous run, and per gap decisions are what keeps the work from being redone
        return [
            item
            for item in items[guard_index + 1 :]
            if not item.extra_attributes.get(ATTR_QUEUE_DJ)
        ]

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
