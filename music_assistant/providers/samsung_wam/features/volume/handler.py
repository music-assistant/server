"""Volume handler."""

from __future__ import annotations

from music_assistant.providers.samsung_wam.features.base import (
    WamPlayerFeatureBase,
    handle_pywam_errors,
    retry_command,
)


class VolumeHandler(WamPlayerFeatureBase):
    """Encapsulates volume-related commands."""

    @retry_command()
    @handle_pywam_errors
    async def set_volume(self, volume_level: int) -> None:
        """
        Set the volume level on the speaker.

        :param volume_level: Volume level (0-100).
        """
        await self.speaker.set_volume(volume_level)

    @retry_command()
    @handle_pywam_errors
    async def set_mute(self, muted: bool) -> None:
        """
        Mute or unmute the speaker.

        :param muted: True to mute, False to unmute.
        """
        await self.speaker.set_mute(muted)
