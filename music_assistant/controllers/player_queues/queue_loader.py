"""
Queue loading for the Player Queues controller.

Applies the enqueue option (play/replace/next/add) to a batch of resolved items, loads a single
media item into the queue, resumes from the play-log when the queue is empty, computes the next
index, and refills the queue (dynamic managed-pool fill and autoplay fill). Owns no per-queue state;
it is mixed into the controller and reads/mutates the controller's `PlayerQueueData` records.
"""
# ruff: noqa: PLR0915

from __future__ import annotations

import random
from contextlib import suppress
from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import (
    MediaType,
    PlaybackState,
    QueueOption,
    RepeatMode,
)
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    MusicAssistantError,
    PlayerUnavailableError,
)
from music_assistant_models.media_items import (
    Album,
    Audiobook,
    BrowseFolder,
    ItemMapping,
    MediaItemType,
    PlayableMediaItemType,
    PodcastEpisode,
    Track,
    UniqueList,
    media_from_dict,
)

from music_assistant.constants import ATTR_ANNOUNCEMENT_IN_PROGRESS
from music_assistant.controllers.player_queues.autoplay import (
    AUTOPLAY_EXCLUDED_MEDIA_TYPES,
    AUTOPLAY_SERIES_MEDIA_TYPES,
    AutoplayMode,
)
from music_assistant.controllers.player_queues.base import _PlayerQueuesBase
from music_assistant.controllers.player_queues.constants import (
    CONF_DEFAULT_ENQUEUE_OPTION_LIVE_SOURCES,
    MANAGED_POOL_MAX,
    ORDERED_MEDIA_TYPES,
    PROBED_DURATION_MEDIA_TYPES,
)
from music_assistant.controllers.player_queues.helpers import (
    build_queue_item,
    handle_play_action,
    has_dynamic_source,
    is_dynamic_source,
)
from music_assistant.controllers.player_queues.managed_pool import gate_tracks
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_user,
    set_current_user,
)
from music_assistant.helpers.audio import get_probed_duration, store_probed_duration
from music_assistant.helpers.compare import compare_item_ids
from music_assistant.helpers.throttle_retry import BYPASS_THROTTLER
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.media_items.metadata import MediaItemImage
    from music_assistant_models.queue_item import QueueItem

    from music_assistant.controllers.player_queues.state import PlayerQueueData
    from music_assistant.providers.radio_playlist import RadioPlaylistProvider


