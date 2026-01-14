"""Per-player Plex remote control instances."""

from __future__ import annotations

import asyncio
import logging
import platform
import re
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from aiohttp import ClientTimeout, web
from music_assistant_models.enums import (
    EventType,
    PlayerFeature,
    PlayerType,
    QueueOption,
    RepeatMode,
)
from plexapi.playqueue import PlayQueue

from .gdm import PlexGDMAdvertiser

if TYPE_CHECKING:
    from music_assistant_models.event import MassEvent

    from music_assistant.providers.plex import PlexProvider


LOGGER = logging.getLogger(__name__)


class PlayerRemoteInstance:
    """Single remote control instance for one MA player."""

    def __init__(
        self,
        plex_provider: PlexProvider,
        ma_player_id: str,
        player_name: str,
        port: int,
        device_class: str = "speaker",
        remote_control: bool = False,
    ) -> None:
        """Initialize player remote instance.

        :param plex_provider: Plex provider instance.
        :param ma_player_id: Music Assistant player ID.
        :param player_name: Display name for the player.
        :param port: Port for the remote control server.
        :param device_class: Device class (speaker, phone, tablet, stb, tv, pc, cloud).
        :param remote_control: Whether to enable remote control.
        """
        self.plex_provider = plex_provider
        self.plex_server = plex_provider._plex_server
        self.ma_player_id = ma_player_id
        self.player_name = player_name
        self.port = port
        self.device_class = device_class
        self.remote_control = remote_control

        self.client_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_DNS,
                f"music-assistant-plex-{plex_provider.instance_id}-{ma_player_id}",
            )
        )

        if self.remote_control:
            # Remote control server
            self.server: PlexRemoteControlServer | None = None
            # GDM advertiser
            self.gdm: PlexGDMAdvertiser | None = None

    async def start(self) -> None:
        """Start this player's remote control."""
        if self.remote_control:
            # Create player-specific PlexServer instance with unique client identification
            LOGGER.info(
                f"Created PlexServer for '{self.player_name}' with client ID: {self.client_id}"
            )

            self.server = PlexRemoteControlServer(
                plex_provider=self.plex_provider,
                port=self.port,
                client_id=self.client_id,
                ma_player_id=self.ma_player_id,
                device_class=self.device_class,
            )
            LOGGER.info(
                f"Remote control server for '{self.player_name}' bound to MA player: "
                f"{self.ma_player_id}"
            )

            await self.server.start()

            # Step 4: Start GDM broadcasting
            self.gdm = PlexGDMAdvertiser(
                instance_id=self.client_id,
                port=self.port,
                publish_ip=str(self.plex_provider.mass.streams.publish_ip),
                name=self.player_name,
                product="Music Assistant",
                version=self.plex_provider.mass.version
                if self.plex_provider.mass.version != "0.0.0"
                else "1.0.0",
            )
            self.gdm.start()

            LOGGER.info(f"Player '{self.player_name}' is now discoverable on port {self.port}")

    async def stop(self) -> None:
        """Stop this player's remote control."""
        if self.remote_control:
            if self.gdm:
                await self.gdm.stop()

            if self.server:
                await self.server.stop()

            LOGGER.info(f"Stopped remote control for player '{self.player_name}'")


class PlexRemoteControlServer:
    """HTTP server to receive Plex remote control commands."""

    def __init__(
        self,
        plex_provider: PlexProvider,
        port: int = 32500,
        client_id: str | None = None,
        ma_player_id: str | None = None,
        device_class: str = "speaker",
    ) -> None:
        """Initialize remote control server.

        :param plex_provider: Plex provider instance.
        :param port: Port for the HTTP server.
        :param client_id: Unique client identifier.
        :param ma_player_id: Music Assistant player ID.
        :param device_class: Device class (speaker, phone, tablet, stb, tv, pc, cloud).
        """
        self.provider = plex_provider
        self.plex_server = plex_provider._plex_server
        self.port = port
        self.client_id = client_id or plex_provider.instance_id
        self.device_class = device_class
        self.app = web.Application()
        self.subscriptions: dict[str, dict[str, object]] = {}
        self.runner: web.AppRunner | None = None
        self.http_site: web.TCPSite | None = None

        # Play queue tracking (Plex-specific state that doesn't exist in MA)
        self.play_queue_id: str | None = None
        self.play_queue_version: int = 1
        # Map queue index to item ID
        self.play_queue_item_ids: dict[int, int] = {}

        # Track MA queue state to detect when we need to sync to Plex
        self._last_synced_ma_queue_length: int = 0
        self._last_synced_ma_queue_keys: list[str] = []

        # Specific MA player this server controls (set by PlayerRemoteInstance)
        self._ma_player_id = ma_player_id

        # Store unsubscribe callbacks
        self._unsub_callbacks: list[Callable[..., None]] = []

        # Flag to prevent circular updates when we modify the queue ourselves
        self._updating_from_plex = False

        self.player = self.provider.mass.players.get(self._ma_player_id)  # type: ignore[arg-type]

        self.device_name = f"{self.player.display_name}" if self.player else "Music Assistant"

        self.headers = {
            "X-Plex-Device-Name": self.device_name,
            "X-Plex-Session-Identifier": self.client_id,
            "X-Plex-Client-Identifier": self.client_id,
            "X-Plex-Product": "Music Assistant",
            "X-Plex-Platform": "Music Assistant",
            "X-Plex-Platform-Version": platform.release(),
        }

        self._setup_routes()

    def _setup_routes(self) -> None:
        """Set up all required endpoints."""
        # Root endpoint
        self.app.router.add_get("/", self.handle_root)

        # Subscription management
        self.app.router.add_get("/player/timeline/subscribe", self.handle_subscribe)
        self.app.router.add_get("/player/timeline/unsubscribe", self.handle_unsubscribe)
        self.app.router.add_get("/player/timeline/poll", self.handle_poll)

        # Playback commands
        self.app.router.add_get("/player/playback/playMedia", self.handle_play_media)
        self.app.router.add_get("/player/playback/refreshPlayQueue", self.handle_refresh_play_queue)
        self.app.router.add_get("/player/playback/createPlayQueue", self.handle_create_play_queue)
        self.app.router.add_get("/player/playback/pause", self.handle_pause)
        self.app.router.add_get("/player/playback/play", self.handle_play)
        self.app.router.add_get("/player/playback/stop", self.handle_stop)
        self.app.router.add_get("/player/playback/skipNext", self.handle_skip_next)
        self.app.router.add_get("/player/playback/skipPrevious", self.handle_skip_previous)
        self.app.router.add_get("/player/playback/stepForward", self.handle_step_forward)
        self.app.router.add_get("/player/playback/stepBack", self.handle_step_back)
        self.app.router.add_get("/player/playback/seekTo", self.handle_seek_to)
        self.app.router.add_get("/player/playback/setParameters", self.handle_set_parameters)
        self.app.router.add_get("/player/playback/skipTo", self.handle_skip_to)

        # Resources endpoint
        self.app.router.add_get("/resources", self.handle_resources)

        # CORS OPTIONS handler (for all routes)
        self.app.router.add_route("OPTIONS", "/{tail:.*}", self.handle_options)

        # --- Catch-all fallback for debugging purposes ---
        # self.app.router.add_route("*", "/{path_info:.*}", self.handle_unknown)

    async def start(self) -> None:
        """Start HTTP server and GDM advertising."""
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()

        # Start HTTP server
        self.http_site = web.TCPSite(self.runner, "0.0.0.0", self.port)
        await self.http_site.start()
        LOGGER.info(f"Plex remote control server started on HTTP port {self.port}")

        # Note: GDM advertising is handled by PlexProvider in __init__.py
        # to avoid duplicate broadcasts

        # Subscribe to player and queue events for state synchronization
        if self._ma_player_id:
            self._unsub_callbacks.append(
                self.provider.mass.subscribe(
                    self._handle_player_event,
                    EventType.PLAYER_UPDATED,
                    id_filter=self._ma_player_id,
                )
            )
            self._unsub_callbacks.append(
                self.provider.mass.subscribe(
                    self._handle_queue_event,
                    EventType.QUEUE_UPDATED,
                    id_filter=self._ma_player_id,
                )
            )
            self._unsub_callbacks.append(
                self.provider.mass.subscribe(
                    self._handle_queue_event,
                    EventType.QUEUE_TIME_UPDATED,
                    id_filter=self._ma_player_id,
                )
            )
            self._unsub_callbacks.append(
                self.provider.mass.subscribe(
                    self._handle_queue_items_updated,
                    EventType.QUEUE_ITEMS_UPDATED,
                    id_filter=self._ma_player_id,
                )
            )

    async def stop(self) -> None:
        """Stop the HTTP server."""
        # Unsubscribe from events
        for unsub in self._unsub_callbacks:
            unsub()
        self._unsub_callbacks.clear()

        # Stop HTTP server
        if self.http_site:
            await self.http_site.stop()
        if self.runner:
            await self.runner.cleanup()
        LOGGER.info("Plex remote control server stopped")

    async def handle_root(self, request: web.Request) -> web.Response:
        """Handle root endpoint - return basic player info."""
        # Get player name
        player_name = "Music Assistant"
        if self._ma_player_id:
            player = self.provider.mass.players.get(self._ma_player_id)
            if player:
                player_name = player.display_name

        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<MediaContainer machineIdentifier="{self.client_id}" version="1.0">
    <Player title="{player_name}" machineIdentifier="{self.client_id}"/>
