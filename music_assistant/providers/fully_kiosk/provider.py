"""FullyKiosk Player provider for Music Assistant."""

from __future__ import annotations

import asyncio
import logging

from fullykiosk import FullyKiosk
from music_assistant_models.errors import SetupFailedError

from music_assistant.constants import CONF_IP_ADDRESS, CONF_PASSWORD, CONF_PORT, VERBOSE_LOG_LEVEL
from music_assistant.models.player_provider import PlayerProvider

from .player import FullyKioskPlayer


class FullyKioskProvider(PlayerProvider):
    """Player provider for FullyKiosk based players."""

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # set-up fullykiosk logging
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            logging.getLogger("fullykiosk").setLevel(logging.DEBUG)
        else:
            logging.getLogger("fullykiosk").setLevel(self.logger.level + 10)
        fully_kiosk = FullyKiosk(
            self.mass.http_session_no_ssl,
            self.config.get_value(CONF_IP_ADDRESS),
            self.config.get_value(CONF_PORT),
            self.config.get_value(CONF_PASSWORD),
        )
        try:
            async with asyncio.timeout(15):
                await fully_kiosk.getDeviceInfo()
        except Exception as err:
            msg = f"Unable to start the FullyKiosk connection ({err!s}"
            raise SetupFailedError(msg) from err
        player_id = fully_kiosk.deviceInfo["deviceID"]
        address = (
            f"http://{self.config.get_value(CONF_IP_ADDRESS)}:{self.config.get_value(CONF_PORT)}"
        )
        player = FullyKioskPlayer(self, player_id, fully_kiosk, address)
        player.set_attributes()
        await self.mass.players.register(player)
