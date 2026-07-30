"""Device configuration handler."""

from __future__ import annotations

from pywam.lib.exceptions import FeatureNotSupportedError

from music_assistant.providers.samsung_wam.features.base import (
    WamPlayerFeatureBase,
    handle_pywam_errors,
    retry_command,
)


class DeviceConfigHandler(WamPlayerFeatureBase):
    """Encapsulates device configuration commands."""

    @retry_command()
    @handle_pywam_errors
    async def set_name(self, name: str) -> None:
        """
        Set the friendly name on the device.

        :param name: The desired friendly name.
        """
        try:
            await self.speaker.set_name(name)
        except FeatureNotSupportedError:
            # pywam raises this when the speaker is grouped or not yet fully initialised
            self.logger.debug("Cannot rename %s in its current mode", self.player.log_name)