</MediaContainer>"""
        return web.Response(
            text=xml, content_type="text/xml", headers={"Access-Control-Allow-Origin": "*"}
        )

    async def handle_subscribe(self, request: web.Request) -> web.Response:
        """Handle timeline subscription from controller."""
        client_id = request.headers.get("X-Plex-Client-Identifier")
        protocol = request.query.get("protocol", "http")
        port = request.query.get("port")
        command_id = int(request.query.get("commandID", 0))

        if not client_id or not port:
            return web.Response(status=400)

        self.subscriptions[client_id] = {
            "url": f"{protocol}://{request.remote}:{port}",
            "command_id": command_id,
            "last_update": time.time(),
        }

        LOGGER.info(f"Controller {client_id} subscribed for timeline updates")
        await self._send_timeline(client_id)
        return web.Response(status=200)

    async def handle_unsubscribe(self, request: web.Request) -> web.Response:
        """Handle unsubscribe request."""
        client_id = request.headers.get("X-Plex-Client-Identifier")
        if client_id in self.subscriptions:
            del self.subscriptions[client_id]
            LOGGER.info(f"Controller {client_id} unsubscribed")
        return web.Response(status=200)

    async def handle_poll(self, request: web.Request) -> web.Response:
        """Handle timeline poll request."""
        # Extract parameters
        include_metadata = request.query.get("includeMetadata", "0") == "1"
        command_id = request.query.get("commandID", "0")

        # Update subscription timestamp if this client is subscribed
        client_id = request.headers.get("X-Plex-Client-Identifier")
        if client_id and client_id in self.subscriptions:
            self.subscriptions[client_id]["last_update"] = time.time()

        # Build timeline from current MA player state
        timeline_xml = await self._build_timeline_xml(
            include_metadata=include_metadata, command_id=command_id
        )
        return web.Response(
            text=timeline_xml,
            content_type="text/xml",
            headers={
                "X-Plex-Client-Identifier": self.client_id,
                "Access-Control-Expose-Headers": "X-Plex-Client-Identifier",
                "Access-Control-Allow-Origin": "*",
            },
        )

    async def _ungroup_player_if_needed(self, player_id: str) -> None:
        """Ungroup player before playback if it's part of a group/sync."""
        player = self.provider.mass.players.get(player_id)
        if not player or player.type == PlayerType.GROUP:
            return

        if not (player.synced_to or player.group_members or player.active_group):
            return

        LOGGER.debug("Ungrouping player %s before starting playback from Plex", player.display_name)
        # Use set_members directly on the group to bypass static member check
        if (
            player.active_group
            and (group := self.provider.mass.players.get(player.active_group))
            and group.supports_feature(PlayerFeature.SET_MEMBERS)
        ):
            await group.set_members(player_ids_to_remove=[player_id])
        elif (
            player.synced_to
            and (sync_leader := self.provider.mass.players.get(player.synced_to))
            and sync_leader.supports_feature(PlayerFeature.SET_MEMBERS)
        ):
            await sync_leader.set_members(player_ids_to_remove=[player_id])
        elif player.group_members and player.supports_feature(PlayerFeature.SET_MEMBERS):
            await player.set_members(player_ids_to_remove=player.group_members)

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
        # Set flag to prevent circular updates
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

            # Use the assigned player for this server instance
            player_id = self._ma_player_id
            if not player_id:
                return web.Response(status=500, text="No player assigned to this server")

            # Ungroup player if it's part of a group/sync
            # User selected this specific player, so remove from any groups
            await self._ungroup_player_if_needed(player_id)

            if container_key and "/playQueues/" in container_key:
                # Extract play queue ID from container key
                queue_id_match = re.search(r"/playQueues/(\d+)", container_key)
                if queue_id_match:
                    self.play_queue_id = queue_id_match.group(1)
                    self.play_queue_version = 1
                    LOGGER.info(f"Playing from queue: {container_key} starting at {key}")

                    await self._play_from_plex_queue(player_id, container_key, key, shuffle, offset)
                else:
                    # Reset queue tracking if no valid queue ID found
                    self.play_queue_id = None
                    self.play_queue_item_ids = {}
                    # Fall back to single track
                    media = await self._resolve_plex_item(key)
                    await self.provider.mass.player_queues.play_media(
                        queue_id=player_id,
                        media=media,  # type: ignore[arg-type]
                        option=QueueOption.REPLACE,
                    )
            elif container_key:
                # Playing from a regular container (album, playlist, artist) not a play queue
                # Reset queue tracking
                self.play_queue_id = None
                self.play_queue_item_ids = {}

                # The key is the specific track, containerKey is the collection
                media_to_play = await self._resolve_plex_item(container_key)

                # Queue the entire container
                await self.provider.mass.player_queues.play_media(
                    queue_id=player_id,
                    media=media_to_play,  # type: ignore[arg-type]
                    option=QueueOption.REPLACE,
                )

            else:
                # Playing a single item, reset queue tracking
                self.play_queue_id = None
                self.play_queue_item_ids = {}

                media = await self._resolve_plex_item(key)

                # Replace the queue with this media
                await self.provider.mass.player_queues.play_media(
                    queue_id=player_id,
                    media=media,  # type: ignore[arg-type]
                    option=QueueOption.REPLACE,
                )

            # Set shuffle if requested
            if shuffle:
                await self.provider.mass.player_queues.set_shuffle(player_id, shuffle)

            # Seek to offset if specified
            if offset > 0:
                await self._seek_to_offset_after_playback(player_id, offset)

            await self._broadcast_timeline()
            return web.Response(status=200)

        except Exception as e:
            LOGGER.exception(f"Error handling playMedia: {e}")
            return web.Response(status=500, text=str(e))
        finally:
            # Clear flag after processing
            self._updating_from_plex = False

    def _reorder_tracks_for_playback(
        self, tracks: list[Any], start_index: int
    ) -> tuple[list[Any], dict[int, int]]:
        """Reorder tracks to start from a specific index and update item ID mappings.

        :param tracks: List of tracks to reorder.
        :param start_index: Index of the track to start from.
        :return: Tuple of (reordered tracks, updated item ID mappings).
        """
        if start_index <= 0 or start_index >= len(tracks):
            # No reordering needed
            return tracks, self.play_queue_item_ids

        # Reorder: [selected track, tracks after it, tracks before it]
        reordered_tracks = (
            tracks[start_index:]  # From selected to end
            + tracks[:start_index]  # From start to selected
        )

        # Update play queue item ID mappings to reflect new order
        new_item_ids = {}
        for new_idx, old_idx in enumerate(
            list(range(start_index, len(tracks))) + list(range(start_index))
        ):
            if old_idx in self.play_queue_item_ids:
                new_item_ids[new_idx] = self.play_queue_item_ids[old_idx]

        LOGGER.info(f"Started playback from offset {start_index} (reordered queue)")
        return reordered_tracks, new_item_ids

    async def _seek_to_offset_after_playback(self, player_id: str, offset: int) -> None:
        """Seek to the specified offset after playback starts.

        :param player_id: The player ID to seek on.
        :param offset: The offset in milliseconds.
        """
        # Wait for the queue to have items loaded before seeking
        for _ in range(10):  # Try up to 10 times (5 seconds total)
            await asyncio.sleep(0.5)
            queue = self.provider.mass.player_queues.get(player_id)
            if queue and queue.current_item:
                try:
                    await self.provider.mass.players.cmd_seek(player_id, offset // 1000)
                    # Wait briefly for player state to update
                    await asyncio.sleep(0.1)
                    break
                except Exception as e:
                    LOGGER.debug(f"Could not seek to offset {offset}ms: {e}")
                    break
        else:
            LOGGER.warning("Queue not ready for seeking after timeout")

    async def _play_from_plex_queue(  # noqa: PLR0915
        self,
        player_id: str,
        container_key: str,
        starting_key: str | None,
        shuffle: bool,
        offset: int,
    ) -> None:
        """Fetch play queue from Plex and load tracks."""
        try:
            LOGGER.info(f"Fetching play queue: {container_key}")

            # Extract queue ID from container_key (e.g., "/playQueues/123" -> "123")
            queue_id_match = re.search(r"/playQueues/(\d+)", container_key)
            if not queue_id_match:
                raise ValueError(f"Invalid container_key format: {container_key}")

            queue_id = queue_id_match.group(1)

            # Use plexapi to fetch the play queue
            def fetch_queue() -> PlayQueue:
                return PlayQueue.get(self.provider._plex_server, playQueueID=queue_id)

            playqueue = await asyncio.to_thread(fetch_queue)

            if playqueue and playqueue.items:
                # Get selected item offset from PlayQueue - this tells us which track to start from
                selected_offset = getattr(playqueue, "playQueueSelectedItemOffset", 0)
                LOGGER.info(f"PlayQueue selected item offset: {selected_offset}")

                # Track play queue item IDs
                self.play_queue_item_ids = {}
                tracks_to_queue: list[object] = []
                start_index = None

                for plex_idx, item in enumerate(playqueue.items):
                    track_key = item.key if hasattr(item, "key") else None
                    play_queue_item_id = (
                        item.playQueueItemID if hasattr(item, "playQueueItemID") else None
                    )

                    if track_key:
                        try:
                            # Fetch track from MA
                            track = await self.provider.get_track(track_key)
                            ma_idx = len(tracks_to_queue)
                            tracks_to_queue.append(track)

                            # Store play queue item ID mapping
                            if play_queue_item_id:
                                self.play_queue_item_ids[ma_idx] = play_queue_item_id

                            # Check if this is the track at the selected offset
                            if plex_idx == selected_offset:
                                start_index = ma_idx
                                LOGGER.info(
                                    f"Start track at offset {selected_offset}: {track.name}"
                                )
                        except Exception as e:
                            LOGGER.debug(f"Could not fetch track {track_key}: {e}")
                            continue

                if tracks_to_queue:
                    LOGGER.info(
                        f"Loaded queue with {len(tracks_to_queue)} tracks, "
                        f"starting at offset {selected_offset} (MA index {start_index})"
                    )

                    # Reorder tracks if not starting from the first track
                    if start_index is not None and start_index > 0:
                        tracks_to_queue, self.play_queue_item_ids = (
                            self._reorder_tracks_for_playback(tracks_to_queue, start_index)
                        )

                    # Queue all tracks
                    await self.provider.mass.player_queues.play_media(
                        queue_id=player_id,
                        media=tracks_to_queue,  # type: ignore[arg-type]
                        option=QueueOption.REPLACE,
                    )

                    # Update tracked state to prevent sync loop
                    # Store the keys in the order they're in MA queue (after reordering)
                    synced_keys = []
                    for track in tracks_to_queue:  # type: ignore[assignment]
                        for mapping in track.provider_mappings:
                            if mapping.provider_instance == self.provider.instance_id:
                                synced_keys.append(mapping.item_id)
                                break
                    self._last_synced_ma_queue_length = len(synced_keys)
                    self._last_synced_ma_queue_keys = synced_keys

                    # Apply shuffle if requested
                    if shuffle:
                        await self.provider.mass.player_queues.set_shuffle(player_id, shuffle)

                    # Seek to offset if specified
                    if offset > 0:
                        await self._seek_to_offset_after_playback(player_id, offset)
                else:
                    LOGGER.error("No valid tracks in play queue")
                    # Fall back to single track
                    if starting_key:
                        track = await self.provider.get_track(starting_key)
                        await self.provider.mass.player_queues.play_media(
                            queue_id=player_id,
                            media=track,
                            option=QueueOption.REPLACE,
                        )
            else:
                LOGGER.error("Play queue is empty or could not be fetched")
                # Fall back to single track
                if starting_key:
                    track = await self.provider.get_track(starting_key)
                    await self.provider.mass.player_queues.play_media(
                        queue_id=player_id,
                        media=track,
                        option=QueueOption.REPLACE,
                    )

        except Exception as e:
            LOGGER.exception(f"Error playing from queue: {e}")
            # Fall back to single track
            if starting_key:
                track = await self.provider.get_track(starting_key)
                await self.provider.mass.player_queues.play_media(
                    queue_id=player_id,
                    media=track,
                    option=QueueOption.REPLACE,
                )

    async def _replace_entire_queue(self, player_id: str, playqueue: PlayQueue) -> None:
        """Replace the entire queue when nothing is currently playing.

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
        # Fetch tracks that come AFTER the current track in the Plex queue
        remaining_tracks = []
        new_item_mappings = {}

        # Start from the track after current_index
        for i in range(current_index + 1, len(playqueue.items)):
            item = playqueue.items[i]
            track_key = item.key if hasattr(item, "key") else None
            play_queue_item_id = item.playQueueItemID if hasattr(item, "playQueueItemID") else None

            if track_key:
                try:
                    track = await self.provider.get_track(track_key)
                    remaining_tracks.append(track)

                    # Map relative to the current position
                    if play_queue_item_id:
                        new_item_mappings[current_index + 1 + len(remaining_tracks) - 1] = (
                            play_queue_item_id
                        )
                except Exception as e:
                    LOGGER.debug(f"Could not fetch track {track_key}: {e}")
                    continue

        # Replace items after current track
        if remaining_tracks:
            await self.provider.mass.player_queues.play_media(
                queue_id=player_id,
                media=remaining_tracks,  # type: ignore[arg-type]
                option=QueueOption.REPLACE_NEXT,  # Replace everything after current
            )
            # Update mappings for the new items
            self.play_queue_item_ids.update(new_item_mappings)

            LOGGER.info(
                f"Replaced {len(remaining_tracks)} tracks after current track "
                f"(index {current_index})"
            )
        else:
            # No tracks after current - clear remaining queue
            LOGGER.debug("No tracks after current track in Plex queue")

        # Rebuild complete item ID mappings from Plex queue
        # Keep mappings for tracks from index 0 to current_index unchanged
        for i, item in enumerate(playqueue.items):
            play_queue_item_id = item.playQueueItemID if hasattr(item, "playQueueItemID") else None
            if play_queue_item_id:
                self.play_queue_item_ids[i] = play_queue_item_id

    async def handle_refresh_play_queue(self, request: web.Request) -> web.Response:
        """
        Handle refreshPlayQueue command from Plex controller.

        This is called when the play queue is modified (items added, removed, reordered).
        We need to sync the entire updated queue state to MA while preserving playback.
        """
        try:
            play_queue_id = request.query.get("playQueueID")

            if not play_queue_id:
                return web.Response(status=400, text="Missing 'playQueueID' parameter")

            # Log all query parameters to understand what Plex sends
            LOGGER.info(
                f"Received refreshPlayQueue command - playQueueID: {play_queue_id}, "
                f"params: {dict(request.query)}"
            )

            # Verify this is our active play queue
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

            # Update the play queue version (increments on each refresh)
            self.play_queue_version += 1

            # Use plexapi to fetch the updated play queue
            def fetch_queue() -> PlayQueue:
                return PlayQueue.get(self.provider._plex_server, playQueueID=play_queue_id)

            playqueue = await asyncio.to_thread(fetch_queue)

            if not playqueue or not playqueue.items:
                LOGGER.error("Failed to refresh play queue - queue is empty or not found")
                return web.Response(status=404, text="Play queue not found")

            # Get current MA queue state
            player_id = self._ma_player_id
            if not player_id:
                LOGGER.error("No player assigned to this server")
                return web.Response(status=500, text="No player assigned")

            # disable shuffle to avoid infinite loop
            await self.provider.mass.player_queues.set_shuffle(player_id, False)
            ma_queue = self.provider.mass.player_queues.get(player_id)
            if not ma_queue:
                LOGGER.error(f"MA queue not found for player {player_id}")
                return web.Response(status=500, text="MA queue not found")

            # Get current playback state
            current_index = ma_queue.current_index

            # Get MA queue item count
            ma_queue_items = self.provider.mass.player_queues.items(player_id)
            ma_queue_count = len(ma_queue_items) if ma_queue_items else 0

            LOGGER.debug(
                f"Queue refresh: Current index={current_index}, "
                f"MA has {ma_queue_count} items, Plex has {len(playqueue.items)} items"
            )

            # If nothing is playing, replace the entire queue
            if current_index is None:
                LOGGER.debug("No track currently playing, replacing entire queue")
                await self._replace_entire_queue(player_id, playqueue)
            else:
                # Something is playing - update only the remaining queue items
                LOGGER.debug(
                    f"Track at index {current_index} is playing, "
                    f"replacing only items after current track"
                )
                await self._replace_remaining_queue(player_id, playqueue, current_index)

            LOGGER.info(
                f"Refreshed play queue {play_queue_id} - now has {len(playqueue.items)} items"
            )

            # Update tracked state to prevent sync loop
            # Get what's actually in MA queue after the refresh
            queue_items_after = self.provider.mass.player_queues.items(player_id)
            synced_keys = []
            for item in queue_items_after:
                if item.media_item:
                    for mapping in item.media_item.provider_mappings:
                        if mapping.provider_instance == self.provider.instance_id:
                            synced_keys.append(mapping.item_id)
                            break
            self._last_synced_ma_queue_length = len(synced_keys)
            self._last_synced_ma_queue_keys = synced_keys

            return web.Response(status=200)

        except Exception as e:
            LOGGER.exception(f"Error handling refreshPlayQueue: {e}")
            return web.Response(status=500, text=str(e))

    async def handle_create_play_queue(self, request: web.Request) -> web.Response:
        """
        Handle createPlayQueue command from Plex controller.

        Creates a new play queue from a URI (album, playlist, artist tracks, etc.)
        and optionally applies shuffle.
        """
        try:
            uri = request.query.get("uri")
            shuffle = request.query.get("shuffle", "0") == "1"
            continuous = request.query.get("continuous", "0") == "1"

            if not uri:
                return web.Response(status=400, text="Missing 'uri' parameter")

            LOGGER.info(f"Received createPlayQueue command - uri: {uri}, shuffle: {shuffle}")

            # Use the assigned player for this server instance
            player_id = self._ma_player_id
            if not player_id:
                return web.Response(status=500, text="No player assigned to this server")

            # Use plexapi to create play queue
            def create_queue() -> PlayQueue:
                # Fetch the item from URI first
                item = self.provider._plex_server.fetchItem(uri)
                # Create play queue from the item
                return PlayQueue.create(
                    self.provider._plex_server,
                    item,
                    shuffle=1 if shuffle else 0,
                    continuous=1 if continuous else 0,
                )

            playqueue = await asyncio.to_thread(create_queue)

            if playqueue and playqueue.items:
                # Extract play queue ID from response
                self.play_queue_id = str(playqueue.playQueueID)
                self.play_queue_version = 1

                LOGGER.info(
                    f"Created play queue {self.play_queue_id} with {len(playqueue.items)} items"
                )

                # Load tracks from the created queue
                self.play_queue_item_ids = {}
                tracks_to_queue = []

                for i, item in enumerate(playqueue.items):
                    track_key = item.key if hasattr(item, "key") else None
                    play_queue_item_id = (
                        item.playQueueItemID if hasattr(item, "playQueueItemID") else None
                    )

                    if track_key:
                        try:
                            # Fetch track from MA
                            track = await self.provider.get_track(track_key)
                            tracks_to_queue.append(track)

                            # Store play queue item ID mapping
                            if play_queue_item_id:
                                self.play_queue_item_ids[len(tracks_to_queue) - 1] = (
                                    play_queue_item_id
                                )
                        except Exception as e:
                            LOGGER.debug(f"Could not fetch track {track_key}: {e}")
                            continue

                if tracks_to_queue:
                    # Queue all tracks
                    await self.provider.mass.player_queues.play_media(
                        queue_id=player_id,
                        media=tracks_to_queue,  # type: ignore[arg-type]
                        option=QueueOption.REPLACE,
                    )

                    # Apply shuffle if requested (Plex may have already shuffled server-side)
                    if shuffle:
                        await self.provider.mass.player_queues.set_shuffle(player_id, shuffle)
                else:
                    LOGGER.error("No valid tracks in created play queue")
                    return web.Response(status=500, text="Failed to load tracks from play queue")
            else:
                LOGGER.error("Failed to create play queue or queue is empty")
                return web.Response(status=500, text="Failed to create play queue")

            # Broadcast timeline update
            await self._broadcast_timeline()
            return web.Response(status=200)

        except Exception as e:
            LOGGER.exception(f"Error handling createPlayQueue: {e}")
            return web.Response(status=500, text=str(e))

    async def _resolve_plex_item(self, key: str) -> object:
        """Resolve a Plex key to a Music Assistant media item."""
        # Determine item type from the key format
        if "/library/metadata/" in key:
            # Could be track, album, or artist
            # Try to fetch as track first
            try:
                return await self.provider.get_track(key)
            except Exception as exc:
                LOGGER.debug(f"Failed to resolve Plex item as track for key '{key}': {exc}")

            # Try as album
            try:
                return await self.provider.get_album(key)
            except Exception as exc:
                LOGGER.debug(f"Failed to resolve Plex item as album for key '{key}': {exc}")

            # Try as artist
            try:
                return await self.provider.get_artist(key)
            except Exception:
                raise ValueError(f"Could not resolve Plex item: {key}") from None

        elif "/playlists/" in key:
            return await self.provider.get_playlist(key)
        else:
            raise ValueError(f"Unknown Plex key format: {key}")

    async def handle_pause(self, request: web.Request) -> web.Response:
        """Handle pause command (test-client.py line 98-101)."""
        self._updating_from_plex = True
        try:
            if self._ma_player_id:
                await self.provider.mass.players.cmd_pause(self._ma_player_id)
            await self._broadcast_timeline()
            return web.Response(status=200)
        finally:
            self._updating_from_plex = False

    async def handle_play(self, request: web.Request) -> web.Response:
        """Handle play/resume command (test-client.py line 103-106)."""
        self._updating_from_plex = True
        try:
            if self._ma_player_id:
                # Ungroup player before resuming playback
                await self._ungroup_player_if_needed(self._ma_player_id)
                await self.provider.mass.players.cmd_play(self._ma_player_id)
            await self._broadcast_timeline()
            return web.Response(status=200)
        finally:
            self._updating_from_plex = False

    async def handle_stop(self, request: web.Request) -> web.Response:
        """Handle stop command - stops playback and clears the queue."""
        self._updating_from_plex = True
        try:
            if self._ma_player_id:
                # Clear the queue (which also stops playback)
                self.provider.mass.player_queues.clear(self._ma_player_id)

                # Reset play queue tracking since the queue is now cleared
                self.play_queue_id = None
                self.play_queue_item_ids = {}

            await self._broadcast_timeline()
            return web.Response(status=200)
        finally:
            self._updating_from_plex = False

    async def handle_skip_next(self, request: web.Request) -> web.Response:
        """Handle skip next command."""
        self._updating_from_plex = True
        try:
            if self._ma_player_id:
                await self.provider.mass.player_queues.next(self._ma_player_id)
            await self._broadcast_timeline()
            return web.Response(status=200)
        finally:
            self._updating_from_plex = False

    async def handle_skip_previous(self, request: web.Request) -> web.Response:
        """Handle skip previous command."""
        self._updating_from_plex = True
        try:
            if self._ma_player_id:
                await self.provider.mass.player_queues.previous(self._ma_player_id)
            await self._broadcast_timeline()
            return web.Response(status=200)
        finally:
            self._updating_from_plex = False

    async def handle_step_forward(self, request: web.Request) -> web.Response:
        """Handle step forward command (small skip forward)."""
        self._updating_from_plex = True
        try:
            if self._ma_player_id:
                queue = self.provider.mass.player_queues.get(self._ma_player_id)
                if queue:
                    # Step forward 30 seconds
                    new_position = queue.corrected_elapsed_time + 30
                    if queue.current_item and queue.current_item.media_item:
                        # Don't seek past the track duration
                        max_duration = queue.current_item.media_item.duration or new_position
                        new_position = min(new_position, max_duration)
                    await self.provider.mass.players.cmd_seek(self._ma_player_id, int(new_position))
                    # Wait briefly for player state to update
                    await asyncio.sleep(0.1)
            await self._broadcast_timeline()
            return web.Response(status=200)
        finally:
            self._updating_from_plex = False

    async def handle_step_back(self, request: web.Request) -> web.Response:
        """Handle step back command (small skip backward)."""
        self._updating_from_plex = True
        try:
            if self._ma_player_id:
                queue = self.provider.mass.player_queues.get(self._ma_player_id)
                if queue:
                    # Step back 10 seconds
                    new_position = max(0, queue.corrected_elapsed_time - 10)
                    await self.provider.mass.players.cmd_seek(self._ma_player_id, int(new_position))
                    # Wait briefly for player state to update
                    await asyncio.sleep(0.1)
            await self._broadcast_timeline()
            return web.Response(status=200)
        finally:
            self._updating_from_plex = False

    async def handle_skip_to(self, request: web.Request) -> web.Response:
        """Handle skip to specific queue item."""
        key = request.query.get("key")
        if not self._ma_player_id or not key:
            return web.Response(status=400, text="Missing player ID or key")

        self._updating_from_plex = True
        try:
            ma_index = None

            # Check if key is a play queue item ID (numeric) or a library path
            if key.isdigit():
                # Key is a play queue item ID
                play_queue_item_id = int(key)

                # Find the MA queue index for this play queue item ID
                for idx, pq_item_id in self.play_queue_item_ids.items():
                    if pq_item_id == play_queue_item_id:
                        ma_index = idx
                        break

                if ma_index is None:
                    LOGGER.warning(
                        f"Could not find MA queue index for play queue item ID: "
                        f"{play_queue_item_id}"
                    )
                    return web.Response(status=404, text="Queue item not found")

                LOGGER.info(
                    f"Skipping to queue index {ma_index} (play queue item ID: {play_queue_item_id})"
                )
            else:
                # Key is a library path (e.g., "/library/metadata/856761")
                # Find the track in the MA queue by matching the Plex key
                queue_items = self.provider.mass.player_queues.items(self._ma_player_id)
                if not queue_items:
                    return web.Response(status=404, text="Queue is empty")

                for idx, item in enumerate(queue_items):
                    if not item.media_item:
                        continue

                    # Find Plex mapping for this track
                    for mapping in item.media_item.provider_mappings:
                        if (
                            mapping.provider_instance == self.provider.instance_id
                            and mapping.item_id == key
                        ):
                            ma_index = idx
                            break

                    if ma_index is not None:
                        break

                if ma_index is None:
                    LOGGER.warning(f"Could not find track with key {key} in MA queue")
                    return web.Response(status=404, text="Track not found in queue")

                LOGGER.info(f"Skipping to queue index {ma_index} (track key: {key})")

            # Skip to this index in the MA queue
            await self.provider.mass.player_queues.play_index(self._ma_player_id, ma_index)

            await self._broadcast_timeline()
            return web.Response(status=200)

        except Exception as e:
            LOGGER.exception(f"Error handling skipTo: {e}")
            return web.Response(status=500, text=str(e))
        finally:
            self._updating_from_plex = False

    async def handle_seek_to(self, request: web.Request) -> web.Response:
        """Handle seek command."""
        self._updating_from_plex = True
        try:
            offset_ms = int(request.query.get("offset", 0))
            if self._ma_player_id:
                await self.provider.mass.players.cmd_seek(self._ma_player_id, int(offset_ms / 1000))
                # Wait briefly for player state to update
                await asyncio.sleep(0.1)
            await self._broadcast_timeline()
            return web.Response(status=200)
        finally:
            self._updating_from_plex = False

    async def handle_set_parameters(self, request: web.Request) -> web.Response:
        """Handle parameter changes (volume, shuffle, repeat)."""
        if not self._ma_player_id:
            return web.Response(status=200)

        self._updating_from_plex = True
        try:
            if "volume" in request.query:
                volume = int(request.query["volume"])
                await self.provider.mass.players.cmd_volume_set(self._ma_player_id, volume)

            if "shuffle" in request.query:
                # Plex sends shuffle as "0" or "1"
                shuffle = request.query["shuffle"] == "1"
                await self.provider.mass.player_queues.set_shuffle(self._ma_player_id, shuffle)

            if "repeat" in request.query:
                # Plex repeat: 0=off, 1=repeat one, 2=repeat all
                repeat_value = int(request.query["repeat"])

                # Map Plex repeat to MA repeat mode
                if repeat_value == 0:
                    # Repeat off
                    self.provider.mass.player_queues.set_repeat(self._ma_player_id, RepeatMode.OFF)
                elif repeat_value == 1:
                    # Repeat one track
                    self.provider.mass.player_queues.set_repeat(self._ma_player_id, RepeatMode.ONE)
                elif repeat_value == 2:
                    # Repeat all
                    self.provider.mass.player_queues.set_repeat(self._ma_player_id, RepeatMode.ALL)

            await self._broadcast_timeline()
            return web.Response(status=200)
        finally:
            self._updating_from_plex = False

    async def handle_options(self, request: web.Request) -> web.Response:
        """Handle OPTIONS requests for CORS (like test-client.py)."""
        return web.Response(
            status=200,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "*",
            },
        )

    async def handle_resources(self, request: web.Request) -> web.Response:
        """Return player information (matching test-client.py format exactly)."""
        # Get player name
        player_name = "Music Assistant"
        if self._ma_player_id:
            player = self.provider.mass.players.get(self._ma_player_id)
            if player:
                player_name = player.display_name

        # Get player state
        state = "stopped"
        if self._ma_player_id:
            player = self.provider.mass.players.get(self._ma_player_id)
            if player and player.state:
                state_value = (
                    player.state.value if hasattr(player.state, "value") else str(player.state)
                )
                if state_value in ["playing", "paused"]:
                    state = state_value

        local_ip = self.provider.mass.streams.publish_ip
        version = self.provider.mass.version if self.provider.mass.version != "0.0.0" else "1.0.0"

        # Match test-client.py format exactly
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<MediaContainer>
    <Player title="{player_name}"
            protocol="plex"
            protocolVersion="1"
            protocolCapabilities="timeline,playback,navigation,playqueues"
            machineIdentifier="{self.client_id}"
            product="Music Assistant"
            platform="{platform.system()}"
            platformVersion="{platform.release()}"
            deviceClass="{self.device_class}"
            state="{state}"
            address="{local_ip}"
            port="{self.port}"
            version="{version}"
            provides="client,player,pubsub-player">
        <Connection protocol="http" address="{local_ip}" port="{self.port}"
                    uri="http://{local_ip}:{self.port}" local="1"/>
    </Player>
</MediaContainer>"""
        return web.Response(
            text=xml, content_type="text/xml", headers={"Access-Control-Allow-Origin": "*"}
        )

    def _build_timeline_attributes(
        self,
        track: Any,
        state: str,
        duration: int,
        time: int,
        volume: int,
        shuffle: int,
        repeat: int,
        controllable: str,
        queue: Any | None,
    ) -> list[str]:
        """Build timeline attributes for a playing track.

        :param track: The current track media item.
        :param state: Playback state (playing, paused, etc.).
        :param duration: Track duration in milliseconds.
        :param time: Current playback time in milliseconds.
        :param volume: Volume level (0-100).
        :param shuffle: Shuffle state (0 or 1).
        :param repeat: Repeat mode (0=off, 1=one, 2=all).
        :param controllable: Controllable features string.
        :param queue: The MA queue object.
        :return: List of timeline attribute strings.
        """
        # Get Plex key and ratingKey
        key = None
        rating_key = None
        for mapping in track.provider_mappings:
            if mapping.provider_instance == self.provider.instance_id:
                key = mapping.item_id
                rating_key = key.split("/")[-1]
                break

        if not key:
            return []

        # Server identification
        plex_url = urlparse(self.provider._baseurl)
        machine_identifier = self.provider._plex_server.machineIdentifier
        address = plex_url.hostname
        port = plex_url.port or (443 if plex_url.scheme == "https" else 32400)
        protocol = plex_url.scheme

        # Build timeline attributes
        attrs = [
            f'state="{state}"',
            f'duration="{duration}"',
            f'time="{time}"',
            f'ratingKey="{rating_key}"',
            f'key="{key}"',
        ]

        # Add play queue info if available
        if self.play_queue_id and queue:
            if queue.current_index is not None:
                play_queue_item_id = self.play_queue_item_ids.get(
                    queue.current_index, queue.current_index + 1
                )
                attrs.append(f'playQueueItemID="{play_queue_item_id}"')
            attrs.append(f'playQueueID="{self.play_queue_id}"')
            attrs.append(f'playQueueVersion="{self.play_queue_version}"')
            attrs.append(f'containerKey="/playQueues/{self.play_queue_id}"')

        # Add standard attributes
        attrs.extend(
            [
                'type="music"',
                f'volume="{volume}"',
                f'shuffle="{shuffle}"',
                f'repeat="{repeat}"',
                f'controllable="{controllable}"',
                f'machineIdentifier="{machine_identifier}"',
                f'address="{address}"',
                f'port="{port}"',
                f'protocol="{protocol}"',
            ]
        )

        return attrs

    async def _build_timeline_xml(
        self, include_metadata: bool = False, command_id: str = "0"
    ) -> str:
        """Build timeline XML from current Music Assistant player state."""
        player_id = self._ma_player_id

        # Get MA player and queue
        player = self.provider.mass.players.get(player_id) if player_id else None
        queue = self.provider.mass.player_queues.get(player_id) if player_id else None

        # Controllable features for music
        controllable = (
            "volume,repeat,skipPrevious,seekTo,stepBack,stepForward,stop,playPause,shuffle,skipNext"
        )

        # Map MA playback state to Plex state (stopped, paused, playing, buffering, error)
        state = "stopped"
        if player and player.playback_state:
            state_value = (
                player.playback_state.value
                if hasattr(player.playback_state, "value")
                else str(player.playback_state)
            )

            # Map MA states to Plex states
            if state_value == "playing":
                state = "playing"
            elif state_value == "paused":
                state = "paused"
            elif state_value == "buffering":
                state = "buffering"
            elif state_value == "idle":
                # Idle with a current track = paused, idle without track = stopped
                state = (
                    "paused"
                    if queue and queue.current_item and queue.current_item.media_item
                    else "stopped"
                )
            else:
                state = "stopped"

        # Get volume (0-100) - use group_volume for groups, volume_level for others
        volume = 0
        if player:
            volume = (
                int(player.group_volume)
                if (player.type == PlayerType.GROUP or player.group_members)
                else (int(player.volume_level) if player.volume_level else 0)
            )

        # Get shuffle (0/1) and repeat (0=off, 1=one, 2=all)
        shuffle = 0
        repeat = 0
        if queue:
            shuffle = 1 if queue.shuffle_enabled else 0
            if hasattr(queue, "repeat_mode"):
                repeat_mode = queue.repeat_mode
                if hasattr(repeat_mode, "value"):
                    repeat_value = repeat_mode.value
                    if repeat_value == "one":
                        repeat = 1
                    elif repeat_value == "all":
                        repeat = 2

        # Build music timeline
        if (
            state in ["playing", "paused"]
            and queue
            and queue.current_item
            and queue.current_item.media_item
        ):
            track = queue.current_item.media_item

            # Duration in milliseconds
            duration = round(track.duration * 1000) if track.duration else 0

            # Current playback time in milliseconds
            time = round(queue.corrected_elapsed_time * 1000)

            # Build timeline attributes
            attrs = self._build_timeline_attributes(
                track, state, duration, time, volume, shuffle, repeat, controllable, queue
            )

            if attrs:
                music_timeline = f"<Timeline {' '.join(attrs)}/>"
            else:
                # No Plex mapping, send basic timeline with actual state
                music_timeline = (
                    f'<Timeline state="{state}" time="{time}" type="music" volume="{volume}" '
                    f'shuffle="{shuffle}" repeat="{repeat}" controllable="{controllable}"/>'
                )
        else:
            # No current track - send stopped state with time=0
            time = 0
            music_timeline = (
                f'<Timeline state="{state}" time="{time}" type="music" volume="{volume}" '
                f'shuffle="{shuffle}" repeat="{repeat}" controllable="{controllable}"/>'
            )

        # Video and photo timelines (always stopped for music player)
        video_timeline = '<Timeline type="video" state="stopped"/>'
        photo_timeline = '<Timeline type="photo" state="stopped"/>'

        # Combine all timelines
        return (
            f'<MediaContainer commandID="{command_id}">'
            f"{music_timeline}{video_timeline}{photo_timeline}"
            f"</MediaContainer>"
        )

    async def _handle_player_event(self, event: MassEvent) -> None:
        """Handle player state change events."""
        if not self._ma_player_id or event.object_id != self._ma_player_id:
            return

        # Skip if we're the ones making the changes
        if self._updating_from_plex:
            return

        try:
            # Send timeline to Plex server (for activity tracking)
            await self._send_timeline_to_server()

            # Broadcast timeline to subscribed controllers
            # Timeline will be built from current MA player state
            await self._broadcast_timeline()
        except Exception as e:
            LOGGER.debug(f"Error handling player event: {e}")

    async def _handle_queue_event(self, event: MassEvent) -> None:
        """Handle queue change events."""
        if not self._ma_player_id or event.object_id != self._ma_player_id:
            return

        # Skip if we're the ones making the changes
        if self._updating_from_plex:
            return

        try:
            # Send timeline to Plex server (for activity tracking)
            await self._send_timeline_to_server()

            # Broadcast timeline to subscribed controllers
            # Timeline will be built from current MA player state
            await self._broadcast_timeline()
        except Exception as e:
            LOGGER.debug(f"Error handling queue event: {e}")

    async def _handle_queue_items_updated(self, event: MassEvent) -> None:
        """Handle queue items being added/removed/reordered."""
        if not self._ma_player_id or event.object_id != self._ma_player_id:
            return

        # Skip if we're the ones making the changes
        if self._updating_from_plex:
            return

        # Get current MA queue state
        queue_items = self.provider.mass.player_queues.items(self._ma_player_id)
        if not queue_items:
            return

        current_keys = []
        for item in queue_items:
            if not item.media_item:
                continue
            # Find Plex mapping
            for mapping in item.media_item.provider_mappings:
                if mapping.provider_instance == self.provider.instance_id:
                    current_keys.append(mapping.item_id)
                    break

        # Check if queue actually changed from what we last synced FROM Plex
        if (
            len(current_keys) == self._last_synced_ma_queue_length
            and current_keys == self._last_synced_ma_queue_keys
        ):
            # Queue hasn't changed from last sync, skip
            LOGGER.debug("MA queue matches last synced state, skipping Plex sync")
            return

        LOGGER.info(
            f"MA queue changed: {self._last_synced_ma_queue_length} -> {len(current_keys)} items"
        )

        # (Re)create Plex PlayQueue from MA queue
        try:
            await self._create_plex_playqueue_from_ma()
            # Update tracked state
            self._last_synced_ma_queue_length = len(current_keys)
            self._last_synced_ma_queue_keys = current_keys
        except Exception as e:
            LOGGER.debug(f"Error creating Plex PlayQueue: {e}")

        # Broadcast timeline update
        try:
            await self._broadcast_timeline()
        except Exception as e:
            LOGGER.debug(f"Error broadcasting timeline: {e}")

    async def _create_plex_playqueue_from_ma(self) -> None:
        """Create a new Plex PlayQueue from current MA queue."""
        ma_queue = self.provider.mass.player_queues.get(self._ma_player_id)  # type: ignore[arg-type]
        queue_items = self.provider.mass.player_queues.items(self._ma_player_id)  # type: ignore[arg-type]

        if not ma_queue or not queue_items:
            return

        # Fetch Plex items for all tracks in MA queue
        async def fetch_plex_item(plex_key: str) -> object | None:
            """Fetch a single Plex item."""
            try:

                def fetch_item() -> object:
                    return self.plex_server.fetchItem(plex_key)

                return await asyncio.to_thread(fetch_item)
            except Exception as e:
                LOGGER.debug(f"Failed to fetch Plex item {plex_key}: {e}")
                return None

        # Collect all fetch tasks
        fetch_tasks = []
        for item in queue_items:
            if not item.media_item:
                continue

            # Find Plex mapping
            plex_key = None
            for mapping in item.media_item.provider_mappings:
                if mapping.provider_instance == self.provider.instance_id:
                    plex_key = mapping.item_id
                    break

            if plex_key:
                fetch_tasks.append(fetch_plex_item(plex_key))

        # Fetch all items concurrently
        plex_items = []
        if fetch_tasks:
            fetched_items = await asyncio.gather(*fetch_tasks, return_exceptions=True)
            plex_items = [item for item in fetched_items if item is not None]

        if not plex_items:
            LOGGER.debug("No Plex tracks in MA queue, skipping PlayQueue creation")
            return

        # Determine which track should be selected (currently playing)
        start_item = None
        if ma_queue.current_index is not None and ma_queue.current_index < len(plex_items):
            start_item = plex_items[ma_queue.current_index]

        # Create Plex PlayQueue - don't pass shuffle since MA queue is already in desired order
        def create_queue() -> PlayQueue:
            return PlayQueue.create(
                self.plex_server,
                items=plex_items,
                startItem=start_item,
                shuffle=0,  # Don't shuffle, plex_items is already in MA queue order
                continuous=1,
            )

        try:
            playqueue = await asyncio.to_thread(create_queue)

            if playqueue:
                self.play_queue_id = str(playqueue.playQueueID)
                self.play_queue_version = playqueue.playQueueVersion

                # Build item ID mappings
                self.play_queue_item_ids = {}
                for i, item in enumerate(playqueue.items):
                    if hasattr(item, "playQueueItemID"):
                        self.play_queue_item_ids[i] = item.playQueueItemID

                LOGGER.info(
                    f"Created Plex PlayQueue {self.play_queue_id} with {len(plex_items)} tracks"
                )
        except Exception as e:
            LOGGER.exception(f"Error creating Plex PlayQueue: {e}")

    async def _send_timeline(self, client_id: str) -> None:
        """Send timeline update to specific controller."""
        subscription = self.subscriptions.get(client_id)
        if not subscription:
            return

        timeline_xml = await self._build_timeline_xml()

        try:
            await self.provider.mass.http_session.post(
                f"{subscription['url']}/:/timeline",
                data=timeline_xml,
                headers={
                    "X-Plex-Client-Identifier": self.client_id,
                    "Content-Type": "text/xml",
                },
                timeout=ClientTimeout(total=5),
            )
            # Update last_update timestamp on successful send
            subscription["last_update"] = time.time()
        except Exception as e:
            LOGGER.debug(f"Failed to send timeline to {client_id}: {e}")

    async def _send_timeline_to_server(self) -> None:
        """Send timeline update to Plex server for activity tracking."""
        if not self._ma_player_id:
            return

        try:
            player = self.provider.mass.players.get(self._ma_player_id)
            queue = self.provider.mass.player_queues.get(self._ma_player_id)

            if (
                not player
                or not queue
                or not queue.current_item
                or not queue.current_item.media_item
            ):
                return

            track = queue.current_item.media_item

            # Find Plex mapping
            plex_key = None
            for mapping in track.provider_mappings:
                if mapping.provider_instance == self.provider.instance_id:
                    plex_key = mapping.item_id
                    break

            if not plex_key:
                return

            # Extract rating key from plex_key (e.g., "/library/metadata/12345" -> "12345")
            rating_key = plex_key.split("/")[-1]

            # Get playback state
            state_value = (
                player.playback_state.value
                if hasattr(player.playback_state, "value")
                else str(player.playback_state)
            )

            # Map to Plex state
            if state_value == "playing":
                plex_state = "playing"
            elif state_value == "paused":
                plex_state = "paused"
            else:
                plex_state = "stopped"

            # Get position and duration in milliseconds
            position_ms = round(queue.corrected_elapsed_time * 1000)
            duration_ms = round(track.duration * 1000) if track.duration else 0

            # Get play queue info if available
            container_key = ""
            play_queue_item_id = ""
            if self.play_queue_id:
                container_key = f"/playQueues/{self.play_queue_id}"
                if queue.current_index is not None:
                    play_queue_item_id = str(
                        self.play_queue_item_ids.get(queue.current_index, queue.current_index + 1)
                    )

            # Build timeline params (only Plex timeline data)
            params = {
                "ratingKey": rating_key,
                "key": plex_key,
                "state": plex_state,
                "time": str(position_ms),
                "duration": str(duration_ms),
            }

            # Add play queue info if available
            if container_key:
                params["containerKey"] = container_key
            if play_queue_item_id:
                params["playQueueItemID"] = play_queue_item_id

            def send_timeline() -> None:
                # Pass session headers to identify this specific player instance
                self.plex_server.query("/:/timeline", params=params, headers=self.headers)

            await asyncio.to_thread(send_timeline)

        except Exception as e:
            LOGGER.debug(f"Failed to send timeline to Plex server: {e}")

    async def _broadcast_timeline(self) -> None:
        """Send timeline to all subscribed controllers."""
        current_time = time.time()
        stale_clients = []
        for client_id, sub in self.subscriptions.items():
            try:
                last_update = float(sub["last_update"])  # type: ignore[arg-type]
                if current_time - last_update > 90:
                    stale_clients.append(client_id)
            except (ValueError, TypeError):
                # If conversion fails, treat client as stale
                LOGGER.debug(f"Invalid last_update for client {client_id}, treating as stale")
                stale_clients.append(client_id)

        for client_id in stale_clients:
            del self.subscriptions[client_id]

        await asyncio.gather(
            *(self._send_timeline(client_id) for client_id in list(self.subscriptions.keys())),
            return_exceptions=True,  # Don't fail all if one fails
        )

    # for debugging purposes only
    # async def handle_unknown(self, request: web.Request) -> web.Response:
    #     """Catch-all handler for unexpected or unsupported paths."""
    #     LOGGER.debug(
    #         "Unhandled request: %s %s from %s",
    #         request.method,
    #         request.path,
    #         request.remote,
    #     )
    #
    #     # You can log query/body if needed (be careful not to leak tokens)
    #     if request.query:
    #         LOGGER.debug("Query params for %s: %s", request.path, dict(request.query))
    #     try:
    #         data = await request.text()
    #         if data:
    #             LOGGER.debug("Body for %s: %s", request.path, data)
    #     except Exception as e:
    #         LOGGER.debug("Could not read request body: %s", e)
    #
    #     return web.Response(status=404, text=f"Unhandled path: {request.path}")
