"""
Player-state reconciliation for the Player Queues controller.

Translates a player's reported state into the queue's state: tracks the current index/elapsed time,
detects track changes and end-of-queue, drives the playback-progress reports (and the user-initiated
/ album-credit play-counting), and computes the flow-mode stream index. Owns no per-queue state of
its own; it reads and mutates the controller's `PlayerQueueData` records via its owning controller.
"""

# ruff: noqa: PLR0915  -- the player-state reconciliation methods are large state machines by nature

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from typing import TYPE_CHECKING

from music_assistant_models.enums import (
    EventType,
    MediaType,
    PlaybackState,
)
from music_assistant_models.errors import (
    MusicAssistantError,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    ItemMapping,
    MediaItemType,
)
from music_assistant_models.playback_progress_report import MediaItemPlaybackProgressReport

from music_assistant.constants import (
    PLAYBACK_REPORT_INTERVAL_SECONDS,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.controllers.player_queues.base import _PlayerQueuesBase
from music_assistant.controllers.player_queues.helpers import (
    CompareState,
    build_queue_item,
    find_dynamic_source,
    get_current_playback_speed,
)
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    set_current_user,
)
from music_assistant.helpers.audio import resolve_output_player_ids
from music_assistant.helpers.compare import compare_item_ids
from music_assistant.helpers.util import get_changed_keys, percentage
from music_assistant.models.player import Player

if TYPE_CHECKING:
    from music_assistant_models.player_queue import PlayerQueue
    from music_assistant_models.queue_item import QueueItem

    from music_assistant.controllers.player_queues.state import PlayerQueueData


# media types that never put a queue in the ended state: a live source has no natural end, so it
# going idle means the source stopped and not that the queue ran out (marking it ended would strand
# a later resume), and a sound effect is a one-off that leaves the queue as it found it.
UNENDABLE_MEDIA_TYPES = (MediaType.RADIO, MediaType.AUDIO_SOURCE, MediaType.SOUND_EFFECT)


