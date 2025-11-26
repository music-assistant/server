"""Nicovideo provider configuration system."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .categories import AuthConfigCategory
from .factory import get_config_entries_impl

if TYPE_CHECKING:
    from music_assistant.models.provider import Provider


class NicovideoConfig:
    """Configuration system for Nicovideo provider."""

    def __init__(self, provider: Provider) -> None:
        """Initialize with all category instances."""
        self.auth = AuthConfigCategory(provider)


__all__ = [
    "NicovideoConfig",
    "get_config_entries_impl",
]
