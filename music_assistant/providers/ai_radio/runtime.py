"""Runtime execution mixin for AI Radio."""
# mypy: disable-error-code="attr-defined"

from __future__ import annotations

import asyncio
import logging
import random
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

from aiohttp import ClientTimeout
from music_assistant_models.enums import (
    ImageType,
    MediaType,
    ProviderFeature,
    QueueOption,
    TaskStatus,
)
from music_assistant_models.errors import MusicAssistantError

from music_assistant.helpers.datetime import utc
from music_assistant.helpers.json import json_loads
from music_assistant.helpers.playlists import (
    ImageInfo,
    PlaylistItem,
    ProviderMappingInfo,
    generate_m3u,
    media_item_to_playlist_item,
)
from music_assistant.helpers.uri import create_uri

from .constants import (
    DEFAULT_LLM_INSTRUCTIONS,
    DEFAULT_WEATHER_PROVIDER,
    DEFAULT_WEATHER_TIMEOUT_SECONDS,
    VALID_WEB_SEARCH_MODES,
    WEB_SEARCH_MODE_RANK,
)
from .models import (
    AudioSection,
    GeneratedSection,
    PlannedSection,
    SessionState,
    Slot,
    build_slots,
    coerce_float,
    coerce_int,
    is_empty_section,
    pick_weighted_choice,
    slugify,
    soft_limit_text,
    track_songinfo,
    utc_now_iso,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.media_items import PlayableMediaItemType

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.plugin import PluginProvider


class AIRadioRuntimeMixin:
    """Mixin with all runtime logic for AI Radio runs."""

    if TYPE_CHECKING:
        mass: MusicAssistant
        config: ProviderConfig
        logger: logging.Logger
        _sessions: dict[str, SessionState]

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

    async def _run_session(self, session_id: str, station: dict[str, Any]) -> None:
        """Run one session in the background."""
        session = self._sessions[session_id]
        session.started_at = utc_now_iso()
        self.logger.info(
            "AI Radio run started: session=%s station=%s mode=%s",
            session.session_id,
            session.station_id,
            session.mode,
        )
        try:
            if session.mode == "playlist":
                result = await self._run_playlist_mode(session, station)
            else:
                result = await self._run_dynamic_mode(session, station)
            session.result = result
            session.status = "completed"
            self.logger.info(
                "AI Radio run completed: session=%s station=%s mode=%s",
                session.session_id,
                session.station_id,
                session.mode,
            )
        except asyncio.CancelledError:
            session.status = "stopped"
            self.logger.info(
                "AI Radio run cancelled: session=%s station=%s mode=%s",
                session.session_id,
                session.station_id,
                session.mode,
            )
            raise
        except Exception as err:
            session.status = "failed"
            session.error = str(err).strip() or err.__class__.__name__
            self.logger.exception("AI Radio session failed: %s", err)
        finally:
            session.ended_at = utc_now_iso()

    async def _run_playlist_mode(
        self,
        session: SessionState,
        station: dict[str, Any],
    ) -> dict[str, Any]:
        """Generate one target playlist run."""
        station = deepcopy(station)
        self.logger.debug(
            "Playlist mode starting for station '%s' (%s)",
            station.get("name", "AI Radio"),
            station.get("id", ""),
        )
        self._set_session_progress(session, "fetch_source_tracks")
        runtime_tokens = await self._prepare_runtime_tokens(station)
        tracks, playlist_name = await self._fetch_source_tracks(station)
        tracks = self._apply_track_duration_limit(tracks, station)
        if not tracks:
            raise MusicAssistantError("No source tracks available after applying station limits")
        self._set_session_progress(
            session,
            "planning_sections",
            source_tracks=len(tracks),
        )

        planned_sections, _history = self._plan_sections(
            tracks=tracks,
            station=station,
            track_index_offset=0,
            minute_offset=0.0,
            history_state={},
            allowed_slot_when=None,
            runtime_tokens=runtime_tokens,
        )
        self._set_session_progress(
            session,
            "generating_llm",
            source_tracks=len(tracks),
            sections_planned=len(planned_sections),
        )
        generated_sections = await self._generate_sections(station, planned_sections)
        self._set_session_progress(
            session,
            "generating_tts",
            source_tracks=len(tracks),
            sections=len(generated_sections),
        )
        target_provider = str(station.get("target_playlist_provider") or "builtin")
        run_id = session.session_id[:8]
        station_name = str(station.get("name") or "AI Radio").strip()
        timezone_name = str(station.get("general", {}).get("timezone") or "UTC")
        now_utc = utc()
        try:
            now_local = now_utc.astimezone(ZoneInfo(timezone_name))
        except Exception:
            now_local = now_utc
        date_suffix = f"{now_local.strftime('%a')}. {now_local.strftime('%d.%m.')}"
        target_playlist_name = f"AI Radio: {station_name} ({date_suffix}) [{run_id}]"

        audio_sections = await self._synthesize_sections(generated_sections=generated_sections)
        if self._is_builtin_provider_reference(target_provider):
            playlist_items, track_count = self._compose_builtin_playlist_items(
                tracks=tracks,
                sections=audio_sections,
            )
            if not playlist_items:
                raise MusicAssistantError("No playlist entries were generated")
            self._set_session_progress(
                session,
                "publishing_playlist",
                entries=len(playlist_items),
                tracks=track_count,
            )
            playlist = await self._import_builtin_playlist(
                target_playlist_name,
                playlist_items,
            )
            entries_added = len(playlist_items)
        else:
            entries, track_count = self._compose_entries(tracks=tracks, sections=audio_sections)
            if not entries:
                raise MusicAssistantError("No playlist entries were generated")
            self._set_session_progress(
                session,
                "publishing_playlist",
                entries=len(entries),
                tracks=track_count,
            )
            playlist = await self.mass.music.playlists.create_playlist(
                target_playlist_name,
                provider_instance_or_domain=target_provider,
            )
            add_task = await self.mass.music.playlists.add_playlist_tracks(
                int(playlist.item_id), entries
            )
            await self._wait_for_background_task_completion(add_task.id, timeout_seconds=120)
            entries_added = len(entries)
        self.logger.info(
            "Playlist mode published '%s' (%s entries, %s generated sections)",
            target_playlist_name,
            entries_added,
            len(audio_sections),
        )
        return {
            "mode": "playlist",
            "source_playlist_name": playlist_name,
            "source_tracks": len(tracks),
            "generated_sections": len(audio_sections),
            "target_playlist_id": playlist.item_id,
            "target_playlist_name": target_playlist_name,
            "entries_added": entries_added,
        }

    async def _run_dynamic_mode(  # noqa: PLR0915
        self,
        session: SessionState,
        station: dict[str, Any],
    ) -> dict[str, Any]:
        """Run dynamic generation mode."""
        station = deepcopy(station)
        self.logger.debug(
            "Dynamic mode starting for station '%s' (%s)",
            station.get("name", "AI Radio"),
            station.get("id", ""),
        )
        self._set_session_progress(session, "fetch_source_tracks")
        runtime_tokens = await self._prepare_runtime_tokens(station)
        player_id = str(station.get("default_player_id") or "").strip()
        if not player_id:
            raise MusicAssistantError("Dynamic mode requires a target player_id")
        if not self.mass.players.get_player(player_id):
            raise MusicAssistantError(f"Unknown target player: {player_id}")

        tracks, playlist_name = await self._fetch_source_tracks(station)
        tracks = self._apply_track_duration_limit(tracks, station)
        if not tracks:
            raise MusicAssistantError("No source tracks available after applying station limits")

        batch_size = max(1, int(station.get("dynamic_batch_size", 1) or 1))
        poll_seconds = max(1, int(station.get("dynamic_poll_seconds", 5) or 5))
        prefetch_remaining_tracks = max(
            1, int(station.get("dynamic_prefetch_remaining_tracks", 2) or 2)
        )
        self.logger.debug(
            "Dynamic runtime settings: batch_size=%d poll_seconds=%d prefetch_remaining_tracks=%d",
            batch_size,
            poll_seconds,
            prefetch_remaining_tracks,
        )
        clear_queue = bool(station.get("clear_queue_on_start", True))
        queue_id = player_id
        self._set_session_progress(
            session,
            "initializing_queue",
            total_tracks=len(tracks),
            batch_size=batch_size,
            prefetch_remaining_tracks=prefetch_remaining_tracks,
            queue_id=queue_id,
        )
        if clear_queue:
            self.mass.player_queues.clear(queue_id)

        cumulative_minutes = [0.0]
        for track in tracks:
            duration = track.get("duration")
            seconds = (
                float(duration) if isinstance(duration, (int, float)) and duration > 0 else 210.0
            )
            cumulative_minutes.append(cumulative_minutes[-1] + (seconds / 60.0))

        cursor = 0
        batch_index = 0
        total_entries_queued = 0
        wait_trigger_global_index = -1
        history_state: dict[str, list[tuple[int, float]]] = {}
        self._set_session_progress(
            session,
            "running",
            queued_tracks=0,
            total_tracks=len(tracks),
            batch_index=0,
            queue_entries=0,
        )

        while cursor < len(tracks):
            batch_tracks = tracks[cursor : cursor + batch_size]
            is_first = cursor == 0
            is_last = (cursor + len(batch_tracks)) >= len(tracks)
            lookahead = None if is_last else tracks[cursor + len(batch_tracks)]
            generation_tracks = [*batch_tracks, lookahead] if lookahead else batch_tracks

            if is_first and is_last:
                allowed_slot_when = ["start_of_playlist", "between_songs", "end_of_playlist"]
            elif is_first:
                allowed_slot_when = ["start_of_playlist", "between_songs"]
            elif is_last:
                allowed_slot_when = ["between_songs", "end_of_playlist"]
            else:
                allowed_slot_when = ["between_songs"]

            self._set_session_progress(
                session,
                "planning_sections",
                queued_tracks=cursor,
                total_tracks=len(tracks),
                batch_index=batch_index + 1,
            )
            planned_sections, history_state = self._plan_sections(
                tracks=generation_tracks,
                station=station,
                track_index_offset=cursor,
                minute_offset=cumulative_minutes[cursor],
                history_state=history_state,
                allowed_slot_when=allowed_slot_when,
                runtime_tokens=runtime_tokens,
            )
            filtered_sections = [
                item
                for item in planned_sections
                if item.insert_at_index <= len(batch_tracks)
                and not (item.when == "start_of_playlist" and not is_first)
                and not (item.when == "end_of_playlist" and not is_last)
            ]
            self._set_session_progress(
                session,
                "generating_llm",
                queued_tracks=cursor,
                total_tracks=len(tracks),
                batch_index=batch_index + 1,
                sections_planned=len(filtered_sections),
            )
            generated_sections = await self._generate_sections(station, filtered_sections)
            self._set_session_progress(
                session,
                "generating_tts",
                queued_tracks=cursor,
                total_tracks=len(tracks),
                batch_index=batch_index + 1,
                sections=len(generated_sections),
            )
            audio_sections = await self._synthesize_sections(generated_sections=generated_sections)
            entries, _track_count = self._compose_entries(batch_tracks, audio_sections)
            if not entries:
                raise MusicAssistantError("Dynamic batch produced no queue entries")

            self._set_session_progress(
                session,
                "queueing_batch",
                queued_tracks=cursor,
                total_tracks=len(tracks),
                batch_index=batch_index + 1,
                batch_entries=len(entries),
                queue_entries=total_entries_queued,
            )
            option = QueueOption.REPLACE if is_first else QueueOption.ADD
            await self.mass.player_queues.play_media(
                queue_id=queue_id,
                media=cast("list[Any]", entries),
                option=option,
            )

            track_entry_local_indices = [
                index for index, uri in enumerate(entries) if "://track/" in uri
            ]
            batch_start_global = total_entries_queued
            total_entries_queued += len(entries)
            if track_entry_local_indices:
                trigger_position = max(
                    0, len(track_entry_local_indices) - prefetch_remaining_tracks
                )
                trigger_track_local_index = track_entry_local_indices[trigger_position]
                wait_trigger_global_index = batch_start_global + trigger_track_local_index

            cursor += len(batch_tracks)
            batch_index += 1
            self.logger.debug(
                "Dynamic batch %d queued (%d tracks processed, %d queue entries total)",
                batch_index,
                cursor,
                total_entries_queued,
            )
            self._set_session_progress(
                session,
                "running",
                queued_tracks=cursor,
                total_tracks=len(tracks),
                batch_index=batch_index,
                queue_entries=total_entries_queued,
            )

            if cursor >= len(tracks):
                break
            self._set_session_progress(
                session,
                "waiting_for_playback",
                queued_tracks=cursor,
                total_tracks=len(tracks),
                batch_index=batch_index,
                queue_entries=total_entries_queued,
                wait_trigger_index=wait_trigger_global_index,
                prefetch_remaining_tracks=prefetch_remaining_tracks,
            )
            while True:
                queue = self.mass.player_queues.get_active_queue(player_id)
                if not queue:
                    queue = self.mass.player_queues.get(queue_id)
                if (
                    queue
                    and queue.current_index is not None
                    and queue.current_index >= wait_trigger_global_index
                ):
                    break
                await asyncio.sleep(poll_seconds)

        return {
            "mode": "dynamic",
            "source_playlist_name": playlist_name,
            "source_tracks": len(tracks),
            "queued_tracks": cursor,
            "queue_id": queue_id,
            "queue_entries": total_entries_queued,
        }

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
        for index, track in enumerate(tracks):
            artist = ""
            track_artists = getattr(track, "artists", None)
            if isinstance(track_artists, list) and track_artists:
                artist = str(track_artists[0].name)
            uri = await self._track_to_uri(track)
            try:
                playlist_item = media_item_to_playlist_item(track)
            except Exception:
                self.logger.debug(
                    "Could not build source playlist metadata for uri=%s",
                    uri,
                    exc_info=True,
                )
                playlist_item = None
            normalized.append(
                {
                    "index": index,
                    "item_id": track.item_id,
                    "name": track.name,
                    "artist": artist,
                    "songinfo": f"{artist} - {track.name}".strip(" -"),
                    "duration": track.duration,
                    "uri": uri,
                    "playlist_item": playlist_item,
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

    def _apply_track_duration_limit(
        self, tracks: list[dict[str, Any]], station: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Apply random max duration truncation if configured."""
        max_duration = float(station.get("max_duration_minutes", 0) or 0)
        if max_duration <= 0 or not tracks:
            return tracks
        indices = list(range(len(tracks)))
        rng = random.Random()
        rng.shuffle(indices)
        chosen: list[int] = []
        total_minutes = 0.0
        for index in indices:
            duration = tracks[index].get("duration")
            seconds = (
                float(duration) if isinstance(duration, (int, float)) and duration > 0 else 210.0
            )
            chosen.append(index)
            total_minutes += seconds / 60.0
            if total_minutes > max_duration:
                break
        result: list[dict[str, Any]] = []
        for new_index, old_index in enumerate(sorted(chosen)):
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
            placeholders = self._resolve_placeholders(
                station=station,
                tracks=tracks,
                slot=slot,
                runtime_tokens=runtime_tokens,
            )
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
                        selected.append((section_id, slot, placeholders))
                        register_event(section_id, slot)
                        continue
                    if "ALTERNATIVE" in flow_item:
                        alternative = flow_item["ALTERNATIVE"]
                        if not isinstance(alternative, dict):
                            continue
                        section_id = pick_weighted_choice(alternative.get("choices", []), rng)
                        if is_empty_section(section_id):
                            continue
                        selected.append((section_id, slot, placeholders))
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
                            placeholders=placeholders,
                            track_index_offset=track_index_offset,
                            minute_offset=minute_offset,
                        ):
                            continue
                        if is_empty_section(section_id):
                            continue
                        selected.append((section_id, slot, placeholders))
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
        min_gap_songs = int(guards.get("min_gap_songs", 0) or 0)
        max_per_60min = int(guards.get("max_per_60min", 0) or 0)
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
            section_id=section_id,
            section_name=section_name,
            when=slot.when,
            insert_at_index=slot.at_index,
            prompt=meta_prompt,
            max_chars=total_max_chars,
            web_search_mode=max_web_mode,
        )

    async def _generate_sections(
        self,
        station: dict[str, Any],
        planned_sections: list[PlannedSection],
    ) -> list[GeneratedSection]:
        """Generate section texts from planning entries."""
        generated: list[GeneratedSection] = []
        sorted_sections = sorted(planned_sections, key=lambda section: section.order)
        self.logger.info(
            "LLM generation started: station=%s sections=%d",
            station.get("id", "unknown"),
            len(sorted_sections),
        )
        for index, item in enumerate(sorted_sections, start=1):
            started = perf_counter()
            self.logger.info(
                "LLM section %d/%d: section=%s when=%s web_mode=%s",
                index,
                len(sorted_sections),
                item.section_id,
                item.when,
                item.web_search_mode,
            )
            try:
                text = await self._generate_text(
                    station=station,
                    prompt=item.prompt,
                    web_mode=item.web_search_mode,
                )
            except Exception:
                self.logger.exception(
                    "LLM generation failed: section=%s when=%s",
                    item.section_id,
                    item.when,
                )
                raise
            if item.max_chars > 0:
                text = soft_limit_text(text, max_chars=item.max_chars)
            self.logger.debug(
                "LLM section done: section=%s chars=%d elapsed=%.2fs",
                item.section_id,
                len(text),
                perf_counter() - started,
            )
            generated.append(
                GeneratedSection(
                    order=item.order,
                    section_id=item.section_id,
                    section_name=item.section_name,
                    when=item.when,
                    insert_at_index=item.insert_at_index,
                    text=text,
                )
            )
        self.logger.info("LLM generation finished: generated_sections=%d", len(generated))
        return generated

    async def _synthesize_sections(
        self,
        generated_sections: list[GeneratedSection],
    ) -> list[AudioSection]:
        """Convert generated section texts to playable TTS URIs."""
        output: list[AudioSection] = []
        if not generated_sections:
            return output
        self.logger.info("TTS synthesis started: sections=%d", len(generated_sections))
        for index, section in enumerate(generated_sections):
            started = perf_counter()
            self.logger.info(
                "TTS section %d/%d: section=%s chars=%d",
                index + 1,
                len(generated_sections),
                section.section_id,
                len(section.text),
            )
            try:
                section_uri = await self._render_tts(text=section.text)
            except Exception:
                self.logger.exception(
                    "TTS synthesis failed: section=%s",
                    section.section_id,
                )
                raise
            self.logger.debug(
                "TTS section done: section=%s elapsed=%.2fs uri=%s",
                section.section_id,
                perf_counter() - started,
                section_uri,
            )
            output.append(
                AudioSection(
                    order=section.order,
                    section_id=section.section_id,
                    section_name=section.section_name,
                    insert_at_index=section.insert_at_index,
                    uri=section_uri,
                )
            )
        self.logger.info("TTS synthesis finished: sections=%d", len(output))
        return output

    def _compose_entries(
        self,
        tracks: list[dict[str, Any]],
        sections: list[AudioSection],
    ) -> tuple[list[str], int]:
        """Compose final queue/playlist entries with insert positions."""
        sections_by_index: dict[int, list[AudioSection]] = defaultdict(list)
        for item in sections:
            sections_by_index[item.insert_at_index].append(item)
        entries: list[str] = []
        track_count = 0
        for index in range(len(tracks) + 1):
            for section in sorted(sections_by_index.get(index, []), key=lambda item: item.order):
                entries.append(section.uri)
            if index < len(tracks):
                track_uri = str(tracks[index].get("uri", "")).strip()
                if track_uri:
                    entries.append(track_uri)
                    track_count += 1
        return entries, track_count

    def _compose_builtin_playlist_items(
        self,
        tracks: list[dict[str, Any]],
        sections: list[AudioSection],
    ) -> tuple[list[PlaylistItem], int]:
        """Compose builtin playlist items with stable AI Radio section metadata."""
        sections_by_index: dict[int, list[AudioSection]] = defaultdict(list)
        for item in sections:
            sections_by_index[item.insert_at_index].append(item)
        entries: list[PlaylistItem] = []
        track_count = 0
        for index in range(len(tracks) + 1):
            for section in sorted(sections_by_index.get(index, []), key=lambda item: item.order):
                entries.append(self._section_to_playlist_item(section))
            if index < len(tracks):
                track_item = self._track_to_playlist_item(tracks[index])
                if track_item:
                    entries.append(track_item)
                    track_count += 1
        return entries, track_count

    def _track_to_playlist_item(self, track: dict[str, Any]) -> PlaylistItem | None:
        """Return the prebuilt source track playlist item, falling back to URI metadata."""
        playlist_item = track.get("playlist_item")
        if isinstance(playlist_item, PlaylistItem):
            return deepcopy(playlist_item)
        track_uri = str(track.get("uri", "")).strip()
        if not track_uri:
            return None
        name = str(track.get("name") or track_uri).strip()
        title = str(track.get("songinfo") or name).strip()
        return PlaylistItem(
            path=track_uri,
            title=title,
            length=str(track["duration"]) if track.get("duration") else None,
            metadata={
                "media_type": MediaType.TRACK.value,
                "name": name,
            },
            providers=self._provider_mapping_infos_from_uri(track_uri),
        )

    def _section_to_playlist_item(self, section: AudioSection) -> PlaylistItem:
        """Create a fully described playlist item for a generated AI Radio section."""
        uri = str(section.uri).strip()
        display_name = f"AI Radio: {section.section_name}"
        media_type = self._extract_media_type_from_uri(uri) or MediaType.TRACK.value
        return PlaylistItem(
            path=uri,
            title=display_name,
            length="-1",
            metadata={
                "media_type": media_type,
                "name": display_name,
            },
            providers=self._provider_mapping_infos_from_uri(uri),
            images=[self._ai_radio_cover_image_info()],
        )

    async def _import_builtin_playlist(
        self,
        playlist_name: str,
        items: list[PlaylistItem],
    ) -> Any:
        """Create a builtin playlist by importing an M3U with preserved metadata."""
        m3u_data = generate_m3u(
            playlist_name,
            items,
            self._ai_radio_cover_image_path(),
        )
        return await self.mass.music.playlists.import_playlist(m3u_data)

    @staticmethod
    def _extract_media_type_from_uri(uri: str) -> str | None:
        """Extract MediaType string from an MA URI."""
        if "://" not in uri:
            return None
        _provider, rest = uri.split("://", 1)
        if "/" not in rest:
            return None
        media_type, _item_id = rest.split("/", 1)
        return media_type or None

    @staticmethod
    def _ai_radio_cover_image_path() -> str:
        """Return the explicit AI Radio playlist cover image path."""
        return str(Path(__file__).with_name("air.png"))

    def _ai_radio_cover_image_info(self) -> ImageInfo:
        """Return the AI Radio thumbnail image metadata for M3U entries."""
        return ImageInfo(
            type=ImageType.THUMB.value,
            path=self._ai_radio_cover_image_path(),
            provider="builtin",
            remotely_accessible=False,
        )

    def _provider_mapping_infos_from_uri(self, uri: str) -> list[ProviderMappingInfo]:
        """Build minimal provider mapping metadata for a Music Assistant URI."""
        if "://" not in uri:
            return []
        provider_ref, rest = uri.split("://", 1)
        if "/" not in rest:
            return []
        _media_type, item_id = rest.split("/", 1)
        if not item_id:
            return []
        provider = self.mass.get_provider(provider_ref)
        provider_domain = str(getattr(provider, "domain", "") or provider_ref).strip()
        provider_instance = str(getattr(provider, "instance_id", "") or provider_ref).strip()
        return [
            ProviderMappingInfo(
                domain=provider_domain,
                item_id=item_id,
                instance_id=provider_instance,
            )
        ]

    async def _wait_for_background_task_completion(
        self, task_id: str, timeout_seconds: int
    ) -> None:
        """Wait for a background task to complete so dependent file operations can run safely."""
        deadline = asyncio.get_running_loop().time() + max(1, timeout_seconds)
        while True:
            try:
                task = self.mass.tasks.get_task(task_id)
            except Exception:
                # If task history is gone, assume it is no longer active.
                return
            status = task.status
            if status in {TaskStatus.SUCCESS, TaskStatus.PARTIAL_SUCCESS, TaskStatus.IDLE}:
                return
            if status in {TaskStatus.FAILED, TaskStatus.CANCELLED}:
                self.logger.warning(
                    "Background task %s ended with status '%s' before AI Radio post-processing",
                    task_id,
                    status.value,
                )
                return
            if asyncio.get_running_loop().time() >= deadline:
                self.logger.warning(
                    "Timed out waiting for background task %s (status=%s)",
                    task_id,
                    status.value,
                )
                return
            await asyncio.sleep(0.1)

    def _is_builtin_provider_reference(self, provider_ref: str) -> bool:
        """Return True when provider reference points to builtin provider."""
        value = str(provider_ref or "").strip()
        if value == "builtin":
            return True
        provider = self.mass.get_provider(value)
        return str(getattr(provider, "domain", "") or "").strip() == "builtin"

    async def _prepare_runtime_tokens(self, station: dict[str, Any]) -> dict[str, str]:
        """Prepare runtime tokens (including weather placeholders) for one run."""
        runtime_tokens: dict[str, str] = {}

        if not self._station_uses_weather_placeholders(station):
            return runtime_tokens

        general = cast("dict[str, Any]", station.get("general", {}))
        weather_provider = (
            str(general.get("weather_provider") or DEFAULT_WEATHER_PROVIDER).strip().lower()
        )
        if weather_provider in {"", "none", "disabled", "off"}:
            return runtime_tokens
        if weather_provider != "open_meteo":
            self.logger.warning(
                "Unsupported weather provider '%s' for AI Radio station",
                weather_provider,
            )
            return runtime_tokens

        city, country = self._extract_location(station)
        if not city or not country:
            self.logger.warning(
                "Weather placeholders used but no location configured "
                "(expected station.general.location.city/country)"
            )
            return runtime_tokens

        timeout_seconds = max(
            5,
            coerce_int(general.get("weather_timeout_seconds"), DEFAULT_WEATHER_TIMEOUT_SECONDS),
        )
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

    def _station_uses_weather_placeholders(self, station: dict[str, Any]) -> bool:
        """Return whether the station references weather placeholders."""
        weather_tokens = ("<weather_hourly>", "<weather_daily>")
        for section in station.get("sections", []):
            prompt = str(section.get("prompt", ""))
            if any(token in prompt for token in weather_tokens):
                return True

        for rule in station.get("section_order", []):
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

    def _extract_location(self, station: dict[str, Any]) -> tuple[str, str]:
        """Extract weather location (city/country) from station general config."""
        general = cast("dict[str, Any]", station.get("general", {}))
        location = cast("dict[str, Any]", general.get("location", {}))
        city = str(location.get("city", "")).strip()
        country = str(location.get("country", "")).strip()
        return city, country

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
    ) -> dict[str, str]:
        """Resolve placeholders for one slot."""
        general = cast("dict[str, Any]", station.get("general", {}))
        timezone_name = str(general.get("timezone", "UTC"))
        now_utc = utc()
        try:
            now = now_utc.astimezone(ZoneInfo(timezone_name))
        except Exception:
            now = now_utc

        values: dict[str, str] = {str(key): str(value) for key, value in runtime_tokens.items()}
        prev_track = tracks[slot.prev_index] if slot.prev_index is not None else None
        next_track = tracks[slot.next_index] if slot.next_index is not None else None
        very_next_track = tracks[slot.very_next_index] if slot.very_next_index is not None else None
        values["<prev_songinfo>"] = track_songinfo(prev_track)
        values["<next_songinfo>"] = track_songinfo(next_track)
        values["<very_next_songinfo>"] = track_songinfo(very_next_track)
        values["<timestamp>"] = now.strftime("%Y-%m-%d %H:%M %Z")
        values.setdefault("<weather_hourly>", "")
        values.setdefault("<weather_daily>", "")
        return values

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

    async def _generate_text(self, station: dict[str, Any], prompt: str, web_mode: str) -> str:
        """Generate one section text using the configured AI_QUERY plugin feature."""
        general = cast("dict[str, Any]", station.get("general", {}))
        instructions = str(general.get("instructions") or DEFAULT_LLM_INSTRUCTIONS).strip()
        query_parts: list[str] = []
        if instructions:
            query_parts.append(f"Program instructions:\n{instructions}")
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
        plugin = self._get_ai_plugin()
        self.logger.debug(
            "AI query prepared: plugin=%s web_mode=%s query_chars=%d",
            plugin.instance_id,
            web_mode,
            len(query),
        )
        try:
            response = await plugin.ai_query(query)
        except Exception as err:
            error_name = err.__class__.__name__
            error_text = str(err).strip()
            if error_name == "NotConnected":
                raise MusicAssistantError(
                    "AI provider "
                    f"'{plugin.instance_id}' is not connected. Reconnect the provider "
                    "(for example Home Assistant) and retry."
                ) from err
            details = error_text or error_name
            raise MusicAssistantError(
                f"AI provider '{plugin.instance_id}' query failed: {details}"
            ) from err
        if not response or not str(response).strip():
            raise MusicAssistantError(
                f"AI provider '{plugin.instance_id}' returned an empty response for section text"
            )
        text = str(response).strip()
        self.logger.debug(
            "AI query response received: plugin=%s chars=%d",
            plugin.instance_id,
            len(text),
        )
        return text

    async def _render_tts(self, text: str) -> str:
        """Render text to a playable URI using the configured TTS plugin feature."""
        plugin = self._get_tts_plugin()
        self.logger.debug(
            "TTS render prepared: plugin=%s text_chars=%d", plugin.instance_id, len(text)
        )
        stream_details = await plugin.get_tts_message(text)
        uri = str(getattr(stream_details, "path", "") or "").strip()
        if uri:
            if uri.startswith(("http://", "https://", "rtsp://", "rtmp://")):
                builtin_provider = self.mass.get_provider("builtin")
                builtin_instance_id = str(
                    getattr(builtin_provider, "instance_id", "") or ""
                ).strip()
                return create_uri(MediaType.TRACK, builtin_instance_id or "builtin", uri)
            return uri
        raise MusicAssistantError(
            f"TTS provider '{plugin.instance_id}' returned no direct stream path in StreamDetails.path"
        )

    def _get_ai_plugin(self) -> PluginProvider:
        """Return the plugin used for AI_QUERY tasks."""
        plugins = sorted(
            self.mass.get_plugins_by_feature(ProviderFeature.AI_QUERY),
            key=lambda plugin: plugin.instance_id,
        )
        if not plugins:
            raise MusicAssistantError(
                "No AI provider available. Configure a plugin with ProviderFeature.AI_QUERY "
                "(for example Home Assistant with an ai_task entity)."
            )
        return plugins[0]

    def _get_tts_plugin(self) -> PluginProvider:
        """Return the plugin used for TTS tasks."""
        plugins = sorted(
            self.mass.get_plugins_by_feature(ProviderFeature.TTS),
            key=lambda plugin: plugin.instance_id,
        )
        if not plugins:
            raise MusicAssistantError(
                "No TTS provider available. Configure a plugin with ProviderFeature.TTS "
                "(for example Home Assistant with a TTS entity)."
            )
        return plugins[0]
