"""FullyKiosk Player implementation."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import PlayerCommandFailed, PlayerUnavailableError

from music_assistant.constants import (
    CONF_ENTRY_FLOW_MODE_ENFORCED,
    CONF_ENTRY_HTTP_PROFILE,
    CONF_ENTRY_OUTPUT_CODEC_DEFAULT_MP3,
)
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia

if TYPE_CHECKING:
    from fullykiosk import FullyKiosk
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType

    from .provider import FullyKioskProvider

AUDIOMANAGER_STREAM_MUSIC = 3


class FullyKioskPlayer(Player):
    """FullyKiosk Player implementation."""

    def __init__(
        self,
        provider: FullyKioskProvider,
        player_id: str,
        fully_kiosk: FullyKiosk,
        address: str,
    ) -> None:
        """Initialize the FullyKiosk Player."""
        super().__init__(provider, player_id)
        self.fully_kiosk = fully_kiosk
        # Set player attributes
        self._attr_type = PlayerType.PLAYER
        self._attr_supported_features = {PlayerFeature.VOLUME_SET}
        self._attr_name = self.fully_kiosk.deviceInfo["deviceName"]
        self._attr_device_info = DeviceInfo(
            model=self.fully_kiosk.deviceInfo["deviceModel"],
            manufacturer=self.fully_kiosk.deviceInfo["deviceManufacturer"],
            ip_address=address,
        )
        self._attr_available = True
        self._attr_needs_poll = True
        self._attr_poll_interval = 10

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        base_entries = await super().get_config_entries(action=action, values=values)
        return [
            *base_entries,
            CONF_ENTRY_FLOW_MODE_ENFORCED,
            CONF_ENTRY_OUTPUT_CODEC_DEFAULT_MP3,
            CONF_ENTRY_HTTP_PROFILE,
        ]

    def set_attributes(self) -> None:
        """Set/update FullyKiosk player attributes."""
        self._attr_name = self.fully_kiosk.deviceInfo["deviceName"]
        for volume_dict in self.fully_kiosk.deviceInfo.get("audioVolumes", []):
            if str(AUDIOMANAGER_STREAM_MUSIC) in volume_dict:
                volume = volume_dict[str(AUDIOMANAGER_STREAM_MUSIC)]
                self._attr_volume_level = volume
                break
        current_url = self.fully_kiosk.deviceInfo.get("soundUrlPlaying")
        if not current_url:
            self._attr_playback_state = PlaybackState.IDLE
        self._attr_available = True

    async def volume_set(self, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        await self.fully_kiosk.setAudioVolume(volume_level, AUDIOMANAGER_STREAM_MUSIC)
        self._attr_volume_level = volume_level
        self.update_state()

    async def stop(self) -> None:
        """Send STOP command to given player."""
        await self.fully_kiosk.stopSound()
        self._attr_playback_state = PlaybackState.IDLE
        self._attr_current_media = None
        self.update_state()

    async def play(self) -> None:
        """Handle PLAY command on the player."""
        raise PlayerCommandFailed("Playback can not be resumed.")

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on given player."""
        await self.fully_kiosk.playSound(media.uri, AUDIOMANAGER_STREAM_MUSIC)
        self._attr_current_media = media
        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time.time()
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def poll(self) -> None:
        """Poll player for state updates."""
        try:
            async with asyncio.timeout(15):
                await self.fully_kiosk.getDeviceInfo()
                self.set_attributes()
                self.update_state()
        except Exception as err:
            msg = f"Unable to start the FullyKiosk connection ({err!s}"
            raise PlayerUnavailableError(msg) from err
