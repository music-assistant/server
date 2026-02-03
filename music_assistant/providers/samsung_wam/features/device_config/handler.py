"""Device configuration handler."""

from __future__ import annotations

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
        """Set the friendly hostname on the physical device.

        :param name: The desired friendly name.
        """
        await self.speaker.set_name(name)
