"""HEOS Player Provider implementation."""

from __future__ import annotations

import logging

from music_assistant_models.errors import SetupFailedError
from music_assistant_models.player import PlayerSource
from pyheos import Heos, HeosError, HeosOptions, MediaItem, PlayerUpdateResult, const

from music_assistant.constants import (
    CONF_IP_ADDRESS,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.heos.constants import HEOS_PASSIVE_SOURCES

from .player import HeosPlayer


class HeosPlayerProvider(PlayerProvider):
    """Player provided for Denon HEOS."""

    _heos: Heos
    _music_source_list: list[PlayerSource] = []
    _input_source_list: list[MediaItem] = []

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

            # Build player configs
            devices = await self._heos.get_players()
            for device in devices.values():
                heos_player = HeosPlayer(self, device)

                await heos_player.setup()
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

            for removed_player_id in result.removed_player_ids:
                await self.mass.players.unregister(str(removed_player_id))

            for new_player_id in result.added_player_ids:
                try:
                    device = await self._heos.get_player_info(new_player_id)
                    heos_player = HeosPlayer(self, device)

                    await heos_player.setup()
                except HeosError as e:
                    self.logger.error(
                        "Error adding new HEOS player with id %s: %s", new_player_id, e
                    )
                    continue

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

    def on_player_disabled(self, player_id: str) -> None:
        """Unregister player when it is disabled, cleans up connections."""
        # Clean up event handling connection
        self.mass.create_task(self.mass.players.unregister(player_id))

    # TODO: Re-enable when MA lifecycles get updated.
    # Currently a race-condition prevents `register_or_update` to finish because Enabled is still false  # noqa: E501
    # def on_player_enabled(self, player_id: str) -> None:
    #     """Reregister player when it is enabled."""
    #     self.logger.debug("Attempting player re-enabling")
    #     if device := self._device_map.get(player_id):
    #         # Reinstantiate the player
    #         heos_player = HeosPlayer(self, device)
    #         self.mass.create_task(heos_player.setup())
