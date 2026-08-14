"""Per-player Plex remote control instances."""

from __future__ import annotations

import logging
import platform
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from aiohttp import web
from music_assistant_models.enums import EventType

from .gdm import PlexGDMAdvertiser
from .playback import PlaybackMixin
from .plextv import compute_client_id
from .queue_commands import QueueCommandsMixin
from .queue_sync import QueueSyncMixin
from .timeline import TimelineMixin

if TYPE_CHECKING:
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
        """
        Initialize player remote instance.

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

        self.client_id = compute_client_id(plex_provider.instance_id, ma_player_id)

        if self.remote_control:
            self.server: PlexRemoteControlServer | None = None
            self.gdm: PlexGDMAdvertiser | None = None

    async def start(self) -> None:
        """Start this player's remote control."""
        if self.remote_control:
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

            self.gdm = PlexGDMAdvertiser(
                instance_id=self.client_id,
                port=self.port,
                publish_ip=str(self.plex_provider.mass.streams.publish_ip),
                name=self.player_name,
                product="Music Assistant",
                version=self.plex_provider.mass.version
                if self.plex_provider.mass.version != "0.0.0"
                else "1.0.0",
                device_class=self.device_class,
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


class PlexRemoteControlServer(QueueCommandsMixin, PlaybackMixin, QueueSyncMixin, TimelineMixin):
    """HTTP server implementing the Plex remote control protocol for one MA player."""

    def __init__(
        self,
        plex_provider: PlexProvider,
        port: int = 32500,
        client_id: str | None = None,
        ma_player_id: str | None = None,
        device_class: str = "speaker",
    ) -> None:
        """
        Initialize remote control server.

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
        self.play_queue_item_ids: dict[int, int] = {}

        # Track MA queue state to detect when we need to sync to Plex
        self._last_synced_ma_queue_length: int = 0
        self._last_synced_ma_queue_keys: list[str] = []

        self._ma_player_id = ma_player_id

        self._unsub_callbacks: list[Callable[..., None]] = []

        # Flag to prevent circular updates when we modify the queue ourselves
        self._updating_from_plex = False

        self.player = self.provider.mass.players.get_player(self._ma_player_id)  # type: ignore[arg-type]

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
        """Set up all HTTP endpoints."""
        self.app.router.add_get("/", self.handle_root)

        self.app.router.add_get("/player/timeline/subscribe", self.handle_subscribe)
        self.app.router.add_get("/player/timeline/unsubscribe", self.handle_unsubscribe)
        self.app.router.add_get("/player/timeline/poll", self.handle_poll)

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

        self.app.router.add_get("/resources", self.handle_resources)

        self.app.router.add_route("OPTIONS", "/{tail:.*}", self.handle_options)

    async def start(self) -> None:
        """Start HTTP server and subscribe to MA events."""
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()

        self.http_site = web.TCPSite(self.runner, "0.0.0.0", self.port)
        await self.http_site.start()
        LOGGER.info(f"Plex remote control server started on HTTP port {self.port}")

        if self._ma_player_id:
            self._unsub_callbacks.append(
                self.provider.mass.subscribe(
                    self._handle_state_event,
                    (
                        EventType.PLAYER_UPDATED,
                        EventType.QUEUE_UPDATED,
                        EventType.QUEUE_TIME_UPDATED,
                    ),
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

            # Mirror an already-active MA queue to Plex (runs in the background so it
            # never blocks startup on Plex network calls).
            self.provider.mass.create_task(self._sync_initial_queue_to_plex())

    async def stop(self) -> None:
        """Stop the HTTP server and unsubscribe from events."""
        for unsub in self._unsub_callbacks:
            unsub()
        self._unsub_callbacks.clear()

        if self.http_site:
            await self.http_site.stop()
        if self.runner:
            await self.runner.cleanup()
        LOGGER.info("Plex remote control server stopped")

    async def handle_root(self, request: web.Request) -> web.Response:
        """Handle root endpoint - return basic player info."""
        player_name = "Music Assistant"
        if self._ma_player_id:
            player = self.provider.mass.players.get_player(self._ma_player_id)
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
        include_metadata = request.query.get("includeMetadata", "0") == "1"
        command_id = request.query.get("commandID", "0")

        client_id = request.headers.get("X-Plex-Client-Identifier")
        if client_id and client_id in self.subscriptions:
            self.subscriptions[client_id]["last_update"] = time.time()

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

    async def handle_options(self, request: web.Request) -> web.Response:
        """Handle OPTIONS requests for CORS."""
        return web.Response(
            status=200,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "*",
            },
        )

    async def handle_resources(self, request: web.Request) -> web.Response:
        """Return player capabilities and connection information."""
        player_name = "Music Assistant"
        state = "stopped"
        player = (
            self.provider.mass.players.get_player(self._ma_player_id)
            if self._ma_player_id
            else None
        )
        if player:
            player_name = player.display_name
            queue = self.provider.mass.players.get_active_queue(player)
            state = self._resolve_plex_state(player, queue)

        local_ip = self.provider.mass.streams.publish_ip
        version = self.provider.mass.version if self.provider.mass.version != "0.0.0" else "1.0.0"

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
