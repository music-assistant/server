"""Local Audio Out player provider for Music Assistant."""

from __future__ import annotations

import ctypes
import sys

from music_assistant_models.errors import SetupFailedError

from music_assistant.models.player_provider import PlayerProvider

from .constants import (
    AUDIO_BACKEND_AUTO,
    AUDIO_BACKEND_PULSEAUDIO,
    CONF_AUDIO_BACKEND,
)
from .sendspin_bridge import LocalAudioBridgeManager


class LocalAudioProvider(PlayerProvider):
    """Player provider that exposes locally attached soundcards as Sendspin players."""

    _bridge_manager: LocalAudioBridgeManager

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        if sys.platform == "linux":
            configured_backend: str = str(
                self.config.get_value(CONF_AUDIO_BACKEND) or AUDIO_BACKEND_AUTO
            )
            needs_pulse = configured_backend in (AUDIO_BACKEND_PULSEAUDIO, AUDIO_BACKEND_AUTO)
            if needs_pulse:
                # Verify libpulse-simple is present before attempting PA output.
                # On AUTO we probe but don't hard-fail — bridge manager will fall
                # back to ALSA if pactl returns no sinks at enumeration time.
                try:
                    ctypes.CDLL("libpulse-simple.so.0")
                except OSError:
                    if configured_backend == AUDIO_BACKEND_PULSEAUDIO:
                        raise SetupFailedError(
                            "libpulse-simple.so.0 not found — is PulseAudio installed?"
                        ) from None
                    # AUTO: libpulse absent, bridge manager will use ALSA

        self._bridge_manager = LocalAudioBridgeManager(self)

    async def loaded_in_mass(self) -> None:
        """Handle provider fully loaded in Music Assistant."""
        await self._bridge_manager.discover_and_register()

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/removal of the provider."""
        if bridge_manager := getattr(self, "_bridge_manager", None):
            await bridge_manager.stop_all()

    def on_player_enabled(self, player_id: str) -> None:
        """
        Override to suppress re-discovery on player enable.

        ALSA devices are statically enumerated once at startup —
        there is no need to re-run discovery when a player is enabled.
        """
