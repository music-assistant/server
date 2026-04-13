"""
NicovideoMusicProviderMixinBase: Interface definitions for _for_mixin patterns.

This abstract base class defines the common interface for all nicovideo provider mixins:
- Abstract properties for shared resources (config, adapter)
- _for_mixin method signatures for delegation patterns
- Default implementations returning None for optional functionality
"""

from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING

from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant.providers.nicovideo.config import NicovideoConfig
    from music_assistant.providers.nicovideo.services.manager import NicovideoServiceManager


class NicovideoMusicProviderMixinBase(MusicProvider):
    """Interface for _for_mixin delegation patterns."""

    @property
    @abstractmethod
    def nicovideo_config(self) -> NicovideoConfig:
        """Get the config helper instance."""

    @property
    @abstractmethod
    def service_manager(self) -> NicovideoServiceManager:
        """Get the nicovideo service manager instance."""

    async def handle_async_init_for_mixin(self) -> None:
        """Handle async initialization for this mixin."""

    async def unload_for_mixin(self, is_removed: bool = False) -> None:
        """Handle unload/close for this mixin."""
