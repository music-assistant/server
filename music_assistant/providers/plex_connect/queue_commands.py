"""Play queue command handlers for Plex remote control.

Handles playMedia, createPlayQueue and refreshPlayQueue HTTP commands.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any

from aiohttp import web
from music_assistant_models.enums import QueueOption
from plexapi.playqueue import PlayQueue

if TYPE_CHECKING:
    from music_assistant.providers.plex import PlexProvider

LOGGER = logging.getLogger(__name__)


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

        async def _ungroup_player_if_needed(self, player_id: str) -> None: ...

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

        def _collect_synced_keys(self, player_id: str) -> list[str]: ...

    async def _resolve_plex_item(self, key: str) -> Any:
        """Resolve a Plex key to a Music Assistant media item.

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
        """Fetch a Plex PlayQueue and start playback, loading remaining tracks in the background.

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

            def fetch_queue() -> PlayQueue:
                return PlayQueue.get(self.provider._plex_server, playQueueID=queue_id, window=10000)

            playqueue = await asyncio.to_thread(fetch_queue)

            if playqueue and playqueue.items:
                selected_offset = getattr(playqueue, "playQueueSelectedItemOffset", 0)
                LOGGER.info(f"PlayQueue selected item offset: {selected_offset}")

                self.play_queue_item_ids = {}

                first_item = (
                    playqueue.items[selected_offset]
                    if selected_offset < len(playqueue.items)
                    else playqueue.items[0]
                )
                first_track_key = first_item.key if hasattr(first_item, "key") else None
                first_play_queue_item_id = (
                    first_item.playQueueItemID if hasattr(first_item, "playQueueItemID") else None
                )

                if not first_track_key:
                    LOGGER.error("No valid first track in play queue")
                    if starting_key:
                        track = await self.provider.get_track(starting_key)
                        await self.provider.mass.player_queues.play_media(
                            queue_id=player_id,
                            media=track,
                            option=QueueOption.REPLACE,
                        )
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
                    )

                    if offset > 0:
                        await self._seek_to_offset_after_playback(player_id, offset)

                    await self._broadcast_timeline()

                    self.provider.mass.create_task(
                        self._load_remaining_queue_tracks(
                            player_id, playqueue, selected_offset, shuffle
                        )
                    )

                except Exception as e:
                    LOGGER.exception(f"Error starting playback with first track: {e}")
                    if starting_key:
                        track = await self.provider.get_track(starting_key)
                        await self.provider.mass.player_queues.play_media(
                            queue_id=player_id,
                            media=track,
                            option=QueueOption.REPLACE,
                        )
            else:
                LOGGER.error("Play queue is empty or could not be fetched")
                if starting_key:
                    track = await self.provider.get_track(starting_key)
                    await self.provider.mass.player_queues.play_media(
                        queue_id=player_id,
                        media=track,
                        option=QueueOption.REPLACE,
                    )

        except Exception as e:
            LOGGER.exception(f"Error playing from queue: {e}")
            if starting_key:
                track = await self.provider.get_track(starting_key)
                await self.provider.mass.player_queues.play_media(
                    queue_id=player_id,
                    media=track,
                    option=QueueOption.REPLACE,
                )

    async def handle_play_media(self, request: web.Request) -> web.Response:
        """Handle playMedia command from Plex controller.

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

            await self._ungroup_player_if_needed(player_id)

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
                    )
            elif container_key:
                self.play_queue_id = None
                self.play_queue_item_ids = {}
                media_to_play = await self._resolve_plex_item(container_key)
                await self.provider.mass.player_queues.play_media(
                    queue_id=player_id,
                    media=media_to_play,
                    option=QueueOption.REPLACE,
                )
            else:
                self.play_queue_id = None
                self.play_queue_item_ids = {}
                media = await self._resolve_plex_item(key)
                await self.provider.mass.player_queues.play_media(
                    queue_id=player_id,
                    media=media,
                    option=QueueOption.REPLACE,
                )

            if shuffle:
                await self.provider.mass.player_queues.set_shuffle(player_id, shuffle)

            if offset > 0:
                await self._seek_to_offset_after_playback(player_id, offset)

            await self._broadcast_timeline()
            return web.Response(status=200)

        except Exception as e:
            LOGGER.exception(f"Error handling playMedia: {e}")
            return web.Response(status=500, text=str(e))
        finally:
            self._updating_from_plex = False

    async def handle_create_play_queue(self, request: web.Request) -> web.Response:
        """Handle createPlayQueue command from Plex controller.

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

            if playqueue and playqueue.items:
                self.play_queue_id = str(playqueue.playQueueID)
                self.play_queue_version = 1

                LOGGER.info(
                    f"Created play queue {self.play_queue_id} with {len(playqueue.items)} items"
                )

                self.play_queue_item_ids = {}
                first_item = playqueue.items[0]
                first_track_key = first_item.key if hasattr(first_item, "key") else None
                first_play_queue_item_id = (
                    first_item.playQueueItemID if hasattr(first_item, "playQueueItemID") else None
                )

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
                    )

                    if len(playqueue.items) > 1:
                        self.provider.mass.create_task(
                            self._load_remaining_queue_tracks(player_id, playqueue, 0, shuffle)
                        )

                    await self._broadcast_timeline()
                    return web.Response(status=200)

                except Exception as e:
                    LOGGER.exception(f"Error starting playback with first track: {e}")
                    return web.Response(status=500, text=f"Failed to start playback: {e}")
            else:
                LOGGER.error("Failed to create play queue or queue is empty")
                return web.Response(status=500, text="Failed to create play queue")

        except Exception as e:
            LOGGER.exception(f"Error handling createPlayQueue: {e}")
            return web.Response(status=500, text=str(e))
        finally:
            self._updating_from_plex = False

    async def handle_refresh_play_queue(self, request: web.Request) -> web.Response:
        """Handle refreshPlayQueue command from Plex controller.

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

            def fetch_queue() -> PlayQueue:
                return PlayQueue.get(self.provider._plex_server, playQueueID=play_queue_id)

            playqueue = await asyncio.to_thread(fetch_queue)

            if not playqueue or not playqueue.items:
                LOGGER.error("Failed to refresh play queue - queue is empty or not found")
                return web.Response(status=404, text="Play queue not found")

            player_id = self._ma_player_id
            if not player_id:
                LOGGER.error("No player assigned to this server")
                return web.Response(status=500, text="No player assigned")

            await self.provider.mass.player_queues.set_shuffle(player_id, False)
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

            LOGGER.info(
                f"Refreshed play queue {play_queue_id} - now has {len(playqueue.items)} items"
            )

            synced_keys = self._collect_synced_keys(player_id)
            self._last_synced_ma_queue_length = len(synced_keys)
            self._last_synced_ma_queue_keys = synced_keys

            return web.Response(status=200)

        except Exception as e:
            LOGGER.exception(f"Error handling refreshPlayQueue: {e}")
            return web.Response(status=500, text=str(e))
        finally:
            self._updating_from_plex = False
