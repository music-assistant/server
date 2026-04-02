"""MPD Player Provider implementation."""

from __future__ import annotations

from typing import cast

from music_assistant.constants import CONF_PASSWORD, CONF_PORT
from music_assistant.models.player_provider import PlayerProvider

from .constants import CONF_HOST
from .player import MPDPlayer


class MPDPlayerProvider(PlayerProvider):
    """
    MPD Player provider.

    One provider instance represents one MPD server. The host, port and
    optional password are configured at the provider level.
    """

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.logger.debug(
            "Initializing MPD provider for %s:%s",
            self.config.get_value(CONF_HOST),
            self.config.get_value(CONF_PORT),
        )

    async def discover_players(self) -> None:
        """Set up the single MPD player from provider config."""
        host = cast("str", self.config.get_value(CONF_HOST))
        port = cast("int", self.config.get_value(CONF_PORT))
        password = cast("str | None", self.config.get_value(CONF_PASSWORD) or None)

        player_id = f"mpd_{host}_{port}"
        if self.mass.players.get_player(player_id):
            return
        player = MPDPlayer(
            provider=self,
            player_id=player_id,
            host=host,
            port=port,
            password=password,
        )
        await self.mass.players.register(player)
