"""Model/base for a Provider implementation within Music Assistant."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from music_assistant.constants import CONF_LOG_LEVEL, ROOT_LOGGER_NAME

if TYPE_CHECKING:
    from zeroconf import ServiceStateChange
    from zeroconf.asyncio import AsyncServiceInfo

    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.enums import ProviderFeature, ProviderType
    from music_assistant.common.models.provider import ProviderInstance, ProviderManifest
    from music_assistant.server import MusicAssistant


class Provider:
    """Base representation of a Provider implementation within Music Assistant."""

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize MusicProvider."""
        self.mass = mass
        self.manifest = manifest
        self.config = config
        mass_logger = logging.getLogger(ROOT_LOGGER_NAME)
        self.logger = mass_logger.getChild(f"providers.{self.domain}")
        self.log_level = log_level = config.get_value(CONF_LOG_LEVEL)
        if log_level == "GLOBAL":
            self.logger.setLevel(mass_logger.level)
        else:
            self.logger.setLevel("DEBUG" if log_level == "VERBOSE" else log_level)
            # if the root logger's level is higher, we need to adjust that too
            if logging.getLogger().level > self.logger.level:
                logging.getLogger().setLevel(self.logger.level)
        self.logger.debug("Log level configured to %s", log_level)
        self.cache = mass.cache
        self.available = False

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return ()

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""

    async def unload(self) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        """

    async def on_mdns_service_state_change(
        self, name: str, state_change: ServiceStateChange, info: AsyncServiceInfo | None
    ) -> None:
        """Handle MDNS service state callback."""

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

    def to_dict(self, *args, **kwargs) -> ProviderInstance:
        """Return Provider(instance) as serializable dict."""
        return {
            "type": self.type.value,
            "domain": self.domain,
            "name": self.name,
            "instance_id": self.instance_id,
            "supported_features": [x.value for x in self.supported_features],
            "available": self.available,
        }
