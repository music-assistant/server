"""HEOS Player Provider implementation."""

from __future__ import annotations

import logging

from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.player import PlayerSource
from pyheos import Credentials, Heos, HeosError, HeosOptions, MediaItem, PlayerUpdateResult, const
from pyheos import HeosPlayer as PyHeosPlayer

from music_assistant.constants import (
    CONF_IP_ADDRESS,
    CONF_PASSWORD,
    CONF_USERNAME,
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

    _device_map: dict[str, PyHeosPlayer] = {}

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
        self._heos.add_on_user_credentials_invalid(self.invalid_credentials)

        try:
            await self._heos.connect()

            self._heos.add_on_controller_event(self._handle_controller_event)
        except HeosError as e:
            self.logger.error(f"Failed to connect to HEOS controller: {e}")
            raise MusicAssistantError("Failed to connect to HEOS controller") from e

        # Initialize library values
        try:
            # Populate source lists
            await self._populate_sources()

            # Build player configs
            devices = await self._heos.get_players()
            for device_id, device in devices.items():
                self._device_map[str(device_id)] = device

                heos_player = HeosPlayer(self, device)
                await heos_player.setup()
        except HeosError as e:
            self.logger.error(f"Unexpected error setting up HEOS controller: {e}")
            raise MusicAssistantError("Unexpected error setting up HEOS controller") from e

    async def invalid_credentials(self) -> None:
        """Handle invalid login credentials."""
        # Just log a warning, HEOS works fine without login, just no favorites and music services
        self.logger.warning("Invalid login credentials provided for HEOS")

    async def _handle_controller_event(
        self, event: str, result: PlayerUpdateResult | None = None
    ) -> None:
        self.logger.debug("Controller event received: %s", event)

        if event == const.EVENT_GROUPS_CHANGED:
            for player in self.mass.players.all(provider_filter=self.instance_id):
                assert isinstance(player, HeosPlayer)  # for type checking
                await player.build_group_list()

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
