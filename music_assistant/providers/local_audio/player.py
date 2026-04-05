"""Local Audio Player implementation."""

from __future__ import annotations

import asyncio
import sys
import uuid
from typing import TYPE_CHECKING

from music_assistant_models.enums import IdentifierType, PlayerFeature, PlayerType
from music_assistant_models.player import DeviceInfo

from music_assistant.helpers.process import check_output
from music_assistant.models.player import Player

from .constants import (
    CONF_VOLUME_CONTROL,
    DEVICE_UUID_NAMESPACE,
    VOLUME_CONTROL_HARDWARE,
    VOLUME_CONTROL_SOFTWARE,
)

if sys.platform == "darwin":
    from .coreaudio_volume import set_device_mute, set_device_volume

if TYPE_CHECKING:
    from .provider import LocalAudioProvider


def get_device_uuid(device_name: str, hostapi_index: int) -> str:
    """Generate a stable UUID for a local audio device.

    :param device_name: The device name reported by PortAudio.
    :param hostapi_index: The host API index (e.g. CoreAudio=0, ALSA=0).
    """
    return str(uuid.uuid5(DEVICE_UUID_NAMESPACE, f"{device_name}:{hostapi_index}"))


class LocalAudioPlayer(Player):
    """Player for a locally attached soundcard."""

    def __init__(
        self,
        provider: LocalAudioProvider,
        player_id: str,
        device_name: str,
        hostapi_index: int,
        device_index: int,
    ) -> None:
        """
        Initialize the Local Audio player.

        :param provider: The Local Audio provider instance.
        :param player_id: Stable player ID derived from device UUID.
        :param device_name: The device name reported by PortAudio.
        :param hostapi_index: The host API index.
        :param device_index: The PortAudio device index (maps to ALSA card on Linux).
        """
        super().__init__(provider, player_id)
        self._attr_type = PlayerType.PLAYER
        self._attr_name = device_name
        self._attr_available = True
        self._attr_supported_features = {
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
        }
        self._attr_device_info = DeviceInfo(
            model=device_name,
            manufacturer="Local Audio",
        )
        device_uuid = get_device_uuid(device_name, hostapi_index)
        self._attr_device_info.add_identifier(IdentifierType.UUID, device_uuid)
        self._attr_can_group_with = set()
        self._attr_volume_level = 100
        self._device_index = device_index
        # Set when hardware volume fails, causes automatic fallback to software
        self._hardware_volume_fallback = False

    @property
    def volume_control_mode(self) -> str:
        """Return the effective volume control mode for this player."""
        if self._hardware_volume_fallback:
            return VOLUME_CONTROL_SOFTWARE
        # Volume control mode is configured at provider level
        return str(self._provider.config.get_value(CONF_VOLUME_CONTROL) or VOLUME_CONTROL_HARDWARE)

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command."""
        self._attr_volume_level = volume_level
        if self.volume_control_mode == VOLUME_CONTROL_HARDWARE:
            await self._set_hardware_volume(volume_level)
        self.update_state()

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME_MUTE command."""
        self._attr_volume_muted = muted
        mode = self.volume_control_mode
        if mode == VOLUME_CONTROL_HARDWARE:
            await self._set_hardware_mute(muted)
        self.update_state()

    async def _set_hardware_volume(self, volume: int) -> None:
        """Set the OS-level volume for this device.

        :param volume: Volume level 0-100.
        """
        try:
            if sys.platform == "darwin":
                loop = asyncio.get_running_loop()
                ok = await loop.run_in_executor(None, set_device_volume, self.name, volume)
                if not ok:
                    self.logger.warning("CoreAudio volume control failed for %s", self.name)
                    self._hardware_volume_fallback = True
            elif sys.platform == "linux":
                # Use -c to target the specific ALSA card by index
                rc, _ = await check_output(
                    "amixer", "-c", str(self._device_index), "sset", "Master", f"{volume}%"
                )
                if rc != 0:
                    self.logger.warning("amixer volume failed for card %d", self._device_index)
                    self._hardware_volume_fallback = True
            else:
                self.logger.warning(
                    "Hardware volume not supported on %s, falling back to software",
                    sys.platform,
                )
                self._hardware_volume_fallback = True
        except FileNotFoundError:
            self.logger.warning("Volume control command not found, falling back to software")
            self._hardware_volume_fallback = True

    async def _set_hardware_mute(self, muted: bool) -> None:
        """Set the OS-level mute state for this device.

        :param muted: Whether to mute or unmute.
        """
        try:
            if sys.platform == "darwin":
                loop = asyncio.get_running_loop()
                ok = await loop.run_in_executor(None, set_device_mute, self.name, muted)
                if not ok:
                    self.logger.warning("CoreAudio mute control failed for %s", self.name)
                    self._hardware_volume_fallback = True
            elif sys.platform == "linux":
                toggle = "mute" if muted else "unmute"
                rc, _ = await check_output(
                    "amixer", "-c", str(self._device_index), "sset", "Master", toggle
                )
                if rc != 0:
                    self.logger.warning("amixer mute failed for card %d", self._device_index)
                    self._hardware_volume_fallback = True
            else:
                self._hardware_volume_fallback = True
        except FileNotFoundError:
            self.logger.warning("Mute control command not found, falling back to software")
            self._hardware_volume_fallback = True
