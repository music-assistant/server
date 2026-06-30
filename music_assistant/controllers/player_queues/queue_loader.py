"""
Queue loading for the Player Queues controller.

Applies the enqueue option (play/replace/next/add) to a batch of resolved items, loads a single
media item into the queue, resumes from the play-log when the queue is empty, computes the next
index, and refills the queue (dynamic managed-pool fill and autoplay fill). Owns no per-queue state;
it reads and mutates the controller's `PlayerQueueData` records via its owning controller.
"""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import (
    MediaType,
    PlaybackState,
    QueueOption,
    RepeatMode,
)
from music_assistant_models.errors import (
    MediaNotFoundError,
    MusicAssistantError,
)
from music_assistant_models.media_items import (
    Album,
    Track,
    UniqueList,
)
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues.autoplay import (
    AutoplayMode,
)
from music_assistant.controllers.player_queues.constants import (
    MANAGED_POOL_MAX,
)
from music_assistant.controllers.player_queues.managed_pool import gate_tracks
from music_assistant.controllers.streams.audio_buffer import AudioBuffer
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    set_current_user,
)
from music_assistant.helpers.throttle_retry import BYPASS_THROTTLER

if TYPE_CHECKING:
    from music_assistant_models.media_items.metadata import MediaItemImage
    from music_assistant_models.player_queue import PlayerQueue

    from music_assistant.controllers.player_queues.controller import PlayerQueuesController


