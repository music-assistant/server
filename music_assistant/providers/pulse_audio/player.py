"""Local PulseAudio Player implementation."""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from music_assistant_models.enums import IdentifierType, PlayerFeature, PlayerType
from music_assistant_models.player import DeviceInfo

from music_assistant.helpers.process import check_output
from music_assistant.models.player import Player

import os
from .helpers import find_pactl, pactl_env

from .constants import (
    CONF_VOLUME_CONTROL,
    DEVICE_UUID_NAMESPACE,
    VOLUME_CONTROL_HARDWARE,
    VOLUME_CONTROL_SOFTWARE,
)

if TYPE_CHECKING:
    from .provider import LocalPulseAudioProvider


def get_sink_uuid(sink_name: str) -> str:
    """Generate a stable UUID for a PulseAudio sink from its internal name."""
    return str(uuid.uuid5(DEVICE_UUID_NAMESPACE, sink_name))


class LocalPulseAudioPlayer(Player):
    """Player for a PulseAudio sink (remap sink, combined sink, etc.)."""

    def __init__(
        self,
        provider: LocalPulseAudioProvider,
        player_id: str,
        display_name: str,
        pa_sink_name: str,
    ) -> None:
        super().__init__(provider, player_id)
        self._attr_type = PlayerType.PLAYER
        self._attr_name = display_name
        self._attr_available = True
        self._attr_supported_features = {
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
        }
        self._attr_device_info = DeviceInfo(
            model=display_name,
            manufacturer="PulseAudio",
        )
        self._attr_device_info.add_identifier(IdentifierType.UUID, player_id)
        self._attr_can_group_with = set()
        self._attr_volume_level = 100
        self._pa_sink_name = pa_sink_name
        self._hardware_volume_fallback = False

    @property
    def pa_sink_name(self) -> str:
        """Return the internal PulseAudio sink name."""
        return self._pa_sink_name

    @property
    def volume_control_mode(self) -> str:
        """Return the effective volume control mode."""
        if self._hardware_volume_fallback:
            return VOLUME_CONTROL_SOFTWARE
        return str(
            self._provider.config.get_value(CONF_VOLUME_CONTROL) or VOLUME_CONTROL_HARDWARE
        )

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command."""
        self._attr_volume_level = volume_level
        if self.volume_control_mode == VOLUME_CONTROL_HARDWARE:
            await self._set_pa_volume(volume_level)
        self.update_state()

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME_MUTE command."""
        self._attr_volume_muted = muted
        if self.volume_control_mode == VOLUME_CONTROL_HARDWARE:
            await self._set_pa_mute(muted)
        self.update_state()

async def _set_pa_volume(self, volume: int) -> None:
    """Set PulseAudio sink volume via pactl."""
    try:
        rc, _ = await check_output(
            find_pactl(), "set-sink-volume", self._pa_sink_name, f"{volume}%",
            env=pactl_env(),
        )
        if rc != 0:
            self.logger.warning("pactl volume failed for sink %s", self._pa_sink_name)
            self._hardware_volume_fallback = True
    except FileNotFoundError as err:
        self.logger.warning("pactl not found: %s", err)
        self._hardware_volume_fallback = True

async def _set_pa_mute(self, muted: bool) -> None:
    """Set PulseAudio sink mute via pactl."""
    try:
        rc, _ = await check_output(
            find_pactl(), "set-sink-mute", self._pa_sink_name, "1" if muted else "0",
            env=pactl_env(),
        )
        if rc != 0:
            self.logger.warning("pactl mute failed for sink %s", self._pa_sink_name)
            self._hardware_volume_fallback = True
    except FileNotFoundError as err:
        self.logger.warning("pactl not found: %s", err)
        self._hardware_volume_fallback = True
