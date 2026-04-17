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
    CONF_HARDWARE_VOLUME_CEILING,
    CONF_VOLUME_CONTROL,
    DEFAULT_HARDWARE_VOLUME_CEILING,
    DEVICE_UUID_NAMESPACE,
    VOLUME_CONTROL_HARDWARE,
    VOLUME_CONTROL_SOFTWARE,
)

if sys.platform == "darwin":
    from .coreaudio_volume import set_device_mute, set_device_volume
elif sys.platform == "linux":
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
        """
        Initialize the Local Audio player.

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
        self._attr_volume_level = 25 if (sys.platform == "linux" and pa_sink_name) else 100
        self._device_index = device_index
        self._pa_sink_name = pa_sink_name
        self._is_remap = is_remap
        # Set when hardware volume fails, causes automatic fallback to software
        self._hardware_volume_fallback = False

    @property
    def volume_control_mode(self) -> str:
        """Return the effective volume control mode for this player."""
        if self._hardware_volume_fallback:
            return VOLUME_CONTROL_SOFTWARE
        # On Linux with a PA sink, use software volume — hardware ceiling
        # is set once at startup via apply_hardware_ceiling()
        if sys.platform == "linux" and self._pa_sink_name:
            return VOLUME_CONTROL_SOFTWARE
        # On other platforms, volume control mode is configured at provider level
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
                if _PULSECTL_AVAILABLE and self._pa_sink_name:
                    loop = asyncio.get_running_loop()
                    ok = await loop.run_in_executor(
                        None, self._set_pulse_volume, self._pa_sink_name, volume
                    )
                    if not ok:
                        self.logger.warning(
                            "PulseAudio volume control failed for %s, falling back to software",
                            self._pa_sink_name,
                        )
                        self._hardware_volume_fallback = True
                else:
                    self.logger.warning(
                        "No PulseAudio sink available for %s, falling back to software",
                        self.name,
                    )
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
                if _PULSECTL_AVAILABLE and self._pa_sink_name:
                    loop = asyncio.get_running_loop()
                    ok = await loop.run_in_executor(
                        None, self._set_pulse_mute, self._pa_sink_name, muted
                    )
                    if not ok:
                        self.logger.warning(
                            "PulseAudio mute control failed for %s, falling back to software",
                            self._pa_sink_name,
                        )
                        self._hardware_volume_fallback = True
                else:
                    self._hardware_volume_fallback = True
            else:
                self._hardware_volume_fallback = True
        except FileNotFoundError:
            self.logger.warning("Mute control command not found, falling back to software")
            self._hardware_volume_fallback = True

    async def apply_hardware_ceiling(self) -> None:
        """Set PA sink hardware volume ceiling via pulsectl (Linux only).

        Called on every startup to cap the hardware output level at the
        configured ceiling percentage. Only applied to physical ALSA sinks —
        remap/filter sinks inherit the ceiling from their parent device.
        No-op on non-Linux, if no PA sink, or if this is a remap sink.
        """
        if not self._pa_sink_name or self._is_remap:
            return
        ceiling: int = int(
            self._provider.config.get_value(
                CONF_HARDWARE_VOLUME_CEILING, DEFAULT_HARDWARE_VOLUME_CEILING
            )
        )
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(
            None, self._set_pulse_volume, self._pa_sink_name, ceiling
        )
        if ok:
            self.logger.debug(
                "Hardware ceiling set to %d%% for sink %s", ceiling, self._pa_sink_name
            )
        else:
            self.logger.warning(
                "Failed to set hardware ceiling for sink %s", self._pa_sink_name
            )

    def _set_pulse_volume(self, pa_sink_name: str, volume: int) -> bool:
        """Set PulseAudio sink volume. Returns True on success.

        Intended to be called via run_in_executor.

        :param pa_sink_name: The PulseAudio sink name.
        :param volume: Volume level 0-100.
        """
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

    def _set_pulse_mute(self, pa_sink_name: str, muted: bool) -> bool:
        """Set PulseAudio sink mute state. Returns True on success.

        Intended to be called via run_in_executor.

        :param pa_sink_name: The PulseAudio sink name.
        :param muted: Whether to mute or unmute.
        """
        try:
            with pulsectl.Pulse("ma-local-audio") as pulse:
                for sink in pulse.sink_list():
                    if sink.name == pa_sink_name:
                        pulse.mute(sink, muted)
                        return True
            self.logger.warning("PA sink %s not found for mute control", pa_sink_name)
            return False
        except Exception as err:
            self.logger.warning("pulsectl mute error for %s: %s", pa_sink_name, err)
            return False
