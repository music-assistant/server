"""Squeezelite Player Provider implementation."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass

from aioslimproto.client import SlimClient
from aioslimproto.models import EventType as SlimEventType
from aioslimproto.server import SlimServer
from music_assistant_models.enums import ProviderFeature
from music_assistant_models.errors import SetupFailedError

from music_assistant.constants import CONF_PORT, VERBOSE_LOG_LEVEL
from music_assistant.helpers.util import is_port_in_use
from music_assistant.models.player_provider import PlayerProvider

from .multi_client_stream import MultiClientStream
from .player import SqueezelitePlayer


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
        return {ProviderFeature.SYNC_PLAYERS}

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # set-up aioslimproto logging
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            logging.getLogger("aioslimproto").setLevel(logging.DEBUG)
        else:
            logging.getLogger("aioslimproto").setLevel(self.logger.level + 10)

        # setup slimproto server
        port = self.config.get_value(CONF_PORT)
        if await is_port_in_use(port):
            msg = f"Port {port} is not available"
            raise SetupFailedError(msg)

        self.slimproto = SlimServer(
            port=port,
            name=f"Music Assistant ({self.mass.webserver.ip})",
            player_join_callback=self._player_join,
            player_leave_callback=self._player_leave,
            player_event_callback=self._player_update,
        )

        await self.slimproto.start()
        self.logger.info("Slimproto server started on port %s", port)

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        if self.slimproto:
            await self.slimproto.stop()

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

        if player := self._players.pop(player_id, None):
            if mass_player := self.mass.players.get(player_id):
                mass_player.available = False
                self.mass.players.update(player_id)

    async def _player_update(self, player_id: str, event: SlimEventType) -> None:
        """Handle player update from slimproto server."""
        if player := self._players.get(player_id):
            await player.handle_slim_event(event)

    def _get_sync_clients(self, player_id: str) -> Iterator[SlimClient]:
        """Get all sync clients for a player."""
        player = self.mass.players.get(player_id)
        yield self.slimproto.get_player(player_id)
        for member_id in player.group_members:
            yield self.slimproto.get_player(member_id)

    async def poll_player(self, player_id: str) -> None:
        """Poll player for state updates."""
        if player := self._players.get(player_id):
            await player.poll()
