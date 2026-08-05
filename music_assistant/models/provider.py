"""Model/base for a Provider implementation within Music Assistant."""

from __future__ import annotations

import asyncio
import builtins
import logging
from typing import TYPE_CHECKING, Any, TypeVar, final, overload

from music_assistant_models.config_entries import ConfigValueType
from music_assistant_models.enums import ConfigEntryType, EventType
from music_assistant_models.errors import ActionUnavailable, UnsupportedFeaturedException

from music_assistant.constants import CONF_LOG_LEVEL, CONF_PROVIDERS, MASS_LOGGER_NAME

if TYPE_CHECKING:
    from async_upnp_client.utils import CaseInsensitiveDict
    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.enums import ProviderFeature, ProviderStage, ProviderType
    from music_assistant_models.provider import ProviderManifest
    from zeroconf import ServiceStateChange
    from zeroconf.asyncio import AsyncServiceInfo

    from music_assistant.helpers.json import SerializableType
    from music_assistant.mass import MusicAssistant

# TypeVar for config value type inference
_ConfigValueT = TypeVar("_ConfigValueT", bound=ConfigValueType)


class Provider:
    """Base representation of a Provider implementation within Music Assistant."""

    mass: MusicAssistant
    manifest: ProviderManifest
    config: ProviderConfig
    # set to True in providers that capture a mass.streams address or port while loading,
    # to have them reloaded onto the new one when the streamserver config changes
    reload_on_streams_network_change: bool = False

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
        self.initialized = asyncio.Event()

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        # should not be overridden in normal circumstances
        return self._supported_features

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """
        Return the (options) config entries to configure this provider instance.

        Called only for an existing (loaded) instance: read the current values via
        ``self.config``/``self.get_config_value`` and the capabilities via
        ``self.supported_features``. One-time setup input is collected by the setup flow
        (see ``setup_flow.py``), not here. Include ``ConfigEntryType.ACTION`` entries for
        one-shot buttons and handle their presses in ``handle_config_action``.
        """
        return ()

    async def handle_config_action(self, action: str) -> tuple[ConfigEntry, ...]:
        """
        Handle a one-shot action button press from this provider's options and re-render.

        Override to run the side effect for each ``ConfigEntryType.ACTION`` entry this
        provider declares, then return the (possibly refreshed) config entries to display.

        :param action: The action id of the pressed button (an entry's ``action`` key).
        """
        raise ActionUnavailable(f"Unknown action: {action}")

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

    async def get_diagnostics(self) -> dict[str, SerializableType] | None:
        """
        Return optional diagnostics info for this provider to include in diagnostics reports.

        Return None (the default) when this provider has nothing to contribute.
        Keep the returned data small, JSON serializable and free of sensitive values.
        """
        return None

    async def on_mdns_service_state_change(
        self, name: str, state_change: ServiceStateChange, info: AsyncServiceInfo | None
    ) -> None:
        """Handle MDNS service state callback."""

    async def on_upnp_service_discovered(
        self, search_target: str, discovery_info: CaseInsensitiveDict
    ) -> None:
        """Handle UPNP/SSDP discovery callback."""

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
    def translation_owner(self) -> str:
        """Return the "provider.<domain>" namespace this provider's translation strings resolve under."""
        return f"provider.{self.domain}"

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
        return f"{self.manifest.name} [{instance_name_postfix}]"

    @property
    def instance_name_postfix(self) -> str | None:
        """Return a (default) instance name postfix for this provider instance."""
        return None

    @property
    @final
    def stage(self) -> ProviderStage:
        """Return the stage of this provider."""
        return self.manifest.stage

    def unload_with_error(self, error: str | Exception) -> None:
        """
        Unload this provider and record an error for the user to act on.

        :param error: The originating exception (preferred, so its error code and localized
            message are preserved) or a plain string for a generic error message.
        """
        self.mass.call_later(1, self.mass.unload_provider_with_error, self.instance_id, error)

    def to_dict(self) -> dict[str, Any]:
        """Return Provider(instance) as serializable dict."""
        return {
            "type": self.type.value,
            "domain": self.domain,
            "name": self.name,
            "instance_id": self.instance_id,
            "supported_features": [x.value for x in self.supported_features],
            "available": self.available,
            "is_streaming_provider": getattr(self, "is_streaming_provider", None),
            "lookup_key": self.instance_id,  # include for backwards compatibility
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

    @final
    def signal_provider_event(self, data: SerializableType, sub_scope: str | None = None) -> None:
        """
        Signal a custom provider event to all subscribers (e.g. connected clients).

        Emits a PROVIDER_EVENT with this provider's instance_id as object_id,
        optionally suffixed with /sub_scope to allow clients to distinguish
        multiple event streams from the same provider.

        :param data: The JSON serializable event payload, defined by the provider.
        :param sub_scope: Optional sub scope to append to the object_id.
        """
        object_id = f"{self.instance_id}/{sub_scope}" if sub_scope else self.instance_id
        self.mass.signal_event(EventType.PROVIDER_EVENT, object_id=object_id, data=data)

    @overload
    def get_config_value(
        self, key: str, default: _ConfigValueT, *, return_type: builtins.type[_ConfigValueT] = ...
    ) -> _ConfigValueT: ...

    @overload
    def get_config_value(
        self, key: str, default: ConfigValueType = ..., *, return_type: builtins.type[_ConfigValueT]
    ) -> _ConfigValueT: ...

    @overload
    def get_config_value(
        self, key: str, default: ConfigValueType = ..., *, return_type: None = ...
    ) -> ConfigValueType: ...

    def get_config_value(
        self,
        key: str,
        default: ConfigValueType = None,
        *,
        return_type: builtins.type[_ConfigValueT | ConfigValueType] | None = None,
    ) -> _ConfigValueT | ConfigValueType:
        """
        Return the current persisted config value for this provider.

        Falls back to the active config entry value or default when no value is persisted.

        :param key: The config key to retrieve.
        :param default: Value to return when the key is not present in the active config.
        :param return_type: Optional type hint for type inference (e.g., str, int, bool).
            Note: This parameter is used purely for static type checking and does not
            perform runtime type validation. Callers are responsible for ensuring the
            specified type matches the actual config value type.
        """
        if (entry := self.config.values.get(key)) is None:
            return self.config.get_value(key, default)
        value = self.mass.config.get_raw_provider_config_value(self.instance_id, key)
        if value is None:
            return self.config.get_value(key, default)
        if entry.type == ConfigEntryType.SECURE_STRING:
            assert isinstance(value, str)
            return self.mass.config.decrypt_string(value)
        return value

    def get_setup_value(self, key: str, default: ConfigValueType = None) -> ConfigValueType:
        """
        Return a value collected by this provider's setup flow (from setup_data).

        Encrypted (string) values are decrypted transparently. When the key is not
        present in setup_data, the active config entry value or the given default is
        returned.

        :param key: The setup data key to retrieve.
        :param default: Value to return when the key is not present anywhere.
        """
        setup_data = self.mass.config.get(f"{CONF_PROVIDERS}/{self.instance_id}/setup_data") or {}
        if key in setup_data:
            value = setup_data[key]
            return self.mass.config.decrypt_string(value) if isinstance(value, str) else value
        return self.get_config_value(key, default)

    def _update_setup_data(self, key: str, value: ConfigValueType, immediate: bool = True) -> None:
        """
        Update a single setup_data value for this provider (e.g. a rotated auth token).

        :param key: The setup data key to update.
        :param value: The new value; strings are encrypted at rest.
        :param immediate: Persist to disk right away (the default) instead of on the
            debounced save timer, so a critical value survives a crash.
        """
        if not self.mass.config.get(f"{CONF_PROVIDERS}/{self.instance_id}"):
            # only allow setting setup data if the main config entry exists
            msg = f"Invalid provider instance: {self.instance_id}"
            raise KeyError(msg)
        stored_value = self.mass.config.encrypt_string(value) if isinstance(value, str) else value
        self.mass.config.set(
            f"{CONF_PROVIDERS}/{self.instance_id}/setup_data/{key}",
            stored_value,
            immediate=immediate,
        )
        # keep the in-memory config copy in sync with storage
        self.config.setup_data[key] = stored_value

    def _update_config_value(
        self, key: str, value: ConfigValueType, encrypted: bool = False, immediate: bool = False
    ) -> None:
        """
        Update a config value.

        :param immediate: Persist to disk right away instead of on the debounced save timer;
            use for critical values (e.g. a rotated auth token) that must survive a crash.
        """
        self.mass.config.set_raw_provider_config_value(
            self.instance_id, key, value, encrypted=encrypted, immediate=immediate
        )
        if (entry := self.config.values.get(key)) is not None:
            entry.value = self.mass.config.get_raw_provider_config_value(self.instance_id, key)

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
        # fall back to the entry's own default: a config that reaches us without its
        # entries resolved must not take the whole provider down over a log level
        log_level = str(config.get_value(CONF_LOG_LEVEL) or "GLOBAL")
        if log_level == "GLOBAL":
            self.logger.setLevel(mass_logger.level)
        else:
            self.logger.setLevel(log_level)
        self.logger.debug("Log level configured to %s", log_level)