class PlaybackTrackerMixin(_PlayerQueuesBase):
    """Reconcile a queue's state against its player and drive playback-progress reporting."""

    def _update_current_index_from_player(self, queue: PlayerQueue, player: Player) -> bool:
        """
        Update the current item/index/elapsed time on the queue from the player state.

        Returns True if the update was successful, False if the caller should return early.
        """
        queue_id = queue.queue_id
        if queue.active and queue.state in (
            PlaybackState.PLAYING,
            PlaybackState.PAUSED,
        ):
            # NOTE: If the queue is not playing (yet) we will not update the current index
            # to ensure we keep the previously known current index
            if queue.flow_mode:
                # flow mode active, the player is playing one long stream
                # so we need to calculate the current index and elapsed time
                # (already returned in media-time)
                current_index, elapsed_time = self._get_flow_queue_stream_index(queue, player)
            elif item_id := self._parse_player_current_item_id(queue_id, player):
                # normal mode, the player itself will report the current item
                elapsed_time = player.state.corrected_elapsed_time or 0
                current_index = self.index_by_id(queue_id, item_id)
            else:
                # this may happen if the player is still transitioning between tracks
                # we ignore this for now and keep the current index as is
                return False

            # get current/next item based on current index
            queue.current_index = current_index
            queue.current_item = current_item = self.get_item(queue_id, current_index)
            queue.next_item = (
                self.get_next_item(queue_id, current_index)
                if current_item and current_index is not None
                else None
            )

            # convert player's stream-time to media-time and add seek offset (non-flow only;
            # flow mode already returns media-time from _get_flow_queue_stream_index above)
            speed = get_current_playback_speed(queue)
            if not queue.flow_mode:
                elapsed_time *= speed
                if (
                    current_item
                    and current_item.streamdetails
                    and current_item.streamdetails.seek_position
                ):
                    elapsed_time += current_item.streamdetails.seek_position
            queue.elapsed_time = elapsed_time
            queue.elapsed_time_last_updated = time.time()
            queue.playback_speed = speed

        elif not queue.current_item and queue.current_index is not None:
            current_index = queue.current_index
            queue.current_item = current_item = self.get_item(queue_id, current_index)
            queue.next_item = (
                self.get_next_item(queue_id, current_index)
                if current_item and current_index is not None
                else None
            )
        return True

    def _update_queue_from_player(
        self,
        player: Player,
    ) -> None:
        """Update the Queue when the player state changed."""
        queue_id = player.player_id
        queue_data = self._queue_data[queue_id]
        queue = queue_data.queue

        # basic properties
        queue.display_name = player.state.name
        queue.available = player.state.available
        queue.smart_fades_active = self.mass.streams.is_smart_fades_active(queue)
        queue.smart_shuffle_active = self.is_smart_shuffle_active(queue)
        queue.items = len(self._queue_data[queue_id].items)

        queue.state = (
            player.state.playback_state or PlaybackState.IDLE
            if queue.active
            else PlaybackState.IDLE
        )
        # update current item/index from player report
        if not self._update_current_index_from_player(queue, player):
            return

        output_player_ids = self._get_output_player_ids(player)

        # basic throttle: do not send state changed events if queue did not actually change
        prev_state: CompareState = self._queue_data[queue_id].prev_state or CompareState(
            queue_id=queue_id,
            state=PlaybackState.IDLE,
            current_item_id=None,
            next_item_id=None,
            current_item=None,
            elapsed_time=0,
            last_playing_elapsed_time=0,
            stream_title=None,
            codec_type=None,
            output_player_ids=None,
        )
        # update last_playing_elapsed_time only when the player is actively playing
        # use corrected_elapsed_time which accounts for time since last update
        # this preserves the last known elapsed time when transitioning to idle/paused
        prev_playing_elapsed = prev_state["last_playing_elapsed_time"]
        prev_item_id = prev_state["current_item_id"]
        current_item_id = queue.current_item.queue_item_id if queue.current_item else None
        if queue.state == PlaybackState.PLAYING:
            current_elapsed = int(queue.corrected_elapsed_time)
            if current_item_id != prev_item_id:
                # new track started, reset the elapsed time tracker
                last_playing_elapsed_time = current_elapsed
            else:
                # same track, use the max of current and previous to handle timing issues
                last_playing_elapsed_time = max(current_elapsed, prev_playing_elapsed)
        else:
            last_playing_elapsed_time = prev_playing_elapsed
        new_state = CompareState(
            queue_id=queue_id,
            state=queue.state,
            current_item_id=queue.current_item.queue_item_id if queue.current_item else None,
            next_item_id=queue.next_item.queue_item_id if queue.next_item else None,
            current_item=queue.current_item,
            elapsed_time=int(queue.elapsed_time),
            last_playing_elapsed_time=last_playing_elapsed_time,
            stream_title=(
                queue.current_item.streamdetails.stream_title
                if queue.current_item and queue.current_item.streamdetails
                else None
            ),
            codec_type=(
                queue.current_item.streamdetails.audio_format.codec_type
                if queue.current_item and queue.current_item.streamdetails
                else None
            ),
            output_player_ids=sorted(output_player_ids),
        )
        changed_keys = get_changed_keys(dict(prev_state), dict(new_state))
        with suppress(KeyError):
            changed_keys.remove("next_item_id")
        with suppress(KeyError):
            changed_keys.remove("last_playing_elapsed_time")

        # store the new state
        if queue.active:
            self._queue_data[queue_id].prev_state = new_state
        else:
            self._queue_data[queue_id].prev_state = None

        # return early if nothing changed
        if len(changed_keys) == 0:
            return

        # signal update and store state
        send_update = True
        if changed_keys == {"elapsed_time"}:
            # only elapsed time changed, do not send full queue update
            send_update = False
            prev_time = prev_state.get("elapsed_time") or 0
            cur_time = new_state.get("elapsed_time") or 0
            if abs(cur_time - prev_time) > 2:
                # send dedicated event for time updates when seeking
                self.mass.signal_event(
                    EventType.QUEUE_TIME_UPDATED,
                    object_id=queue_id,
                    data=queue.elapsed_time,
                )
                # also signal update to the player itself so it can update its current_media
                self.mass.players.trigger_player_update(queue_id)

        processing_update_sent = False
        if "output_player_ids" in changed_keys:
            processing_update_sent = self.mass.streams.audio_processing.retain_outputs(
                queue_id,
                output_player_ids,
            )
        if send_update and not processing_update_sent:
            self.signal_update(queue_id)

        # handle updating stream_metadata if needed
        if (
            queue.current_item
            and (streamdetails := queue.current_item.streamdetails)
            and streamdetails.stream_metadata_update_callback
            and (
                streamdetails.stream_metadata_last_updated is None
                or (
                    time.time() - streamdetails.stream_metadata_last_updated
                    >= streamdetails.stream_metadata_update_interval
                )
            )
        ):
            streamdetails.stream_metadata_last_updated = time.time()
            self.mass.create_task(
                streamdetails.stream_metadata_update_callback(
                    streamdetails, int(queue.corrected_elapsed_time)
                )
            )

        # handle sending a playback progress report
        # we do this every 30 seconds or when the state changes
        if (
            changed_keys.intersection({"state", "current_item_id"})
            or int(queue.elapsed_time) % PLAYBACK_REPORT_INTERVAL_SECONDS == 0
        ):
            self._handle_playback_progress_report(queue, prev_state, new_state)

        # check if we need to clear the queue if we reached the end
        if "state" in changed_keys and queue.state == PlaybackState.IDLE:
            self._handle_end_of_queue(queue, prev_state, new_state)

        # refill the queue (dynamic mode or autoplay) when running low on tracks
        if "current_item_id" in changed_keys:
            running_low = (
                queue.current_index is not None and (queue.items - queue.current_index) < 5
            )
            if queue.is_dynamic and running_low:
                # a dynamic queue tops up its bounded managed pool from its (dynamic + finite) sources
                task_id = f"fill_dynamic_tracks_{queue_id}"
                self.mass.call_later(5, self._fill_dynamic_tracks, queue_id, task_id=task_id)
            elif queue.autoplay_enabled and running_low:
                # autoplay appends whatever continues the queue's last item (more music, the
                # next podcast episode/audiobook, or nothing at all)
                task_id = f"fill_autoplay_tracks_{queue_id}"
                self.mass.call_later(5, self._fill_autoplay_tracks, queue_id, task_id=task_id)

    def _get_output_player_ids(self, player: Player) -> set[str]:
        """Return destination player IDs represented in the processing chain."""
        return resolve_output_player_ids(
            self.mass,
            [player.player_id, *player.state.group_members],
        )

    def _get_flow_queue_stream_index(
        self, queue: PlayerQueue, player: Player
    ) -> tuple[int | None, float]:
        """
        Calculate current queue index and current track elapsed time when flow mode is active.

        The player reports cumulative stream-time (post-atempo). The returned
        track elapsed time is in media-time, scaled by the current item's
        playback_speed when we hit the active entry.
        """
        queue_data = self._queue_data[queue.queue_id]
        elapsed_time_queue_total = player.state.corrected_elapsed_time or 0
        if queue.current_index is None and not queue_data.flow_mode_stream_log:
            return queue.current_index, queue.elapsed_time

        # For each track that has been streamed/buffered to the player,
        # a playlog entry will be created with the queue item id
        # and the amount of seconds streamed. We traverse the playlog to figure
        # out where we are in the queue, accounting for actual streamed
        # seconds (and not duration) and skipped seconds. If a track has been repeated,
        # it will simply be in the playlog multiple times.
        played_time = 0.0
        queue_index: int | None = queue.current_index or 0
        track_time = 0.0
        flow_log = queue_data.flow_mode_stream_log
        for log_index, play_log_entry in enumerate(flow_log):
            # seconds_streamed is bytes-derived stream-time, so the boundary check
            # doesn't need a speed factor. Normally only the still-streaming tail entry
            # has seconds_streamed=None (we'll break inside it before the sentinel
            # matters); an abandoned probe entry is the exception, handled below.
            if play_log_entry.seconds_streamed is not None:
                # NOTE: 'seconds_streamed' can be 0 if there was a stream error
                entry_stream_duration = play_log_entry.seconds_streamed
            elif log_index < len(flow_log) - 1:
                # Some players open the same flow URL several times while probing the
                # stream. A probe can leave an unfinished entry behind before the
                # connection that actually plays the audio appends the next entry.
                # Recover the completed stream duration from the shared QueueItem;
                # treating this non-tail entry as the active sentinel would pin the
                # queue to the previous track and let elapsed time overflow its duration.
                stale_queue_item = self.get_item(queue.queue_id, play_log_entry.queue_item_id)
                if (
                    stale_queue_item
                    and stale_queue_item.streamdetails
                    and stale_queue_item.streamdetails.seconds_streamed is not None
                ):
                    entry_stream_duration = stale_queue_item.streamdetails.seconds_streamed
                else:
                    entry_stream_duration = 0
            else:
                entry_stream_duration = 3600 * 24 * 7
            if elapsed_time_queue_total > (entry_stream_duration + played_time):
                # total elapsed time is more than (streamed) track duration
                # this track has been fully played, move on.
                played_time += entry_stream_duration
            else:
                # no more seconds left to divide, this is our track
                # account for any seeking by adding the skipped/seeked seconds
                queue_index = self.index_by_id(queue.queue_id, play_log_entry.queue_item_id)
                queue_item = self.get_item(queue.queue_id, queue_index)
                if queue_item and queue_item.streamdetails:
                    track_sec_skipped = queue_item.streamdetails.seek_position
                else:
                    track_sec_skipped = 0
                # stream-time within this entry, scaled to media-time using the
                # speed of the entry we broke on (queue.current_item may still be
                # the previous entry during a transition)
                entry_speed = (
                    float(queue_item.extra_attributes.get("playback_speed") or 1.0)
                    if queue_item
                    else 1.0
                )
                stream_pos_in_item = elapsed_time_queue_total - played_time
                track_time = track_sec_skipped + stream_pos_in_item * entry_speed
                break
        if player.state.playback_state != PlaybackState.PLAYING:
            # if the player is not playing, we can't be sure that the elapsed time is correct
            # so we just return the queue index and the elapsed time
            return queue.current_index, queue.elapsed_time
        return queue_index, track_time

    def _parse_player_current_item_id(self, queue_id: str, player: Player) -> str | None:
        """Parse QueueItem ID from Player's current url."""
        protocol_player = player
        if player.active_output_protocol and player.active_output_protocol != "native":
            protocol_player = self.mass.players.get_player(player.active_output_protocol) or player
        if not protocol_player.current_media:
            # YES, we use player.current_media on purpose here because we need the raw metadata
            return None
        # prefer queue_id and queue_item_id within the current media
        if (
            protocol_player.current_media.source_id == queue_id
            and protocol_player.current_media.queue_item_id
        ):
            return protocol_player.current_media.queue_item_id
        # special case for sonos players
        if protocol_player.current_media.uri and protocol_player.current_media.uri.startswith(
            f"mass:{queue_id}"
        ):
            if protocol_player.current_media.queue_item_id:
                return protocol_player.current_media.queue_item_id
            current_item_id = protocol_player.current_media.uri.split(":")[-1]
            if self.get_item(queue_id, current_item_id):
                return current_item_id
            return None
        # try to extract the item id from a mass stream url
        # URL format: {base_url}/{mode}/{session_id}/{queue_id}/{queue_item_id}/{player_id}.{fmt}
        base_url = self.mass.streams.base_url
        if (
            protocol_player.current_media.uri
            and base_url
            and protocol_player.current_media.uri.startswith(base_url)
        ):
            path_parts = protocol_player.current_media.uri[len(base_url) :].strip("/").split("/")
            # path_parts: [mode, session_id, queue_id, queue_item_id, player_id.fmt]
            if len(path_parts) >= 5:
                current_item_id = path_parts[3]
                if self.get_item(queue_id, current_item_id):
                    return current_item_id

        return None

    def _handle_end_of_queue(
        self, queue: PlayerQueue, prev_state: CompareState, new_state: CompareState
    ) -> None:
        """Check if the queue should be cleared after the current item."""
        queue_data = self._queue_data[queue.queue_id]
        # check if queue state changed to stopped (from playing/paused to idle)
        if not (
            prev_state["state"] in (PlaybackState.PLAYING, PlaybackState.PAUSED)
            and new_state["state"] == PlaybackState.IDLE
        ):
            return
        # check if no more items in the queue (next_item should be None at end of queue)
        if queue.next_item is not None:
            return
        # check if we had a previous item playing
        if prev_state["current_item_id"] is None:
            return

        # retrieve prev_item here so it's available in the _settle_or_resume_delayed closure
        # regardless of which code path (flow mode or non-flow mode) creates the task
        prev_item = prev_state["current_item"]

        if prev_item is not None and prev_item.media_type in UNENDABLE_MEDIA_TYPES:
            return

        async def _settle_or_resume_delayed() -> None:
            for _ in range(5):
                await asyncio.sleep(1)
                if queue.state != PlaybackState.IDLE:
                    return
                if queue.next_item is not None:
                    return
                # check the actual queue items list for newly added items
                # queue.next_item may be stale as it's only updated during PLAYING/PAUSED
                if queue.current_index is not None and (
                    next_item := self.get_next_item(queue.queue_id, queue.current_index)
                ):
                    next_index = self.index_by_id(queue.queue_id, next_item.queue_item_id)
                    if next_index is not None:
                        self.logger.info(
                            "Items added to queue while idle, resuming playback for %s",
                            queue.display_name,
                        )
                        await self.play_index(queue.queue_id, next_index)
                    return
            # If the queue was started from a dynamic source, fetch fresh tracks and continue.
            qdata = self._queue_data.get(queue.queue_id)
            dynamic_source = find_dynamic_source(qdata) if qdata else None
            if dynamic_source is not None:
                try:
                    # Restore the queue owner's user context so provider filters and
                    # per-user logic (e.g. smart playlist dedup) are respected during
                    # this background refill, mirroring _fill_dynamic_tracks.
                    playback_user = (
                        await self.mass.webserver.auth.get_user(queue_data.userid)
                        if queue_data.userid
                        else None
                    )
                    set_current_user(playback_user)
                    dynamic_tracks = await self._media_resolver.get_dynamic_source_tracks(
                        dynamic_source
                    )
                    if dynamic_tracks:
                        queue_items = [
                            build_queue_item(queue.queue_id, x)
                            for x in dynamic_tracks
                            if x.available
                        ]
                        if queue_items:
                            cur_index = queue.current_index or 0
                            await self.load(
                                queue.queue_id,
                                queue_items,
                                insert_at_index=cur_index + 1,
                                keep_remaining=False,
                                keep_played=True,
                                shuffle=False,
                            )
                            if queue.current_index is not None and (
                                next_item := self.get_next_item(queue.queue_id, queue.current_index)
                            ):
                                next_index = self.index_by_id(
                                    queue.queue_id, next_item.queue_item_id
                                )
                                if next_index is not None:
                                    await self.play_index(queue.queue_id, next_index)
                                    return
                except MusicAssistantError as err:
                    self.logger.warning(
                        "Failed to refresh dynamic source %s for queue %s: %s",
                        getattr(dynamic_source, "name", repr(dynamic_source)),
                        queue.display_name,
                        err,
                    )
            self._finish_queue(queue, prev_item)

        # all checks passed, we stopped playback at the last (or single) track of the queue
        # now determine if the item was fully played before settling/resuming

        # For flow mode, check if the last track was fully streamed using the stream log
        # This is more reliable than elapsed_time which can be reset/incorrect
        if queue.flow_mode and queue_data.flow_mode_stream_log:
            last_log_entry = queue_data.flow_mode_stream_log[-1]
            if last_log_entry.seconds_streamed is not None:
                # Guard: if a next item (e.g. a radio that caused the flow stream to break
                # out early) is already queued, the queue_buffer_completed path
                # (_resume_on_idle) is responsible for starting it. Creating
                # _settle_or_resume_delayed here would race with that restart and could
                # incorrectly settle the queue or trigger a double play_index call.
                if queue.current_index is not None and self.get_next_item(
                    queue.queue_id, queue.current_index
                ):
                    return
                self.mass.create_task(_settle_or_resume_delayed())
            return

        # For non-flow mode, use prev_state values since queue state may have been updated/reset
        if prev_item and (streamdetails := prev_item.streamdetails):
            duration = streamdetails.duration or prev_item.duration or 24 * 3600
        elif prev_item:
            duration = prev_item.duration or 24 * 3600
        else:
            # No current item means player has already cleared it, safe to clear queue
            self.mass.create_task(_settle_or_resume_delayed())
            return

        # use last_playing_elapsed_time which preserves the elapsed time from when the player
        # was still playing (before transitioning to idle where elapsed_time may be reset to 0)
        seconds_played = int(prev_state["last_playing_elapsed_time"])
        # debounce this a bit to make sure we're not clearing the queue by accident
        # only clear if the last track was played to near completion (within 5 seconds of end)
        if seconds_played >= (duration or 3600) - 5:
            self.mass.create_task(_settle_or_resume_delayed())

    def _finish_queue(self, queue: PlayerQueue, prev_item: QueueItem | None) -> None:
        """
        Settle a queue that has nothing left to play, based on the item it ended on.

        :param queue: The queue that ran out of items.
        :param prev_item: The item the queue was playing when it went idle, if it is still known.
        """
        queue_data = self._queue_data.get(queue.queue_id)
        # prev_item is gone when the player dropped its current item before we got here; the
        # queue's last item is the one that finished, so fall back to that
        ending_item = prev_item or (
            queue_data.items[-1] if queue_data and queue_data.items else None
        )
        if ending_item is not None and ending_item.media_type in UNENDABLE_MEDIA_TYPES:
            # normally caught before the debounce; reachable only when prev_item was lost
            return
        self.logger.info("End of queue reached for %s, marking it as ended", queue.display_name)
        self.mark_ended(queue.queue_id)

    def _handle_playback_progress_report(
        self, queue: PlayerQueue, prev_state: CompareState, new_state: CompareState
    ) -> None:
        """Handle playback progress report."""
        queue_data = self._queue_data[queue.queue_id]
        # detect change in current index to report that a item has been played
        prev_item_id = prev_state["current_item_id"]
        cur_item_id = new_state["current_item_id"]
        if prev_item_id is None and cur_item_id is None:
            return

        if prev_item_id is not None and prev_item_id != cur_item_id:
            # we have a new item, so we need report the previous one
            is_current_item = False
            item_to_report = prev_state["current_item"]
            seconds_played = int(prev_state["last_playing_elapsed_time"])
        else:
            # report on current item
            is_current_item = True
            item_to_report = self.get_item(queue.queue_id, cur_item_id) or new_state["current_item"]
            seconds_played = int(new_state["elapsed_time"])

        if not item_to_report:
            return  # guard against invalid items

        if not (media_item := item_to_report.media_item):
            # only report on media items
            return
        assert media_item.uri is not None  # uri is set in __post_init__

        if item_to_report.streamdetails and item_to_report.streamdetails.stream_error:
            #  Ignore items that had a stream error
            return

        # a preloaded item is only probed once it actually streams
        self._apply_probed_duration(item_to_report)

        if item_to_report.streamdetails and item_to_report.streamdetails.duration:
            duration = int(item_to_report.streamdetails.duration)
        else:
            duration = int(item_to_report.duration or 3 * 3600)

        if seconds_played < 5:
            # ignore items that have been played less than 5 seconds
            # this also filters out a bounce effect where the previous item
            # gets reported with 0 elapsed seconds after a new item starts playing
            return

        if (
            prev_state.get("state") != PlaybackState.PLAYING.value
            and not duration < PLAYBACK_REPORT_INTERVAL_SECONDS
        ):
            # Do not report when resuming from idle or paused.
            # (unless track has less seconds than PLAYBACK_REPORT_INTERVAL_SECONDS).
            # Handles edge case: Queue still holds an audiobook/ podcast, and is paused/ idle.
            # Audiobook is continued outside of MA. Then playback of another media item is
            # started in MA on that queue. This triggers a progress report with the old position
            # overwriting the newest one.
            # We still want to report when transitioning to pause or idle.
            return

        # determine if item is fully played
        # for podcasts and audiobooks we account for the last 60 seconds
        percentage_played = percentage(seconds_played, duration)
        if not is_current_item and item_to_report.media_type in (
            MediaType.AUDIOBOOK,
            MediaType.PODCAST_EPISODE,
        ):
            fully_played = seconds_played >= duration - 60
        elif not is_current_item:
            # 90% of the track must be played to be considered fully played
            fully_played = percentage_played >= 90
        else:
            fully_played = seconds_played >= duration - 10

        is_playing = is_current_item and queue.state == PlaybackState.PLAYING

        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            self.logger.debug(
                "%s %s '%s' (%s) - Fully played: %s - Progress: %s (%s/%ss)",
                queue.display_name,
                "is playing" if is_playing else "played",
                item_to_report.name,
                item_to_report.uri,
                fully_played,
                f"{percentage_played}%",
                seconds_played,
                duration,
            )
        # add entry to playlog - this also handles resume of podcasts/audiobooks
        if self._should_mark_played(
            queue.queue_id, item_to_report.queue_item_id, fully_played, is_playing
        ):
            self.mass.create_task(
                self.mass.music.mark_item_played(
                    media_item,
                    fully_played=fully_played,
                    seconds_played=seconds_played,
                    is_playing=is_playing,
                    userid=queue_data.userid,
                    queue_id=queue.queue_id,
                    user_initiated=self._is_user_initiated_play(queue_data, media_item),
                    playback_speed=float(
                        item_to_report.extra_attributes.get("playback_speed") or 1.0
                    )
                    if item_to_report.media_type in (MediaType.AUDIOBOOK, MediaType.PODCAST_EPISODE)
                    else None,
                )
            )
            if fully_played and not is_playing:
                if credit_album := self._enqueued_album_for_track(
                    queue_data, item_to_report, media_item
                ):
                    self.mass.create_task(
                        self._mark_album_played(credit_album, media_item, queue_data)
                    )

        album: Album | ItemMapping | None = getattr(media_item, "album", None)
        # signal 'media item played' event,
        # which is useful for plugins that want to do scrobbling
        artists: list[Artist | ItemMapping] = getattr(media_item, "artists", [])
        artists_names = [a.name for a in artists]
        self.mass.signal_event(
            EventType.MEDIA_ITEM_PLAYED,
            object_id=media_item.uri,
            data=MediaItemPlaybackProgressReport(
                uri=media_item.uri,
                media_type=media_item.media_type,
                name=media_item.name,
                version=getattr(media_item, "version", None),
                artist=(
                    getattr(media_item, "artist_str", None) or artists_names[0]
                    if artists_names
                    else None
                ),
                artists=artists_names,
                artist_mbids=[a.mbid for a in artists if a.mbid] if artists else None,
                album=album.name if album else None,
                album_mbid=album.mbid if album else None,
                album_artist=(album.artist_str if isinstance(album, Album) else None),
                album_artist_mbids=(
                    [a.mbid for a in album.artists if a.mbid] if isinstance(album, Album) else None
                ),
                image_url=(
                    self.mass.metadata.get_image_url(
                        item_to_report.media_item.image, prefer_proxy=False
                    )
                    if item_to_report.media_item.image
                    else None
                ),
                duration=duration,
                mbid=(getattr(media_item, "mbid", None)),
                seconds_played=seconds_played,
                fully_played=fully_played,
                is_playing=is_playing,
                userid=queue_data.userid,
                player_id=queue.queue_id,
            ),
        )

    def _enqueued_album_for_track(
        self, queue_data: PlayerQueueData, item_to_report: QueueItem, media_item: MediaItemType
    ) -> Album | None:
        """
        Return the album to credit for this played track, or None.

        Only an album the user explicitly enqueued is eligible, and only on the first
        track of a contiguous run of its tracks (the previous queue item must belong to
        a different album), so a single album play is credited once.
        """
        album = getattr(media_item, "album", None)
        if album is None:
            return None
        # the album the user pressed play on keeps the shape of the listing it was picked
        # from, while the queue's tracks carry the library album. Matching on the provider
        # mappings recognises both shapes as the same album.
        enqueued = next(
            (
                item
                for item in queue_data.enqueued_media_items
                if isinstance(item, Album) and compare_item_ids(item, album)
            ),
            None,
        )
        if enqueued is None:
            return None
        queue_id = queue_data.queue.queue_id
        index = self.index_by_id(queue_id, item_to_report.queue_item_id)
        if index:
            prev_item = self.get_item(queue_id, index - 1)
            prev_album = (
                getattr(prev_item.media_item, "album", None)
                if prev_item and prev_item.media_item
                else None
            )
            if prev_album == album:
                return None
        return enqueued

    def _is_user_initiated_play(
        self, queue_data: PlayerQueueData, media_item: MediaItemType
    ) -> bool:
        """Return whether a played item was explicitly chosen by the user."""
        # a played item is the library one where the enqueued item may still be the provider
        # one it was picked from. The media type is compared alongside it because the library
        # numbers each type from one, so ids collide freely across types.
        return any(
            item.media_type == media_item.media_type and compare_item_ids(item, media_item)
            for item in queue_data.enqueued_media_items
        )

    async def _mark_album_played(
        self, album: Album, track: MediaItemType, queue_data: PlayerQueueData
    ) -> None:
        """Mark an enqueued album played, skipping artists already credited via its track."""
        self.logger.debug(
            "Credited album '%s' as played (triggered by track '%s')", album.name, track.name
        )
        skip = await self.mass.music.resolve_library_artist_ids(getattr(track, "artists", []))
        await self.mass.music.mark_item_played(
            album,
            userid=queue_data.userid,
            queue_id=queue_data.queue.queue_id,
            user_initiated=True,
            skip_artist_ids=list(skip),
        )

    def _should_mark_played(
        self, queue_id: str, queue_item_id: str, fully_played: bool, is_playing: bool
    ) -> bool:
        """
        Return whether this playback report should be forwarded to ``mark_item_played``.

        :param queue_id: The id of the queue the report belongs to.
        :param queue_item_id: The id of the queue item being reported.
        :param fully_played: Whether the item was played to completion.
        :param is_playing: Whether the item is still playing.
        """
        queue_data = self._queue_data[queue_id]
        if fully_played and not is_playing:
            # the final queue track is reported twice at end-of-queue; skip the duplicate
            # so a completed play is only counted once
            if queue_data.last_counted_play == queue_item_id:
                return False
            queue_data.last_counted_play = queue_item_id
            return True
        # a not-fully-played report for the same item means it restarted (e.g. on repeat),
        # so re-arm the guard to count its next completion
        if not fully_played and queue_data.last_counted_play == queue_item_id:
            queue_data.last_counted_play = None
        return True
