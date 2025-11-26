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

from music_assistant.providers.nicovideo.config import NicovideoConfig
from music_assistant.providers.nicovideo.provider_mixins.base import (
    NicovideoMusicProviderMixinBase,
)
from music_assistant.providers.nicovideo.services.manager import NicovideoServiceManager


class NicovideoMusicProviderCoreMixin(NicovideoMusicProviderMixinBase):
    """Core mixin handling instance management and provider lifecycle."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the core mixin."""
        super().__init__(*args, **kwargs)
        self._nicovideo_config = NicovideoConfig(self)
        self._service_manager = NicovideoServiceManager(self, self.nicovideo_config)

    @property
    @override
    def nicovideo_config(self) -> NicovideoConfig:
        """Get the config helper instance."""
        return self._nicovideo_config

    @property
    @override
    def service_manager(self) -> NicovideoServiceManager:
        """Get the nicovideo service manager instance."""
        return self._service_manager

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
            has_credentials = bool(
                self.nicovideo_config.auth.user_session
                or (self.nicovideo_config.auth.mail and self.nicovideo_config.auth.password)
            )

            if has_credentials:
                # Try login if credentials are provided
                login_success = await self.service_manager.auth.try_login()
                if not login_success:
                    raise LoginFailed("Login failed with provided credentials")
                self.service_manager.auth.start_periodic_relogin_task()
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
            # Stop the periodic relogin task
            self.service_manager.auth.stop_periodic_relogin_task()
            self.logger.debug("nicovideo provider unloaded successfully")
        except Exception as err:
            self.logger.warning("Error during nicovideo provider unload: %s", err)
