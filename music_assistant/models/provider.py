"""Model/base for a Provider implementation within Music Assistant."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, final

from music_assistant_models.errors import UnsupportedFeaturedException

from music_assistant.constants import CONF_LOG_LEVEL, MASS_LOGGER_NAME

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature, ProviderStage, ProviderType
    from music_assistant_models.provider import ProviderManifest
    from zeroconf import ServiceStateChange
    from zeroconf.asyncio import AsyncServiceInfo

    from music_assistant.mass import MusicAssistant


class Provider:
    """Base representation of a Provider implementation within Music Assistant."""

    mass: MusicAssistant
    manifest: ProviderManifest
    config: ProviderConfig

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature] | None = None,
    ) -> None:
        """Initialize MusicProvider."""
        self.mass = mass
        self.manifest = manifest
        self.config = config
        self._supported_features = supported_features or set()
        self._set_log_level_from_config(config)
        self.cache = mass.cache
        self.available = False

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        # should not be overridden in normal circumstances
        return self._supported_features

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        is_removed will be set to True when the provider is removed from the configuration.
        """

    async def update_config(self, config: ProviderConfig, changed_keys: set[str]) -> None:
        """
        Handle logic when the config is updated.

        Override this method in your provider implementation if you need
        to perform any additional setup logic after the provider is registered and
        the self.config was loaded, and whenever the config changes.

        The default implementation reloads the provider on any config change
        (except log-level-only changes), since provider reloads are lightweight
        and most providers cache config values at setup time.
        """
        # always update the stored config so dynamic reads pick up new values
        self.config = config

        # update log level if changed
        if f"values/{CONF_LOG_LEVEL}" in changed_keys or "name" in changed_keys:
            self._set_log_level_from_config(config)

        # reload if any non-log-level value keys changed
        value_keys_changed = {
            k for k in changed_keys if k.startswith("values/") and k != f"values/{CONF_LOG_LEVEL}"
        }
        if value_keys_changed:
            self.logger.info(
                "Config updated, reloading provider %s (instance_id=%s)",
                self.domain,
                self.instance_id,
            )
            task_id = f"provider_reload_{self.instance_id}"
            self.mass.call_later(1, self.mass.load_provider_config, config, task_id=task_id)

    async def on_mdns_service_state_change(
        self, name: str, state_change: ServiceStateChange, info: AsyncServiceInfo | None
    ) -> None:
        """Handle MDNS service state callback."""

    @property
    @final
    def type(self) -> ProviderType:
        """Return type of this provider."""
        return self.manifest.type

    @property
    @final
    def domain(self) -> str:
        """Return domain for this provider."""
        return self.manifest.domain

    @property
    @final
    def instance_id(self) -> str:
        """Return instance_id for this provider(instance)."""
        return self.config.instance_id

    @property
    @final
    def name(self) -> str:
        """Return (custom) friendly name for this provider instance."""
        if self.config.name:
            # always prefer user-set name from config
            return self.config.name
        return self.default_name

    @property
    @final
    def default_name(self) -> str:
        """Return a default friendly name for this provider instance."""
        # create default name based on instance count
        prov_confs = self.mass.config.get("providers", {}).values()
        instances = [x["instance_id"] for x in prov_confs if x["domain"] == self.domain]
        if len(instances) <= 1:
            # only one instance (or no instances yet at all) - return provider name
            return self.manifest.name
        instance_name_postfix = self.instance_name_postfix
        if not instance_name_postfix:
            # default implementation - simply use the instance number/index
            instance_name_postfix = str(instances.index(self.instance_id) + 1)
        # append instance name to provider name
        return f"{self.manifest.name} [{self.instance_name_postfix}]"

    @property
    def instance_name_postfix(self) -> str | None:
        """Return a (default) instance name postfix for this provider instance."""
        return None

    @property
    @final
    def stage(self) -> ProviderStage:
        """Return the stage of this provider."""
        return self.manifest.stage

    def unload_with_error(self, error: str) -> None:
        """Unload provider with error message."""
        self.mass.call_later(1, self.mass.unload_provider, self.instance_id, error)

    def to_dict(self) -> dict[str, Any]:
        """Return Provider(instance) as serializable dict."""
        return {
            "type": self.type.value,
            "domain": self.domain,
            "name": self.name,
            "default_name": self.default_name,
            "instance_name_postfix": self.instance_name_postfix,
            "instance_id": self.instance_id,
            "lookup_key": self.instance_id,  # include for backwards compatibility
            "supported_features": [x.value for x in self.supported_features],
            "available": self.available,
            "is_streaming_provider": getattr(self, "is_streaming_provider", None),
        }

    def supports_feature(self, feature: ProviderFeature) -> bool:
        """Return True if this provider supports the given feature."""
        return feature in self.supported_features

    def check_feature(self, feature: ProviderFeature) -> None:
        """Check if this provider supports the given feature."""
        if not self.supports_feature(feature):
            raise UnsupportedFeaturedException(
                f"Provider {self.name} does not support feature {feature.name}"
            )

    def _update_config_value(self, key: str, value: Any, encrypted: bool = False) -> None:
        """Update a config value."""
        self.mass.config.set_raw_provider_config_value(self.instance_id, key, value, encrypted)
        # also update the cached copy within the provider instance
        self.config.values[key].value = value

    def _set_log_level_from_config(self, config: ProviderConfig) -> None:
        """Set log level from config."""
        mass_logger = logging.getLogger(MASS_LOGGER_NAME)
        # self.name is only available after async_init. Otherwise we run into a race condition.
        # see https://github.com/music-assistant/support/issues/4801
        logging_name = self.domain
        if getattr(self, "available", False):
            # async_init completed
            logging_name = self.name
        self.logger = mass_logger.getChild(logging_name)
        log_level = str(config.get_value(CONF_LOG_LEVEL))
        if log_level == "GLOBAL":
            self.logger.setLevel(mass_logger.level)
        else:
            self.logger.setLevel(log_level)
        if logging.getLogger().level > self.logger.level:
            # if the root logger's level is higher, we need to adjust that too
            logging.getLogger().setLevel(self.logger.level)
        self.logger.debug("Log level configured to %s", log_level)
