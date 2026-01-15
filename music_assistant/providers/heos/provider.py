"""Demo Player Provider implementation."""

from __future__ import annotations

import logging

from pyheos import Credentials, Heos, HeosOptions, MediaMusicSource

from music_assistant.constants import (
    CONF_IP_ADDRESS,
    CONF_PASSWORD,
    CONF_USERNAME,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.models.player_provider import PlayerProvider

from .player import HeosPlayer


class HeosPlayerProvider(PlayerProvider):
    """Player provided for Denon HEOS."""

    _heos: Heos

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            logging.getLogger("pyheos").setLevel(logging.DEBUG)
        else:
            logging.getLogger("pyheos").setLevel(self.logger.level + 10)

        # Credentials are not needed, only used to grab favorites and music services
        credentials: Credentials | None = None

        if (username := self.config.get_value(CONF_USERNAME)) is not None and (
            password := self.config.get_value(CONF_PASSWORD)
        ) is not None:
            credentials = Credentials(str(username), str(password))

        self._heos = Heos(
            HeosOptions(
                str(self.config.get_value(CONF_IP_ADDRESS)),
                credentials=credentials,
                auto_reconnect=True,
            )
        )

        # TODO: Handle connection failures
        await self._heos.connect()

        self.logger.info("Connected to HEOS System")

        players = await self._heos.get_players()
        await self._heos.get_music_sources()

        self.logger.info("Found %s players", len(players))

        for player_id, player in players.items():
            self.logger.info(f"Found player {player_id}, {player.name}")

            heos_player = HeosPlayer(self, player)
            await heos_player.setup()

    @property
    def source_list(self) -> dict[int, MediaMusicSource]:
        """Get the music source list for the system."""
        return self._heos.music_sources

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        is_removed will be set to True when the provider is removed from the configuration.
        """
        # OPTIONAL
        # this is an optional method that you can implement if
        # relevant or leave out completely if not needed.
        # it will be called when the provider is unloaded from Music Assistant.
        # this means also when the provider is getting reloaded
        await self._heos.disconnect()

        for player in self.players:
            # if you have any cleanup logic for the players, you can do that here.
            # e.g. disconnecting from the player, closing connections, etc.
            self.logger.debug("Unloading player %s", player.name)
            await self.mass.players.unregister(player.player_id)
