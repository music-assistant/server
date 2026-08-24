"""
Queue synchronisation logic for Plex remote control.

Handles background queue loading, MA→Plex sync, and MA event handlers.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import PlaybackState, QueueOption
from plexapi.playqueue import PlayQueue

from .parsing import plex_item_fields, plex_key_for_item

if TYPE_CHECKING:
    from music_assistant_models.event import MassEvent

    from music_assistant.providers.plex import PlexProvider

LOGGER = logging.getLogger(__name__)


class QueueSyncMixin:
    """Mixin providing queue synchronisation between MA and Plex."""

    if TYPE_CHECKING:
        provider: PlexProvider
        plex_server: Any
        _ma_player_id: str | None
        _updating_from_plex: bool
        play_queue_id: str | None
        play_queue_version: int
        play_queue_item_ids: dict[int, int]
        _last_synced_ma_queue_length: int
        _last_synced_ma_queue_keys: list[str]

        async def _broadcast_timeline(self) -> None: ...

        async def _send_timeline_to_server(self) -> None: ...

        def _selected_item_index(self, playqueue: PlayQueue) -> int: ...

    def _collect_synced_keys(self, player_id: str) -> list[str]:
        """
        Return Plex item_id keys for every track currently in the MA queue.

        :param player_id: The Music Assistant player ID.
        :return: Ordered list of Plex item IDs matching the current MA queue.
        """
        synced_keys = []
        for item in self.provider.mass.player_queues.items(player_id):
            if key := plex_key_for_item(item.media_item, self.provider.instance_id):
                synced_keys.append(key)
        return synced_keys

    def _remember_synced_queue(self, player_id: str, keys: list[str] | None = None) -> list[str]:
        """
        Snapshot the current MA queue keys as the last state synced to Plex.

        :param player_id: The Music Assistant player ID.
        :param keys: Pre-collected keys to store (avoids recomputing); collected if None.
        :return: The stored Plex keys, so callers can reuse them.
        """
        synced_keys = self._collect_synced_keys(player_id) if keys is None else keys
        self._last_synced_ma_queue_length = len(synced_keys)
        self._last_synced_ma_queue_keys = synced_keys
        return synced_keys

    def _reorder_tracks_for_playback(
        self, tracks: list[Any], start_index: int
    ) -> tuple[list[Any], dict[int, int]]:
        """
        Reorder tracks to start from a specific index and update item ID mappings.

        :param tracks: List of tracks to reorder.
        :param start_index: Index of the track to start from.
        :return: Tuple of (reordered tracks, updated item ID mappings).
        """
        if start_index <= 0 or start_index >= len(tracks):
            return tracks, self.play_queue_item_ids

        reordered_tracks = tracks[start_index:] + tracks[:start_index]

        new_item_ids = {}
        for new_idx, old_idx in enumerate(
            list(range(start_index, len(tracks))) + list(range(start_index))
        ):
            if old_idx in self.play_queue_item_ids:
                new_item_ids[new_idx] = self.play_queue_item_ids[old_idx]

        LOGGER.info(f"Started playback from offset {start_index} (reordered queue)")
        return reordered_tracks, new_item_ids

    async def _load_remaining_queue_tracks(
        self,
        player_id: str,
        playqueue: PlayQueue,
        selected_offset: int,
        shuffle: bool,
    ) -> None:
        """
        Load remaining tracks from play queue in the background.

        :param player_id: The Music Assistant player ID.
        :param playqueue: The Plex play queue.
        :param selected_offset: The offset of the track that's already playing.
        :param shuffle: Whether shuffle is enabled.
        """
        try:
            remaining_items = []

            for i in range(selected_offset + 1, len(playqueue.items)):
                remaining_items.append((i, playqueue.items[i]))

            for i in range(selected_offset):
                remaining_items.append((i, playqueue.items[i]))

            if not remaining_items:
                LOGGER.debug("No remaining tracks to load")
                return

            async def fetch_track(
                plex_idx: int, item: Any
            ) -> tuple[int, object | None, int | None]:
                """Fetch a single track from Plex."""
                track_key, play_queue_item_id = plex_item_fields(item)
                if track_key:
                    try:
                        track = await self.provider.get_track(track_key)
                        return plex_idx, track, play_queue_item_id
                    except Exception as e:
                        LOGGER.debug(f"Could not fetch track {track_key}: {e}")
                return plex_idx, None, None

            fetch_tasks = [fetch_track(idx, item) for idx, item in remaining_items]
            results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

            tracks_to_add: list[object] = []
            for result in results:
                if isinstance(result, Exception):
                    LOGGER.debug(f"Error fetching track: {result}")
                    continue
                _plex_idx, track, play_queue_item_id = result  # type: ignore[misc]
                if track:
                    ma_idx = len(tracks_to_add) + 1  # +1 because first track is already queued
                    tracks_to_add.append(track)
                    if play_queue_item_id:
                        self.play_queue_item_ids[ma_idx] = play_queue_item_id

            if tracks_to_add:
                LOGGER.info(f"Adding {len(tracks_to_add)} remaining tracks to queue")

                # Guard against the ADD firing QUEUE_ITEMS_UPDATED before we update
                # _last_synced_ma_queue_keys, which would trigger a spurious MA→Plex sync.
                self._updating_from_plex = True
                try:
                    await self.provider.mass.player_queues.play_media(
                        queue_id=player_id,
                        media=tracks_to_add,  # type: ignore[arg-type]
                        option=QueueOption.ADD,
                    )

                    self._remember_synced_queue(player_id)
                finally:
                    self._updating_from_plex = False

                if shuffle:
                    await self.provider.mass.player_queues.set_shuffle(player_id, shuffle)

                LOGGER.info(
                    f"Successfully loaded {len(tracks_to_add)} remaining tracks "
                    f"(total queue: {self._last_synced_ma_queue_length} tracks)"
                )
            else:
                LOGGER.warning("No valid remaining tracks found in play queue")

        except Exception:
            LOGGER.exception("Error loading remaining queue tracks")

    async def _replace_entire_queue(self, player_id: str, playqueue: PlayQueue) -> None:
        """
        Replace the entire MA queue from a Plex play queue.

        :param player_id: The Music Assistant player ID.
        :param playqueue: The Plex play queue to load.
        """
        all_tracks = []
        self.play_queue_item_ids = {}

        for item in playqueue.items:
            track_key, play_queue_item_id = plex_item_fields(item)

            if track_key:
                try:
                    track = await self.provider.get_track(track_key)
                    all_tracks.append(track)
                    if play_queue_item_id:
                        self.play_queue_item_ids[len(all_tracks) - 1] = play_queue_item_id
                except Exception as e:
                    LOGGER.debug(f"Could not fetch track {track_key}: {e}")
                    continue

        if all_tracks:
            await self.provider.mass.player_queues.play_media(
                queue_id=player_id,
                media=all_tracks,  # type: ignore[arg-type]
                option=QueueOption.REPLACE,
                # the tracks are already in the order Plex wants them played (a shuffled play
                # queue arrives shuffled), and play_queue_item_ids above is keyed on that order,
                # so the queue's own shuffle must not reorder them. refreshPlayQueue mirrors
                # Plex's shuffle flag onto the queue once the items have landed.
                shuffle=False,
            )
            LOGGER.info(f"Replaced queue with {len(all_tracks)} tracks")

    def _plex_index_of(self, playqueue: PlayQueue, ma_index: int) -> int | None:
        """
        Return where the MA queue item at ``ma_index`` sits in the fetched Plex queue.

        :param playqueue: The Plex play queue.
        :param ma_index: An index into the Music Assistant queue.
        :return: The index within ``playqueue.items``, or None if that item is absent.
        """
        item_id = self.play_queue_item_ids.get(ma_index)
        if item_id is None:
            return None
        for index, item in enumerate(playqueue.items):
            if plex_item_fields(item)[1] == item_id:
                return index
        return None

    def _wrap_boundary_index(self, playqueue: PlayQueue, base_index: int) -> int | None:
        """
        Return the Plex index at which the MA queue wraps back to its own start.

        :param playqueue: The Plex play queue.
        :param base_index: The last MA queue index that survives the replace.
        :return: The index within ``playqueue.items``, or None if it cannot be determined.
        """
        if (origin := self._plex_index_of(playqueue, 0)) is not None:
            return origin

        # A truncated response cannot tell a removed track from one that merely fell
        # outside it, so leave the boundary unknown. Otherwise the track playback began
        # on is gone from the queue and the boundary moves up to the earliest item MA
        # still holds that Plex does list.
        total = getattr(playqueue, "playQueueTotalCount", None)
        if total is not None and len(playqueue.items) < total:
            return None

        for ma_index in range(1, base_index + 1):
            if (index := self._plex_index_of(playqueue, ma_index)) is not None:
                return index
        return None

    def _replace_next_base_index(self, player_id: str, current_index: int) -> int:
        """
        Return the last MA queue index that a REPLACE_NEXT will keep.

        :param player_id: The Music Assistant player ID.
        :param current_index: The current track index in the MA queue.
        :return: The index of the last item that survives the replace.
        """
        # REPLACE_NEXT inserts after the buffered index rather than the playing one, so a
        # player already handed the next track keeps one item more than current_index.
        queue = self.provider.mass.player_queues.get(player_id)
        if (
            queue is not None
            and queue.state in (PlaybackState.PLAYING, PlaybackState.PAUSED)
            and queue.index_in_buffer is not None
        ):
            return int(queue.index_in_buffer)
        return current_index

    async def _replace_remaining_queue(
        self, player_id: str, playqueue: PlayQueue, current_index: int
    ) -> None:
        """
        Replace only items after the current track.

        :param player_id: The Music Assistant player ID.
        :param playqueue: The Plex play queue to load.
        :param current_index: The current track index in the MA queue.
        """
        base_index = self._replace_next_base_index(player_id, current_index)

        # MA positions are not Plex positions: the MA queue is a rotation of the Plex one
        # and the server may return only a window of it. So walk forward from the item MA
        # keeps last, stopping where the queue wraps.
        anchor = self._plex_index_of(playqueue, base_index)
        if anchor is None:
            anchor = self._selected_item_index(playqueue)
        origin = self._wrap_boundary_index(playqueue, base_index)

        items = playqueue.items
        if origin is None:
            remaining_items = items[anchor + 1 :]
        else:
            count = (origin - anchor - 1) % len(items)
            remaining_items = [items[(anchor + 1 + i) % len(items)] for i in range(count)]

        remaining_tracks = []
        new_item_mappings = {}

        for item in remaining_items:
            track_key, play_queue_item_id = plex_item_fields(item)

            if track_key:
                try:
                    track = await self.provider.get_track(track_key)
                    remaining_tracks.append(track)
                    if play_queue_item_id:
                        new_item_mappings[base_index + len(remaining_tracks)] = play_queue_item_id
                except Exception as e:
                    LOGGER.debug(f"Could not fetch track {track_key}: {e}")
                    continue

        if remaining_tracks:
            await self.provider.mass.player_queues.play_media(
                queue_id=player_id,
                media=remaining_tracks,  # type: ignore[arg-type]
                option=QueueOption.REPLACE_NEXT,
            )
            # REPLACE_NEXT drops every item after the buffered one, so their ids go with
            # them; rebuild the map so it stays keyed by MA index.
            self.play_queue_item_ids = {
                index: item_id
                for index, item_id in self.play_queue_item_ids.items()
                if index <= base_index
            }
            self.play_queue_item_ids.update(new_item_mappings)
            LOGGER.info(f"Replaced {len(remaining_tracks)} tracks after queue index {base_index}")
        else:
            LOGGER.debug("No tracks after current track in Plex queue")

    async def _create_plex_playqueue_from_ma(self) -> None:
        """Create a new Plex PlayQueue mirroring the current MA queue."""
        ma_queue = self.provider.mass.player_queues.get(self._ma_player_id)  # type: ignore[arg-type]
        queue_items = self.provider.mass.player_queues.items(self._ma_player_id)  # type: ignore[arg-type]

        if not ma_queue or not queue_items:
            return

        async def fetch_plex_item(plex_key: str) -> object | None:
            """Fetch a single Plex item."""
            try:
                plex_server = self.plex_server

                def fetch_item() -> object:
                    return plex_server.fetchItem(plex_key)

                return await asyncio.to_thread(fetch_item)
            except Exception as e:
                LOGGER.debug(f"Failed to fetch Plex item {plex_key}: {e}")
                return None

        fetch_tasks = []
        for item in queue_items:
            if plex_key := plex_key_for_item(item.media_item, self.provider.instance_id):
                fetch_tasks.append(fetch_plex_item(plex_key))

        plex_items = []
        if fetch_tasks:
            fetched_items = await asyncio.gather(*fetch_tasks, return_exceptions=True)
            plex_items = [item for item in fetched_items if item is not None]

        if not plex_items:
            LOGGER.debug("No Plex tracks in MA queue, skipping PlayQueue creation")
            return

        start_item = None
        if ma_queue.current_index is not None and ma_queue.current_index < len(plex_items):
            start_item = plex_items[ma_queue.current_index]

        plex_server = self.plex_server

        def create_queue() -> PlayQueue:
            return PlayQueue.create(
                plex_server,
                items=plex_items,
                startItem=start_item,
                shuffle=0,
                continuous=1,
            )

        try:
            playqueue = await asyncio.to_thread(create_queue)

            # `if playqueue:` would route through plexapi's __len__ (playQueueTotalCount),
            # which can be None for continuous/radio queues, so test identity instead.
            if playqueue is not None:
                self.play_queue_id = str(playqueue.playQueueID)
                self.play_queue_version = playqueue.playQueueVersion

                self.play_queue_item_ids = {}
                for i, item in enumerate(playqueue.items):
                    _, play_queue_item_id = plex_item_fields(item)
                    if play_queue_item_id is not None:
                        self.play_queue_item_ids[i] = play_queue_item_id

                LOGGER.info(
                    f"Created Plex PlayQueue {self.play_queue_id} with {len(plex_items)} tracks"
                )
        except Exception:
            LOGGER.exception("Error creating Plex PlayQueue")

    async def _sync_initial_queue_to_plex(self) -> None:
        """
        Mirror an already-loaded MA queue to a Plex PlayQueue on startup.

        When the plugin starts the MA player may already have an active queue. Creating
        the matching Plex PlayQueue immediately makes that queue visible and controllable
        from Plex apps without waiting for the next queue change. No-op if the queue is
        empty.
        """
        if not self._ma_player_id:
            return
        if not self.provider.mass.player_queues.items(self._ma_player_id):
            return
        try:
            await self._create_plex_playqueue_from_ma()
            self._remember_synced_queue(self._ma_player_id)
        except Exception as e:
            LOGGER.debug(f"Failed to sync initial queue to Plex on startup: {e}")

    async def _handle_state_event(self, event: MassEvent) -> None:
        """Forward an MA player/queue state change to the subscribed Plex controllers."""
        if not self._ma_player_id or event.object_id != self._ma_player_id:
            return

        if self._updating_from_plex:
            return

        try:
            await self._send_timeline_to_server()
            await self._broadcast_timeline()
        except Exception as e:
            LOGGER.debug(f"Error handling state event: {e}")

    async def _handle_queue_items_updated(self, event: MassEvent) -> None:
        """Handle queue items being added/removed/reordered."""
        if not self._ma_player_id or event.object_id != self._ma_player_id:
            return

        if self._updating_from_plex:
            return

        if not self.provider.mass.player_queues.items(self._ma_player_id):
            return

        current_keys = self._collect_synced_keys(self._ma_player_id)

        if (
            len(current_keys) == self._last_synced_ma_queue_length
            and current_keys == self._last_synced_ma_queue_keys
        ):
            LOGGER.debug("MA queue matches last synced state, skipping Plex sync")
            return

        LOGGER.info(
            f"MA queue changed: {self._last_synced_ma_queue_length} -> {len(current_keys)} items"
        )

        try:
            await self._create_plex_playqueue_from_ma()
            self._remember_synced_queue(self._ma_player_id, current_keys)
        except Exception as e:
            LOGGER.debug(f"Error creating Plex PlayQueue: {e}")

        try:
            await self._broadcast_timeline()
        except Exception as e:
            LOGGER.debug(f"Error broadcasting timeline: {e}")
