"""Queue synchronisation logic for Plex remote control.

Handles background queue loading, MA→Plex sync, and MA event handlers.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import QueueOption
from plexapi.playqueue import PlayQueue

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

    def _collect_synced_keys(self, player_id: str) -> list[str]:
        """Return Plex item_id keys for every track currently in the MA queue.

        :param player_id: The Music Assistant player ID.
        :return: Ordered list of Plex item IDs matching the current MA queue.
        """
        synced_keys = []
        for item in self.provider.mass.player_queues.items(player_id):
            if item.media_item:
                for mapping in item.media_item.provider_mappings:
                    if mapping.provider_instance == self.provider.instance_id:
                        synced_keys.append(mapping.item_id)
                        break
        return synced_keys

    def _reorder_tracks_for_playback(
        self, tracks: list[Any], start_index: int
    ) -> tuple[list[Any], dict[int, int]]:
        """Reorder tracks to start from a specific index and update item ID mappings.

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
        """Load remaining tracks from play queue in the background.

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
                track_key = item.key if hasattr(item, "key") else None
                play_queue_item_id = (
                    item.playQueueItemID if hasattr(item, "playQueueItemID") else None
                )
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

                    synced_keys = self._collect_synced_keys(player_id)
                    self._last_synced_ma_queue_length = len(synced_keys)
                    self._last_synced_ma_queue_keys = synced_keys
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
        """Replace the entire MA queue from a Plex play queue.

        :param player_id: The Music Assistant player ID.
        :param playqueue: The Plex play queue to load.
        """
        all_tracks = []
        self.play_queue_item_ids = {}

        for i, item in enumerate(playqueue.items):
            track_key = item.key if hasattr(item, "key") else None
            play_queue_item_id = item.playQueueItemID if hasattr(item, "playQueueItemID") else None

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

    async def _replace_remaining_queue(
        self, player_id: str, playqueue: PlayQueue, current_index: int
    ) -> None:
        """Replace only items after the current track.

        :param player_id: The Music Assistant player ID.
        :param playqueue: The Plex play queue to load.
        :param current_index: The current track index in the MA queue.
        """
        remaining_tracks = []
        new_item_mappings = {}

        for i in range(current_index + 1, len(playqueue.items)):
            item = playqueue.items[i]
            track_key = item.key if hasattr(item, "key") else None
            play_queue_item_id = item.playQueueItemID if hasattr(item, "playQueueItemID") else None

            if track_key:
                try:
                    track = await self.provider.get_track(track_key)
                    remaining_tracks.append(track)
                    if play_queue_item_id:
                        new_item_mappings[current_index + 1 + len(remaining_tracks) - 1] = (
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
            self.play_queue_item_ids.update(new_item_mappings)
            LOGGER.info(
                f"Replaced {len(remaining_tracks)} tracks after current track "
                f"(index {current_index})"
            )
        else:
            LOGGER.debug("No tracks after current track in Plex queue")

        for i, item in enumerate(playqueue.items):
            play_queue_item_id = item.playQueueItemID if hasattr(item, "playQueueItemID") else None
            if play_queue_item_id:
                self.play_queue_item_ids[i] = play_queue_item_id

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
            if not item.media_item:
                continue
            plex_key = None
            for mapping in item.media_item.provider_mappings:
                if mapping.provider_instance == self.provider.instance_id:
                    plex_key = mapping.item_id
                    break
            if plex_key:
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

            if playqueue:
                self.play_queue_id = str(playqueue.playQueueID)
                self.play_queue_version = playqueue.playQueueVersion

                self.play_queue_item_ids = {}
                for i, item in enumerate(playqueue.items):
                    if hasattr(item, "playQueueItemID"):
                        self.play_queue_item_ids[i] = item.playQueueItemID

                LOGGER.info(
                    f"Created Plex PlayQueue {self.play_queue_id} with {len(plex_items)} tracks"
                )
        except Exception as e:
            LOGGER.exception(f"Error creating Plex PlayQueue: {e}")

    async def _handle_player_event(self, event: MassEvent) -> None:
        """Handle player state change events."""
        if not self._ma_player_id or event.object_id != self._ma_player_id:
            return

        if self._updating_from_plex:
            return

        try:
            await self._send_timeline_to_server()
            await self._broadcast_timeline()
        except Exception as e:
            LOGGER.debug(f"Error handling player event: {e}")

    async def _handle_queue_event(self, event: MassEvent) -> None:
        """Handle queue change events."""
        if not self._ma_player_id or event.object_id != self._ma_player_id:
            return

        if self._updating_from_plex:
            return

        try:
            await self._send_timeline_to_server()
            await self._broadcast_timeline()
        except Exception as e:
            LOGGER.debug(f"Error handling queue event: {e}")

    async def _handle_queue_items_updated(self, event: MassEvent) -> None:
        """Handle queue items being added/removed/reordered."""
        if not self._ma_player_id or event.object_id != self._ma_player_id:
            return

        if self._updating_from_plex:
            return

        queue_items = self.provider.mass.player_queues.items(self._ma_player_id)
        if not queue_items:
            return

        current_keys = []
        for item in queue_items:
            if not item.media_item:
                continue
            for mapping in item.media_item.provider_mappings:
                if mapping.provider_instance == self.provider.instance_id:
                    current_keys.append(mapping.item_id)
                    break

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
            self._last_synced_ma_queue_length = len(current_keys)
            self._last_synced_ma_queue_keys = current_keys
        except Exception as e:
            LOGGER.debug(f"Error creating Plex PlayQueue: {e}")

        try:
            await self._broadcast_timeline()
        except Exception as e:
            LOGGER.debug(f"Error broadcasting timeline: {e}")
