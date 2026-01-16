"""Demo Player Provider implementation."""

from __future__ import annotations

import logging

from music_assistant_models.player import PlayerSource
from pyheos import Credentials, Heos, HeosOptions

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
    _source_list: list[PlayerSource] = []

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            logging.getLogger("pyheos").setLevel(logging.DEBUG)
        else:
            logging.getLogger("pyheos").setLevel(self.logger.level + 10)

        self.logger.info("handle_async_init")

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

        # Initialize library values
        await self._build_source_list()

        # Build player configs
        devices = await self._heos.get_players()
        self.logger.info("Found %s players", len(devices))
        for player_id, device in devices.items():
            self.logger.info(f"Found player {player_id}, {device.name}")

            heos_player = HeosPlayer(self, device)
            await heos_player.setup()

    async def _build_source_list(self) -> None:
        """Build source list based on data from controller."""
        music_sources = await self._heos.get_music_sources()

        for source_id, source in music_sources.items():
            self._source_list.append(
                PlayerSource(
                    id=str(source_id),
                    name=source.name,
                    passive=not source.available,
                    can_play_pause=source_id == 1024,  # TODO: properly check
                    can_next_previous=source_id == 1024,  # TODO: properly check
                )
            )

    @property
    def source_list(self) -> list[PlayerSource]:
        """Get mapped source list from controller info."""
        return self._source_list

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
