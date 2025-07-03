"""FullyKiosk Player implementation."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import PlayerUnavailableError

from music_assistant.models.player import DeviceInfo, Player, PlayerMedia

if TYPE_CHECKING:
    from fullykiosk import FullyKiosk

    from .provider import FullyKioskProvider

AUDIOMANAGER_STREAM_MUSIC = 3


class FullyKioskPlayer(Player):
    """FullyKiosk Player implementation."""

    def __init__(
        self,
        provider: FullyKioskProvider,
        player_id: str,
        fully: FullyKiosk,
        address: str,
    ) -> None:
        """Initialize the FullyKiosk Player."""
        super().__init__(provider, player_id)
        self.fully = fully
        self.address = address

        # Set player attributes
        self._attr_type = PlayerType.PLAYER
        self._attr_supported_features = {PlayerFeature.VOLUME_SET}
        self._attr_name = self.fully.deviceInfo["deviceName"]
        self._attr_device_info = DeviceInfo(
            model=self.fully.deviceInfo["deviceModel"],
            manufacturer=self.fully.deviceInfo["deviceManufacturer"],
            ip_address=address,
        )
        self._attr_available = True
        self._attr_needs_poll = True
        self._attr_poll_interval = 10

    async def setup(self) -> None:
        """Set up the player."""
        await self.mass.players.register_or_update(self)
        self._handle_player_update()

    def _handle_player_update(self) -> None:
        """Update FullyKiosk player attributes."""
        self._attr_name = self.fully.deviceInfo["deviceName"]
        for volume_dict in self.fully.deviceInfo.get("audioVolumes", []):
            if str(AUDIOMANAGER_STREAM_MUSIC) in volume_dict:
                volume = volume_dict[str(AUDIOMANAGER_STREAM_MUSIC)]
                self._attr_volume_level = volume
                break
        current_url = self.fully.deviceInfo.get("soundUrlPlaying")
        if not current_url:
            self._attr_playback_state = PlaybackState.IDLE
        self._attr_available = True
        self.update_state()

    async def volume_set(self, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        await self.fully.setAudioVolume(volume_level, AUDIOMANAGER_STREAM_MUSIC)
        self._attr_volume_level = volume_level
        self.update_state()

    async def play(self) -> None:
        """Send PLAY command to given player."""
        # FullyKiosk doesn't have a separate play command

    async def stop(self) -> None:
        """Send STOP command to given player."""
        await self.fully.stopSound()
        self._attr_playback_state = PlaybackState.IDLE
        self.update_state()

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on given player."""
        await self.fully.playSound(media.uri, AUDIOMANAGER_STREAM_MUSIC)
        self._attr_current_media = media
        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time.time()
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def poll(self) -> None:
        """Poll player for state updates."""
        try:
            async with asyncio.timeout(15):
                await self.fully.getDeviceInfo()
                self._handle_player_update()
        except Exception as err:
            msg = f"Unable to start the FullyKiosk connection ({err!s}"
            raise PlayerUnavailableError(msg) from err
