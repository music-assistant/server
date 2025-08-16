"""Squeezelite Player Provider implementation."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aioslimproto.server import SlimServer
from music_assistant_models.enums import ProviderFeature
from music_assistant_models.errors import SetupFailedError

from music_assistant.constants import CONF_PORT, VERBOSE_LOG_LEVEL
from music_assistant.helpers.util import is_port_in_use
from music_assistant.models.player_provider import PlayerProvider

from .constants import CONF_CLI_JSON_PORT, CONF_CLI_TELNET_PORT
from .multi_client_stream import MultiClientStream
from .player import SqueezelitePlayer

if TYPE_CHECKING:
    from aioslimproto.client import SlimClient
    from aioslimproto.models import EventType as SlimEventType


@dataclass
class StreamInfo:
    """Dataclass to store stream information."""

    stream_id: str
    players: list[str]
    stream_obj: MultiClientStream


class SqueezelitePlayerProvider(PlayerProvider):
    """Player provider for players using slimproto (like Squeezelite)."""

    def __init__(self, *args, **kwargs) -> None:
        """Initialize the provider."""
        super().__init__(*args, **kwargs)
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
        # setup slimproto server
        control_port = self.config.get_value(CONF_PORT)
        if await is_port_in_use(control_port):
            msg = f"Port {control_port} is not available"
            raise SetupFailedError(msg)
        telnet_port = self.config.get_value(CONF_CLI_TELNET_PORT)
        if telnet_port is not None and await is_port_in_use(telnet_port):
            msg = f"Telnet port {telnet_port} is not available"
            raise SetupFailedError(msg)
        json_port = self.config.get_value(CONF_CLI_JSON_PORT)
        if json_port is not None and await is_port_in_use(json_port):
            msg = f"JSON port {json_port} is not available"
            raise SetupFailedError(msg)
        # silence aioslimproto logger a bit
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            logging.getLogger("aioslimproto").setLevel(logging.DEBUG)
        else:
            logging.getLogger("aioslimproto").setLevel(self.logger.level + 10)
        self.slimproto = SlimServer(
            cli_port=telnet_port or None,
            cli_port_json=json_port or None,
            ip_address=self.mass.streams.publish_ip,
            name="Music Assistant",
            control_port=control_port,
        )
        # start slimproto socket server
        await self.slimproto.start()

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await super().loaded_in_mass()
        self.slimproto.subscribe(self._client_callback)
        self.mass.streams.register_dynamic_route(
            "/slimproto/multi", self._serve_multi_client_stream
        )
        # it seems that WiiM devices do not use the json rpc port that is broadcasted
        # in the discovery info but instead they just assume that the jsonrpc endpoint
        # lives on the same server as stream URL. So we need to provide a jsonrpc.js
        # endpoint that just redirects to the jsonrpc handler within the slimproto package.
        self.mass.streams.register_dynamic_route(
            "/jsonrpc.js", self.slimproto.cli._handle_jsonrpc_client
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        if self.slimproto:
            await self.slimproto.stop()
        self.mass.streams.unregister_dynamic_route("/slimproto/multi")
        self.mass.streams.unregister_dynamic_route("/jsonrpc.js")

    async def _player_join(self, slimplayer: SlimClient) -> None:
        """Handle player joining the slimproto server."""
        player_id = slimplayer.player_id
        if player_id in self._players:
            return

        self.logger.debug("Player %s joined the server", player_id)

        # Create SqueezelitePlayer instance
        player = SqueezelitePlayer(self, player_id, slimplayer)
        self._players[player_id] = player

        # Register with Music Assistant
        await player.setup()

    async def _player_leave(self, player_id: str) -> None:
        """Handle player leaving the slimproto server."""
        self.logger.debug("Player %s left the server", player_id)

        if self._players.pop(player_id, None):
            if mass_player := self.mass.players.get(player_id):
                mass_player.available = False
                self.mass.players.update(player_id)

    async def _player_update(self, player_id: str, event: SlimEventType) -> None:
        """Handle player update from slimproto server."""
        if player := self._players.get(player_id):
            await player.handle_slim_event(event)
