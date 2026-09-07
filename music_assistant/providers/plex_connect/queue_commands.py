"""
Play queue command handlers for Plex remote control.

Handles playMedia, createPlayQueue and refreshPlayQueue HTTP commands.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

from aiohttp import web
from music_assistant_models.enums import QueueOption
from plexapi.playqueue import PlayQueue

from .parsing import plex_item_fields

if TYPE_CHECKING:
    from music_assistant.providers.plex import PlexProvider

LOGGER = logging.getLogger(__name__)

# Maximum number of tracks loaded from a Plex play queue into Music Assistant.
# Plex play queues can be very large (whole libraries); capping keeps queue loading
# and MA<->Plex sync responsive.
MAX_QUEUE_ITEMS = 100


class QueueCommandsMixin:
    """Mixin providing HTTP handlers for Plex play queue commands."""

    if TYPE_CHECKING:
        provider: PlexProvider
        _ma_player_id: str | None
        _updating_from_plex: bool
        play_queue_id: str | None
        play_queue_version: int
        play_queue_item_ids: dict[int, int]
        _last_synced_ma_queue_length: int
        _last_synced_ma_queue_keys: list[str]

        async def _broadcast_timeline(self) -> None: ...

        async def _seek_to_offset_after_playback(self, player_id: str, offset: int) -> None: ...

        async def _load_remaining_queue_tracks(
            self,
            player_id: str,
            playqueue: PlayQueue,
            selected_offset: int,
            shuffle: bool,
        ) -> None: ...

        async def _replace_entire_queue(self, player_id: str, playqueue: PlayQueue) -> None: ...

        async def _replace_remaining_queue(
            self, player_id: str, playqueue: PlayQueue, current_index: int
        ) -> None: ...

        def _remember_synced_queue(
            self, player_id: str, keys: list[str] | None = None
        ) -> list[str]: ...

        async def _create_plex_playqueue_from_ma(self) -> None: ...

    async def _play_single_track(self, player_id: str, key: str) -> None:
        """
        Resolve a single Plex track key and play it as a fresh (replaced) queue.

        Used as the fallback whenever loading a full Plex play queue fails.

        :param player_id: The Music Assistant player ID.
        :param key: The Plex track key to play.
        """
        track = await self.provider.get_track(key)
        await self.provider.mass.player_queues.play_media(
            queue_id=player_id,
            media=track,
            option=QueueOption.REPLACE,
        )

    async def _fetch_full_play_queue(self, queue_id: str) -> PlayQueue | None:
        """
        Fetch a PlayQueue, paginating past the server's per-request window cap.

        The Plex server caps each response to ~200 items regardless of the requested
        window size. We use playQueueTotalCount to detect truncation and keep fetching
        forward pages until we have every item, up to :data:`MAX_QUEUE_ITEMS`.

        :param queue_id: The Plex PlayQueue ID to fetch.
        :return: A PlayQueue whose items list contains the tracks (capped), or None.
        """
        page_size = 200
        plex_server = self.provider._plex_server

        def fetch_initial() -> PlayQueue:
            # own=True transfers ownership of this queue to MA so we can control it fully.
            return PlayQueue.get(plex_server, playQueueID=queue_id, own=True, window=page_size)

        playqueue = await asyncio.to_thread(fetch_initial)

        # Avoid truthiness checks on the PlayQueue object: plexapi's __len__ returns
        # playQueueTotalCount, which is None for track-radio (and other on-the-fly)
        # queues, so `not playqueue` would raise a TypeError.
        if playqueue is None or not playqueue.items:
            return playqueue

        all_items = list(playqueue.items)
        seen_ids = {item.playQueueItemID for item in all_items}

        # playQueueTotalCount is absent for track-radio queues; fall back to the
        # number of items we already have so pagination simply stops.
        total_count = playqueue.playQueueTotalCount or len(all_items)
        target_count = min(total_count, MAX_QUEUE_ITEMS)
        while len(all_items) < target_count:
            last_id = all_items[-1].playQueueItemID

            def fetch_next(_last_id: int = last_id) -> PlayQueue:
                # own=False — we already claimed ownership on the first fetch.
                # includeBefore=False — only items strictly after center are returned.
                return PlayQueue.get(
                    plex_server,
                    playQueueID=queue_id,
                    own=False,
                    center=_last_id,
                    window=page_size,
                    includeBefore=False,
                )

            next_page = await asyncio.to_thread(fetch_next)
            if next_page is None or not next_page.items:
                break

            new_items = [i for i in next_page.items if i.playQueueItemID not in seen_ids]
            if not new_items:
                break

            all_items.extend(new_items)
            seen_ids.update(i.playQueueItemID for i in new_items)

        if len(all_items) > MAX_QUEUE_ITEMS:
            LOGGER.info(
                "Capping Plex play queue from %d to %d items", len(all_items), MAX_QUEUE_ITEMS
            )
            all_items = all_items[:MAX_QUEUE_ITEMS]

        # Patch the cached items property on the PlayQueue object.
        playqueue.__dict__["items"] = all_items
        return playqueue

    def _selected_item_index(self, playqueue: PlayQueue) -> int:
        """Return the selected item's index within the fetched queue window."""
        selected_id = getattr(playqueue, "playQueueSelectedItemID", None)
        if selected_id is not None:
            for index, item in enumerate(playqueue.items):
                if getattr(item, "playQueueItemID", None) == selected_id:
                    return index
        selected_offset = getattr(playqueue, "playQueueSelectedItemOffset", 0) or 0
        return selected_offset if 0 <= selected_offset < len(playqueue.items) else 0

    def _source_key_from_play_queue_uri(self, source_uri: str) -> str | None:
        """
        Extract a Plex library key from a playQueueSourceURI string.

        Plex encodes the source as ``library:///directory/ENCODED_PATH``. We decode it
        to a plain library path that :meth:`_resolve_plex_item` can use.

        :param source_uri: The playQueueSourceURI from a Plex PlayQueue.
        :return: A Plex library key (e.g. ``/library/playlists/5329``), or None if unparsable.
        """
        prefix = "library:///directory/"
        if not source_uri.startswith(prefix):
            return None

        path = unquote(source_uri[len(prefix) :]).lstrip("/")
        if not path:
            return None

        path = "/" + path.split("?")[0]

        for suffix in ("/children", "/allLeaves", "/items"):
            if path.endswith(suffix):
                path = path[: -len(suffix)]
                break

        return path if path and path != "/" else None

    async def _play_from_shuffled_source(
        self,
        player_id: str,
        source_uri: str,
        offset: int,
    ) -> bool:
        """
        Load a shuffled PlayQueue's source in original order, then defer shuffle to MA.

        When Plex reports a shuffled PlayQueue the items are already in Plex's shuffled
        order. Loading them directly would cause MA to shuffle an already-shuffled list.
        Instead we parse the source URI, load the source collection unshuffled into MA,
        and schedule a deferred task that applies MA's own shuffle and then recreates the
        Plex PlayQueue so Plexamp sees the new order.

        :param player_id: The Music Assistant player ID.
        :param source_uri: The playQueueSourceURI from the Plex PlayQueue.
        :param offset: Starting position in milliseconds.
        :return: True if handled, False if the caller should fall back to regular loading.
        """
        source_key = self._source_key_from_play_queue_uri(source_uri)
        if not source_key:
            return False

        try:
            LOGGER.info(f"Shuffled queue detected — loading source in original order: {source_key}")
            source_media = await self._resolve_plex_item(source_key)
            await self.provider.mass.player_queues.play_media(
                queue_id=player_id,
                media=source_media,
                option=QueueOption.REPLACE,
                # the deferred task below applies MA's shuffle; loading the source already
                # shuffled would leave the queue's own order out of step with the Plex one
                shuffle=False,
            )
            if offset > 0:
                await self._seek_to_offset_after_playback(player_id, offset)
            await self._broadcast_timeline()

            async def _apply_shuffle_deferred() -> None:
                await self.provider.mass.player_queues.set_shuffle(player_id, True)
                await asyncio.sleep(0.2)
                await self._create_plex_playqueue_from_ma()
                self._remember_synced_queue(player_id)

            self.provider.mass.create_task(_apply_shuffle_deferred())
            return True
        except Exception as e:
            LOGGER.debug(f"Could not resolve source for shuffled queue, falling back: {e}")
            return False

    async def _resolve_plex_item(self, key: str) -> Any:
        """
        Resolve a Plex key to a Music Assistant media item.

        :param key: The Plex key to resolve.
        """
        if "/library/metadata/" in key:
            try:
                return await self.provider.get_track(key)
            except Exception as exc:
                LOGGER.debug(f"Failed to resolve Plex item as track for key '{key}': {exc}")

            try:
                return await self.provider.get_album(key)
            except Exception as exc:
                LOGGER.debug(f"Failed to resolve Plex item as album for key '{key}': {exc}")

            try:
                return await self.provider.get_artist(key)
            except Exception:
                raise ValueError(f"Could not resolve Plex item: {key}") from None

        elif "/playlists/" in key:
            return await self.provider.get_playlist(key)
        else:
            raise ValueError(f"Unknown Plex key format: {key}")

    async def _play_from_plex_queue(
        self,
        player_id: str,
        container_key: str,
        starting_key: str | None,
        shuffle: bool,
        offset: int,
    ) -> None:
        """
        Fetch a Plex PlayQueue and start playback, loading remaining tracks in the background.

        :param player_id: The Music Assistant player ID.
        :param container_key: The Plex container key (e.g. /playQueues/123).
        :param starting_key: Fallback track key if queue fetch fails.
        :param shuffle: Whether shuffle is enabled.
        :param offset: Starting position in milliseconds.
        """
        try:
            LOGGER.info(f"Fetching play queue: {container_key}")

            queue_id_match = re.search(r"/playQueues/(\d+)", container_key)
            if not queue_id_match:
                raise ValueError(f"Invalid container_key format: {container_key}")

            queue_id = queue_id_match.group(1)

            playqueue = await self._fetch_full_play_queue(queue_id)

            if playqueue is not None and playqueue.items:
                selected_offset = self._selected_item_index(playqueue)
                LOGGER.info(f"PlayQueue selected item index: {selected_offset}")

                # When Plex reports a shuffled queue, load the original source into MA
                # unshuffled and let MA apply its own shuffle, then propagate back to Plex.
                if playqueue.playQueueShuffled and getattr(playqueue, "playQueueSourceURI", None):
                    if await self._play_from_shuffled_source(
                        player_id, playqueue.playQueueSourceURI, offset
                    ):
                        return

                self.play_queue_item_ids = {}

                first_item = playqueue.items[selected_offset]
                first_track_key, first_play_queue_item_id = plex_item_fields(first_item)

                if not first_track_key:
                    LOGGER.error("No valid first track in play queue")
                    if starting_key:
                        await self._play_single_track(player_id, starting_key)
                    return

                try:
                    first_track = await self.provider.get_track(first_track_key)
                    LOGGER.info(f"Starting playback with first track: {first_track.name}")

                    if first_play_queue_item_id:
                        self.play_queue_item_ids[0] = first_play_queue_item_id

                    await self.provider.mass.player_queues.play_media(
                        queue_id=player_id,
                        media=first_track,
                        option=QueueOption.REPLACE,
                        # Plex owns the order and a shuffled play queue already arrives
                        # shuffled, so the remaining tracks must be appended to an unshuffled
                        # queue; _load_remaining_queue_tracks sets the flag once they landed
                        shuffle=False,
                    )

                    if offset > 0:
                        await self._seek_to_offset_after_playback(player_id, offset)

                    await self._broadcast_timeline()

                    # Use the queue's own shuffle flag as the authoritative state,
                    # falling back to the request parameter if not set.
                    effective_shuffle = playqueue.playQueueShuffled or shuffle
                    self.provider.mass.create_task(
                        self._load_remaining_queue_tracks(
                            player_id, playqueue, selected_offset, effective_shuffle
                        )
                    )

                except Exception:
                    LOGGER.exception("Error starting playback with first track")
                    if starting_key:
                        await self._play_single_track(player_id, starting_key)
            else:
                LOGGER.error("Play queue is empty or could not be fetched")
                if starting_key:
                    await self._play_single_track(player_id, starting_key)

        except Exception:
            LOGGER.exception("Error playing from queue")
            if starting_key:
                await self._play_single_track(player_id, starting_key)

    async def handle_play_media(self, request: web.Request) -> web.Response:
        """
        Handle playMedia command from Plex controller.

        Plexamp sends various parameters:
        - key: The item to play (track, album, playlist, etc.)
        - containerKey: The container context (play queue)
        - offset: Starting position in milliseconds
        - shuffle: Whether to shuffle
        - repeat: Repeat mode
        """
        self._updating_from_plex = True
        try:
            key = request.query.get("key")
            container_key = request.query.get("containerKey")
            offset = int(request.query.get("offset", 0))
            shuffle = request.query.get("shuffle", "0") == "1"

            if not key:
                return web.Response(
                    status=400, text="Missing required 'key' parameter for playMedia command"
                )

            LOGGER.info(
                f"Received playMedia command - key: {key}, "
                f"containerKey: {container_key}, offset: {offset}ms"
            )

            player_id = self._ma_player_id
            if not player_id:
                return web.Response(status=500, text="No player assigned to this server")

            if container_key and "/playQueues/" in container_key:
                queue_id_match = re.search(r"/playQueues/(\d+)", container_key)
                if queue_id_match:
                    self.play_queue_id = queue_id_match.group(1)
                    self.play_queue_version = 1
                    LOGGER.info(f"Playing from queue: {container_key} starting at {key}")
                    await self._play_from_plex_queue(player_id, container_key, key, shuffle, offset)
                else:
                    self.play_queue_id = None
                    self.play_queue_item_ids = {}
                    media = await self._resolve_plex_item(key)
                    await self.provider.mass.player_queues.play_media(
                        queue_id=player_id,
                        media=media,
                        option=QueueOption.REPLACE,
                        shuffle=shuffle,
                    )
            elif container_key:
                self.play_queue_id = None
                self.play_queue_item_ids = {}
                media_to_play = await self._resolve_plex_item(container_key)
                await self.provider.mass.player_queues.play_media(
                    queue_id=player_id,
                    media=media_to_play,
                    option=QueueOption.REPLACE,
                    shuffle=shuffle,
                )
            else:
                self.play_queue_id = None
                self.play_queue_item_ids = {}
                media = await self._resolve_plex_item(key)
                await self.provider.mass.player_queues.play_media(
                    queue_id=player_id,
                    media=media,
                    option=QueueOption.REPLACE,
                    shuffle=shuffle,
                )

            # Always sync shuffle state so that a previously enabled MA shuffle
            # does not reorder an unshuffled Plex queue.
            await self.provider.mass.player_queues.set_shuffle(player_id, shuffle)

            if offset > 0:
                await self._seek_to_offset_after_playback(player_id, offset)

            await self._broadcast_timeline()
            return web.Response(status=200)

        except Exception:
            LOGGER.exception("Error handling playMedia")
            return web.Response(status=500, text="Internal error")
        finally:
            self._updating_from_plex = False

    async def handle_create_play_queue(self, request: web.Request) -> web.Response:
        """
        Handle createPlayQueue command from Plex controller.

        Creates a new play queue from a URI (album, playlist, artist tracks, etc.)
        and optionally applies shuffle.
        """
        self._updating_from_plex = True
        try:
            uri = request.query.get("uri")
            shuffle = request.query.get("shuffle", "0") == "1"
            continuous = request.query.get("continuous", "0") == "1"

            if not uri:
                return web.Response(status=400, text="Missing 'uri' parameter")

            LOGGER.info(f"Received createPlayQueue command - uri: {uri}, shuffle: {shuffle}")

            player_id = self._ma_player_id
            if not player_id:
                return web.Response(status=500, text="No player assigned to this server")

            def create_queue() -> PlayQueue:
                item = self.provider._plex_server.fetchItem(uri)
                return PlayQueue.create(
                    self.provider._plex_server,
                    item,
                    shuffle=1 if shuffle else 0,
                    continuous=1 if continuous else 0,
                )

            playqueue = await asyncio.to_thread(create_queue)

            if playqueue is not None and playqueue.items:
                self.play_queue_id = str(playqueue.playQueueID)
                self.play_queue_version = 1

                if len(playqueue.items) > MAX_QUEUE_ITEMS:
                    LOGGER.info(
                        "Capping created Plex play queue from %d to %d items",
                        len(playqueue.items),
                        MAX_QUEUE_ITEMS,
                    )
                    playqueue.__dict__["items"] = playqueue.items[:MAX_QUEUE_ITEMS]

                LOGGER.info(
                    f"Created play queue {self.play_queue_id} with {len(playqueue.items)} items"
                )

                self.play_queue_item_ids = {}
                first_item = playqueue.items[0]
                first_track_key, first_play_queue_item_id = plex_item_fields(first_item)

                if not first_track_key:
                    LOGGER.error("No valid first track in created play queue")
                    return web.Response(status=500, text="Failed to load tracks from play queue")

                try:
                    first_track = await self.provider.get_track(first_track_key)
                    LOGGER.info(f"Starting playback with first track: {first_track.name}")

                    if first_play_queue_item_id:
                        self.play_queue_item_ids[0] = first_play_queue_item_id

                    await self.provider.mass.player_queues.play_media(
                        queue_id=player_id,
                        media=first_track,
                        option=QueueOption.REPLACE,
                        # as above: the tracks that follow are appended in Plex's order
                        shuffle=False,
                    )

                    if len(playqueue.items) > 1:
                        self.provider.mass.create_task(
                            self._load_remaining_queue_tracks(player_id, playqueue, 0, shuffle)
                        )

                    await self._broadcast_timeline()
                    return web.Response(status=200)

                except Exception:
                    LOGGER.exception("Error starting playback with first track")
                    return web.Response(status=500, text="Failed to start playback")
            else:
                LOGGER.error("Failed to create play queue or queue is empty")
                return web.Response(status=500, text="Failed to create play queue")

        except Exception:
            LOGGER.exception("Error handling createPlayQueue")
            return web.Response(status=500, text="Internal error")
        finally:
            self._updating_from_plex = False

    async def handle_refresh_play_queue(self, request: web.Request) -> web.Response:
        """
        Handle refreshPlayQueue command from Plex controller.

        Called when the play queue is modified (items added, removed, reordered).
        Syncs the updated queue state to MA while preserving current playback.
        """
        self._updating_from_plex = True
        try:
            play_queue_id = request.query.get("playQueueID")

            if not play_queue_id:
                return web.Response(status=400, text="Missing 'playQueueID' parameter")

            LOGGER.info(
                f"Received refreshPlayQueue command - playQueueID: {play_queue_id}, "
                f"params: {dict(request.query)}"
            )

            if self.play_queue_id != play_queue_id:
                LOGGER.warning(
                    f"Refresh requested for queue {play_queue_id} but active queue is "
                    f"{self.play_queue_id}"
                )
                return web.Response(
                    status=409,
                    text=(
                        f"Requested playQueueID {play_queue_id} does not match "
                        f"active queue {self.play_queue_id}"
                    ),
                )

            self.play_queue_version += 1

            playqueue = await self._fetch_full_play_queue(play_queue_id)

            if playqueue is None or not playqueue.items:
                LOGGER.error("Failed to refresh play queue - queue is empty or not found")
                return web.Response(status=404, text="Play queue not found")

            player_id = self._ma_player_id
            if not player_id:
                LOGGER.error("No player assigned to this server")
                return web.Response(status=500, text="No player assigned")

            ma_queue = self.provider.mass.player_queues.get(player_id)
            if not ma_queue:
                LOGGER.error(f"MA queue not found for player {player_id}")
                return web.Response(status=500, text="MA queue not found")

            current_index = ma_queue.current_index
            ma_queue_items = self.provider.mass.player_queues.items(player_id)
            ma_queue_count = len(ma_queue_items) if ma_queue_items else 0

            LOGGER.debug(
                f"Queue refresh: Current index={current_index}, "
                f"MA has {ma_queue_count} items, Plex has {len(playqueue.items)} items"
            )

            if current_index is None:
                LOGGER.debug("No track currently playing, replacing entire queue")
                await self._replace_entire_queue(player_id, playqueue)
            else:
                LOGGER.debug(
                    f"Track at index {current_index} is playing, "
                    f"replacing only items after current track"
                )
                await self._replace_remaining_queue(player_id, playqueue, current_index)

            # Sync shuffle state from Plex to MA.
            await self.provider.mass.player_queues.set_shuffle(
                player_id, playqueue.playQueueShuffled
            )

            LOGGER.info(
                f"Refreshed play queue {play_queue_id} - now has {len(playqueue.items)} items"
            )

            self._remember_synced_queue(player_id)

            return web.Response(status=200)

        except Exception:
            LOGGER.exception("Error handling refreshPlayQueue")
            return web.Response(status=500, text="Internal error")
        finally:
            self._updating_from_plex = False
