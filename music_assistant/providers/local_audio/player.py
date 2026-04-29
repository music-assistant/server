"""Local Audio Player implementation."""

from __future__ import annotations

import asyncio
import sys
import uuid
from typing import TYPE_CHECKING

from music_assistant_models.enums import IdentifierType, PlayerFeature, PlayerType
from music_assistant_models.player import DeviceInfo

from music_assistant.models.player import Player

from .constants import (
    CACHE_CATEGORY_PREV_STATE,
    DEFAULT_PLAYER_VOLUME,
    DEVICE_UUID_NAMESPACE,
)

if sys.platform == "linux":
    try:
        import pulsectl

        _PULSECTL_AVAILABLE = True
    except ImportError:
        _PULSECTL_AVAILABLE = False

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
        pa_sink_name: str | None = None,
        is_remap: bool = False,
    ) -> None:
        """Initialize the Local Audio player.

        :param provider: The Local Audio provider instance.
        :param player_id: Stable player ID derived from device UUID.
        :param device_name: The device name reported by PortAudio.
        :param hostapi_index: The host API index.
        :param device_index: The PortAudio device index (maps to ALSA card on Linux).
        :param pa_sink_name: The PulseAudio sink name for this device (Linux only).
        :param is_remap: True if this is a remap/filter sink (not a physical ALSA sink).
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
        self._attr_volume_level = DEFAULT_PLAYER_VOLUME
        self._device_index = device_index
        self._pa_sink_name = pa_sink_name
        self._is_remap = is_remap

    async def restore_state(self) -> None:
        """Restore cached volume/mute state from a previous session."""
        if last_state := await self.mass.cache.get(
            key=self.player_id,
            provider=self._provider.instance_id,
            category=CACHE_CATEGORY_PREV_STATE,
        ):
            self._attr_volume_muted = last_state[0]
            self._attr_volume_level = last_state[1]
        else:
            self._attr_volume_muted = False
            self._attr_volume_level = DEFAULT_PLAYER_VOLUME

    async def _save_state(self) -> None:
        """Persist current volume/mute state to cache."""
        await self.mass.cache.set(
            key=self.player_id,
            data=[self._attr_volume_muted, self._attr_volume_level],
            provider=self._provider.instance_id,
            category=CACHE_CATEGORY_PREV_STATE,
        )

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command."""
        self._attr_volume_level = volume_level
        await self._save_state()
        self.update_state()

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME_MUTE command."""
        self._attr_volume_muted = muted
        await self._save_state()
        self.update_state()

    async def apply_hardware_ceiling(self) -> None:
        """Set PA sink hardware volume to 100% via pulsectl (Linux only).

        Ensures the PA sink is at full hardware volume so that software
        volume scaling in the bridge has full dynamic range to work with.
        No-op on non-Linux or if no PA sink.
        """
        if sys.platform != "linux" or not self._pa_sink_name:
            return
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(None, self._set_pulse_volume, self._pa_sink_name, 100)
        if ok:
            self.logger.debug("PA sink %s set to 100%% hardware volume", self._pa_sink_name)
        else:
            self.logger.warning("Failed to set hardware volume for sink %s", self._pa_sink_name)

    def _set_pulse_volume(self, pa_sink_name: str, volume: int) -> bool:
        """Set PulseAudio sink volume. Returns True on success.

        Intended to be called via run_in_executor.

        :param pa_sink_name: The PulseAudio sink name.
        :param volume: Volume level 0-100.
        """
        if not _PULSECTL_AVAILABLE:
            return False
        try:
            with pulsectl.Pulse("ma-local-audio") as pulse:
                for sink in pulse.sink_list():
                    if sink.name == pa_sink_name:
                        pulse.volume_set_all_chans(sink, volume / 100.0)
                        return True
            self.logger.warning("PA sink %s not found for volume control", pa_sink_name)
            return False
        except Exception as err:
            self.logger.warning("pulsectl volume error for %s: %s", pa_sink_name, err)
            return False
