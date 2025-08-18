"""Squeezelite Player Provider implementation."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

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
        self.slimproto.subscribe(self._handle_slimproto_event)
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

    def get_corrected_elapsed_milliseconds(self, slimplayer: SlimClient) -> int:
        """Return corrected elapsed milliseconds for a slimplayer."""
        sync_delay = self.mass.config.get_raw_player_config_value(
            slimplayer.player_id, CONF_SYNC_ADJUST, 0
        )
        return slimplayer.elapsed_milliseconds - sync_delay

    def _handle_slimproto_event(
        self,
        event: SlimEvent,
    ) -> None:
        if self.mass.closing:
            return

        # handle new player connect (or reconnect of existing player)
        if event.type == SlimEventType.PLAYER_CONNECTED:
            if not (slimclient := self.slimproto.get_player(event.player_id)):
                return  # should not happen, but guard anyways
            player = SqueezelitePlayer(self, event.player_id, slimclient)
            self.mass.create_task(player.setup())
            return

        if not (player := self.mass.players.get(event.player_id)):
            return  # guard for unknown player
        if TYPE_CHECKING:
            player = cast("SqueezelitePlayer", player)

        # handle player disconnect
        if event.type == SlimEventType.PLAYER_DISCONNECTED:
            self.mass.create_task(self.mass.players.unregister(player.player_id))
            return

        # forward all other events to the player itself
        player.handle_slim_event(event)
