"""
NicovideoMusicProviderCoreMixin: Core functionality not belonging to specific domains.

This mixin handles core functionality that doesn't belong to any specific feature area:
- Instance management (adapter, config)
- Authentication and session management
- Provider lifecycle management (initialization/cleanup)
- Basic provider properties
"""

from __future__ import annotations

from typing import Any, override

from music_assistant_models.errors import LoginFailed

from music_assistant.providers.nicovideo.adapter import NicovideoMusicAssistantAdapter
from music_assistant.providers.nicovideo.config import NicovideoConfig
from music_assistant.providers.nicovideo.provider_mixins.mixin_base import (
    NicovideoMusicProviderMixinBase,
)


class NicovideoMusicProviderCoreMixin(NicovideoMusicProviderMixinBase):
    """Core mixin handling instance management and provider lifecycle."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the core mixin."""
        super().__init__(*args, **kwargs)
        self._nicovideo_config = NicovideoConfig(self)
        self._nicovideo_adapter = NicovideoMusicAssistantAdapter(self, self.nicovideo_config)

    @property
    @override
    def nicovideo_config(self) -> NicovideoConfig:
        """Get the config helper instance."""
        return self._nicovideo_config

    @property
    @override
    def nicovideo_adapter(self) -> NicovideoMusicAssistantAdapter:
        """Get the nicovideo adapter instance."""
        return self._nicovideo_adapter

    @property
    @override
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        # For streaming providers return True here but for local file based providers return False.
        return True

    @override
    async def handle_async_init_for_mixin(self) -> None:
        """Handle async initialization of the provider."""
        try:
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
                self.logger.debug("nicovideo provider initialized successfully with login")
            else:
                # No credentials provided - initialize without login
                self.logger.debug("nicovideo provider initialized successfully without login")
        except Exception as err:
            self.logger.error("Failed to initialize nicovideo provider: %s", err)
            raise

    @override
    async def unload_for_mixin(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        try:
            if hasattr(self, "_nicovideo_adapter") and self._nicovideo_adapter:
                # Stop the periodic relogin task
                self.nicovideo_adapter.auth.stop_periodic_relogin_task()
                # Logout from niconico
                await self.nicovideo_adapter.auth.try_logout()
                self.logger.debug("nicovideo provider unloaded successfully")
        except Exception as err:
            self.logger.warning("Error during nicovideo provider unload: %s", err)
