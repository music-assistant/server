"""Squeezelite Player Provider implementation."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from aioslimproto.models import EventType as SlimEventType
from aioslimproto.models import SlimEvent
from aioslimproto.server import SlimServer
from music_assistant_models.enums import ProviderFeature
from music_assistant_models.errors import SetupFailedError

from music_assistant.constants import CONF_PORT, CONF_SYNC_ADJUST, VERBOSE_LOG_LEVEL
from music_assistant.helpers.util import is_port_in_use
from music_assistant.models.player_provider import PlayerProvider

from .constants import CONF_CLI_JSON_PORT, CONF_CLI_TELNET_PORT
from .multi_client_stream import MultiClientStream
from .player import SqueezelitePlayer

if TYPE_CHECKING:
    from aioslimproto.client import SlimClient
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant


@dataclass
class StreamInfo:
    """Dataclass to store stream information."""

    stream_id: str
    players: list[str]
    stream_obj: MultiClientStream


class SqueezelitePlayerProvider(PlayerProvider):
    """Player provider for players using slimproto (like Squeezelite)."""

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
    ) -> None:
        """Initialize the provider."""
        super().__init__(mass, manifest, config)
        self.slimproto: SlimServer | None = None
        self._players: dict[str, SqueezelitePlayer] = {}
        self._multi_client_streams: dict[str, StreamInfo] = {}

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {
            ProviderFeature.SYNC_PLAYERS,
            # support sync groups by reporting create/remove player group support
            ProviderFeature.CREATE_GROUP_PLAYER,
            ProviderFeature.REMOVE_GROUP_PLAYER,
        }

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

        # Only proceed with server creation after all ports are validated
        try:
            self.slimproto = SlimServer(
                cli_port=telnet_port or None,
                cli_port_json=json_port or None,
                ip_address=self.mass.streams.publish_ip,
                name="Music Assistant",
                control_port=control_port,
            )
            # start slimproto socket server
            await self.slimproto.start()
        except Exception as err:
            # Ensure cleanup on any initialization failure
            await self._cleanup_server()
            raise SetupFailedError(f"Failed to start SlimProto server: {err}") from err

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
            except Exception as err:
                self.logger.warning("Error stopping SlimProto server during cleanup: %s", err)
            finally:
                self.slimproto = None

        # Clear any associated state that might have been created
        self._players.clear()
        self._multi_client_streams.clear()

    async def _serve_multi_client_stream(self, request: Any) -> Any:
        """Serve the multi-client stream endpoint (stub)."""
        raise NotImplementedError

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await super().loaded_in_mass()
        if not self.slimproto:
            return  # or raise SetupFailedError if this should never happen
        self.slimproto.subscribe(self._handle_slimproto_event)
        self.mass.streams.register_dynamic_route(
            "/slimproto/multi", self._serve_multi_client_stream
        )
        self.mass.streams.register_dynamic_route(
            "/jsonrpc.js", self.slimproto.cli._handle_jsonrpc_client
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        # Ensure complete cleanup
        await self._cleanup_server()
        self.mass.streams.unregister_dynamic_route("/slimproto/multi")
        self.mass.streams.unregister_dynamic_route("/jsonrpc.js")

    def get_corrected_elapsed_milliseconds(self, slimplayer: SlimClient) -> int:
        """Return corrected elapsed milliseconds for a slimplayer."""
        sync_delay = cast(
            "int",
            self.mass.config.get_raw_player_config_value(slimplayer.player_id, CONF_SYNC_ADJUST, 0),
        )
        return cast("int", slimplayer.elapsed_milliseconds - sync_delay)

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
            player = SqueezelitePlayer(self, event.player_id, slimclient)
            self.mass.create_task(player.setup())
            return

        # Get existing player from Music Assistant
        player_generic = self.mass.players.get(event.player_id)
        if not player_generic:
            return  # unknown player, ignore event
        player = cast("SqueezelitePlayer", player_generic)

        # Handle player disconnect
        if event.type == SlimEventType.PLAYER_DISCONNECTED:
            self.mass.create_task(self.mass.players.unregister(player.player_id))
            return
