"""Local PulseAudio Player implementation."""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from music_assistant_models.enums import IdentifierType, PlayerFeature, PlayerType
from music_assistant_models.player import DeviceInfo

from music_assistant.helpers.process import check_output
from music_assistant.models.player import Player

from .constants import (
    CONF_HARDWARE_VOLUME_CEILING,
    DEFAULT_HARDWARE_VOLUME_CEILING,
    DEVICE_UUID_NAMESPACE,
    VOLUME_CONTROL_SOFTWARE,
)
from .helpers import find_pactl, pactl_env

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
        self._attr_volume_level = 25
        self._pa_sink_name = pa_sink_name

    @property
    def pa_sink_name(self) -> str:
        """Return the internal PulseAudio sink name."""
        return self._pa_sink_name

    @property
    def volume_control_mode(self) -> str:
        """Always use software volume — hardware ceiling is set once on startup."""
        return VOLUME_CONTROL_SOFTWARE

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command."""
        self.logger.warning("volume_set called: %s", volume_level)
        self._attr_volume_level = volume_level
        self.update_state()

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME_MUTE command."""
        self._attr_volume_muted = muted
        self.update_state()

    async def apply_hardware_ceiling(self) -> None:
        """Set PA sink hardware volume ceiling via pactl.

        Called on every startup to ensure the hardware output level is
        capped at the configured ceiling percentage.
        """
        provider: LocalPulseAudioProvider = self.provider
        ceiling = provider.config.get_value(
            CONF_HARDWARE_VOLUME_CEILING, DEFAULT_HARDWARE_VOLUME_CEILING
        )
        sink_name = self._pa_sink_name
        try:
            await check_output(
                find_pactl(),
                "set-sink-volume",
                sink_name,
                f"{ceiling}%",
                env=pactl_env(),
            )
            provider.logger.debug(
                "Hardware ceiling set to %d%% for sink %s", ceiling, sink_name
            )
        except Exception as err:
            provider.logger.warning(
                "Failed to set hardware ceiling for sink %s: %s", sink_name, err
            )