class QueueLoader:
    """Load items into a queue: apply the enqueue option, resolve single items, refill the pool."""

    def __init__(self, queues: PlayerQueuesController) -> None:
        """
        Initialize the queueloader.

        :param queues: The owning player queues controller.
        """
        self.queues = queues
        self.mass = queues.mass
        self.logger = queues.logger

    async def _enqueue_with_option(
        self,
        queue_id: str,
        queue_items: list[QueueItem],
        option: QueueOption | None,
        managed_pool: bool,
    ) -> None:
        """Load queue items into the queue according to the given enqueue option."""
        queue = self.queues._queue_data[queue_id].queue
        if queue.state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            cur_index = (
                queue.index_in_buffer
                if queue.index_in_buffer is not None
                else (queue.current_index if queue.current_index is not None else 0)
            )
        else:
            cur_index = queue.current_index or 0
        insert_at_index = cur_index + 1
        # Managed-pool tracks are already ordered in a pattern we want to keep.
        shuffle = queue.shuffle_enabled and len(queue_items) > 1 and not managed_pool

        # handle replace: clear all items and replace with the new items
        if option == QueueOption.REPLACE:
            await self.queues.load(
                queue_id,
                queue_items=queue_items,
                keep_remaining=False,
                keep_played=False,
                shuffle=shuffle,
            )
            await self.queues.play_index(queue_id, 0)
            return
        # handle next: add item(s) in the index next to the playing/loaded/buffered index
        if option == QueueOption.NEXT:
            await self.queues.load(
                queue_id,
                queue_items=queue_items,
                insert_at_index=insert_at_index,
                shuffle=shuffle,
            )
            return
        if option == QueueOption.REPLACE_NEXT:
            await self.queues.load(
                queue_id,
                queue_items=queue_items,
                insert_at_index=insert_at_index,
                keep_remaining=False,
                shuffle=shuffle,
            )
            return
        # handle play: replace current loaded/playing index with new item(s)
        if option == QueueOption.PLAY:
            await self.queues.load(
                queue_id,
                queue_items=queue_items,
                insert_at_index=insert_at_index,
                shuffle=shuffle,
            )
            next_index = min(insert_at_index, len(self.queues._queue_data[queue_id].items) - 1)
            await self.queues.play_index(queue_id, next_index)
            return
        # handle add: add/append item(s) to the remaining queue items
        if option == QueueOption.ADD:
            # When shuffling, mix the new items into the not-yet-played tail. While playing,
            # keep the item right after the buffered one in place: it has already been enqueued
            # to the player (and prepared for crossfade), so reshuffling it would swap the
            # upcoming track underneath the player and cause an abrupt, non-crossfaded switch.
            if not queue.shuffle_enabled:
                add_at_index = len(self.queues._queue_data[queue_id].items) + 1
            elif queue.state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
                add_at_index = insert_at_index + 1
            else:
                add_at_index = insert_at_index
            await self.queues.load(
                queue_id=queue_id,
                queue_items=queue_items,
                insert_at_index=add_at_index,
                # managed-pool tracks are already ordered in a pattern we want to keep
                shuffle=queue.shuffle_enabled and not managed_pool,
            )
            # handle edgecase, queue is empty and items are only added (not played)
            # mark first item as new index
            if queue.current_index is None:
                queue.current_index = 0
                queue.current_item = self.queues.get_item(queue_id, 0)
                queue.items = len(queue_items)
                self.queues.signal_update(queue_id)

    async def _load_item(
        self,
        queue_item: QueueItem,
        next_index: int | None,
        is_start: bool = False,
        seek_position: int = 0,
        fade_in: bool = False,
    ) -> None:
        """Try to load the stream details for the given queue item."""
        queue_id = queue_item.queue_id
        queue = self.queues._queue_data[queue_id].queue

        # we use a contextvar to bypass the throttler for this asyncio task/context
        # this makes sure that playback has priority over other requests that may be
        # happening in the background
        BYPASS_THROTTLER.set(True)

        self.logger.debug(
            "(pre)loading (next) item for queue %s...",
            queue.display_name,
        )

        if not queue_item.available:
            raise MediaNotFoundError(f"Item {queue_item.uri} is not available")

        # work out if we are playing an album and if we should prefer album
        # loudness
        next_track_from_same_album = (
            next_index is not None
            and (next_item := self.queues.get_item(queue_id, next_index))
            and (
                queue_item.media_item
                and hasattr(queue_item.media_item, "album")
                and queue_item.media_item.album
                and next_item.media_item
                and hasattr(next_item.media_item, "album")
                and next_item.media_item.album
                and queue_item.media_item.album.item_id == next_item.media_item.album.item_id
            )
        )
        current_index = self.queues.index_by_id(queue_id, queue_item.queue_item_id)
        if current_index is None:
            previous_track_from_same_album = False
        else:
            previous_index = max(current_index - 1, 0)
            previous_track_from_same_album = (
                previous_index > 0
                and (previous_item := self.queues.get_item(queue_id, previous_index)) is not None
                and previous_item.media_item is not None
                and hasattr(previous_item.media_item, "album")
                and previous_item.media_item.album is not None
                and queue_item.media_item is not None
                and hasattr(queue_item.media_item, "album")
                and queue_item.media_item.album is not None
                and queue_item.media_item.album.item_id == previous_item.media_item.album.item_id
            )
        playing_album_tracks = next_track_from_same_album or previous_track_from_same_album
        if queue_item.media_item and isinstance(queue_item.media_item, Track):
            album = queue_item.media_item.album
            # prefer the full library media item so we have all metadata and provider(quality) info
            # always request the full library item as there might be other qualities available
            if library_item := await self.mass.music.get_library_item_by_prov_id(
                queue_item.media_item.media_type,
                queue_item.media_item.item_id,
                queue_item.media_item.provider,
            ):
                queue_item.media_item = cast("Track", library_item)
            elif not queue_item.media_item.image or queue_item.media_item.provider.startswith(
                "ytmusic"
            ):
                # Youtube Music has poor thumbs by default, so we always fetch the full item
                # this also catches the case where they have an unavailable item in a listing
                fetched_item = await self.mass.music.get_item_by_uri(queue_item.uri)
                queue_item.media_item = cast("Track", fetched_item)

            # ensure we got the full (original) album set
            if album and (
                library_album := await self.mass.music.get_library_item_by_prov_id(
                    album.media_type,
                    album.item_id,
                    album.provider,
                )
            ):
                queue_item.media_item.album = cast("Album", library_album)
            elif album:
                # Restore original album if we have no better alternative from the library
                queue_item.media_item.album = album
            # prefer album image over track image
            if queue_item.media_item.album and queue_item.media_item.album.image:
                org_images: list[MediaItemImage] = queue_item.media_item.metadata.images or []
                queue_item.media_item.metadata.images = UniqueList(
                    [
                        queue_item.media_item.album.image,
                        *org_images,
                    ]
                )
        # Fetch streamdetails (reuses existing if buffer is still valid for the seek).
        queue_item.streamdetails = await self.mass.streams.audio.get_stream_details(
            queue_item=queue_item,
            seek_position=seek_position,
            fade_in=fade_in,
            prefer_album_loudness=bool(playing_album_tracks),
        )
        # update queue_item.duration from streamdetails if we got a better value
        if queue_item.streamdetails.duration and not queue_item.duration:
            queue_item.duration = queue_item.streamdetails.duration
            self.queues.signal_update(queue_id, items_changed=True)

        # pre-initialize the AudioBuffer so audio is ready
        # when the player requests it. For the current/first track this ensures
        # immediate playback start. For preloaded next tracks we skip this and
        # initialize the buffer ~30s before the current track ends instead.
        # AudioSource items are realtime/live and bypass the AudioBuffer.
        if is_start and queue_item.streamdetails.media_type != MediaType.AUDIO_SOURCE:
            await AudioBuffer.get_buffer(
                self.mass,
                queue_item.streamdetails,
                seek_position_ms=int(seek_position * 1000),
                wait_ready=True,
                reason="prepare",
            )

    def _get_next_index(
        self,
        queue_id: str,
        cur_index: int | None,
        is_skip: bool = False,
        allow_repeat: bool = True,
    ) -> int | None:
        """
        Return the next index for the queue, accounting for repeat settings.

        Will return None if there are no (more) items in the queue.
        """
        queue = self.queues._queue_data[queue_id].queue
        queue_items = self.queues._queue_data[queue_id].items
        if not queue_items or cur_index is None:
            # queue is empty
            return None
        # handle repeat single track
        if queue.repeat_mode == RepeatMode.ONE and not is_skip:
            return cur_index if allow_repeat else None
        # handle cur_index is last index of the queue
        if cur_index >= (len(queue_items) - 1):
            if allow_repeat and queue.repeat_mode == RepeatMode.ALL:
                # if repeat all is enabled, we simply start again from the beginning
                return 0
            return None
        # all other: just the next index
        return cur_index + 1

    async def _fill_dynamic_tracks(self, queue_id: str) -> None:
        """Fill a Queue with (additional) tracks from its dynamic sources."""
        self.logger.debug(
            "Filling dynamic tracks for queue %s",
            queue_id,
        )
        queue = self.queues._queue_data[queue_id].queue
        # restore the queue owner's user context so provider filters are respected during this
        # background refill (dynamic-playlist generation honours the current user)
        playback_user = (
            await self.mass.webserver.auth.get_user(queue.userid) if queue.userid else None
        )
        set_current_user(playback_user)
        # Top up from the queue's dynamic sources (dynamic playlists and any mixed-in finite items),
        # weighted per source and recency-gated. fill() already sizes the batch to the pool target;
        # the tail cap below is a defensive ceiling so the unplayed tail never grows past
        # MANAGED_POOL_MAX.
        pool_tracks = await self.queues._managed_pool.fill(queue_id, is_initial=False)
        # keep the unplayed tail within the bounded pool size (no current_index => nothing played yet)
        played = 0 if queue.current_index is None else queue.current_index + 1
        unplayed = max(len(self.queues._queue_data[queue_id].items) - played, 0)
        headroom = max(MANAGED_POOL_MAX - unplayed, 0)
        queue_items = [
            QueueItem.from_media_item(queue_id, x) for x in pool_tracks[:headroom] if x.available
        ]
        if not queue_items:
            return
        await self.queues.load(
            queue_id,
            queue_items,
            insert_at_index=len(self.queues._queue_data[queue_id].items) + 1,
        )

    async def _fill_autoplay_tracks(self, queue_id: str) -> None:
        """Fill a Queue with additional tracks based on the configured Autoplay mode."""
        queue = self.queues.get(queue_id)
        if queue is None or not queue.autoplay_enabled:
            return
        mode = self.queues._autoplay.resolve_mode(queue_id)
        self.logger.debug(
            "Filling autoplay tracks (mode: %s) for queue %s", mode.value, queue.display_name
        )
        # Restore the queue owner's user context so provider filters and library access
        # are respected during this background refill, mirroring _fill_dynamic_tracks.
        playback_user = (
            await self.mass.webserver.auth.get_user(queue.userid) if queue.userid else None
        )
        set_current_user(playback_user)
        existing_tracks = {
            item.media_item
            for item in self.queues._queue_data[queue_id].items
            if isinstance(item.media_item, Track)
        }
        try:
            if mode == AutoplayMode.PLAYLIST:
                tracks = await self.queues._autoplay.get_playlist_tracks(queue, existing_tracks)
            elif mode == AutoplayMode.LIBRARY:
                tracks = await self.queues._autoplay.get_library_tracks(queue, existing_tracks)
            elif mode == AutoplayMode.SIMILAR:
                tracks = await self.queues._get_similar_tracks(
                    queue_id, seed_items=queue.enqueued_media_items
                )
            else:
                # AUTO: try similar tracks first, fall back to the library mix. The similar
                # fetch raises when no provider can supply base/similar tracks, so suppress
                # that here to make sure the library fallback still runs.
                tracks = []
                with suppress(MusicAssistantError):
                    tracks = await self.queues._get_similar_tracks(
                        queue_id, seed_items=queue.enqueued_media_items
                    )
                if not tracks:
                    tracks = await self.queues._autoplay.get_library_tracks(queue, existing_tracks)
        except MusicAssistantError as err:
            self.logger.warning(
                "Autoplay failed to fetch tracks for queue %s: %s", queue.display_name, err
            )
            return
        # route the autoplay batch through the recency engine so a recently-heard track isn't
        # immediately re-added (ungated fallback keeps autoplay going if everything is recent)
        windows = self.queues._smart_shuffle._windows(queue_id)
        snapshot = await self.mass.music.recency.snapshot(windows, userid=queue.userid)
        tracks = gate_tracks(
            [track for track in tracks if isinstance(track, Track)], snapshot, windows
        )
        queue_items = [QueueItem.from_media_item(queue_id, x) for x in tracks if x.available]
        if not queue_items:
            self.logger.info("Autoplay found no new tracks to add for queue %s", queue.display_name)
            return
        await self.queues.load(
            queue_id,
            queue_items,
            insert_at_index=len(self.queues._queue_data[queue_id].items) + 1,
        )

    async def _try_resume_from_playlog(self, queue: PlayerQueue) -> bool:
        """
        Try to resume playback from playlog when queue is empty.

        Attempts to find user-initiated recently played items in the following order:
        1. By userid AND queue_id
        2. By queue_id only
        3. By userid only (if available)
        4. Any recently played item

        :param queue: The queue to resume playback on.
        :return: True if playback was started, False otherwise.
        """
        # Try different filter combinations in order of specificity
        filter_attempts: list[tuple[str | None, str | None, str]] = []
        if queue.userid:
            filter_attempts.append((queue.userid, queue.queue_id, "userid + queue_id match"))
        filter_attempts.append((None, queue.queue_id, "queue_id match"))
        if queue.userid:
            filter_attempts.append((queue.userid, None, "userid match"))
        filter_attempts.append((None, None, "any recent item"))

        for userid, queue_id, match_type in filter_attempts:
            items = await self.mass.music.recently_played(
                limit=5,
                fully_played_only=False,
                user_initiated_only=True,
                userid=userid,
                queue_id=queue_id,
            )
            for item in items:
                if not item.uri:
                    continue
                try:
                    await self.queues._handle_play_media(queue.queue_id, item)
                    self.logger.info(
                        "Resumed queue %s from playlog (%s)", queue.display_name, match_type
                    )
                    return True
                except MusicAssistantError as err:
                    self.logger.debug("Failed to resume with item %s: %s", item.name, err)
                    continue

        return False
