"""Model/base for a Provider implementation within Music Assistant."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from music_assistant.common.models.config_entries import ProviderConfig
from music_assistant.common.models.enums import ProviderFeature, ProviderType
from music_assistant.common.models.provider import ProviderInstance, ProviderManifest
from music_assistant.constants import CONF_LOG_LEVEL, ROOT_LOGGER_NAME

if TYPE_CHECKING:
    from music_assistant.server import MusicAssistant

# noqa: ARG001


class Provider:
    """Base representation of a Provider implementation within Music Assistant."""

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize MusicProvider."""
        self.mass = mass
        self.manifest = manifest
        self.config = config
        self.logger = logging.getLogger(f"{ROOT_LOGGER_NAME}.providers.{self.instance_id}")
        log_level = config.get_value(CONF_LOG_LEVEL)
        if log_level != "GLOBAL":
            self.logger.setLevel(log_level)
        self.cache = mass.cache
        self.available = False

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return tuple()

    async def unload(self) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        """

    @property
    def type(self) -> ProviderType:
        """Return type of this provider."""
        return self.manifest.type

    @property
    def domain(self) -> str:
        """Return domain for this provider."""
        return self.manifest.domain

    @property
    def instance_id(self) -> str:
        """Return instance_id for this provider(instance)."""
        return self.config.instance_id

    @property
    def name(self) -> str:
        """Return (custom) friendly name for this provider instance."""
        if self.config.name:
            return self.config.name
        inst_count = len([x for x in self.mass.music.providers if x.domain == self.domain])
        if inst_count > 1:
            postfix = self.instance_id[:-8]
            return f"{self.manifest.name}.{postfix}"
        return self.manifest.name

    def to_dict(self, *args, **kwargs) -> ProviderInstance:  # noqa: ARG002
        """Return Provider(instance) as serializable dict."""
        return {
            "type": self.type.value,
            "domain": self.domain,
            "name": self.name,
            "instance_id": self.instance_id,
            "supported_features": [x.value for x in self.supported_features],
            "available": self.available,
        }
