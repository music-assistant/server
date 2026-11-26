"""Squeezelite Player Provider implementation."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from aiohttp import web
from aioslimproto.models import EventType as SlimEventType
from aioslimproto.models import SlimEvent
from aioslimproto.server import SlimServer
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, EventType, ProtocolRole
from music_assistant_models.errors import MusicAssistantError, SetupFailedError

from music_assistant.constants import CONF_PORT, VERBOSE_LOG_LEVEL
from music_assistant.helpers.util import is_port_in_use
from music_assistant.models.player_provider import PlayerProvider

from .constants import (
    CACHE_CATEGORY_PREV_STATE,
    CONF_CLI_JSON_PORT,
    CONF_CLI_TELNET_PORT,
    CONF_DISCOVERY,
    CONF_STORED_VOLUME,
    DEFAULT_PLAYER_VOLUME,
    DEFAULT_SLIMPROTO_PORT,
)
from .player import SqueezelitePlayer
from .sendspin_bridge import BRIDGE_STREAM_PATH, SendspinBridgeManager

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiosendspin.clock import Clock
    from aioslimproto.cli import SlimCLICommand
    from music_assistant_models.event import MassEvent


class SqueezelitePlayerProvider(PlayerProvider):
    """Player provider for players using slimproto (like Squeezelite)."""

    reload_on_streams_network_change = True
    slimproto: SlimServer | None = None
    # the clock every server time of slimproto is on: the one Sendspin runs on
    clock: Clock
    bridge_manager: SendspinBridgeManager
    _unsub_queue_updated: Callable[[], None] | None = None
    _unsub_timeline_rate: Callable[[], None] | None = None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to setup this provider."""
        return (
            ConfigEntry(
                key=CONF_CLI_TELNET_PORT,
                type=ConfigEntryType.INTEGER,
                default_value=9090,
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_CLI_JSON_PORT,
                type=ConfigEntryType.INTEGER,
                default_value=9000,
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_DISCOVERY,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_PORT,
                type=ConfigEntryType.INTEGER,
                default_value=DEFAULT_SLIMPROTO_PORT,
            ),
        )

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # set-up aioslimproto logging
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            logging.getLogger("aioslimproto").setLevel(logging.DEBUG)
        else:
            logging.getLogger("aioslimproto").setLevel(self.logger.level + 10)

        # Get all port configurations
        control_port = cast("int", self.config.get_value(CONF_PORT))
        telnet_port = cast("int | None", self.config.get_value(CONF_CLI_TELNET_PORT))
        json_port = cast("int | None", self.config.get_value(CONF_CLI_JSON_PORT))

        # Validate ALL required ports before starting ANY services
        await self._validate_all_ports(control_port, telnet_port, json_port)

        self.clock = self.mass.timeline_clock
        self.bridge_manager = SendspinBridgeManager(self)
        # a bridge stream maps the timeline onto the clock of its player
        self._unsub_timeline_rate = self.mass.timeline_clock.add_rate_listener(
            pinned=self.bridge_manager.has_stream
        )

        # create the server here (also validates config and sets up the CLI) but defer
        # start() to loaded_in_mass, so we subscribe to events before accepting clients
        self.slimproto = SlimServer(
            cli_port=telnet_port or None,
            cli_port_json=json_port or None,
            ip_address=self.mass.streams.publish_ip,
            name="Music Assistant",
            control_port=control_port,
            cli_command_handler=self._handle_cli_command,
            clock=self._server_time,
            initial_volume=self._get_initial_volume,
        )

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await super().loaded_in_mass()
        assert self.slimproto is not None  # for type checker
        self.slimproto.cli.display_name_lookup = self._lookup_display_name
        self.slimproto.cli.elapsed_lookup = self._lookup_elapsed
        # subscribe before starting the socket server: aioslimproto does not buffer
        # events, so a client connecting before we subscribe would be missed entirely
        self.slimproto.subscribe(self._handle_slimproto_event)
        try:
            await self.slimproto.start()
        except OSError as err:
            # ports were validated during setup, so a failure here is unlikely
            self.unload_with_error(err)
            return
        self.mass.streams.register_dynamic_route(BRIDGE_STREAM_PATH, self._serve_bridge_stream)
        # it seems that WiiM devices do not use the json rpc port that is broadcasted
        # in the discovery info but instead they just assume that the jsonrpc endpoint
        # lives on the same server as stream URL. So we need to provide a jsonrpc.js
        # endpoint that just redirects to the jsonrpc handler within the slimproto package.
        self.mass.streams.register_dynamic_route(
            "/jsonrpc.js", self.slimproto.cli._handle_jsonrpc_client
        )
        self._unsub_queue_updated = self.mass.subscribe(
            self._on_queue_updated, EventType.QUEUE_UPDATED
        )

    async def discover_players(self) -> None:
        """Attach the connected players that have no attached protocol, such as a removed one."""
        if not self.slimproto:
            return
        for slimclient in self.slimproto.players:
            protocol_id = slimclient.player_id
            if self.get_protocol(protocol_id) is None and self.mass.players.wants_protocol(
                protocol_id
            ):
                await SqueezelitePlayer(self, protocol_id, slimclient).setup()

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        if self._unsub_queue_updated is not None:
            self._unsub_queue_updated()
            self._unsub_queue_updated = None
        if self._unsub_timeline_rate is not None:
            self._unsub_timeline_rate()
            self._unsub_timeline_rate = None
        # Stop all Sendspin bridges
        bridge_manager = getattr(self, "bridge_manager", None)
        if bridge_manager:
            await bridge_manager.close()
        # Ensure complete cleanup
        await self._cleanup_server()
        self.mass.streams.unregister_dynamic_route(BRIDGE_STREAM_PATH)
        self.mass.streams.unregister_dynamic_route("/jsonrpc.js")

    def _server_time(self) -> float:
        """Return the time of the clock of this provider in seconds, for aioslimproto."""
        return self.clock.now_us() / 1_000_000

    def _lookup_display_name(self, player_id: str) -> str | None:
        """Return Music Assistant's display name for a slimproto player, or None if unknown."""
        mass_player = self.mass.players.get_player_by_protocol(player_id)
        return mass_player.name if mass_player else None

    def _lookup_elapsed(self, player_id: str) -> float | None:
        """Return a player's position in the track from its Sendspin bridge or queue, else None."""
        bridge = self.bridge_manager.get_bridge(player_id)
        if bridge and (position := bridge.position_in_track) is not None:
            return position
        slimclient = self.slimproto.get_player(player_id) if self.slimproto else None
        media = slimclient.current_media if slimclient else None
        if media and (source_id := media.metadata.get("source_id")):
            queue = self.mass.player_queues.get(cast("str", source_id))
            # the end of the previous track can still play while the queue is on the next one
            if (
                queue
                and queue.current_item
                and queue.current_item.queue_item_id == media.metadata.get("queue_item_id")
            ):
                return queue.corrected_elapsed_time
        return None

    async def _validate_all_ports(
        self, control_port: int, telnet_port: int | None, json_port: int | None
    ) -> None:
        """Validate that all required ports are available before starting any services."""
        ports_to_check = [(control_port, "SlimProto control")]

        if telnet_port and telnet_port > 0:
            ports_to_check.append((telnet_port, "Telnet CLI"))

        if json_port and json_port > 0:
            ports_to_check.append((json_port, "JSON-RPC CLI"))

        # Collect all port conflicts before raising any errors
        occupied_ports = []
        for port, port_description in ports_to_check:
            if await is_port_in_use(port):
                occupied_ports.append(f"{port_description} port {port}")

        # If any ports are occupied, raise a comprehensive error message
        if occupied_ports:
            if len(occupied_ports) == 1:
                msg = f"{occupied_ports[0]} is not available"
            else:
                msg = f"Multiple ports are not available: {', '.join(occupied_ports)}"
            raise SetupFailedError(msg)

    async def _cleanup_server(self) -> None:
        """Ensure complete cleanup of the SlimProto server on initialization failure."""
        if self.slimproto:
            try:
                await self.slimproto.stop()
            except Exception as err:  # noqa: BLE001 - cleanup must not hide the setup error
                self.logger.warning("Error stopping SlimProto server during cleanup: %s", err)
            finally:
                self.slimproto = None

    def _handle_slimproto_event(
        self,
        event: SlimEvent,
    ) -> None:
        """Handle events from SlimProto players."""
        # Exit early if system is closing or slimproto server is not initialized
        if self.mass.closing or not self.slimproto:
            return

        # Handle new player connect (or reconnect of existing player)
        if event.type == SlimEventType.PLAYER_CONNECTED:
            slimclient = self.slimproto.get_player(event.player_id)
            if not slimclient:
                return  # should not happen, but guard anyways
            if not self.mass.players.wants_protocol(event.player_id):
                return
            player = SqueezelitePlayer(self, event.player_id, slimclient)
            self.mass.create_task(player.setup())
            return

        if not (mass_player := self.get_protocol(event.player_id)):
            return  # guard for unknown player
        player = cast("SqueezelitePlayer", mass_player)

        # Handle player disconnect
        if event.type == SlimEventType.PLAYER_DISCONNECTED:
            self.mass.create_task(self.mass.players.unregister(player.protocol_id))
            return

        # the Sendspin bridge of the player goes first: what it does with an event
        # decides what is left of it for the player itself
        if (bridge := player.sendspin_bridge) is not None:
            bridge.handle_slim_event(event)

        # forward all other events to the player itself
        player.handle_slim_event(event)

    def _on_queue_updated(self, event: MassEvent) -> None:
        """Let the players show repeat and shuffle of the queue they play."""
        if (queue_id := event.object_id) is None:
            return
        for protocol in self.protocols:
            cast("SqueezelitePlayer", protocol).on_queue_updated(queue_id)

    async def _serve_bridge_stream(self, request: web.Request) -> web.StreamResponse:
        """Serve the stream of a Sendspin bridge to its player."""
        player_id = request.query.get("player_id")
        if not player_id or not (bridge := self.bridge_manager.get_bridge(player_id)):
            raise web.HTTPNotFound(reason=f"Unknown player: {player_id}")
        return await bridge.serve_stream(request)

    async def _handle_cli_command(self, command: SlimCLICommand) -> None:
        """
        Handle play, pause and stop sent by a client, acting on its whole group like LMS does.

        A stop from a protocol-only device stops only that device, which then leaves the
        Sendspin session of its bridge. A client that plays for its Sendspin bridge acts on
        the player that leads its Sendspin group.

        :param command: The CLI command as received from the client.
        :raises NotImplementedError: For any other command, an unknown player and a stop
            from a protocol-only device, so aioslimproto handles it for that client alone.
        """
        if (
            not command.player_id
            or not (protocol := self.get_protocol(command.player_id))
            or not (player := protocol.player)
        ):
            raise NotImplementedError
        arg = str(command.args[0]) if command.args else None
        match command.command, arg:
            case ("play", _) | ("mode", "play") | ("pause", "0"):
                player_cmd = self.mass.players.cmd_play
            case ("pause", "1") | ("mode", "pause"):
                player_cmd = self.mass.players.cmd_pause
            case ("pause", None):
                player_cmd = self.mass.players.cmd_play_pause
            # a protocol-only device also sends stop when another source or app takes
            # over, so that stop must only stop the device itself and not its group
            case ("stop", _) | ("mode", "stop") if protocol.role != ProtocolRole.AUDIO:
                player_cmd = self.mass.players.cmd_stop
            case ("stop", _) | ("mode", "stop"):
                # it most likely switched to another source: its Sendspin bridge would
                # otherwise put its stream right back
                self.bridge_manager.stop_streaming(protocol.protocol_id)
                raise NotImplementedError
            case _:
                raise NotImplementedError
        player_id = (
            self.bridge_manager.get_transport_command_target(protocol.protocol_id)
            or player.player_id
        )
        try:
            await player_cmd(player_id)
        except MusicAssistantError as err:
            self.logger.warning(
                "Failed to handle %s command from %s: %s",
                command.command,
                protocol.display_name,
                err,
            )

    async def _get_initial_volume(self, player_id: str) -> int:
        """Return the volume a slimproto player starts at on connect: its last one, else default."""
        if (
            volume := self.mass.config.get_protocol_value(player_id, CONF_STORED_VOLUME)
        ) is not None:
            return cast("int", volume)
        # older versions kept it in an (expiring) cache entry
        last_state = await self.mass.cache.get(
            key=player_id, provider=self.instance_id, category=CACHE_CATEGORY_PREV_STATE
        )
        return cast("int", last_state[1]) if last_state else DEFAULT_PLAYER_VOLUME
