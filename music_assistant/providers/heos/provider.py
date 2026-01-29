"""HEOS Player Provider implementation."""

from __future__ import annotations

import logging
from typing import cast

from music_assistant_models.errors import SetupFailedError
from music_assistant_models.player import PlayerSource
from pyheos import Heos, HeosError, HeosOptions, MediaItem, PlayerUpdateResult, const

from music_assistant.constants import CONF_ENABLED, CONF_IP_ADDRESS, VERBOSE_LOG_LEVEL
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.heos.constants import HEOS_PASSIVE_SOURCES

from .player import HeosPlayer


class HeosPlayerProvider(PlayerProvider):
    """Player provided for Denon HEOS."""

    _heos: Heos
    _music_source_list: list[PlayerSource] = []
    _input_source_list: list[MediaItem] = []
    _discovery_running: bool = False

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            logging.getLogger("pyheos").setLevel(logging.DEBUG)
        else:
            logging.getLogger("pyheos").setLevel(self.logger.level + 10)

        self._heos = Heos(
            HeosOptions(
                str(self.config.get_value(CONF_IP_ADDRESS)),
                auto_reconnect=True,
            )
        )

        try:
            await self._heos.connect()

            self._heos.add_on_controller_event(self._handle_controller_event)
        except HeosError as e:
            self.logger.error(f"Failed to connect to HEOS controller: {e}")
            raise SetupFailedError("Failed to connect to HEOS controller") from e

        # Initialize library values
        try:
            # Populate source lists
            await self._populate_sources()
            # NOTE: players are discovered via discovery method (called automatically by core)
        except HeosError as e:
            self.logger.error(f"Unexpected error setting up HEOS controller: {e}")
            raise SetupFailedError("Unexpected error setting up HEOS controller") from e

    async def _handle_controller_event(
        self, event: str, result: PlayerUpdateResult | None = None
    ) -> None:
        self.logger.debug("Controller event received: %s", event)

        if event == const.EVENT_GROUPS_CHANGED:
            for player in self.mass.players.all(provider_filter=self.instance_id):
                assert isinstance(player, HeosPlayer)  # for type checking
                await player.build_group_list()

        if event == const.EVENT_PLAYERS_CHANGED:
            if result is None:
                return

            await self.discover_players()

    async def _populate_sources(self) -> None:
        """Build source list based on data from controller."""
        self._input_source_list = list(await self._heos.get_input_sources())

        music_sources = await self._heos.get_music_sources()
        for source_id, source in music_sources.items():
            self._music_source_list.append(
                PlayerSource(
                    id=str(source_id),
                    name=source.name,
                    passive=source_id in HEOS_PASSIVE_SOURCES or not source.available,
                    can_play_pause=True,  # All sources support play/pause
                    can_next_previous=source_id == 1024,  # TODO: properly check
                )
            )

    @property
    def music_source_list(self) -> list[PlayerSource]:
        """Get mapped music source list from controller info."""
        return self._music_source_list

    @property
    def input_source_list(self) -> list[MediaItem]:
        """Get input list from controller info. This represents all inputs across all players."""
        return self._input_source_list

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        self._heos.dispatcher.disconnect_all()  # Remove all event connections
        await self._heos.disconnect()

        for player in self.players:
            self.logger.debug("Unloading player %s", player.name)
            await self.mass.players.unregister(player.player_id)

    async def discover_players(self) -> None:
        """Discover players for this provider."""
        if self._discovery_running or not self._heos:
            return  # discovery already running or not set up

        try:
            self._discovery_running = True
            self.logger.debug("Discovering HEOS players")
            devices = await self._heos.get_players()
            for device in devices.values():
                player_id = str(device.player_id)
                if player := self.mass.players.get(player_id):
                    self.logger.debug(
                        "Updating existing HEOS player: %s (%s)", device.name, player_id
                    )
                    # Update properties such as name or availability
                    cast("HeosPlayer", player).set_static_attributes()
                    continue

                player_enabled = self.mass.config.get_raw_player_config_value(
                    player_id, CONF_ENABLED, default=True
                )
                if not player_enabled:
                    self.logger.debug("Skipping disabled player: %s (%s)", device.name, player_id)
                    continue
                self.logger.info("Discovered new HEOS player: %s (%s)", device.name, player_id)

                heos_player = HeosPlayer(self, device)
                await heos_player.setup()
        finally:
            self._discovery_running = False
