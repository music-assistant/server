"""MSX Bridge Player Provider implementation."""

from __future__ import annotations

from typing import cast

from music_assistant.models.player_provider import PlayerProvider

from .constants import (
    CONF_HTTP_PORT,
    CONF_OUTPUT_FORMAT,
    DEFAULT_HTTP_PORT,
    DEFAULT_OUTPUT_FORMAT,
)
from .http_server import MSXHTTPServer
from .player import MSXPlayer

DEFAULT_PLAYER_ID = "msx_default"
DEFAULT_PLAYER_NAME = "MSX TV"


class MSXBridgeProvider(PlayerProvider):
    """Player Provider that bridges Music Assistant to Smart TVs via MSX."""

    http_server: MSXHTTPServer | None = None

    async def handle_async_init(self) -> None:
        """Handle async initialization — start embedded HTTP server."""
        port = cast("int", self.config.get_value(CONF_HTTP_PORT, DEFAULT_HTTP_PORT))
        self.http_server = MSXHTTPServer(self, port)
        await self.http_server.start()
        self.logger.info(
            "MSX Bridge provider initialized, HTTP server on port %s", port
        )

    async def loaded_in_mass(self) -> None:
        """Register the default MSX player after provider is loaded."""
        await super().loaded_in_mass()
        output_format = cast(
            "str", self.config.get_value(CONF_OUTPUT_FORMAT, DEFAULT_OUTPUT_FORMAT)
        )
        player = MSXPlayer(
            provider=self,
            player_id=DEFAULT_PLAYER_ID,
            name=DEFAULT_PLAYER_NAME,
            output_format=output_format,
        )
        await self.mass.players.register(player)
        self.logger.info("Registered default MSX player: %s", DEFAULT_PLAYER_NAME)

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload — stop HTTP server first, then unregister players."""
        if self.http_server:
            await self.http_server.stop()
        for player in self.players:
            self.logger.debug("Unloading player %s", player.display_name)
            await self.mass.players.unregister(player.player_id)
        self.logger.info("MSX Bridge provider unloaded")

    async def discover_players(self) -> None:
        """Discover players — MSX players are registered on demand."""
        # The default player is registered in loaded_in_mass().
        # Additional players could be registered when TVs connect via HTTP.
