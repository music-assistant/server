"""Core mixin for nicovideo music provider."""

from __future__ import annotations

from music_assistant_models.errors import LoginFailed

from music_assistant.providers.nicovideo.adapter import NicovideoMusicAssistantAdapter
from music_assistant.providers.nicovideo.provider_mixins.mixin_base import (
    NicovideoMusicProviderMixinBase,
)


class NicovideoMusicProviderCoreMixin(NicovideoMusicProviderMixinBase):
    """Core mixin for nicovideo music provider."""

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        # For streaming providers return True here but for local file based providers return False.
        return True

    @property
    def nicovideo_adapter(self) -> NicovideoMusicAssistantAdapter:
        """NicovideoMusicProviderProtocol implementation."""
        return self._nicovideo_adapter

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        try:
            self._nicovideo_adapter = NicovideoMusicAssistantAdapter(
                self.provider, self.nicovideo_config
            )

            # Check if login credentials are provided
            credentials = self.nicovideo_config.get_auth_credentials()
            has_credentials = bool(
                credentials.user_session or (credentials.username and credentials.password)
            )

            if has_credentials:
                # Try login if credentials are provided
                login_success = await self.nicovideo_adapter.auth.try_login()
                if not login_success:
                    raise LoginFailed("Login failed with provided credentials")
                self.nicovideo_adapter.auth.start_periodic_relogin_task()
                self.provider.logger.debug("nicovideo provider initialized successfully with login")
            else:
                # No credentials provided - initialize without login
                self.provider.logger.debug(
                    "nicovideo provider initialized successfully without login"
                )
        except Exception as err:
            self.provider.logger.error("Failed to initialize nicovideo provider: %s", err)
            raise

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        try:
            if hasattr(self, "_nicovideo_adapter") and self._nicovideo_adapter:
                # Stop the periodic relogin task
                self.nicovideo_adapter.auth.stop_periodic_relogin_task()
                # Logout from niconico
                await self.nicovideo_adapter.auth.try_logout()
                self.provider.logger.debug("nicovideo provider unloaded successfully")
        except Exception as err:
            self.provider.logger.warning("Error during nicovideo provider unload: %s", err)
