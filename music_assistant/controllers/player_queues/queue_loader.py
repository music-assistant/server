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
    MediaNotFoundError,
    MusicAssistantError,
    PlayerUnavailableError,
)
from music_assistant_models.media_items import (
    Album,
    BrowseFolder,
    ItemMapping,
    MediaItemType,
    PlayableMediaItemType,
    Playlist,
    Track,
    UniqueList,
    media_from_dict,
)

from music_assistant.constants import ATTR_ANNOUNCEMENT_IN_PROGRESS
from music_assistant.controllers.player_queues.autoplay import (
    AutoplayMode,
)
from music_assistant.controllers.player_queues.base import _PlayerQueuesBase
from music_assistant.controllers.player_queues.constants import (
    CONF_DEFAULT_ENQUEUE_OPTION_LIVE_SOURCES,
    MANAGED_POOL_MAX,
)
from music_assistant.controllers.player_queues.helpers import (
    build_queue_item,
    handle_play_action,
    has_dynamic_source,
)
from music_assistant.controllers.player_queues.managed_pool import gate_tracks
from music_assistant.controllers.streams.audio_buffer import AudioBuffer
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_user,
    set_current_user,
)
from music_assistant.helpers.throttle_retry import BYPASS_THROTTLER

if TYPE_CHECKING:
    from music_assistant_models.media_items.metadata import MediaItemImage
    from music_assistant_models.queue_item import QueueItem

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

        # handle replace: clear all items and replace with the new items
        if option == QueueOption.REPLACE:
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
        await self.load(
            queue_id,
            queue_items=queue_items[:1],
            insert_at_index=insert_at_index,
            keep_remaining=keep_remaining,
            keep_played=keep_played,
        )
        await self.load(
            queue_id,
            queue_items=queue_items[1:],
            insert_at_index=insert_at_index + 1,
            shuffle=True,
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

        # work out if we are playing an album and if we should prefer album
        # loudness
        next_track_from_same_album = (
            next_index is not None
            and (next_item := self.get_item(queue_id, next_index))
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
        current_index = self.index_by_id(queue_id, queue_item.queue_item_id)
        if current_index is None:
            previous_track_from_same_album = False
        else:
            previous_index = max(current_index - 1, 0)
            previous_track_from_same_album = (
                previous_index > 0
                and (previous_item := self.get_item(queue_id, previous_index)) is not None
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
            self.signal_update(queue_id, items_changed=True)

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
        queue_data = self._queue_data[queue_id]
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
        # keep the unplayed tail within the bounded pool size (no current_index => nothing played yet)
        played = 0 if queue.current_index is None else queue.current_index + 1
        unplayed = max(len(self._queue_data[queue_id].items) - played, 0)
        headroom = max(MANAGED_POOL_MAX - unplayed, 0)
        queue_items = [build_queue_item(queue_id, x) for x in pool_tracks[:headroom] if x.available]
        if not queue_items:
            return
        await self.load(
            queue_id,
            queue_items,
            insert_at_index=len(self._queue_data[queue_id].items) + 1,
        )

    async def _fill_autoplay_tracks(self, queue_id: str) -> None:
        """Fill a Queue with additional tracks based on the configured Autoplay mode."""
        queue = self.get(queue_id)
        if queue is None or not queue.autoplay_enabled:
            return
        queue_data = self._queue_data[queue_id]
        mode = self._autoplay.resolve_mode(queue_id)
        self.logger.debug(
            "Filling autoplay tracks (mode: %s) for queue %s", mode.value, queue.display_name
        )
        # Restore the queue owner's user context so provider filters and library access
        # are respected during this background refill, mirroring _fill_dynamic_tracks.
        playback_user = (
            await self.mass.webserver.auth.get_user(queue_data.userid)
            if queue_data.userid
            else None
        )
        set_current_user(playback_user)
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
        await self.load(
            queue_id,
            queue_items,
            insert_at_index=len(self._queue_data[queue_id].items) + 1,
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

        # clear queue if needed
        if option == QueueOption.REPLACE:
            self.clear(queue_id, skip_stop=True)
        # Clear the 'enqueued media item' list when a new queue is requested
        if option not in (QueueOption.ADD, QueueOption.NEXT):
            queue_data.enqueued_media_items.clear()

        # An ADD/NEXT onto a queue that is already a managed pool (has a dynamic source): a finite
        # item is kept only as a source (the bounded pool materializes it) instead of being expanded
        # into the queue. Any other enqueue (PLAY/REPLACE, or onto a linear queue) expands finite
        # items normally. Keys off is_dynamic since a finite-only queue records sources too.
        already_dynamic = queue.is_dynamic and option in (QueueOption.ADD, QueueOption.NEXT)

        media_items: list[MediaItemType] = []
        source_items: list[MediaItemType] = []
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

                if (
                    isinstance(media_item, ItemMapping)
                    and media_item.media_type == MediaType.PLAYLIST
                ):
                    # Resolve ItemMapping for a playlist so the full Playlist object
                    # so we have access to details such as 'is_dynamic'
                    with suppress(MusicAssistantError):
                        media_item = await self.mass.music.playlists.get(
                            media_item.item_id,
                            media_item.provider,
                        )

                # Save requested media item to play on the queue so we can use it as a source
                # for Autoplay. Use FIFO list to keep track of the last 10 played items
                # Skip ItemMapping and BrowseFolder - only queue full MediaItemType objects
                if not isinstance(
                    media_item, (ItemMapping, BrowseFolder)
                ) and media_item.media_type in (
                    MediaType.TRACK,
                    MediaType.ALBUM,
                    MediaType.PLAYLIST,
                    MediaType.ARTIST,
                ):
                    queue_data.enqueued_media_items.append(media_item)
                    if len(queue_data.enqueued_media_items) > 10:
                        queue_data.enqueued_media_items.pop(0)
                    if isinstance(media_item, Playlist) and media_item.is_dynamic:
                        # a dynamic playlist/station is always a self-managing dynamic source
                        source_items.append(media_item)

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
                    if option == QueueOption.REPLACE:
                        self.clear(queue_id, skip_stop=True)

                # collect media_items to play
                if isinstance(media_item, Playlist) and media_item.is_dynamic:
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
                elif already_dynamic:
                    # feed the already-active pool: keep the finite item as a (materialized) source
                    if not isinstance(media_item, (ItemMapping, BrowseFolder)):
                        source_items.append(media_item)
                else:
                    # not (yet) a managed pool: record the finite parent as a source (kept for a
                    # later dynamic transition and for similar/autoplay seeds) and expand it into
                    # the linear queue
                    if not isinstance(
                        media_item, (ItemMapping, BrowseFolder)
                    ) and media_item.media_type in (
                        MediaType.TRACK,
                        MediaType.ALBUM,
                        MediaType.PLAYLIST,
                        MediaType.ARTIST,
                    ):
                        source_items.append(media_item)
                    # Convert start_item to string URI if needed
                    start_item_uri: str | None = None
                    if isinstance(start_item, str):
                        start_item_uri = start_item
                    elif start_item is not None:
                        start_item_uri = start_item.uri
                    media_items += await self._media_resolver._resolve_media_items(
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

            except MusicAssistantError as err:
                # invalid MA uri or item not found error
                self.logger.warning("Skipping %s: %s", item, str(err))

        # overwrite or append the queue's source items
        replace_sources = option not in (QueueOption.ADD, QueueOption.NEXT)
        if replace_sources:
            self.store_sources(queue, source_items)
        else:
            self.store_sources(queue, self._queue_data[queue_id].source_items + source_items)
        source_items = self._queue_data[queue_id].source_items
        queue.is_dynamic = has_dynamic_source(source_items)

        if queue.is_dynamic:
            # the queue has (or just gained) a dynamic source: (re)build the upcoming tail into a
            # single bounded, recency-orchestrated mix over ALL sources — existing finite content as
            # materialized TRACKS seed(s), dynamic playlists as DYNAMIC seed(s). Every add rebuilds
            # from the buffer position, so the queue stays a fixed-size mix instead of growing by
            # each added source's own batch.
            await self._enter_dynamic_mode(queue_id, option)
            return

        # only add valid/available items
        queue_items: list[QueueItem] = [
            build_queue_item(queue_id, cast("PlayableMediaItemType", x))
            for x in media_items
            if x and x.available
        ]

        if not queue_items:
            raise MediaNotFoundError("No playable items found")

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
        # PLAY/REPLACE start playback on the rebuilt pool; ADD/NEXT/REPLACE_NEXT only stage it and
        # never start playback (an idle/empty queue stays idle on an add, just like the linear path)
        start_playing = option in (QueueOption.PLAY, QueueOption.REPLACE)
        # drop the finite upcoming tail up front so the pool is sized and deduped against the kept
        # head only (the tail we are discarding must not exclude its own tracks from the new pool)
        queue_data.items = queue_data.items[:insert_at]
        queue.items = len(queue_data.items)
        pool_tracks = await self._managed_pool.fill(queue_id, is_initial=False)
        queue_items = [
            build_queue_item(queue_id, track) for track in pool_tracks if track.available
        ]
        if not queue_items:
            raise MediaNotFoundError("No playable items found")
        # the managed pool already interleaved the sources in a recency-aware order; load as-is
        await self.load(queue_id, queue_items, insert_at_index=insert_at, keep_remaining=False)
        if start_playing:
            await self.play_index(queue_id, insert_at)
        else:
            # give an idle/empty queue a current item without starting playback
            self._ensure_current_index(queue_id)

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
