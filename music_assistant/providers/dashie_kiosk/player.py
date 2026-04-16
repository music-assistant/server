"""Dashie Kiosk Player implementation."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from music_assistant_models.enums import IdentifierType, PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import PlayerUnavailableError

from music_assistant.constants import CONF_ENTRY_OUTPUT_CODEC_DEFAULT_MP3
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia

if TYPE_CHECKING:
    from typing import Any

    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType

    from .client import DashieKioskClient
    from .provider import DashieKioskProvider

AUDIOMANAGER_STREAM_MUSIC = 4


class DashieKioskPlayer(Player):
    """Dashie Kiosk Player implementation."""

    def __init__(
        self,
        provider: DashieKioskProvider,
        player_id: str,
        client: DashieKioskClient,
        address: str,
        dev_info: dict[str, Any] | None = None,
    ) -> None:
        """Initialize the Dashie Kiosk Player."""
        super().__init__(provider, player_id)
        self.client = client
        self._attr_type = PlayerType.PLAYER
        self._attr_supported_features = {
            PlayerFeature.VOLUME_SET,
            PlayerFeature.PAUSE,
            PlayerFeature.SEEK,
            PlayerFeature.PLAY_MEDIA,
        }
        self._attr_name = self.client.device_info.get("deviceName", "Dashie Kiosk")
        self._attr_device_info = DeviceInfo(
            model=(dev_info or {}).get(
                "model", self.client.device_info.get("deviceModel", "Android")
            ),
            manufacturer=(dev_info or {}).get("manufacturer", "Dashie"),
            software_version=(dev_info or {}).get("software_version"),
        )
        self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, address)
        self._attr_available = True
        self._attr_needs_poll = True
        self._attr_poll_interval = 10

    @property
    def requires_flow_mode(self) -> bool:
        """Return if the player requires flow mode."""
        return True

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return player-specific config entries."""
        return [CONF_ENTRY_OUTPUT_CODEC_DEFAULT_MP3]

    def set_attributes(self) -> None:
        """Set/update player attributes from device info."""
        info = self.client.device_info
        self._attr_name = info.get("deviceName", "Dashie Kiosk")
        volume = info.get("audioVolume")
        if volume is not None:
            self._attr_volume_level = int(volume)
        current_url = info.get("soundUrlPlaying", "")
        if not current_url:
            self._attr_playback_state = PlaybackState.IDLE
        self._attr_available = True

    async def volume_set(self, volume_level: int) -> None:
        """Set the volume level."""
        await self.client.set_audio_volume(volume_level, AUDIOMANAGER_STREAM_MUSIC)
        self._attr_volume_level = volume_level
        self.update_state()

    async def stop(self) -> None:
        """Stop playback."""
        await self.client.stop_sound()
        self._attr_playback_state = PlaybackState.IDLE
        self._attr_current_media = None
        self.update_state()

    async def play(self) -> None:
        """Resume playback."""
        await self.client.resume_sound()
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def pause(self) -> None:
        """Pause playback."""
        await self.client.pause_sound()
        self._attr_playback_state = PlaybackState.PAUSED
        self.update_state()

    async def seek(self, position: int) -> None:
        """Seek to a position (seconds)."""
        await self.client.seek_sound(position * 1000)
        self._attr_elapsed_time = float(position)
        self._attr_elapsed_time_last_updated = time.time()
        self.update_state()

    async def play_media(self, media: PlayerMedia) -> None:
        """Play media on the device."""
        url = await self.provider.mass.streams.resolve_stream_url(self.player_id, media)
        await self.client.play_sound(url, AUDIOMANAGER_STREAM_MUSIC)
        self._attr_current_media = media
        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time.time()
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def poll(self) -> None:
        """Poll player for state updates."""
        try:
            async with asyncio.timeout(15):
                await self.client.get_device_info()
                self.set_attributes()
                info = self.client.device_info
                position_ms = info.get("audioPosition")
                if position_ms is not None and self._attr_playback_state == PlaybackState.PLAYING:
                    self._attr_elapsed_time = float(position_ms) / 1000.0
                    self._attr_elapsed_time_last_updated = time.time()
                self.update_state()
        except Exception as err:
            msg = f"Unable to connect to Dashie Kiosk device: {err!s}"
            raise PlayerUnavailableError(msg) from err
