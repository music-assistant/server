"""Runtime execution mixin for AI Radio."""
# mypy: disable-error-code="attr-defined"

from __future__ import annotations

import asyncio
import datetime
import logging
import random
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiohttp import ClientTimeout
from music_assistant_models.enums import (
    EventType,
    ImageType,
    MediaType,
    PlaybackState,
)
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.media_items import (
    MediaItemImage,
    ProviderMapping,
    SoundEffect,
    UniqueList,
)

from music_assistant.controllers.player_queues.helpers import build_queue_item
from music_assistant.helpers.datetime import now, utc
from music_assistant.helpers.json import json_loads
from music_assistant.helpers.plugin_engines import resolve_ai_engine, resolve_tts_engine
from music_assistant.helpers.uri import create_uri

from .constants import (
    AI_QUERY_TIMEOUT_SECONDS,
    ATTR_HOST_ID,
    ATTR_MAX_CHARS,
    ATTR_PROMPT,
    ATTR_SESSION_ID,
    ATTR_STATION_ID,
    ATTR_WEB_SEARCH_MODE,
    CONF_AI_ENGINE,
    CONF_TIMEZONE,
    CONF_TTS_ENGINE,
    CONF_WEATHER_CITY,
    CONF_WEATHER_COUNTRY,
    CONF_WEATHER_PROVIDER,
    CONF_WEATHER_TIMEOUT,
    DEFAULT_LLM_INSTRUCTIONS,
    DEFAULT_WEATHER_PROVIDER,
    DEFAULT_WEATHER_TIMEOUT_SECONDS,
    DEFERRED_PLACEHOLDERS,
    SHOW_START_TIMEOUT_SECONDS,
    TTS_PRONUNCIATION_INSTRUCTIONS,
    VALID_WEB_SEARCH_MODES,
    WEB_SEARCH_MODE_RANK,
)
from .helpers import (
    build_slots,
    coerce_float,
    coerce_int,
    is_empty_section,
    pick_weighted_choice,
    slugify,
    track_songinfo,
    utc_now_iso,
)
from .models import (
    PlannedSection,
    SessionState,
    Slot,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.event import MassEvent
    from music_assistant_models.media_items import PlayableMediaItemType
    from music_assistant_models.queue_item import QueueItem

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.plugin import AIEngine, TTSEngine


class AIRadioRuntimeMixin:
    """Mixin with all runtime logic for AI Radio runs."""

    if TYPE_CHECKING:
        mass: MusicAssistant
        config: ProviderConfig
        logger: logging.Logger
        _sessions: dict[str, SessionState]

        def get_setup_value(self, key: str, default: ConfigValueType = None) -> ConfigValueType:
            """Return a value collected by this provider's setup flow."""

    def _set_session_progress(
        self,
        session: SessionState,
        phase: str,
        **details: Any,
    ) -> None:
        """Set progress payload with a stable phase key."""
        session.progress = {
            "phase": phase,
            # Keep legacy key for compatibility with older UI code.
            "step": phase,
            **details,
        }

    def _build_program(self, station: dict[str, Any], host: dict[str, Any]) -> dict[str, Any]:
        """Merge a station and its host into the dict the planner consumes."""
        sections, missing = self._materialize_sections(list(host.get("section_ids", [])))
        if missing:
            raise MusicAssistantError(
                f"Host references unknown sections: {', '.join(sorted(set(missing)))}"
            )
        return {
            **deepcopy(station),
            "host_id": str(host.get("id", "")),
            "instructions": str(host.get("instructions", "")),
            "tts_engine": str(host.get("tts_engine", "")),
            "sections": sections,
            "section_order": deepcopy(host.get("section_order", [])),
            "merge_section_id": str(host.get("merge_section_id", "")),
        }

    async def _run_session(self, session_id: str, program: dict[str, Any]) -> None:
        """Run one session in the background."""
        session = self._sessions[session_id]
        session.started_at = utc_now_iso()
        self.logger.info(
            "AI Radio run started: session=%s station=%s",
            session.session_id,
            session.station_id,
        )
        try:
            result = await self._run_show(session, program)
            session.result = result
            queue_stopped = result.get("ended_reason") == "queue_stopped"
            session.status = "stopped" if queue_stopped else "completed"
            self.logger.info(
                "AI Radio run %s: session=%s station=%s",
                session.status,
                session.session_id,
                session.station_id,
            )
        except asyncio.CancelledError:
            session.status = "stopped"
            self.logger.info(
                "AI Radio run cancelled: session=%s station=%s",
                session.session_id,
                session.station_id,
            )
            raise
        except Exception as err:
            session.status = "failed"
            session.error = str(err).strip() or err.__class__.__name__
            self.logger.exception("AI Radio session failed: %s", err)
        finally:
            session.ended_at = utc_now_iso()

    async def _run_show(
        self,
        session: SessionState,
        program: dict[str, Any],
    ) -> dict[str, Any]:
        """Plan and queue the whole show in one pass, then start playback."""
        program = deepcopy(program)
        self.logger.debug(
            "Show starting for station '%s' (%s)",
            program.get("name", "AI Radio"),
            program.get("id", ""),
        )
        self._set_session_progress(session, "fetch_source_tracks")
        # runtime_tokens only feeds the require_placeholders_present guards below; its
        # resolved text is discarded here and re-fetched fresh when each clip renders
        runtime_tokens = await self._prepare_runtime_tokens(program)
        player_id = str(program.get("default_player_id") or "").strip()
        if not player_id:
            raise MusicAssistantError("AI Radio requires a target player")
        if not self.mass.players.get_player(player_id):
            raise MusicAssistantError(f"Unknown target player: {player_id}")

        tracks, playlist_name = await self._fetch_source_tracks(program)
        tracks = self._apply_source_shuffle(tracks, program)
        tracks = self._apply_track_duration_limit(tracks, program)
        if not tracks:
            raise MusicAssistantError("No source tracks available after applying station limits")

        # a grouped player plays from the group leader's queue, so resolve the
        # active queue up front and target that one for queueing and polling
        queue_id = player_id
        active_queue = self.mass.player_queues.get_active_queue(player_id)
        if active_queue is not None:
            queue_id = str(active_queue.queue_id)
        self.mass.player_queues.clear(queue_id)
        session.queue_id = queue_id

        # a shuffled queue reorders each batch, scattering sections away from their tracks
        await self.mass.player_queues.set_shuffle(queue_id, False)

        cumulative_minutes = [0.0]
        for track in tracks:
            duration = track.get("duration")
            seconds = (
                float(duration) if isinstance(duration, (int, float)) and duration > 0 else 210.0
            )
            cumulative_minutes.append(cumulative_minutes[-1] + (seconds / 60.0))

        self._set_session_progress(session, "planning_sections", total_tracks=len(tracks))
        planned_sections, _history = self._plan_sections(
            session_id=session.session_id,
            tracks=tracks,
            station=program,
            track_index_offset=0,
            minute_offset=0.0,
            history_state={},
            allowed_slot_when=None,
            runtime_tokens=runtime_tokens,
        )
        queue_items = self._compose_queue_items(
            queue_id=queue_id,
            session=session,
            program=program,
            tracks=tracks,
            sections=planned_sections,
        )
        if not queue_items:
            raise MusicAssistantError("No queue entries were generated")

        self._set_session_progress(
            session,
            "initializing_queue",
            total_tracks=len(tracks),
            queue_entries=len(queue_items),
            queue_id=queue_id,
        )
        # load() stages the items without starting playback, so every clip already carries its
        # prompt by the time anything can ask for its audio
        await self.mass.player_queues.load(
            queue_id,
            queue_items=queue_items,
            keep_remaining=False,
            keep_played=False,
            shuffle=False,
        )
        await self.mass.player_queues.play_index(queue_id, 0)
        self._set_session_progress(
            session,
            "running",
            total_tracks=len(tracks),
            queue_entries=len(queue_items),
            queue_id=queue_id,
        )
        has_clips = any(ATTR_SESSION_ID in item.extra_attributes for item in queue_items)
        ended_reason = await self._await_show_end(
            session, queue_id, len(queue_items) - 1, has_clips=has_clips
        )
        return {
            "ended_reason": ended_reason,
            "source_playlist_name": playlist_name,
            "source_tracks": len(tracks),
            "queue_id": queue_id,
            "queue_entries": len(queue_items),
            "planned_sections": len(planned_sections),
            "skipped_sections": session.skipped_sections,
        }

    async def _await_show_end(
        self, session: SessionState, queue_id: str, last_index: int, *, has_clips: bool
    ) -> str:
        """
        Block until this session's show is over and report why it ended.

        :param session: The session whose clips are in the queue.
        :param queue_id: The queue playing the show.
        :param last_index: Queue index of the final entry this session enqueued.
        :param has_clips: Whether this run enqueued any AI Radio clips at all. A clip-free
            show (every section was skipped by its rules) must not be mistaken for one whose
            clips were cleared out from under it, so that rule is skipped entirely here.
        :return: ``"source_exhausted"`` when the show played out, ``"queue_stopped"`` when the
            queue was stopped or taken over before reaching the end.
        :raises MusicAssistantError: if playback never starts within
            :data:`SHOW_START_TIMEOUT_SECONDS`.
        """
        finished = asyncio.Event()
        playback_started = asyncio.Event()
        # a queue that has not started yet must never be mistaken for a stopped one
        playback_seen = False
        ended_reason = "queue_stopped"

        def _check_show_state() -> None:
            nonlocal playback_seen, ended_reason
            queue = self.mass.player_queues.get(queue_id)
            if queue is None:
                finished.set()
                return
            if queue.state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
                playback_seen = True
                playback_started.set()
            if has_clips and not self._session_has_clips(queue_id, session.session_id):
                finished.set()
                return
            if not playback_seen or queue.state != PlaybackState.IDLE:
                return
            # playing out and being stopped both end IDLE, so position is the discriminator
            current_index = queue.current_index
            if current_index is not None and current_index >= last_index:
                ended_reason = "source_exhausted"
            self.logger.info(
                "Queue %s went idle at index %s of %s, ending show (%s)",
                queue_id,
                current_index,
                last_index,
                ended_reason,
            )
            finished.set()

        def _on_queue_event(_event: MassEvent) -> None:
            _check_show_state()

        unsubscribe = self.mass.subscribe(
            _on_queue_event,
            (EventType.QUEUE_UPDATED, EventType.QUEUE_ITEMS_UPDATED, EventType.PLAYER_REMOVED),
            id_filter=queue_id,
        )
        try:
            # the queue may already have gone away, or (for a show with clips) already lost
            # them, by the time this subscribes; IDLE-after-playout still needs a fresh event,
            # since playback_seen is not latched yet
            _check_show_state()
            await self._await_playback_start(playback_started, finished)
            await finished.wait()
        finally:
            unsubscribe()
        return ended_reason

    async def _await_playback_start(
        self, playback_started: asyncio.Event, finished: asyncio.Event
    ) -> None:
        """
        Wait for the show to either start playing or end before it ever did.

        :param playback_started: Set once the queue is first observed playing or paused.
        :param finished: Set once the show is over, however that came about.
        :raises MusicAssistantError: if neither happens within
            :data:`SHOW_START_TIMEOUT_SECONDS`.
        """
        if playback_started.is_set() or finished.is_set():
            return
        # a player that never comes online (or whose clips all fail) must not pin this
        # session's "running" status, and its max-concurrent-runs slot, forever
        wait_tasks = (
            asyncio.ensure_future(playback_started.wait()),
            asyncio.ensure_future(finished.wait()),
        )
        try:
            done, _pending = await asyncio.wait(
                wait_tasks,
                timeout=SHOW_START_TIMEOUT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in wait_tasks:
                if not task.done():
                    task.cancel()
        if not done:
            raise MusicAssistantError(
                f"Playback did not start within {SHOW_START_TIMEOUT_SECONDS}s"
            )

    def _session_has_clips(self, queue_id: str, session_id: str) -> bool:
        """Return whether any queue item still belongs to the given session."""
        page_size = 500
        offset = 0
        while True:
            page = self.mass.player_queues.items(queue_id, limit=page_size, offset=offset)
            if not page:
                return False
            if any(item.extra_attributes.get(ATTR_SESSION_ID) == session_id for item in page):
                return True
            if len(page) < page_size:
                return False
            offset += page_size

    async def _fetch_source_tracks(
        self, station: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], str]:
        """Load and normalize source playlist tracks."""
        playlist_id = str(station.get("source_playlist_id", "")).strip()
        provider = str(station.get("source_playlist_provider", "library")).strip() or "library"
        if not playlist_id:
            raise MusicAssistantError("Station is missing source_playlist_id")

        playlist = await self.mass.music.playlists.get(playlist_id, provider)
        playlist_name = playlist.name
        tracks = [track async for track in self.mass.music.playlists.tracks(playlist_id, provider)]
        normalized: list[dict[str, Any]] = []
        for track in tracks:
            artist = ""
            track_artists = getattr(track, "artists", None)
            if isinstance(track_artists, list) and track_artists:
                artist = str(track_artists[0].name)
            uri = await self._track_to_uri(track)
            if not uri:
                self.logger.warning(
                    "Skipping source track with no resolvable uri: %s - %s (item_id=%s)",
                    artist,
                    track.name,
                    track.item_id,
                )
                continue
            normalized.append(
                {
                    "index": len(normalized),
                    "item_id": track.item_id,
                    "name": track.name,
                    "artist": artist,
                    "songinfo": f"{artist} - {track.name}".strip(" -"),
                    "duration": track.duration,
                    "uri": uri,
                    "media_item": track,
                }
            )
        return normalized, playlist_name

    async def _track_to_uri(self, track: PlayableMediaItemType) -> str:
        """Resolve a stable URI for a source track."""
        if track.uri:
            return track.uri
        ordered_mappings = sorted(
            track.provider_mappings,
            key=lambda mapping: mapping.quality,
            reverse=True,
        )
        for mapping in ordered_mappings:
            if not mapping.available:
                continue
            return create_uri(MediaType.TRACK, mapping.provider_instance, mapping.item_id)
        return ""

    def _apply_source_shuffle(
        self, tracks: list[dict[str, Any]], station: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Return the source tracks in random order when the station asks for it."""
        if not station.get("shuffle_source_tracks", True) or not tracks:
            return tracks
        indices = list(range(len(tracks)))
        random.Random().shuffle(indices)
        result: list[dict[str, Any]] = []
        for new_index, old_index in enumerate(indices):
            updated = deepcopy(tracks[old_index])
            updated["index"] = new_index
            updated["source_index"] = old_index
            result.append(updated)
        self.logger.info("Shuffled %d source tracks", len(result))
        return result

    def _apply_track_duration_limit(
        self, tracks: list[dict[str, Any]], station: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Truncate the given tracks to the configured playtime cap, preserving their order."""
        max_duration = float(station.get("max_duration_minutes", 0) or 0)
        if max_duration <= 0 or not tracks:
            return tracks
        chosen: list[int] = []
        total_minutes = 0.0
        for index, track in enumerate(tracks):
            duration = track.get("duration")
            seconds = (
                float(duration) if isinstance(duration, (int, float)) and duration > 0 else 210.0
            )
            chosen.append(index)
            total_minutes += seconds / 60.0
            if total_minutes > max_duration:
                break
        result: list[dict[str, Any]] = []
        for new_index, old_index in enumerate(chosen):
            updated = deepcopy(tracks[old_index])
            updated["index"] = new_index
            updated["source_index"] = old_index
            result.append(updated)
        self.logger.info(
            "Applied source playtime cap: %.1f min requested, %d -> %d tracks selected",
            max_duration,
            len(tracks),
            len(result),
        )
        return result

    def _plan_sections(  # noqa: PLR0915
        self,
        session_id: str,
        tracks: list[dict[str, Any]],
        station: dict[str, Any],
        track_index_offset: int,
        minute_offset: float,
        history_state: dict[str, list[tuple[int, float]]],
        allowed_slot_when: list[str] | None,
        runtime_tokens: dict[str, str],
    ) -> tuple[list[PlannedSection], dict[str, list[tuple[int, float]]]]:
        """Evaluate section rules and produce planning entries."""
        sections = station.get("sections", [])
        section_order = station.get("section_order", [])
        if not isinstance(sections, list) or not sections:
            raise MusicAssistantError("Station has no sections configured")
        if not isinstance(section_order, list) or not section_order:
            raise MusicAssistantError("Station has no section_order configured")

        section_by_id = {
            str(section.get("id", "")).strip(): section
            for section in sections
            if str(section.get("id", "")).strip()
        }
        slots = build_slots(tracks)
        history = {section_id: list(events) for section_id, events in history_state.items()}
        selected: list[tuple[str, Slot, dict[str, str]]] = []
        rng = random.Random()

        def register_event(section_id: str, slot: Slot) -> None:
            if is_empty_section(section_id):
                return
            song_local = slot.next_index if slot.next_index is not None else len(tracks)
            song_global = track_index_offset + song_local
            minute_global = minute_offset + slot.minute_mark
            history.setdefault(section_id, []).append((song_global, minute_global))

        for slot in slots:
            if allowed_slot_when and slot.when not in allowed_slot_when:
                continue
            matching_rules = [
                rule for rule in section_order if str(rule.get("when", "")).strip() == slot.when
            ]
            if not matching_rules:
                continue
            static, deferred = self._resolve_placeholders(
                station=station,
                tracks=tracks,
                slot=slot,
                runtime_tokens=runtime_tokens,
            )
            # guards may require a deferred token to be present, so they see the merged view;
            # only the static half is substituted into the stored prompt
            guard_values = {**deferred, **static}
            for rule in matching_rules:
                flow = rule.get("flow", [])
                if not isinstance(flow, list):
                    continue
                for flow_item in flow:
                    if not isinstance(flow_item, dict):
                        continue
                    if "MUST" in flow_item:
                        section_id = str(flow_item["MUST"]).strip()
                        if not section_id:
                            continue
                        if is_empty_section(section_id):
                            continue
                        selected.append((section_id, slot, static))
                        register_event(section_id, slot)
                        continue
                    if "ALTERNATIVE" in flow_item:
                        alternative = flow_item["ALTERNATIVE"]
                        if not isinstance(alternative, dict):
                            continue
                        section_id = pick_weighted_choice(alternative.get("choices", []), rng)
                        if is_empty_section(section_id):
                            continue
                        selected.append((section_id, slot, static))
                        register_event(section_id, slot)
                        continue
                    if "OPTIONAL" in flow_item:
                        optional = flow_item["OPTIONAL"]
                        if not isinstance(optional, dict):
                            continue
                        section_id = str(optional.get("section", "")).strip()
                        if not section_id:
                            continue
                        chance_raw = coerce_float(optional.get("chance"), 0.0)
                        chance = chance_raw / 100.0 if chance_raw > 1 else chance_raw
                        if rng.random() > chance:
                            continue
                        guards = optional.get("guards", {}) if isinstance(optional, dict) else {}
                        if not self._passes_optional_guards(
                            section_id=section_id,
                            guards=guards if isinstance(guards, dict) else {},
                            history=history,
                            slot=slot,
                            tracks=tracks,
                            placeholders=guard_values,
                            track_index_offset=track_index_offset,
                            minute_offset=minute_offset,
                        ):
                            continue
                        if is_empty_section(section_id):
                            continue
                        selected.append((section_id, slot, static))
                        register_event(section_id, slot)

        merge_section_id = str(station.get("merge_section_id", "")).strip()
        meta_section = section_by_id.get(merge_section_id) if merge_section_id else None
        grouped: dict[str, list[tuple[str, Slot, dict[str, str]]]] = defaultdict(list)
        for item in selected:
            section_id, slot, placeholders = item
            key = f"{slot.when}:{slot.at_index}"
            grouped[key].append((section_id, slot, placeholders))

        planned: list[PlannedSection] = []
        order_index = 0
        processed_keys: set[str] = set()
        for section_id, slot, placeholders in selected:
            key = f"{slot.when}:{slot.at_index}"
            grouped_items = grouped[key]
            if (
                len(grouped_items) > 1
                and slot.when == "between_songs"
                and meta_section
                and key not in processed_keys
            ):
                processed_keys.add(key)
                merged = self._build_meta_section_plan(
                    grouped_items=grouped_items,
                    meta_section=meta_section,
                    placeholders=placeholders,
                    order=order_index,
                    section_by_id=section_by_id,
                    session_id=session_id,
                )
                planned.append(merged)
                order_index += 1
                continue
            if key in processed_keys:
                continue
            section = section_by_id.get(section_id)
            if not section:
                continue
            if str(section.get("type", "ai_text")).strip().lower() != "ai_text":
                continue
            prompt = self._apply_placeholders(str(section.get("prompt", "")), placeholders)
            max_chars = int((section.get("constraints") or {}).get("max_chars", 0) or 0)
            if max_chars > 0:
                prompt += (
                    f"\n\nTarget length: around {max_chars} characters. It may exceed by up to "
                    "15% if needed to finish naturally. Never stop mid-sentence."
                )
            planned.append(
                PlannedSection(
                    order=order_index,
                    clip_id=f"{session_id}_{order_index:03d}",
                    section_id=section_id,
                    section_name=self._resolve_section_name(section, section_id),
                    when=slot.when,
                    insert_at_index=slot.at_index,
                    prompt=prompt,
                    max_chars=max_chars,
                    web_search_mode=self._resolve_web_search_mode(section, section_id),
                )
            )
            order_index += 1

        return planned, history

    def _passes_optional_guards(
        self,
        section_id: str,
        guards: dict[str, Any],
        history: dict[str, list[tuple[int, float]]],
        slot: Slot,
        tracks: list[dict[str, Any]],
        placeholders: dict[str, str],
        track_index_offset: int,
        minute_offset: float,
    ) -> bool:
        """Evaluate OPTIONAL section guards."""
        min_gap_songs = coerce_int(guards.get("min_gap_songs"), 0)
        max_per_60min = coerce_int(guards.get("max_per_60min"), 0)
        required_placeholders = guards.get("require_placeholders_present", [])
        events = history.get(section_id, [])
        song_local = slot.next_index if slot.next_index is not None else len(tracks)
        song_global = track_index_offset + song_local
        minute_global = minute_offset + slot.minute_mark

        if min_gap_songs > 0 and events:
            if song_global - events[-1][0] < min_gap_songs:
                return False
        if max_per_60min > 0:
            in_window = [event for event in events if (minute_global - event[1]) <= 60.0]
            if len(in_window) >= max_per_60min:
                return False
        if isinstance(required_placeholders, list):
            for token in required_placeholders:
                if not placeholders.get(str(token), "").strip():
                    return False
        return True

    def _build_meta_section_plan(
        self,
        grouped_items: list[tuple[str, Slot, dict[str, str]]],
        meta_section: dict[str, Any],
        placeholders: dict[str, str],
        order: int,
        section_by_id: dict[str, dict[str, Any]],
        session_id: str,
    ) -> PlannedSection:
        """Build a merged ai_meta section for one slot."""
        section_ids = [item[0] for item in grouped_items]
        slot = grouped_items[0][1]
        prompt_lines: list[str] = []
        total_max_chars = 0
        max_web_mode = "disabled"
        merged_names: list[str] = []
        for index, section_id in enumerate(section_ids, start=1):
            section = section_by_id.get(section_id, {})
            section_name = self._resolve_section_name(section, section_id)
            merged_names.append(section_name)
            prompt_base = self._apply_placeholders(str(section.get("prompt", "")), placeholders)
            max_chars = int((section.get("constraints") or {}).get("max_chars", 0) or 0)
            total_max_chars += max_chars
            prompt_lines.append(f"{index}. [{section_id}] {prompt_base}")
            mode = self._resolve_web_search_mode(section, section_id)
            if WEB_SEARCH_MODE_RANK[mode] > WEB_SEARCH_MODE_RANK[max_web_mode]:
                max_web_mode = mode

        meta_prompt = self._apply_placeholders(str(meta_section.get("prompt", "")), placeholders)
        prompt_block = "\n".join(prompt_lines)
        if "<section_drafts>" in meta_prompt:
            meta_prompt = meta_prompt.replace("<section_drafts>", prompt_block)
        else:
            meta_prompt = f"{meta_prompt}\n\nSection prompts:\n{prompt_block}\n"
        meta_prompt += (
            "\n\nCreate one single moderator script that naturally combines all requested parts. "
            "Return plain text only."
        )
        if total_max_chars > 0:
            meta_prompt += (
                f"\n\nTarget length: around {total_max_chars} characters total. It may exceed "
                "by up to 15% if needed to finish naturally. Never stop mid-sentence."
            )
        section_id = f"multi_{'_'.join(slugify(item) for item in section_ids)}"
        section_name = " + ".join(dict.fromkeys(merged_names))
        return PlannedSection(
            order=order,
            clip_id=f"{session_id}_{order:03d}",
            section_id=section_id,
            section_name=section_name,
            when=slot.when,
            insert_at_index=slot.at_index,
            prompt=meta_prompt,
            max_chars=total_max_chars,
            web_search_mode=max_web_mode,
        )

    def _compose_queue_items(
        self,
        queue_id: str,
        session: SessionState,
        program: dict[str, Any],
        tracks: list[dict[str, Any]],
        sections: list[PlannedSection],
    ) -> list[QueueItem]:
        """
        Build the queue items for a whole show.

        Clips carry their render state in ``extra_attributes`` from the moment they are built, so
        a clip is renderable as soon as the queue holds it.

        :param queue_id: The queue the items are built for.
        :param session: The session that owns the show.
        :param program: The station+host program being played.
        :param tracks: The normalized source tracks, in play order.
        :param sections: The planned sections to interleave between them.
        """
        sections_by_index: dict[int, list[PlannedSection]] = defaultdict(list)
        for item in sections:
            sections_by_index[item.insert_at_index].append(item)
        items: list[QueueItem] = []
        for index in range(len(tracks) + 1):
            for section in sorted(sections_by_index.get(index, []), key=lambda item: item.order):
                items.append(
                    self._section_to_clip_item(queue_id, session.session_id, program, section)
                )
            if index < len(tracks) and (media_item := tracks[index].get("media_item")) is not None:
                items.append(build_queue_item(queue_id, media_item))
        return items

    def _section_to_clip_item(
        self,
        queue_id: str,
        session_id: str,
        program: dict[str, Any],
        section: PlannedSection,
    ) -> QueueItem:
        """Build the queue item for a not-yet-rendered clip."""
        clip = SoundEffect(
            item_id=section.clip_id,
            provider=self.instance_id,
            name=section.section_name,
            provider_mappings={
                ProviderMapping(
                    item_id=section.clip_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        clip.metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=self._ai_radio_cover_image_path(),
                    provider="builtin",
                    remotely_accessible=False,
                )
            ]
        )
        queue_item = build_queue_item(queue_id, clip)
        # the section name already travels as the item's own name, so it is not duplicated here
        queue_item.extra_attributes.update(
            {
                ATTR_SESSION_ID: session_id,
                ATTR_STATION_ID: str(program.get("id") or ""),
                ATTR_HOST_ID: str(program.get("host_id") or ""),
                ATTR_PROMPT: section.prompt,
                ATTR_MAX_CHARS: section.max_chars,
                ATTR_WEB_SEARCH_MODE: section.web_search_mode,
            }
        )
        return queue_item

    @staticmethod
    def _ai_radio_cover_image_path() -> str:
        """Return the explicit AI Radio playlist cover image path."""
        return str(Path(__file__).with_name("air.png"))

    async def _prepare_runtime_tokens(self, program: dict[str, Any]) -> dict[str, str]:
        """Prepare runtime tokens (including weather placeholders) for one run."""
        if not self._program_uses_weather_placeholders(program):
            return {}
        return await self._prepare_weather_tokens()

    async def _prepare_weather_tokens(self) -> dict[str, str]:
        """Fetch and format weather placeholder tokens from the configured provider."""
        runtime_tokens: dict[str, str] = {}

        weather_provider = (
            str(self.config.get_value(CONF_WEATHER_PROVIDER) or DEFAULT_WEATHER_PROVIDER)
            .strip()
            .lower()
        )
        if weather_provider in {"", "none", "disabled", "off"}:
            return runtime_tokens
        if weather_provider != "open_meteo":
            self.logger.warning(
                "Unsupported weather provider '%s' for AI Radio station",
                weather_provider,
            )
            return runtime_tokens

        city, country = self._extract_location()
        if not city or not country:
            self.logger.warning(
                "Weather placeholders used but no location configured "
                "(set the weather_city/weather_country provider options)"
            )
            return runtime_tokens

        configured_timeout = self.config.get_value(CONF_WEATHER_TIMEOUT)
        timeout_seconds = max(5, coerce_int(configured_timeout, DEFAULT_WEATHER_TIMEOUT_SECONDS))
        try:
            weather_hourly, weather_daily = await self._fetch_open_meteo_weather(
                city=city,
                country=country,
                timeout_seconds=timeout_seconds,
            )
        except Exception as err:
            self.logger.warning(
                "Weather lookup failed for '%s, %s': %s",
                city,
                country,
                err,
            )
            return runtime_tokens

        if weather_hourly:
            runtime_tokens["<weather_hourly>"] = weather_hourly
        if weather_daily:
            runtime_tokens["<weather_daily>"] = weather_daily
        return runtime_tokens

    def _program_uses_weather_placeholders(self, program: dict[str, Any]) -> bool:
        """Return whether the program references weather placeholders."""
        weather_tokens = ("<weather_hourly>", "<weather_daily>")
        for section in program.get("sections", []):
            prompt = str(section.get("prompt", ""))
            if any(token in prompt for token in weather_tokens):
                return True

        for rule in program.get("section_order", []):
            flow = rule.get("flow", [])
            for item in flow:
                optional = item.get("OPTIONAL")
                if not optional:
                    continue
                guards = optional.get("guards", {})
                required = guards.get("require_placeholders_present", [])
                if any(str(token) in weather_tokens for token in required):
                    return True
        return False

    def _extract_location(self) -> tuple[str, str]:
        """Extract weather location (city/country) from the provider config."""
        city = str(self.config.get_value(CONF_WEATHER_CITY) or "").strip()
        country = str(self.config.get_value(CONF_WEATHER_COUNTRY) or "").strip()
        return city, country

    def _configured_now(self) -> datetime.datetime:
        """Return the current time in the configured timezone, falling back to host local time."""
        tz_name = str(self.config.get_value(CONF_TIMEZONE) or "").strip()
        if tz_name:
            try:
                return utc().astimezone(ZoneInfo(tz_name))
            except ZoneInfoNotFoundError, ValueError:
                # a typo must not take the run down, but it should not pass unnoticed either
                self.logger.warning(
                    "Ignoring invalid timezone %r, falling back to the host timezone", tz_name
                )
        return now()

    async def _fetch_open_meteo_weather(
        self,
        city: str,
        country: str,
        timeout_seconds: int,
    ) -> tuple[str, str]:
        """Fetch weather strings from Open-Meteo for weather placeholders."""
        geocode_params: dict[str, str | int] = {
            "name": city,
            "count": 10,
            "language": "en",
            "format": "json",
        }
        country_code = country.upper() if len(country) == 2 and country.isalpha() else ""
        if country_code:
            geocode_params["country"] = country_code
        geocode = await self._open_meteo_get_json(
            "https://geocoding-api.open-meteo.com/v1/search",
            geocode_params,
            timeout_seconds,
        )
        results = geocode.get("results", [])
        if not isinstance(results, list) or not results:
            raise MusicAssistantError(f"No geocoding result for {city}, {country}")

        selected = results[0]
        country_lc = country.lower()
        for candidate in results:
            if not isinstance(candidate, dict):
                continue
            candidate_country = str(candidate.get("country", "")).strip().lower()
            candidate_country_code = str(candidate.get("country_code", "")).strip().upper()
            if candidate_country and candidate_country == country_lc:
                selected = candidate
                break
            if country_code and candidate_country_code == country_code:
                selected = candidate
                break

        if not isinstance(selected, dict):
            raise MusicAssistantError(f"No valid geocoding result for {city}, {country}")
        latitude_value: object = selected.get("latitude")
        longitude_value: object = selected.get("longitude")
        if not isinstance(latitude_value, (int, float, str)) or not isinstance(
            longitude_value, (int, float, str)
        ):
            raise MusicAssistantError(
                f"Geocoding result for {city}, {country} has invalid coordinates"
            )
        try:
            lat = float(latitude_value)
            lon = float(longitude_value)
        except ValueError as err:
            raise MusicAssistantError(
                f"Geocoding result for {city}, {country} has invalid coordinates"
            ) from err
        timezone_name = str(selected.get("timezone") or "UTC")
        forecast = await self._open_meteo_get_json(
            "https://api.open-meteo.com/v1/forecast",
            {
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,apparent_temperature,weather_code",
                "hourly": "temperature_2m,precipitation_probability,weather_code",
                "daily": (
                    "temperature_2m_max,temperature_2m_min,"
                    "precipitation_probability_max,weather_code"
                ),
                "forecast_days": 3,
                "timezone": timezone_name,
            },
            timeout_seconds,
        )
        return self._format_weather_strings(forecast)

    async def _open_meteo_get_json(
        self,
        base_url: str,
        params: dict[str, Any],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        """Perform one Open-Meteo GET request."""
        async with self.mass.http_session.get(
            base_url,
            params=params,
            timeout=ClientTimeout(total=timeout_seconds),
        ) as response:
            payload = await response.read()
            if response.status >= 400:
                raise MusicAssistantError(
                    f"Open-Meteo request failed ({response.status}): "
                    f"{payload.decode(errors='ignore')}"
                )
        data = json_loads(payload)
        if not isinstance(data, dict):
            raise MusicAssistantError("Open-Meteo response is not a JSON object")
        return data

    def _format_weather_strings(self, payload: dict[str, Any]) -> tuple[str, str]:
        """Format Open-Meteo payload into weather placeholder strings."""
        hourly = payload.get("hourly", {})
        daily = payload.get("daily", {})
        current = payload.get("current", {})
        if not isinstance(hourly, dict):
            hourly = {}
        if not isinstance(daily, dict):
            daily = {}
        if not isinstance(current, dict):
            current = {}

        hourly_times = hourly.get("time", [])
        hourly_temp = hourly.get("temperature_2m", [])
        hourly_prec = hourly.get("precipitation_probability", [])
        if not isinstance(hourly_times, list):
            hourly_times = []
        if not isinstance(hourly_temp, list):
            hourly_temp = []
        if not isinstance(hourly_prec, list):
            hourly_prec = []

        current_time = str(current.get("time") or "").strip()
        start_index = 0
        if current_time and current_time in hourly_times:
            start_index = int(hourly_times.index(current_time))

        max_items = min(len(hourly_times), len(hourly_temp), len(hourly_prec))
        hourly_parts: list[str] = []
        for index in range(start_index, min(start_index + 6, max_items)):
            ts = str(hourly_times[index]).replace("T", " ")
            hourly_parts.append(
                f"{ts}: {self._format_number(hourly_temp[index])}C, "
                f"rain {self._format_number(hourly_prec[index])}%"
            )
        current_text = ""
        if current:
            current_text = (
                f"now {self._format_number(current.get('temperature_2m'))}C "
                f"(feels {self._format_number(current.get('apparent_temperature'))}C)"
            )
        weather_hourly = "; ".join(([current_text] if current_text else []) + hourly_parts)

        daily_times = daily.get("time", [])
        max_t = daily.get("temperature_2m_max", [])
        min_t = daily.get("temperature_2m_min", [])
        max_prec = daily.get("precipitation_probability_max", [])
        if not isinstance(daily_times, list):
            daily_times = []
        if not isinstance(max_t, list):
            max_t = []
        if not isinstance(min_t, list):
            min_t = []
        if not isinstance(max_prec, list):
            max_prec = []
        daily_parts: list[str] = []
        for index in range(min(len(daily_times), len(max_t), len(min_t), len(max_prec))):
            daily_parts.append(
                f"{daily_times[index]}: "
                f"{self._format_number(min_t[index])}-{self._format_number(max_t[index])}C, "
                f"rain {self._format_number(max_prec[index])}%"
            )
        weather_daily = "; ".join(daily_parts)
        return weather_hourly, weather_daily

    def _format_number(self, value: Any) -> str:
        """Format weather numeric values compactly for prompts."""
        try:
            numeric = float(value)
        except Exception:
            return str(value)
        if numeric.is_integer():
            return str(int(numeric))
        return f"{numeric:.1f}"

    def _resolve_placeholders(
        self,
        station: dict[str, Any],
        tracks: list[dict[str, Any]],
        slot: Slot,
        runtime_tokens: dict[str, str],
    ) -> tuple[dict[str, str], dict[str, str]]:
        """
        Resolve placeholders for one slot, split by when they are substituted.

        :param station: The station profile being planned.
        :param tracks: The track list the slot indexes into.
        :param slot: The insertion slot being filled.
        :param runtime_tokens: Weather tokens fetched for this run.
        :return: ``(static, deferred)`` — static values are fixed by the track order and are
            substituted at plan time; deferred values describe the moment of airing and are
            substituted at render time.
        """
        prev_track = tracks[slot.prev_index] if slot.prev_index is not None else None
        next_track = tracks[slot.next_index] if slot.next_index is not None else None
        very_next_track = tracks[slot.very_next_index] if slot.very_next_index is not None else None
        static = {
            "<prev_songinfo>": track_songinfo(prev_track),
            "<next_songinfo>": track_songinfo(next_track),
            "<very_next_songinfo>": track_songinfo(very_next_track),
        }
        deferred = dict.fromkeys(DEFERRED_PLACEHOLDERS, "")
        deferred["<timestamp>"] = self._configured_now().strftime("%Y-%m-%d %H:%M %Z")
        for key, value in runtime_tokens.items():
            if str(key) in DEFERRED_PLACEHOLDERS:
                deferred[str(key)] = str(value)
            else:
                static[str(key)] = str(value)
        return static, deferred

    def _apply_placeholders(self, prompt: str, values: dict[str, str]) -> str:
        """Apply placeholder replacements in a prompt."""
        text = prompt
        for key, value in values.items():
            text = text.replace(key, value)
        return text

    def _resolve_section_name(self, section: dict[str, Any], fallback_id: str) -> str:
        """Resolve section display name."""
        name = str(section.get("name", "")).strip()
        return name or fallback_id.replace("_", " ")

    def _resolve_web_search_mode(self, section: dict[str, Any], section_id: str) -> str:
        """Resolve and validate section web search mode."""
        mode = str(section.get("web_search", "disabled")).strip().lower()
        if mode not in VALID_WEB_SEARCH_MODES:
            raise MusicAssistantError(
                f"Invalid web_search mode '{mode}' in section '{section_id}'. "
                f"Allowed: {sorted(VALID_WEB_SEARCH_MODES)}"
            )
        return mode

    async def _generate_text(self, instructions: str, prompt: str, web_mode: str) -> str:
        """Generate one section text using the configured AI engine."""
        instructions = instructions.strip() or DEFAULT_LLM_INSTRUCTIONS
        query_parts: list[str] = []
        if instructions:
            query_parts.append(f"Program instructions:\n{instructions}")
        query_parts.append(f"Pronunciation rules:\n{TTS_PRONUNCIATION_INSTRUCTIONS}")
        # stated as a default so a station can still ask for another language in its instructions
        query_parts.append(
            "Unless the program instructions ask for another language, write the output "
            f"in the language matching the locale '{self.mass.metadata.locale}'."
        )
        if web_mode == "force":
            query_parts.append(
                "Web mode: force. Use current up-to-date information where relevant."
            )
        elif web_mode == "allow":
            query_parts.append(
                "Web mode: allow. Use current information if it improves the answer."
            )
        query_parts.append(
            f"Task: Write one concise spoken radio section.\n\n{prompt}\n\nReturn plain text only."
        )
        query = "\n\n".join(query_parts)
        engine = await self._get_ai_engine()
        self.logger.debug(
            "AI query prepared: engine=%s web_mode=%s query_chars=%d",
            engine.uid,
            web_mode,
            len(query),
        )
        try:
            async with asyncio.timeout(AI_QUERY_TIMEOUT_SECONDS) as query_timeout:
                response = await engine.provider.ai_query(query, engine_id=engine.id)
        except Exception as err:
            # expired() tells our own cap apart from a timeout raised inside the engine
            if isinstance(err, TimeoutError) and query_timeout.expired():
                raise MusicAssistantError(
                    f"AI engine '{engine.uid}' did not respond within {AI_QUERY_TIMEOUT_SECONDS}s"
                ) from err
            error_name = err.__class__.__name__
            error_text = str(err).strip()
            if error_name == "NotConnected":
                raise MusicAssistantError(
                    "AI engine "
                    f"'{engine.uid}' is not connected. Reconnect the provider "
                    "(for example Home Assistant) and retry."
                ) from err
            details = error_text or error_name
            raise MusicAssistantError(f"AI engine '{engine.uid}' query failed: {details}") from err
        if not response or not str(response).strip():
            raise MusicAssistantError(
                f"AI engine '{engine.uid}' returned an empty response for section text"
            )
        text = str(response).strip()
        self.logger.debug(
            "AI query response received: engine=%s chars=%d",
            engine.uid,
            len(text),
        )
        return text

    async def _get_ai_engine(self) -> AIEngine:
        """Return the engine used for AI_QUERY tasks, honouring the configured selection."""
        selected = cast("str | None", self.get_setup_value(CONF_AI_ENGINE))
        if engine := await resolve_ai_engine(self.mass, selected):
            return engine
        raise MusicAssistantError(
            "No AI engine available. Set up a plugin that provides AI (for example Home "
            "Assistant with an ai_task entity) and select it in the AI Radio settings."
        )

    async def _stop_session_queue(self, session: SessionState) -> None:
        """Stop playback of the queue a run was playing on."""
        queue_id = session.queue_id
        if queue_id is None:
            return
        queue = self.mass.player_queues.get(queue_id)
        if queue is None or getattr(queue, "state", None) == PlaybackState.IDLE:
            return
        try:
            await self.mass.player_queues.stop(queue_id)
        except MusicAssistantError as err:
            self.logger.debug("Could not stop queue %s: %s", queue_id, err)

    async def _get_tts_engine(self, engine_uid: str | None = None) -> TTSEngine:
        """
        Return the engine used for TTS tasks.

        :param engine_uid: A host-specific engine to prefer, falling back to the configured
            selection when unset or unavailable.
        """
        if engine_uid:
            if engine := await resolve_tts_engine(self.mass, engine_uid):
                return engine
            self.logger.warning(
                "Host TTS engine %s is unavailable, falling back to the provider default",
                engine_uid,
            )
        selected = cast("str | None", self.get_setup_value(CONF_TTS_ENGINE))
        if engine := await resolve_tts_engine(self.mass, selected):
            return engine
        raise MusicAssistantError(
            "No text-to-speech engine available. Set up a plugin that provides text-to-speech "
            "(for example Home Assistant with a TTS entity) and select it in the AI Radio "
            "settings."
        )
