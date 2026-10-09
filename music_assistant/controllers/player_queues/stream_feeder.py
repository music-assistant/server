"""
Stream feeding for the Player Queues controller.

Handles handing the next queue item to the player and preparing its audio: enqueuing the upcoming
item on the player, preloading its stream details, warming the next track's AudioBuffer ahead of
playback, and cleaning up stale buffers. Owns no per-queue state; it is mixed into the controller
and reads/mutates the controller's `PlayerQueueData` records.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from music_assistant_models.enums import (
    MediaType,
    PlaybackState,
)
from music_assistant_models.errors import (
    AudioError,
    MediaNotFoundError,
    QueueEmpty,
    ResourceBusyError,
)

from music_assistant.constants import (
    ATTR_ANNOUNCEMENT_IN_PROGRESS,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.controllers.player_queues.base import _PlayerQueuesBase
from music_assistant.controllers.players.constants import PlayerLockPurpose
from music_assistant.controllers.streams.constants import STREAM_SLOT_WAIT_TIMEOUT
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_user,
    has_player_access,
)
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.queue_item import QueueItem


class StreamFeederMixin(_PlayerQueuesBase):
    """Feed the player's stream: enqueue the next item, preload/prepare its audio, clean up."""

    def prepare_next_audio_buffer(
        self, queue_id: str, queue_item_id: str
    ) -> asyncio.Task[None] | None:
        """
        Prepare the AudioBuffer of the item that follows the given item in the queue.

        Call when the stream of the given item nears its end, or when its audio has fully
        arrived, so the next item's audio is ready by the time it is streamed.

        :param queue_id: The queue the item belongs to.
        :param queue_item_id: The item whose audio is being streamed or has fully arrived.
        :return: The preparation of the next item's audio, or None when nothing needs preparing.
        """
        next_item = self.get_next_item(queue_id, queue_item_id)
        if next_item is None or next_item.queue_item_id == queue_item_id:
            return None
        queue_data = self._queue_data[queue_id]
        # AudioSource items are realtime/live and bypass the AudioBuffer
        if next_item.media_type == MediaType.AUDIO_SOURCE:
            return None
        # check if buffer already exists and is valid
        if (
            next_item.streamdetails
            and next_item.streamdetails.buffer
            and next_item.streamdetails.buffer.is_valid()
        ):
            # reusing audio an earlier session left behind claims it for this one, so its
            # stop releases it and the earlier session's stop no longer can
            next_item.streamdetails.queue_session_id = queue_data.session_id
            return None

        async def _do_prepare() -> None:
            prepared_item: QueueItem | None = None
            try:
                try:
                    prepared_item = await self.load_next_queue_item(queue_id, queue_item_id)
                except QueueEmpty:
                    return
                # unplayable items are skipped, so the prepared item can be a later one
                queue_data.next_item_id_preparing = prepared_item.queue_item_id
                # the queue can be replaced while the details are fetched, and audio warmed
                # for an item that left it would sit on a buffer no cleanup reaches
                if self.get_item(queue_id, prepared_item.queue_item_id) is None:
                    return
                # a stop releases the audio of its session once; what gets attached to a queue
                # without a session after that stays until its inactivity timeout
                if queue_data.session_id is None:
                    return
                if (streamed_item := self.get_item(queue_id, queue_item_id)) and (
                    holder := self._single_source_slot_holder(streamed_item, prepared_item)
                ):
                    # the streamed item frees that slot only when its source is done, and that
                    # schedules this preparation again, so waiting for it here is pointless
                    self.logger.debug(
                        "Not preparing %s yet: the streamed item holds the only %s source slot",
                        prepared_item.name,
                        holder.name,
                    )
                    return
                self.logger.debug(
                    "Preparing audio buffer for next track %s on queue %s",
                    prepared_item.name,
                    queue_data.queue.display_name,
                )
                await self.mass.streams.audio.get_audio_buffer(
                    prepared_item,
                    reason="prepare_next",
                    capacity_wait_timeout=STREAM_SLOT_WAIT_TIMEOUT,
                    # speculative preparation gives up softly, so it must stay cheap and
                    # harmless: leave the cross-provider search and stopping a paused
                    # queue to the actual playback start
                    allow_provider_match=False,
                    stop_paused_queues=False,
                )
                # removal paths that do not cancel this task (replace_next, delete) can take
                # the item off the queue while the buffer fills; the stale-buffer sweep walks
                # only current items, so a buffer left here would sit until its inactivity
                # timeout. The same goes for a queue whose session ended meanwhile. A session
                # that rotated (a skip) owns the audio, and its stop releases it.
                # Detached before releasing, as everywhere a buffer is cleared.
                if (
                    self.get_item(queue_id, prepared_item.queue_item_id) is None
                    or queue_data.session_id is None
                ):
                    if (details := prepared_item.streamdetails) and (orphan := details.buffer):
                        details.buffer = None
                        await orphan.clear()
            except (AudioError, MediaNotFoundError) as err:
                self.logger.debug("Failed to prepare next audio buffer: %s", err)
            except asyncio.CancelledError:
                # a replacement prepare aborted this one: release the half-filled source
                # so its slot is not pinned until the inactivity sweep
                if (
                    prepared_item
                    and (sd := prepared_item.streamdetails)
                    and (buf := sd.buffer)
                    and buf.is_buffering
                ):
                    await asyncio.shield(buf.clear())
                raise

        # a call for the same item joins the preparation that is already running,
        # one for another item replaces it
        target_changed = queue_data.next_item_id_preparing != next_item.queue_item_id
        queue_data.next_item_id_preparing = next_item.queue_item_id
        task_id = f"prepare_next_audio_buffer_{queue_id}"
        return self.mass.create_task(
            _do_prepare(),
            task_id=task_id,
            task_name=task_id,
            abort_existing=target_changed,
        )

    def track_fully_buffered(self, queue_id: str, item_id: str) -> None:
        """
        Call when the source of a queue item has delivered all of its audio.

        :param queue_id: The queue the item belongs to.
        :param item_id: The queue item whose audio has fully arrived.
        """
        queue_data = self._queue_data.get(queue_id)
        item = self.get_item(queue_id, item_id)
        if queue_data is None or item is None or (streamdetails := item.streamdetails) is None:
            return
        # a source that fills ahead of playback is done long before its item ends; its
        # successor is prepared when the stream of the item nears its end
        if not streamdetails.is_realtime or streamdetails.media_type != MediaType.TRACK:
            return
        # only the item the player is fetching may chain into preparing its successor,
        # so the fills cannot run ahead of the player on their own
        if queue_data.last_served_item_id != item_id:
            return
        self.prepare_next_audio_buffer(queue_id, item_id)

    def has_paused_stream_slot_holder(self, provider_instance: str, queue_id: str) -> bool:
        """
        Return whether a paused queue other than the given one holds a slot of a full provider.

        :param provider_instance: The provider instance a slot is needed on.
        :param queue_id: The queue that needs the slot.
        """
        return self._paused_stream_slot_holder(provider_instance, queue_id) is not None

    async def release_paused_stream_slot(self, provider_instance: str, queue_id: str) -> bool:
        """
        Stop a paused queue other than the given one that holds a slot of a full provider.

        The stopped queue resumes from where it was paused, with a new source stream.

        :param provider_instance: The provider instance a slot is needed on.
        :param queue_id: The queue that needs the slot, which is never stopped itself.
        :return: Whether a paused queue's session was ended, which frees its slot shortly.
        """
        if (holder_id := self._paused_stream_slot_holder(provider_instance, queue_id)) is None:
            return False
        async with self.mass.players.get_group_and_player_lock(holder_id):
            # while the lock was held elsewhere the queue can have resumed, or its source
            # can have finished and handed the slot to someone else
            if self._paused_stream_slot_holder(provider_instance, queue_id) != holder_id:
                return False
            holder = self._queue_data[holder_id]
            self.logger.info(
                "Stopping paused queue %s, another queue needs its %s stream slot",
                holder.queue.display_name,
                provider_instance,
            )
            try:
                await self._handle_stop(holder_id)
            except Exception as err:
                # deliberately broad: the device stop is a raw provider call that can surface
                # anything its client library raises, on a player the requesting playback has
                # nothing to do with. CancelledError is a BaseException and still propagates.
                self.logger.warning(
                    "Stopping paused queue %s failed: %s", holder.queue.display_name, err
                )
            # a failed device stop still ends the session, and ending it is what frees the slot
            return holder.session_id is None

    def update_next_item_on_player(self, queue_id: str, force: bool = False) -> None:
        """
        Hand the player the track that now follows the one it is playing.

        Does nothing when the player already holds that track, so a queue change that leaves the
        upcoming track alone costs nothing.

        :param queue_id: The queue whose player should be updated.
        :param force: Hand it over even when the player already holds it, for a change that
            alters how the same track is streamed rather than which track it is.
        """
        queue_data = self._queue_data[queue_id]
        queue = queue_data.queue
        if queue.state != PlaybackState.PLAYING or queue.current_index is None:
            return
        if queue.index_in_buffer is None or queue_data.transitioning:
            # no settled position to follow: a replace clears the buffered index while it swaps
            # the items, and a starting track moves the two indexes one after the other
            return
        next_item = self.get_next_item(queue_id, queue.current_index)
        if next_item is None:
            return
        if not force and next_item.queue_item_id == queue_data.next_item_id_enqueued:
            return
        self._enqueue_next_item(queue_id, next_item)

    def _enqueue_next_item(self, queue_id: str, next_item: QueueItem | None) -> None:
        """Enqueue the next item on the player."""
        if not next_item:
            # no next item, nothing to do...
            return

        queue_data = self._queue_data[queue_id]
        queue = queue_data.queue
        session_id = queue_data.session_id
        if queue.flow_mode:
            # ignore this for flow mode
            return

        async def _hand_over_to_player(next_item: QueueItem) -> None:
            player = self.mass.players.get_player(queue_id)
            if (
                player is None
                or player.state.playback_state != PlaybackState.PLAYING
                or player.state.active_source not in (queue.queue_id, None)
                or queue_data.session_id != session_id
                or queue.flow_mode
            ):
                # nothing re-attempts this handover, so a skip here means the player runs out
                # of audio when the current track ends - leave a trace of why it was skipped
                self.logger.debug(
                    "Not enqueuing next track %s on queue %s "
                    "(state: %s, source: %s, same session: %s, flow mode: %s)",
                    next_item.name,
                    queue.display_name,
                    player.state.playback_state if player else "player unavailable",
                    player.state.active_source if player else None,
                    queue_data.session_id == session_id,
                    queue.flow_mode,
                )
                return

            current_item = queue.current_item
            if current_item is None:
                return
            current_next = self.get_next_item(queue_id, current_item.queue_item_id)
            if current_next is None or current_next.queue_item_id != next_item.queue_item_id:
                return

            await self.mass.players.enqueue_next_media(
                player_id=queue_id,
                media=await self.player_media_from_queue_item(next_item),
            )
            if queue_data.next_item_id_enqueued != next_item.queue_item_id:
                queue_data.next_item_id_enqueued = next_item.queue_item_id
                self.logger.debug(
                    "Enqueued next track %s on queue %s",
                    next_item.name,
                    queue.display_name,
                )

        async def _enqueue_next_item_on_player(next_item: QueueItem) -> None:
            # Player state updates can lag behind queue loading, so wait before validating.
            async with self.mass.players.wait_for_player_update(
                queue_id,
                attribute_name="playback_state",
                attribute_value=PlaybackState.PLAYING,
            ):
                pass

            # the checks must see the queue as a play action that holds the lock leaves it,
            # so they only run once this holds the lock too. A handover that cannot get it
            # in time is dropped: a changed queue schedules a fresh one, replacing this one.
            try:
                async with self.mass.players.get_player_lock(
                    queue_id, PlayerLockPurpose.PLAYBACK, strict=True
                ):
                    await _hand_over_to_player(next_item)
            except ResourceBusyError:
                self.logger.debug(
                    "Not enqueuing next track %s on queue %s: the player is still busy",
                    next_item.name,
                    queue.display_name,
                )

        task_id = f"enqueue_next_item_{queue_id}"
        self.mass.call_later(1, _enqueue_next_item_on_player, next_item, task_id=task_id)

    def _preload_next_item(self, queue_id: str, item_id_in_buffer: str) -> None:
        """
        Preload the streamdetails for the next item in the queue/buffer.

        This basically ensures the item is playable and fetches the stream details.
        If an error occurs, the item will be skipped and the next item will be loaded.
        """
        queue = self._queue_data[queue_id].queue

        async def _preload_streamdetails(item_id_in_buffer: str) -> None:
            try:
                # wait for the item that was loaded in the buffer is the actually playing item
                # this prevents a race condition when we preload the next item too soon
                # while the player is actually preloading the previously enqueued item.
                current_item = queue.current_item
                if current_item is None:
                    return  # guard
                retries = max(120, int(current_item.duration or 0) + 10)
                for _ in range(retries):
                    # the queue can drain to empty while we sleep (e.g. all remaining
                    # items skipped as unplayable); stop waiting once it has no current item
                    current_item = queue.current_item
                    if current_item is None:
                        return
                    if current_item.queue_item_id == item_id_in_buffer:
                        break
                    await asyncio.sleep(1)
                next_item = self.get_next_item(queue_id, item_id_in_buffer)
                if queue.flow_mode and (
                    next_item is None or next_item.queue_item_id == item_id_in_buffer
                ):
                    # Loading a repeat resets the shared item's seek offset while its audio
                    # is still playing. A deeper scan can also wrap back to this item when
                    # the short scan finds no candidate. Leave that load to the flow transition.
                    return
                if next_item := await self.load_next_queue_item(queue_id, item_id_in_buffer):
                    self.logger.debug(
                        "Preloaded next item %s for queue %s",
                        next_item.name,
                        queue.display_name,
                    )
                    # enqueue the next item on the player
                    self._enqueue_next_item(queue_id, next_item)

            except QueueEmpty:
                return

        if not (current_item := self.get_item(queue_id, item_id_in_buffer)):
            # this should not happen, but guard anyways
            return
        if current_item.media_type == MediaType.RADIO or not current_item.duration:
            # radio items or no duration, nothing to do
            return

        task_id = f"preload_next_item_{queue_id}"
        self.mass.create_task(
            _preload_streamdetails(item_id_in_buffer),
            task_id=task_id,
            task_name=task_id,
            abort_existing=True,
        )

    async def _cleanup_stale_queue_buffers(self, queue_id: str, current_index: int) -> None:
        """
        Clean up audio buffers for queue items that are no longer needed.

        This clears buffers for items at index <= current_index - 2, keeping only:
        - The previous track (current_index - 1)
        - The current track (current_index)
        - The next track (current_index + 1, handled by preloading)

        :param queue_id: The queue ID to clean up buffers for.
        :param current_index: The current playing index in the queue.
        """
        if current_index < 2:
            return  # Nothing to clean up yet

        queue_items = queue_data.items if (queue_data := self._queue_data.get(queue_id)) else []
        cleanup_threshold = current_index - 2
        buffers_cleared = 0

        for idx, item in enumerate(queue_items):
            if idx > cleanup_threshold:
                break  # No need to check further
            if (streamdetails := item.streamdetails) and (buffer := streamdetails.buffer):
                self.logger.log(
                    VERBOSE_LOG_LEVEL,
                    "Clearing stale audio buffer for queue item %s (index %d) in queue %s",
                    item.name,
                    idx,
                    queue_id,
                )
                # detached before releasing, as in _cleanup_queue_audio_data
                streamdetails.buffer = None
                await buffer.clear()
                buffers_cleared += 1

        if buffers_cleared > 0:
            self.logger.debug(
                "Cleared %d stale audio buffer(s) for queue %s (items before index %d)",
                buffers_cleared,
                queue_id,
                cleanup_threshold + 1,
            )

    async def _cleanup_queue_audio_data(self, queue_id: str, session_id: str | None = None) -> None:
        """
        Clean up all audio-related data for a queue when it is stopped or cleared.

        This clears:
        - All audio buffers attached to queue item streamdetails
        - Any pending crossfade data for the queue

        :param queue_id: The queue ID to clean up.
        :param session_id: The playback session being stopped. Audio the queue's currently
            playing session claimed is left alone; everything else is released, including
            what sessions that ended earlier left behind. None clears every buffer.
        """
        self.mass.streams.audio.clear_crossfade_handover(queue_id)

        queue_data = self._queue_data.get(queue_id)
        queue_items = queue_data.items if queue_data else []
        buffers_cleared = 0

        for item in queue_items:
            if not (streamdetails := item.streamdetails) or not (buffer := streamdetails.buffer):
                continue
            # read the playing session per item rather than once: releasing a buffer suspends,
            # and a session that starts during one of those waits owns what it attaches after.
            # A session id only protects audio while that session is the one playing - sessions
            # rotate without a stop, so a claim that is no longer current marks audio nobody
            # will come back for.
            playing_session = queue_data.session_id if queue_data else None
            if (
                session_id is not None
                and playing_session not in (None, session_id)
                and streamdetails.queue_session_id == playing_session
            ):
                # playback restarted here while this stop was still running; killing its
                # producer would strand the session that is playing now
                continue
            # detach before releasing: clearing suspends on the producer's cancellation, and a
            # session starting in that window attaches its own buffer here
            streamdetails.buffer = None
            await buffer.clear()
            buffers_cleared += 1

        if buffers_cleared > 0:
            self.logger.debug(
                "Cleared %d audio buffer(s) for stopped/cleared queue %s",
                buffers_cleared,
                queue_id,
            )

    def _single_source_slot_holder(
        self, streamed_item: QueueItem, next_item: QueueItem
    ) -> MusicProvider | None:
        """
        Return the next item's source if the streamed realtime item holds its only slot.

        :param streamed_item: The item whose audio is being streamed ahead of the next item.
        :param next_item: The item that is about to be prepared.
        """
        playing = streamed_item.streamdetails
        upcoming = next_item.streamdetails
        if playing is None or upcoming is None or playing.provider != upcoming.provider:
            return None
        # the end of a realtime fill schedules this preload again; for any other source
        # nothing does, so that one keeps waiting for the slot
        if not playing.is_realtime or (buffer := playing.buffer) is None or buffer.eof:
            return None
        # the exact instance: a lookup by domain may land on a sibling instance's budget
        provider = self.mass.get_provider(playing.provider, return_unavailable=True)
        if (
            isinstance(provider, MusicProvider)
            and provider.max_concurrent_streams == 1
            and not provider.has_available_stream_slot
        ):
            return provider
        return None

    def _paused_stream_slot_holder(self, provider_instance: str, queue_id: str) -> str | None:
        """
        Return a paused queue other than the given one that holds a slot of a full provider.

        :param provider_instance: The provider instance a slot is needed on.
        :param queue_id: The queue that needs the slot.
        """
        provider = self.mass.get_provider(provider_instance, return_unavailable=True)
        if not isinstance(provider, MusicProvider) or provider.has_available_stream_slot:
            return None
        user = get_current_user()
        for holder_id, queue_data in self._queue_data.items():
            # a stop only releases the audio of a session, so a queue without one has
            # nothing to hand over
            if (
                holder_id == queue_id
                or queue_data.session_id is None
                or not self._is_paused(holder_id)
            ):
                continue
            # the starting user only takes the slot of a player they may control
            if not has_player_access(user, holder_id, self.mass.players.get_player(holder_id)):
                continue
            # a buffer holds its provider's slot from its first audio until its source stops
            # producing; one that is not ready yet can still be waiting for a slot itself
            if any(
                (details := item.streamdetails) is not None
                and details.provider == provider_instance
                and details.buffer is not None
                and details.buffer.is_buffering
                and details.buffer.ready.is_set()
                for item in queue_data.items
            ):
                return holder_id
        return None

    def _is_paused(self, queue_id: str) -> bool:
        """Return whether the queue is paused and its player confirms it is paused on it."""
        queue_data = self._queue_data.get(queue_id)
        player = self.mass.players.get_player(queue_id)
        return (
            queue_data is not None
            and queue_data.queue.state == PlaybackState.PAUSED
            and player is not None
            and player.state.playback_state == PlaybackState.PAUSED
            and player.state.active_source == queue_id
            and not player.extra_data.get(ATTR_ANNOUNCEMENT_IN_PROGRESS)
        )
