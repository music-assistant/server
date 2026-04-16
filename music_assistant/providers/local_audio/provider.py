"""Local Audio Out player provider for Music Assistant."""

from __future__ import annotations

import sys

from music_assistant.models.player_provider import PlayerProvider

from .sendspin_bridge import LocalAudioBridgeManager


class LocalAudioProvider(PlayerProvider):
    """Player provider that exposes locally attached soundcards as Sendspin players."""

    _bridge_manager: LocalAudioBridgeManager

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        if sys.platform == "linux":
            # Verify libpulse-simple is present before we try to do anything
            try:
                import ctypes
                ctypes.CDLL("libpulse-simple.so.0")
            except OSError as err:
                raise RuntimeError(
                    "libpulse-simple.so.0 not found — is PulseAudio installed?"
                ) from err

        self._bridge_manager = LocalAudioBridgeManager(self)

    async def loaded_in_mass(self) -> None:
        """Handle provider fully loaded in Music Assistant."""
        await self._bridge_manager.discover_and_register()

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/removal of the provider."""
        if bridge_manager := getattr(self, "_bridge_manager", None):
            await bridge_manager.stop_all()

    async def discover_players(self) -> None:
        """Discover players (re-enumerate soundcards)."""
        await self._bridge_manager.discover_and_register()
