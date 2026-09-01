"""
Stream feeding for the Player Queues controller.

Handles handing the next queue item to the player and preparing its audio: enqueuing the upcoming
item on the player, preloading its stream details, warming the next track's AudioBuffer ahead of
playback, and cleaning up stale buffers. Owns no per-queue state; it is mixed into the controller
and reads/mutates the controller's `PlayerQueueData` records.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final

from music_assistant_models.enums import (
    MediaType,
    PlaybackState,
)
from music_assistant_models.errors import (
    AudioError,
    MediaNotFoundError,
    QueueEmpty,
)

from music_assistant.constants import (
    VERBOSE_LOG_LEVEL,
)
from music_assistant.controllers.player_queues.base import _PlayerQueuesBase
from music_assistant.controllers.streams.audio_buffer import AudioBuffer
from music_assistant.controllers.streams.constants import STREAM_SLOT_WAIT_TIMEOUT

if TYPE_CHECKING:
    from music_assistant_models.queue_item import QueueItem

    from music_assistant.controllers.streams.audio_buffer import ProviderAudioFill

# How far ahead of the playing item to look for the item a live source is producing.
# A source that plays on by itself runs ahead of the player, but never far: what it
# produces has to be held until playback gets there.
LIVE_AUDIO_SEARCH_DEPTH: Final[int] = 3


class StreamFeederMixin(_PlayerQueuesBase):
    """Feed the player's stream: enqueue the next item, preload/prepare its audio, clean up."""

    async def open_provider_audio_fill(
        self, queue_id: str, provider_instance_id: str, provider_item_id: str
    ) -> ProviderAudioFill | None:
        """
        Open the audio buffer of an upcoming queue item its provider is producing audio for.

        For a source that plays on by itself an item's audio exists only while the source
        is on that item, so the source says when to buffer it rather than the queue
        guessing. The returned handle is what that audio is written into; a later playback
        request for the item finds the buffer and reuses it.

        :param queue_id: Queue the source is serving.
        :param provider_instance_id: Provider instance producing the audio.
        :param provider_item_id: The provider's own id of the item now being produced.
        :return: The handle to write the item's audio into, or None when the queue has no
            upcoming item this audio belongs to (it moved on, or another provider serves it).
        """
        queue_item = self._upcoming_provider_item(queue_id, provider_instance_id, provider_item_id)
        if queue_item is None:
            return None
        if (streamdetails := queue_item.streamdetails) is None:
            try:
                streamdetails = await self.mass.streams.audio.get_stream_details(
                    queue_item=queue_item
                )
            except (AudioError, MediaNotFoundError) as err:
                self.logger.debug(
                    "No stream details for %s, cannot buffer its live audio: %s",
                    queue_item.name,
                    err,
                )
                return None
            queue_item.streamdetails = streamdetails
        if streamdetails.provider != provider_instance_id:
            # the queue resolved this item to another provider, so this audio is not its
            return None
        if (
            (existing := streamdetails.buffer) is not None
            and not existing.has_error
            and existing.is_valid()
        ):
            # already being filled (or filled) elsewhere; taking it over would split the
            # item. One that failed is replaced instead, as a playback request would
            return None
        # record whose audio this is, so a queue stop releases the buffer with its session
        if (queue_data := self._queue_data.get(queue_id)) is not None:
            streamdetails.queue_session_id = queue_data.session_id
        self.logger.debug(
            "Buffering live %s audio for %s on queue %s",
            provider_instance_id,
            queue_item.name,
            queue_id,
        )
        return AudioBuffer.open_provider_fill(self.mass, streamdetails, reason="source_live")

    def prepare_next_audio_buffer(self, queue_id: str) -> None:
        """
        Prepare the AudioBuffer for the next track in the queue.

        Called ~30-60 seconds before the current track ends to ensure
        the buffer is warm when the next track starts playing.
        """
        queue = self.get(queue_id)
        if not queue or not queue.next_item:
            return
        next_item = queue.next_item
        # AudioSource items are realtime/live and bypass the AudioBuffer
        if next_item.media_type == MediaType.AUDIO_SOURCE:
            return
        # guard against race condition where queue.next_item still points to the
        # currently playing track because the player state hasn't been updated yet
        if queue.current_item and next_item.queue_item_id == queue.current_item.queue_item_id:
            return
        # check if buffer already exists and is valid
        if (
            next_item.streamdetails
            and next_item.streamdetails.buffer
            and next_item.streamdetails.buffer.is_valid()
        ):
            # reusing audio an earlier session left behind claims it for this one, so its
            # stop releases it and the earlier session's stop no longer can
            next_item.streamdetails.queue_session_id = self._queue_data[queue_id].session_id
            return

        async def _do_prepare() -> None:
            try:
                # fetch streamdetails if not yet available
                if not next_item.streamdetails:
                    next_item.streamdetails = await self.mass.streams.audio.get_stream_details(
                        queue_item=next_item
                    )
                self.logger.debug(
                    "Preparing audio buffer for next track %s on queue %s",
                    next_item.name,
                    queue.display_name,
                )
                await self.mass.streams.audio.get_audio_buffer(
                    next_item,
                    reason="prepare_next",
                    capacity_wait_timeout=STREAM_SLOT_WAIT_TIMEOUT,
                    # speculative preparation gives up softly, so it must stay cheap:
                    # leave the cross-provider search to the actual playback start
                    allow_provider_match=False,
                )
            except (AudioError, MediaNotFoundError) as err:
                self.logger.debug("Failed to prepare next audio buffer: %s", err)
            except asyncio.CancelledError:
                # a replacement prepare aborted this one: release the half-filled source
                # so its slot is not pinned until the inactivity sweep
                if (sd := next_item.streamdetails) and (buf := sd.buffer) and buf.is_buffering:
                    await asyncio.shield(buf.clear())
                raise

        self.mass.create_task(
            _do_prepare,
            task_id=f"prepare_next_audio_buffer_{queue_id}",
            abort_existing=True,
        )

    def _upcoming_provider_item(
        self, queue_id: str, provider_instance_id: str, provider_item_id: str
    ) -> QueueItem | None:
        """
        Return the upcoming queue item that maps to the given provider item.

        Located by provider mapping rather than by position: a source producing an item's
        audio knows nothing of the queue's order, which a reorder may have changed.

        :param queue_id: Queue to look in.
        :param provider_instance_id: Provider instance the item must map to.
        :param provider_item_id: The provider's own id of the item.
        """
        queue = self.get(queue_id)
        if queue is None or queue.current_index is None:
            return None
        for offset in range(LIVE_AUDIO_SEARCH_DEPTH):
            item = self.get_item(queue_id, queue.current_index + offset)
            if item is None:
                return None
            if item.media_type == MediaType.AUDIO_SOURCE or (media_item := item.media_item) is None:
                # AudioSource items are live and bypass the AudioBuffer entirely
                continue
            if media_item.provider == provider_instance_id and media_item.item_id == (
                provider_item_id
            ):
                return item
            if any(
                mapping.provider_instance == provider_instance_id
                and mapping.item_id == provider_item_id
                for mapping in media_item.provider_mappings
            ):
                return item
        return None

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

        async def _enqueue_next_item_on_player(next_item: QueueItem) -> None:
            # Player state updates can lag behind queue loading, so wait before validating.
            async with self.mass.players.wait_for_player_update(
                queue_id,
                attribute_name="playback_state",
                attribute_value=PlaybackState.PLAYING,
            ):
                pass

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
            _preload_streamdetails,
            item_id_in_buffer,
            task_id=task_id,
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
        self.mass.streams.audio.clear_crossfade_data(queue_id)

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
