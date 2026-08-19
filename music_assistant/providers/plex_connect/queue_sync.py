"""
Queue synchronisation logic for Plex remote control.

Handles background queue loading, MA→Plex sync, and MA event handlers.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import QueueOption
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

        except Exception as e:
            LOGGER.exception(f"Error loading remaining queue tracks: {e}")

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
            )
            LOGGER.info(f"Replaced queue with {len(all_tracks)} tracks")

    def _plex_index_for_ma_index(self, playqueue: PlayQueue, ma_index: int) -> int:
        """
        Return the index in the Plex queue holding the MA queue item at ``ma_index``.

        The two queues are not positionally aligned: playback starts at the item Plex has
        selected, so MA index 0 holds that item and the items before it wrap around to the
        tail. Positions are therefore matched on playQueueItemID rather than compared
        directly, falling back to whichever item Plex reports as selected.

        :param playqueue: The Plex play queue.
        :param ma_index: An index into the Music Assistant queue.
        :return: The matching index within ``playqueue.items``.
        """
        item_id = self.play_queue_item_ids.get(ma_index)
        if item_id is not None:
            for index, item in enumerate(playqueue.items):
                if plex_item_fields(item)[1] == item_id:
                    return index
        return self._selected_item_index(playqueue)

    async def _replace_remaining_queue(
        self, player_id: str, playqueue: PlayQueue, current_index: int
    ) -> None:
        """
        Replace only items after the current track.

        :param player_id: The Music Assistant player ID.
        :param playqueue: The Plex play queue to load.
        :param current_index: The current track index in the MA queue.
        """
        plex_index = self._plex_index_for_ma_index(playqueue, current_index)

        # Wrap around just like the initial load does, so refreshing an unchanged Plex
        # queue is a no-op instead of dropping the items that wrapped to the tail.
        remaining_items = playqueue.items[plex_index + 1 :] + playqueue.items[:plex_index]

        remaining_tracks = []
        new_item_mappings = {}

        for item in remaining_items:
            track_key, play_queue_item_id = plex_item_fields(item)

            if track_key:
                try:
                    track = await self.provider.get_track(track_key)
                    remaining_tracks.append(track)
                    if play_queue_item_id:
                        new_item_mappings[current_index + len(remaining_tracks)] = (
                            play_queue_item_id
                        )
                except Exception as e:
                    LOGGER.debug(f"Could not fetch track {track_key}: {e}")
                    continue

        if remaining_tracks:
            await self.provider.mass.player_queues.play_media(
                queue_id=player_id,
                media=remaining_tracks,  # type: ignore[arg-type]
                option=QueueOption.REPLACE_NEXT,
            )
            # REPLACE_NEXT drops every item after the current one, so their ids go with
            # them; rebuild the map so it stays keyed by MA index.
            self.play_queue_item_ids = {
                index: item_id
                for index, item_id in self.play_queue_item_ids.items()
                if index <= current_index
            }
            self.play_queue_item_ids.update(new_item_mappings)
            LOGGER.info(
                f"Replaced {len(remaining_tracks)} tracks after current track "
                f"(index {current_index})"
            )
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
        except Exception as e:
            LOGGER.exception(f"Error creating Plex PlayQueue: {e}")

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
