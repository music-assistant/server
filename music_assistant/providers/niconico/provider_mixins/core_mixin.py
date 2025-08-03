"""Core mixin for Niconico music provider."""

from __future__ import annotations

from music_assistant_models.errors import LoginFailed

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
        try:
            self._niconico_adapter = NicoNicoMusicAssistantAdapter(
                self.provider, self.niconico_config
            )

            # Check if login credentials are provided
            credentials = self.niconico_config.get_auth_credentials()
            has_credentials = bool(
                credentials.user_session or (credentials.username and credentials.password)
            )

            if has_credentials:
                # Try login if credentials are provided
                login_success = await self.niconico_adapter.auth.try_login()
                if not login_success:
                    raise LoginFailed("Login failed with provided credentials")
                self.niconico_adapter.auth.start_periodic_relogin_task()
                self.provider.logger.debug("NicoNico provider initialized successfully with login")
            else:
                # No credentials provided - initialize without login
                self.provider.logger.debug(
                    "NicoNico provider initialized successfully without login"
                )
        except Exception as err:
            self.provider.logger.error("Failed to initialize NicoNico provider: %s", err)
            raise

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        try:
            if hasattr(self, "_niconico_adapter") and self._niconico_adapter:
                # Stop the periodic relogin task
                self.niconico_adapter.auth.stop_periodic_relogin_task()
                # Logout from Niconico
                await self.niconico_adapter.auth.try_logout()
                self.provider.logger.debug("NicoNico provider unloaded successfully")
        except Exception as err:
            self.provider.logger.warning("Error during NicoNico provider unload: %s", err)
