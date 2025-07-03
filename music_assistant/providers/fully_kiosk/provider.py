"""FullyKiosk Player provider for Music Assistant."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from fullykiosk import FullyKiosk
from music_assistant_models.errors import SetupFailedError

from music_assistant.constants import (
    CONF_ENTRY_FLOW_MODE_ENFORCED,
    CONF_ENTRY_HTTP_PROFILE,
    CONF_ENTRY_OUTPUT_CODEC_DEFAULT_MP3,
    CONF_IP_ADDRESS,
    CONF_PASSWORD,
    CONF_PORT,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.models.player_provider import PlayerProvider

from .player import FullyKioskPlayer

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry


class FullyKioskProvider(PlayerProvider):
    """Player provider for FullyKiosk based players."""

    _fully: FullyKiosk
    _player: FullyKioskPlayer | None = None

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # set-up fullykiosk logging
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            logging.getLogger("fullykiosk").setLevel(logging.DEBUG)
        else:
            logging.getLogger("fullykiosk").setLevel(self.logger.level + 10)
        self._fully = FullyKiosk(
            self.mass.http_session,
            self.config.get_value(CONF_IP_ADDRESS),
            self.config.get_value(CONF_PORT),
            self.config.get_value(CONF_PASSWORD),
        )
        try:
            async with asyncio.timeout(15):
                await self._fully.getDeviceInfo()
        except Exception as err:
            msg = f"Unable to start the FullyKiosk connection ({err!s}"
            raise SetupFailedError(msg) from err

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        # Add FullyKiosk device to Player controller.
        player_id = self._fully.deviceInfo["deviceID"]
        address = (
            f"http://{self.config.get_value(CONF_IP_ADDRESS)}:{self.config.get_value(CONF_PORT)}"
        )

        self._player = FullyKioskPlayer(self, player_id, self._fully, address)
        await self._player.setup()

    async def get_player_config_entries(self, player_id: str) -> tuple[ConfigEntry, ...]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        base_entries = await super().get_player_config_entries(player_id)
        return (
            *base_entries,
            CONF_ENTRY_FLOW_MODE_ENFORCED,
            CONF_ENTRY_OUTPUT_CODEC_DEFAULT_MP3,
            CONF_ENTRY_HTTP_PROFILE,
        )

    async def poll_player(self, player_id: str) -> None:
        """Poll player for state updates."""
        if self._player:
            await self._player.poll()