class QueueLoaderMixin(_PlayerQueuesBase):
    """Load items into a queue: apply the enqueue option, resolve single items, refill the pool."""

    async def _enqueue_with_option(
        self,
        queue_id: str,
        queue_items: list[QueueItem],
        option: QueueOption | None,
        pin_first: bool = False,
    ) -> None:
        """
        Load queue items into the queue according to the given enqueue option.

        :param queue_id: The queue to load the items into.
        :param queue_items: The items to load.
        :param option: The enqueue option to apply.
        :param pin_first: The first item was explicitly picked by the user (a start_item), so it
            must keep its position when the batch is shuffled instead of being moved at random.
        """
        queue = self._queue_data[queue_id].queue
        # A queue that played to its end is finished, so anything enqueued onto it starts a fresh
        # queue rather than stacking onto the items that already played. Only an explicit ADD keeps
        # them: there the added items continue the queue from where it ended, and the index is moved
        # onto the first of them below so pressing play starts there instead of replaying the last
        # item. ADD never starts playback by itself.
        continues_ended_queue = queue.ended and option == QueueOption.ADD
        items_before_add = len(self._queue_data[queue_id].items)
        if queue.ended and not continues_ended_queue and option != QueueOption.REPLACE:
            # mechanical clear: the shuffle state for this batch was already settled by the caller.
            # Replace is exempt: it swaps the whole queue below without ever emptying it.
            self._clear(queue_id, skip_stop=True)
        if queue.state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            cur_index = (
                queue.index_in_buffer
                if queue.index_in_buffer is not None
                else (queue.current_index if queue.current_index is not None else 0)
            )
        else:
            cur_index = queue.current_index or 0
        insert_at_index = cur_index + 1
        shuffle = queue.shuffle_enabled and len(queue_items) > 1
        # a user-picked start item must be the one that actually starts playing, so keep it in
        # front of the shuffled rest instead of letting the shuffle move it to a random slot
        pin_first = pin_first and shuffle

        # handle replace: swap the queue's contents for the new items in one step
        if option == QueueOption.REPLACE:
            # Release the audio the outgoing items hold while they are still on the queue: the
            # track being started needs their source slot, and once they are swapped out nothing
            # reaches them any more.
            await self._cleanup_queue_audio_data(queue_id)
            # the player is still on the old index, so drop it: the swap would otherwise hand it a
            # "next" item taken from the new list at that position. play_index sets the real one.
            queue.index_in_buffer = None
            # playback starts over below, and play_index reads this to decide whether to honour a
            # stored resume position
            queue.ended = False
            if pin_first:
                await self._load_pinned_first(
                    queue_id,
                    queue_items,
                    insert_at_index=0,
                    keep_remaining=False,
                    keep_played=False,
                )
            else:
                await self.load(
                    queue_id,
                    queue_items=queue_items,
                    keep_remaining=False,
                    keep_played=False,
                    shuffle=shuffle,
                )
            await self.play_index(queue_id, 0)
            return
        # handle next: add item(s) in the index next to the playing/loaded/buffered index
        if option == QueueOption.NEXT:
            if shuffle:
                # honour "play next" under shuffle: the first new item goes right after the
                # buffered index so it plays next, the rest of the batch is shuffled into the tail
                # behind it. insert_at_index is the first un-buffered slot, so the track the player
                # already prepared for crossfade is left untouched.
                await self._load_pinned_first(queue_id, queue_items, insert_at_index)
            else:
                await self.load(
                    queue_id,
                    queue_items=queue_items,
                    insert_at_index=insert_at_index,
                    shuffle=shuffle,
                )
            self._ensure_current_index(queue_id)
            return
        if option == QueueOption.REPLACE_NEXT:
            if pin_first:
                await self._load_pinned_first(
                    queue_id, queue_items, insert_at_index, keep_remaining=False
                )
            else:
                await self.load(
                    queue_id,
                    queue_items=queue_items,
                    insert_at_index=insert_at_index,
                    keep_remaining=False,
                    shuffle=shuffle,
                )
            self._ensure_current_index(queue_id)
            return
        # handle play: replace current loaded/playing index with new item(s)
        if option == QueueOption.PLAY:
            # an idle/empty queue has no current item to insert after, so insert at and
            # start from the very first index instead of skipping past it
            play_at_index = 0 if queue.current_index is None else insert_at_index
            if pin_first:
                await self._load_pinned_first(queue_id, queue_items, play_at_index)
            else:
                await self.load(
                    queue_id,
                    queue_items=queue_items,
                    insert_at_index=play_at_index,
                    shuffle=shuffle,
                )
            next_index = min(play_at_index, len(self._queue_data[queue_id].items) - 1)
            await self.play_index(queue_id, next_index)
            return
        # handle add: add/append item(s) to the remaining queue items
        if option == QueueOption.ADD:
            # When shuffling, mix the new items into the not-yet-played tail. While playing,
            # keep the item right after the buffered one in place: it has already been enqueued
            # to the player (and prepared for crossfade), so reshuffling it would swap the
            # upcoming track underneath the player and cause an abrupt, non-crossfaded switch.
            if not queue.shuffle_enabled:
                add_at_index = len(self._queue_data[queue_id].items) + 1
            elif queue.state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
                add_at_index = insert_at_index + 1
            else:
                add_at_index = insert_at_index
            await self.load(
                queue_id=queue_id,
                queue_items=queue_items,
                insert_at_index=add_at_index,
                shuffle=queue.shuffle_enabled,
            )
            if continues_ended_queue:
                self._continue_ended_queue(queue_id, items_before_add)
                return
            self._ensure_current_index(queue_id)

    async def _load_pinned_first(
        self,
        queue_id: str,
        queue_items: list[QueueItem],
        insert_at_index: int,
        keep_remaining: bool = True,
        keep_played: bool = True,
    ) -> None:
        """
        Insert the first item at the given index and shuffle the rest of the batch behind it.

        :param queue_id: The queue to load the items into.
        :param queue_items: The items to load; the first one keeps the given index.
        :param insert_at_index: The index to place the first item at.
        :param keep_remaining: Keep the queue's existing items from the insert index onwards.
        :param keep_played: Keep the queue's existing items before the insert index.
        """
        # a single load, so the queue is never published holding just the pinned item
        await self.load(
            queue_id,
            queue_items=queue_items,
            insert_at_index=insert_at_index,
            keep_remaining=keep_remaining,
            keep_played=keep_played,
            shuffle=True,
            pin_first=True,
        )

    def _ensure_current_index(self, queue_id: str) -> None:
        """
        Point the current index at the first item when the queue does not have one yet.

        NEXT/ADD/REPLACE_NEXT stage items without starting playback; on an empty queue there is no
        current index, so set it to the first item to give the queue a current item. A queue that
        already has content keeps its current index untouched (its items are inserted after it).

        :param queue_id: The queue to update.
        """
        queue = self._queue_data[queue_id].queue
        if queue.current_index is not None:
            return
        queue.current_index = 0
        queue.current_item = self.get_item(queue_id, 0)
        self.signal_update(queue_id)

    def _continue_ended_queue(self, queue_id: str, first_added_index: int) -> None:
        """
        Point a finished queue at the first item just added to it, without starting playback.

        The items that already played are kept, so the queue is no longer finished but its position
        still sits on its old last item. Moving it onto the added items is what makes a play press
        start there rather than replay the item the queue ended on.

        :param queue_id: The queue that was added to.
        :param first_added_index: Index of the first of the added items.
        """
        queue = self._queue_data[queue_id].queue
        queue.ended = False
        if (current_item := self.get_item(queue_id, first_added_index)) is None:
            return
        queue.current_index = first_added_index
        queue.current_item = current_item
        # ending the queue cleared the next item; refresh it so a batch of added items reports
        # what follows instead of looking like there is nothing after the first one
        queue.next_item = self.get_next_item(queue_id, first_added_index)
        self.signal_update(queue_id)

    async def _load_item(
        self,
        queue_item: QueueItem,
        is_start: bool = False,
        seek_position: int = 0,
        fade_in: bool = False,
    ) -> None:
        """
        Try to load the stream details for the given queue item.

        :param queue_item: The queue item to load.
        :param is_start: Whether this item starts playback, rather than following another item.
        :param seek_position: Position (in seconds) to start playback from.
        :param fade_in: Whether to fade in the audio.
        """
        queue_id = queue_item.queue_id
        queue = self._queue_data[queue_id].queue

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
        # decided once the album above is resolved: a queue item can hold a slim mapping of its
        # album, which carries none of the provider ids the enqueued album is matched on
        playing_album_tracks = self._plays_as_album_track(queue_item)
        if is_start:
            # a track skip should hand its source slot to the item the user is starting
            await self._abort_superseded_source_buffers(queue_item)

        # Fetch streamdetails (reuses existing if buffer is still valid for the seek).
        queue_item.streamdetails = await self.mass.streams.audio.get_stream_details(
            queue_item=queue_item,
            seek_position=seek_position,
            fade_in=fade_in,
            prefer_album_loudness=playing_album_tracks,
        )
        # update queue_item.duration from streamdetails if we got a better value
        self._apply_probed_duration(queue_item)

        # pre-initialize the AudioBuffer so audio is ready
        # when the player requests it. For the current/first track this ensures
        # immediate playback start. For preloaded next tracks we skip this and
        # initialize the buffer ~30s before the current track ends instead.
        # AudioSource items are realtime/live and bypass the AudioBuffer.
        if is_start and queue_item.streamdetails.media_type != MediaType.AUDIO_SOURCE:
            await self.mass.streams.audio.get_audio_buffer(
                queue_item,
                seek_position_ms=int(seek_position * 1000),
                reason="prepare",
            )
            # the first chunk is in, so the source has been probed and a duration the
            # provider did not report is known before playback starts
            self._apply_probed_duration(queue_item)

    def _plays_as_album_track(self, queue_item: QueueItem) -> bool:
        """
        Check whether the given item plays as part of an album the user enqueued.

        :param queue_item: The queue item to decide the loudness reference for.
        """
        queue_data = self._queue_data[queue_item.queue_id]
        # a track repeating on its own is its own playback, whatever seeded the queue around it
        if queue_data.queue.repeat_mode == RepeatMode.ONE:
            return False
        album = getattr(queue_item.media_item, "album", None)
        if album is None:
            return False
        # the album the user pressed play on keeps the shape of the listing it was picked from,
        # while the queue's tracks carry the library album. Matching on the provider mappings
        # recognises both shapes, plain item_id equality does not.
        return any(
            isinstance(item, Album) and compare_item_ids(item, album)
            for item in queue_data.enqueued_media_items
        )

    def _reset_enqueued_media_items(self, queue_data: PlayerQueueData) -> None:
        """
        Forget what was enqueued on a queue that is being replaced by a new one.

        :param queue_data: The queue whose enqueued items are no longer what it plays.
        """
        queue_data.enqueued_media_items.clear()
        # the credits only mark which of those enqueued albums were already counted, so they
        # are meaningless once the items they refer to are gone
        queue_data.credited_albums.clear()

    def _apply_probed_duration(self, queue_item: QueueItem) -> None:
        """
        Apply a duration determined while streaming to the queue item and its media item.

        :param queue_item: The queue item whose streamdetails to take the duration from.
        """
        streamdetails = queue_item.streamdetails
        if streamdetails is None or not streamdetails.duration:
            return
        duration = int(streamdetails.duration)
        if not self._set_missing_duration(queue_item, duration):
            return
        if uri := getattr(queue_item.media_item, "uri", None):
            # store it so listings and later playbacks have it up front
            self.mass.create_task(store_probed_duration(self.mass, uri, duration))

    async def _restore_probed_duration(self, queue_item: QueueItem) -> None:
        """
        Apply the duration determined during an earlier playback to an item that lacks one.

        :param queue_item: The queue item to fill the duration of.
        """
        if queue_item.media_type not in PROBED_DURATION_MEDIA_TYPES:
            return
        if not (uri := getattr(queue_item.media_item, "uri", None)):
            return
        if queue_item.duration and getattr(queue_item.media_item, "duration", None):
            return
        if duration := await get_probed_duration(self.mass, uri):
            self._set_missing_duration(queue_item, duration)

    def _set_missing_duration(self, queue_item: QueueItem, duration: int) -> bool:
        """
        Fill in the duration of a queue item and its media item, leaving known ones alone.

        :param queue_item: The queue item to fill the duration of.
        :param duration: The duration in seconds.
        :return: True if the item (or its media item) did not have a duration yet.
        """
        if queue_item.media_type not in PROBED_DURATION_MEDIA_TYPES:
            return False
        media_item = queue_item.media_item
        # an ItemMapping or any other reference without a duration is left untouched
        media_item_duration = getattr(media_item, "duration", None)
        if queue_item.duration and media_item_duration != 0:
            return False
        if not queue_item.duration:
            queue_item.duration = duration
        if media_item_duration == 0:
            media_item.duration = duration  # type: ignore[union-attr]
        self.signal_update(queue_item.queue_id, items_changed=True)
        return True

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
        queue = self._queue_data[queue_id].queue
        queue_items = self._queue_data[queue_id].items
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
        if (queue_data := self._queue_data.get(queue_id)) is None:
            # the delayed refill timer can fire after the queue was removed
            return
        queue = queue_data.queue
        # restore the queue owner's user context so provider filters are respected during this
        # background refill (dynamic-playlist generation honours the current user)
        playback_user = (
            await self.mass.webserver.auth.get_user(queue_data.userid)
            if queue_data.userid
            else None
        )
        set_current_user(playback_user)
        # Top up from the queue's dynamic sources (dynamic playlists and any mixed-in finite items),
        # weighted per source and recency-gated. fill() already sizes the batch to the pool target;
        # the tail cap below is a defensive ceiling so the unplayed tail never grows past
        # MANAGED_POOL_MAX.
        pool_tracks = await self._managed_pool.fill(queue_id, is_initial=False)
        if self._queue_data.get(queue_id) is not queue_data:
            # the queue was removed or re-registered while tracks were fetched
            return
        # keep the unplayed tail within the bounded pool size (no current_index => nothing played yet)
        played = 0 if queue.current_index is None else queue.current_index + 1
        unplayed = max(len(queue_data.items) - played, 0)
        headroom = max(MANAGED_POOL_MAX - unplayed, 0)
        queue_items = [build_queue_item(queue_id, x) for x in pool_tracks[:headroom] if x.available]
        if not queue_items:
            return
        await self.load(
            queue_id,
            queue_items,
            insert_at_index=len(queue_data.items) + 1,
        )

    async def _fill_autoplay_tracks(self, queue_id: str) -> None:
        """
        Append more items to a queue that is running low, based on what is ending.

        Autoplay is a single "keep going" switch; what it appends is decided by the media type
        of the queue's last item, since that is the item the appended items follow.
        """
        queue = self.get(queue_id)
        if queue is None or not queue.autoplay_enabled:
            return
        queue_data = self._queue_data[queue_id]
        if not queue_data.items:
            return
        last_item = queue_data.items[-1]
        if last_item.media_type in AUTOPLAY_EXCLUDED_MEDIA_TYPES:
            return
        # Restore the queue owner's user context so provider filters, library access and
        # resume positions are respected during this background refill, mirroring
        # _fill_dynamic_tracks.
        playback_user = (
            await self.mass.webserver.auth.get_user(queue_data.userid)
            if queue_data.userid
            else None
        )
        set_current_user(playback_user)
        if self._queue_data.get(queue_id) is not queue_data:
            # the queue was removed or re-registered while the user context was restored
            return
        if last_item.media_type in AUTOPLAY_SERIES_MEDIA_TYPES:
            await self._fill_autoplay_next_in_series(queue_id, last_item)
            return
        await self._fill_autoplay_music_tracks(queue_id)

    async def _fill_autoplay_next_in_series(self, queue_id: str, last_item: QueueItem) -> None:
        """
        Append the episode/book that follows the queue's last item, if there is one.

        Nothing is appended for the last episode of a podcast or a book without a next one in
        its collection, so the queue simply ends there.

        :param queue_id: The queue to append to.
        :param last_item: The queue's last item, an audiobook or podcast episode.
        """
        queue_data = self._queue_data[queue_id]
        media_item = last_item.media_item
        next_item: PodcastEpisode | Audiobook | None
        try:
            if isinstance(media_item, PodcastEpisode):
                next_item = await self._media_resolver.get_next_podcast_episode(
                    media_item, userid=queue_data.userid
                )
            elif isinstance(media_item, Audiobook):
                next_item = await self._media_resolver.get_next_audiobook(
                    media_item, userid=queue_data.userid
                )
            else:
                return
        except MusicAssistantError as err:
            self.logger.warning(
                "Autoplay failed to fetch the item following %s: %s", last_item.name, err
            )
            return
        if next_item is None or not next_item.available:
            self.logger.debug("Autoplay found nothing to play after %s", last_item.name)
            return
        if any(
            item.media_item and item.media_item.uri == next_item.uri for item in queue_data.items
        ):
            # already queued (e.g. the user added it themselves), so there is nothing to do
            return
        if self._queue_data.get(queue_id) is not queue_data:
            # the queue was removed or re-registered while the successor was fetched
            return
        await self.load(
            queue_id,
            [build_queue_item(queue_id, next_item)],
            insert_at_index=len(queue_data.items) + 1,
        )

    async def _fill_autoplay_music_tracks(self, queue_id: str) -> None:
        """Fill a Queue with additional tracks based on the configured Autoplay mode."""
        queue = self.get(queue_id)
        if queue is None:
            return
        queue_data = self._queue_data[queue_id]
        if not queue_data.enqueued_media_items:
            # the music refill needs what the user enqueued as its seed
            return
        mode = self._autoplay.resolve_mode(queue_id)
        self.logger.debug(
            "Filling autoplay tracks (mode: %s) for queue %s", mode.value, queue.display_name
        )
        existing_tracks = {
            item.media_item
            for item in self._queue_data[queue_id].items
            if isinstance(item.media_item, Track)
        }
        try:
            if mode == AutoplayMode.PLAYLIST:
                tracks = await self._autoplay.get_playlist_tracks(queue, existing_tracks)
            elif mode == AutoplayMode.LIBRARY:
                tracks = await self._autoplay.get_library_tracks(queue, existing_tracks)
            elif mode == AutoplayMode.SIMILAR:
                tracks = await self._get_similar_tracks(
                    queue_id, seed_items=queue_data.enqueued_media_items
                )
            else:
                # AUTO: try similar tracks first, fall back to the library mix. The similar
                # fetch raises when no provider can supply base/similar tracks, so suppress
                # that here to make sure the library fallback still runs.
                tracks = []
                with suppress(MusicAssistantError):
                    tracks = await self._get_similar_tracks(
                        queue_id, seed_items=queue_data.enqueued_media_items
                    )
                if not tracks:
                    tracks = await self._autoplay.get_library_tracks(queue, existing_tracks)
        except MusicAssistantError as err:
            self.logger.warning(
                "Autoplay failed to fetch tracks for queue %s: %s", queue.display_name, err
            )
            return
        # route the autoplay batch through the recency engine so a recently-heard track isn't
        # immediately re-added (ungated fallback keeps autoplay going if everything is recent)
        windows = self._smart_shuffle.windows()
        snapshot = await self.mass.music.recency.snapshot(windows, userid=queue_data.userid)
        tracks = gate_tracks(
            [track for track in tracks if isinstance(track, Track)], snapshot, windows
        )
        queue_items = [build_queue_item(queue_id, x) for x in tracks if x.available]
        if not queue_items:
            self.logger.info("Autoplay found no new tracks to add for queue %s", queue.display_name)
            return
        if self._queue_data.get(queue_id) is not queue_data:
            # the queue was removed or re-registered while tracks were fetched
            return
        await self.load(
            queue_id,
            queue_items,
            insert_at_index=len(queue_data.items) + 1,
        )

    @handle_play_action
    async def _handle_play_media(
        self,
        queue_id: str,
        media: MediaItemType | ItemMapping | str | list[MediaItemType | ItemMapping | str],
        option: QueueOption | None = None,
        radio_mode: bool = False,
        start_item: PlayableMediaItemType | str | None = None,
        sort_by: str | None = None,
        start_from_beginning: bool = False,
        shuffle: bool | None = None,
    ) -> None:
        """Handle play media without acquiring the queue lock."""
        # cancel any pending play_index calls for this queue to prevent conflicts
        self.mass.cancel_timer(f"queue_play_index_{queue_id}")
        self._set_transitioning(queue_id, False)
        # we use a contextvar to bypass the throttler for this asyncio task/context
        # this makes sure that playback has priority over other requests that may be
        # happening in the background
        BYPASS_THROTTLER.set(True)
        if not (queue := self.get(queue_id)):
            raise PlayerUnavailableError(f"Queue {queue_id} is not available")
        queue_data = self._queue_data[queue_id]
        # always fetch the underlying player so we can raise early if its not available
        queue_player = self.mass.players.get_player(queue_id, True)
        assert queue_player is not None  # for type checking
        if queue_player.extra_data.get(ATTR_ANNOUNCEMENT_IN_PROGRESS):
            self.logger.warning("Ignore queue command: An announcement is in progress")
            return

        # save the user requesting the playback (clear it for anonymous playback)
        playback_user = get_current_user()
        queue_data.userid = playback_user.user_id if playback_user else None
        if playback_user:
            self.logger.debug(
                "User %s requested playback.", playback_user.display_name or playback_user.username
            )

        # a single item or list of items may be provided
        media_list = media if isinstance(media, list) else [media]

        if radio_mode:
            # radio_mode is deprecated: a "radio" is now a dynamic radio playlist. Translate each
            # seed into the radio_playlist provider's URI and enqueue those (resolved to dynamic
            # playlists that self-manage their refills).
            self.logger.warning(
                "radio_mode is deprecated; enqueue a radio_playlist:// dynamic playlist instead"
            )
            media_list = [
                seed_uri
                if (seed_uri := item if isinstance(item, str) else str(item.uri)).startswith(
                    "radio_playlist://"
                )
                else f"radio_playlist://playlist/{seed_uri}"
                for item in media_list
            ]
            radio_mode = False

        # Forget the previous queue's enqueued items when a new queue is requested. A caller that
        # left the option to the config gets this once the first item resolved it, below: it is the
        # option that says whether this is a new queue or an addition to the current one.
        if option is not None and option not in (QueueOption.ADD, QueueOption.NEXT):
            self._reset_enqueued_media_items(queue_data)
        # An ADD/NEXT onto a queue that is already a managed pool (has a dynamic source): a finite
        # item is kept only as a source (the bounded pool materializes it) instead of being expanded
        # into the queue. Any other enqueue (PLAY/REPLACE, or onto a linear queue) expands finite
        # items normally. Keys off is_dynamic since a finite-only queue records sources too.
        # A play-next track is exempt from this (see plays_next_track below).
        already_dynamic = queue.is_dynamic and option in (QueueOption.ADD, QueueOption.NEXT)

        media_items: list[MediaItemType] = []
        # the subset of media_items the user explicitly picked to play next
        play_next_items: list[MediaItemType] = []
        source_items: list[MediaItemType] = []
        shuffle_settled = False
        # resolve all media items
        for item in media_list:
            try:
                # parse provided uri into a MA MediaItem or Basic QueueItem from URL
                media_item: MediaItemType | ItemMapping | BrowseFolder
                if isinstance(item, str):
                    media_item = await self.mass.music.get_item_by_uri(item)
                elif isinstance(item, dict):  # type: ignore[unreachable]
                    # TODO: Investigate why the API parser sometimes passes raw dicts instead of
                    # converting them to MediaItem objects. The parse_value function in api.py
                    # should handle dict-to-object conversion, but dicts are slipping through
                    # in some cases. This is defensive handling for that parser bug.
                    media_item = media_from_dict(item)  # type: ignore[unreachable]
                    self.logger.debug("Converted to: %s", type(media_item))
                else:
                    # item is MediaItemType | ItemMapping at this point
                    media_item = item

                if isinstance(media_item, ItemMapping):
                    # Resolve any ItemMapping to its full media item, exactly as the str-uri
                    # form above already does. Everything below needs the real object: the
                    # enqueued/source bookkeeping only accepts full items (so a mapping would
                    # otherwise never count as a user-initiated play), and the dynamic check
                    # needs details such as a playlist's 'is_dynamic'.
                    if media_item.uri is None:
                        raise InvalidDataError("ItemMapping has no URI")
                    media_item = await self.mass.music.get_item_by_uri(media_item.uri)

                # handle default enqueue option if needed
                if option is None:
                    # Radio + AudioSource share a single "live_sources" enqueue default —
                    # both are live infinite streams where REPLACE is almost always the
                    # right semantic. Other media types use their per-type config key.
                    if media_item.media_type in (MediaType.RADIO, MediaType.AUDIO_SOURCE):
                        config_key = CONF_DEFAULT_ENQUEUE_OPTION_LIVE_SOURCES
                    else:
                        config_key = f"default_enqueue_option_{media_item.media_type.value}"
                    config_value = self.get_config_value(config_key, return_type=str)
                    option = QueueOption(config_value)
                    if option not in (QueueOption.ADD, QueueOption.NEXT):
                        self._reset_enqueued_media_items(queue_data)
                    # settled from the resolved option for the same reason as the reset above
                    already_dynamic = queue.is_dynamic and option in (
                        QueueOption.ADD,
                        QueueOption.NEXT,
                    )

                # Save requested media item to play on the queue so we can use it as a seed
                # for Autoplay's music refill (the podcast/audiobook continuations resolve
                # their successor from the queue's last item instead) and to tell which of its
                # tracks play as part of an album the user picked.
                # Use FIFO list to keep track of the last 10 played items
                # Skip ItemMapping and BrowseFolder - only queue full MediaItemType objects
                if not isinstance(media_item, BrowseFolder) and (
                    is_dynamic_source(media_item)
                    or media_item.media_type
                    in (MediaType.TRACK, MediaType.ALBUM, MediaType.PLAYLIST, MediaType.ARTIST)
                ):
                    queue_data.enqueued_media_items.append(media_item)
                    if len(queue_data.enqueued_media_items) > 10:
                        evicted = queue_data.enqueued_media_items.pop(0)
                        # an album that dropped off the list can no longer be matched, so its
                        # credit is dead weight unless another entry still stands for it
                        if isinstance(evicted, Album) and evicted not in (
                            queue_data.enqueued_media_items
                        ):
                            queue_data.credited_albums.discard(evicted)
                    # enqueueing an album again is a new play of it, so let it be credited again
                    if isinstance(media_item, Album):
                        queue_data.credited_albums.discard(media_item)
                    if is_dynamic_source(media_item):
                        # a dynamic playlist/station is always a self-managing dynamic source
                        source_items.append(media_item)

                # The shuffle state has to be settled before the items are resolved below: a
                # shuffled queue keeps the items preceding a start_item (chosen track pinned
                # first) instead of dropping them. The first item that resolves decides for the
                # whole batch, because it is the only media type known this early.
                if not shuffle_settled:
                    shuffle_settled = True
                    await self._apply_shuffle(
                        queue_id,
                        option,
                        # an explicit request always wins; only an unset one defers to the
                        # media's own order
                        False
                        if shuffle is None and media_item.media_type in ORDERED_MEDIA_TYPES
                        else shuffle,
                    )

                # the user picked this exact track to play next, so it must be inserted literally
                plays_next_track = (
                    option == QueueOption.NEXT and media_item.media_type == MediaType.TRACK
                )
                # collect media_items to play
                if is_dynamic_source(media_item):
                    # a dynamic playlist/station supplies its own tracks on demand; just mark it
                    # played. The queue goes dynamic below and the bounded pool seeds its batch from
                    # all sources, so there is no need to fetch a batch here.
                    self.mass.create_task(
                        self.mass.music.mark_item_played(
                            media_item,
                            userid=queue_data.userid,
                            queue_id=queue_id,
                            user_initiated=True,
                        )
                    )
                elif already_dynamic and not plays_next_track:
                    # feed the already-active pool: keep the finite item as a (materialized) source
                    if not isinstance(media_item, BrowseFolder):
                        source_items.append(media_item)
                else:
                    # a play-next track never becomes a source: the pool would re-dispatch it later
                    if (
                        not plays_next_track
                        and not isinstance(media_item, BrowseFolder)
                        and media_item.media_type
                        in (
                            MediaType.TRACK,
                            MediaType.ALBUM,
                            MediaType.PLAYLIST,
                            MediaType.ARTIST,
                        )
                    ):
                        # record the finite parent as a source (kept for a later dynamic
                        # transition and for similar/autoplay seeds)
                        source_items.append(media_item)
                    # Convert start_item to string URI if needed
                    start_item_uri: str | None = None
                    if isinstance(start_item, str):
                        start_item_uri = start_item
                    elif start_item is not None:
                        start_item_uri = start_item.uri
                    resolved_items = await self._media_resolver._resolve_media_items(
                        media_item,
                        start_item_uri,
                        userid=queue_data.userid,
                        queue_id=queue_id,
                        sort_by=sort_by,
                        start_from_beginning=start_from_beginning,
                        # under shuffle "start here and play forward" has no meaning, so keep the
                        # whole playlist/album (chosen track first) instead of dropping everything
                        # before it - the chosen track is pinned in front of the shuffled rest
                        keep_preceding_items=queue.shuffle_enabled,
                    )
                    media_items += resolved_items
                    if plays_next_track:
                        play_next_items += resolved_items

            except MusicAssistantError as err:
                # invalid MA uri or item not found error
                self.logger.warning("Skipping %s: %s", item, str(err))

        if not shuffle_settled and option is not None:
            # nothing resolved, so no media type ever decided - but the sources are replaced
            # below all the same, and a dynamic queue's imposed shuffle must not survive that
            await self._apply_shuffle(queue_id, option, shuffle)

        # captured before the reassignment below replaces the local with the stored list
        new_sources = bool(source_items)
        # overwrite or append the queue's source items
        replace_sources = option not in (QueueOption.ADD, QueueOption.NEXT)
        if replace_sources:
            self.store_sources(queue, source_items)
        else:
            self.store_sources(queue, self._queue_data[queue_id].source_items + source_items)
        source_items = self._queue_data[queue_id].source_items
        queue.is_dynamic = has_dynamic_source(source_items)
        # a queue that just gained or lost its dynamic source resolves smart shuffle differently
        queue.smart_shuffle_active = self.is_smart_shuffle_active(queue)

        if queue.is_dynamic:
            if replace_sources or new_sources:
                # the queue has (or just gained) a dynamic source: (re)build the upcoming tail into
                # a single bounded, recency-orchestrated mix over ALL sources — existing finite
                # content as materialized TRACKS seed(s), dynamic playlists as DYNAMIC seed(s).
                # Only rebuilt when this enqueue changed the sources, so a play-next insert
                # leaves the tail untouched.
                await self._enter_dynamic_mode(queue_id, option)
            # only explicit play-next tracks are inserted literally; container expansions are
            # already in the pool via their source
            media_items = play_next_items
            if not media_items:
                return
            # fall through: play-next track(s) are inserted after the buffered index below

        # only add valid/available items
        queue_items: list[QueueItem] = [
            build_queue_item(queue_id, cast("PlayableMediaItemType", x))
            for x in media_items
            if x and x.available
        ]

        if not queue_items:
            raise MediaNotFoundError("No playable items found", translation_key="no_playable_items")

        await self._enqueue_with_option(
            queue_id, queue_items, option, pin_first=start_item is not None
        )

    async def _enter_dynamic_mode(self, queue_id: str, option: QueueOption | None) -> None:
        """
        (Re)build a queue's upcoming tail into a single bounded managed pool over all its sources.

        Runs whenever an enqueue leaves the queue dynamic — both the first transition and every
        later add. Keeps the current + already-buffered track(s), drops the rest of the upcoming
        tail, and replaces it with a bounded, recency-orchestrated mix of all the queue's sources
        (finite sources materialized as TRACKS seeds, dynamic playlists as DYNAMIC seeds), so the
        queue stays a fixed-size mix instead of growing by each added source's own batch. Shuffle is
        enabled implicitly: a dynamic queue is always a smart mix.

        :param queue_id: The queue to (re)build the dynamic pool for.
        :param option: The enqueue option that triggered the (re)build. PLAY/REPLACE start playback
            on the rebuilt pool; ADD/NEXT/REPLACE_NEXT stage it without starting playback (behind the
            current/buffered track, or from the front of an idle/empty queue).
        """
        queue_data = self._queue_data[queue_id]
        queue = queue_data.queue
        # a dynamic queue is an always-on smart mix; reflect that in the (now locked) shuffle state
        queue.shuffle_enabled = True
        queue.smart_shuffle_active = self.is_smart_shuffle_active(queue)
        # rebuild from the buffered position so the already-prepared next track is kept and the
        # crossfade isn't disturbed; fall back to the current index (or the front when idle/empty)
        base_index = (
            queue.index_in_buffer if queue.index_in_buffer is not None else queue.current_index
        )
        insert_at = 0 if base_index is None else base_index + 1
        if option == QueueOption.REPLACE:
            # A replace is a fresh queue, so the pool takes the place of the old items rather than
            # being appended behind the one that is playing (as PLAY, which shares start_playing,
            # deliberately does). Zeroed before the truncation below so the pool is sized against
            # an empty queue and none of the discarded tracks are held back from it.
            insert_at = 0
            # as on the linear path: release the outgoing audio while its items are still on the
            # queue, and drop the stale position
            await self._cleanup_queue_audio_data(queue_id)
            queue.index_in_buffer = None
            queue.ended = False
        # PLAY/REPLACE start playback on the rebuilt pool; ADD/NEXT/REPLACE_NEXT only stage it and
        # never start playback (an idle/empty queue stays idle on an add, just like the linear path)
        start_playing = option in (QueueOption.PLAY, QueueOption.REPLACE)
        # The tail is dropped before the pool is fetched, so the pool is sized and deduped against
        # the kept head only (the tail we are discarding must not exclude its own tracks from it).
        # That leaves the queue holding less than it plays - for a replace, nothing at all - across
        # the fetch, so hold player reconciliation off until the new items are in: it would
        # otherwise publish that half-built state, which is exactly the empty queue this avoids.
        self._set_transitioning(queue_id, True)
        try:
            queue_data.items = queue_data.items[:insert_at]
            queue.items = len(queue_data.items)
            pool_tracks = await self._managed_pool.fill(queue_id, is_initial=False)
            queue_items = [
                build_queue_item(queue_id, track) for track in pool_tracks if track.available
            ]
            if not queue_items:
                raise MediaNotFoundError(
                    "No playable items found", translation_key="no_playable_items"
                )
            # the managed pool already interleaved the sources in a recency-aware order; load as-is
            await self.load(
                queue_id,
                queue_items,
                insert_at_index=insert_at,
                keep_remaining=False,
                keep_played=option != QueueOption.REPLACE,
            )
            if start_playing:
                await self.play_index(queue_id, insert_at)
            else:
                # give an idle/empty queue a current item without starting playback
                self._ensure_current_index(queue_id)
        finally:
            self._set_transitioning(queue_id, False)

    async def _get_similar_tracks(
        self,
        queue_id: str,
        is_initial: bool = False,
        seed_items: list[MediaItemType] | None = None,
    ) -> list[Track]:
        """
        Fetch tracks similar to the given seeds (autoplay's similar/continuation mode).

        :param queue_id: The queue to fetch tracks for.
        :param is_initial: True to interleave the base/seed tracks into the result, False to
            return only similar tracks.
        :param seed_items: Explicit seed items to base the tracks on. Defaults to the queue's
            sources; autoplay passes the enqueued media items instead.
        """
        queue_data = self._queue_data[queue_id]
        queue = queue_data.queue
        queue_track_items: list[Track] = [
            q.media_item
            for q in self._queue_data[queue_id].items
            if q.media_item and isinstance(q.media_item, Track)
        ]
        source_items = (
            seed_items if seed_items is not None else self._queue_data[queue_id].source_items
        )
        if not source_items:
            # this may happen during race conditions as this method is called delayed
            return []
        self.logger.info(
            "Fetching similar tracks for queue %s based on: %s",
            queue.display_name,
            ", ".join([x.name for x in source_items]),
        )

        # Get user's preferred provider instances for steering provider selection
        preferred_provider_instances: list[str] | None = None
        if (
            queue_data.userid
            and (playback_user := await self.mass.webserver.auth.get_user(queue_data.userid))
            and playback_user.provider_filter
        ):
            preferred_provider_instances = playback_user.provider_filter

        # Some providers have very deterministic similar-track algorithms for a single track
        # seed. When continuing from a single track on a refill, seed from the play history
        # instead so the result keeps varying.
        if (
            len(source_items) == 1
            and source_items[0].media_type == MediaType.TRACK
            and not is_initial
            and queue_track_items
        ):
            # Helper samples 5 internally; bound the input.
            seeds: list[MediaItemType] = random.sample(
                queue_track_items, min(len(queue_track_items), 10)
            )
        else:
            seeds = list(source_items)

        radio_prov = self.mass.get_provider("radio_playlist")
        if radio_prov is None:
            return []
        dynamic_tracks = await cast("RadioPlaylistProvider", radio_prov).get_dynamic_tracks(
            seeds,
            include_base_tracks=is_initial,
            target_size=25,
            preferred_provider_instances=preferred_provider_instances,
        )
        # Drop anything already queued/played
        queued_set = set(queue_track_items)
        return [track for track in dynamic_tracks if track not in queued_set]

    async def _abort_superseded_source_buffers(self, queue_item: QueueItem) -> None:
        """
        Abort the still-filling source buffers of other items in the same queue.

        :param queue_item: The queue item that is about to start playing.
        """
        queue_data = self._queue_data.get(queue_item.queue_id)
        items = tuple(queue_data.items) if queue_data else ()
        successor: QueueItem | None = None
        for index, item in enumerate(items):
            if item.queue_item_id == queue_item.queue_item_id and index + 1 < len(items):
                successor = items[index + 1]
                break
        # the started item keeps its own buffer, and its direct successor keeps the prewarm
        # for the upcoming crossfade unless the aborts below leave the provider without a slot
        spared_item_ids = {queue_item.queue_item_id}
        if successor is not None:
            spared_item_ids.add(successor.queue_item_id)
        for item in items:
            if item.queue_item_id in spared_item_ids:
                continue
            await self._abort_source_buffer(item, queue_item)
        if successor is not None:
            await self._abort_source_buffer(successor, queue_item, only_when_saturated=True)

    async def _abort_source_buffer(
        self,
        item: QueueItem,
        started_item: QueueItem,
        only_when_saturated: bool = False,
    ) -> None:
        """
        Cancel one item's still-filling source so its provider stream slot is handed over.

        :param item: The queue item whose source buffer should be aborted.
        :param started_item: The queue item that is about to start playing.
        :param only_when_saturated: Only abort while the provider has no free slot left.
        """
        if item.streamdetails is None:
            return
        audio_buffer = item.streamdetails.buffer
        if audio_buffer is None or not audio_buffer.is_buffering:
            return
        provider = self.mass.get_provider(item.streamdetails.provider, return_unavailable=True)
        if not isinstance(provider, MusicProvider) or provider.max_concurrent_streams is None:
            return
        if only_when_saturated:
            if provider.has_available_stream_slot:
                # an abort above already freed a slot, so this prewarm can stay
                return
            self.logger.debug(
                "Aborting the prewarm of %s: %s has no free stream slot left for %s",
                item.name,
                provider.name,
                started_item.name,
            )
        else:
            self.logger.debug(
                "Aborting the source of %s to free a %s stream slot for %s",
                item.name,
                provider.name,
                started_item.name,
            )
        # the cancelled buffer stays attached: it marks the source as aborted for
        # the flow stream's accounting and fails is_valid() for any later reuse
        await audio_buffer.clear()
