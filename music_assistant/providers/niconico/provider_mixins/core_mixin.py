"""Core mixin for Niconico music provider."""

from __future__ import annotations

from music_assistant.providers.niconico.adapter import NicoNicoMusicAssistantAdapter
from music_assistant.providers.niconico.provider_mixins.mixin_base import (
    NiconicoMusicProviderMixinBase,
)


class NiconicoMusicProviderCoreMixin(NiconicoMusicProviderMixinBase):
    """Core mixin for Niconico music provider."""

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        # For streaming providers return True here but for local file based providers return False.
        return True

    @property
    def niconico_adapter(self) -> NicoNicoMusicAssistantAdapter:
        """NiconicoMusicProviderProtocol implementation."""
        return self._niconico_adapter

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._niconico_adapter = NicoNicoMusicAssistantAdapter(self.provider)
        await self.niconico_adapter.auth.try_login()
        self.niconico_adapter.auth.start_periodic_relogin_task()

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        if hasattr(self, "_niconico_adapter") and self._niconico_adapter:
            # Stop the periodic relogin task
            self.niconico_adapter.auth.stop_periodic_relogin_task()
            # Logout from Niconico
            await self.niconico_adapter.auth.try_logout()
