"""Local Audio Out player provider for Music Assistant."""

from __future__ import annotations

from music_assistant.models.player_provider import PlayerProvider

from .sendspin_bridge import LocalAudioBridgeManager


class LocalAudioProvider(PlayerProvider):
    """Player provider that exposes locally attached soundcards as Sendspin players."""

    _bridge_manager: LocalAudioBridgeManager

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._bridge_manager = LocalAudioBridgeManager(self)

    async def loaded_in_mass(self) -> None:
        """Handle provider fully loaded in Music Assistant."""
        await self._bridge_manager.discover_and_register()

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/removal of the provider."""
        bridge_manager = getattr(self, "_bridge_manager", None)
        if bridge_manager:
            await bridge_manager.stop_all()

    async def discover_players(self) -> None:
        """Discover players (re-enumerate soundcards)."""
        await self._bridge_manager.discover_and_register()
