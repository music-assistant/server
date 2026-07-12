"""Provider configuration handling for the ConfigController."""

from __future__ import annotations

import builtins
import logging
from typing import TYPE_CHECKING, Any, cast, overload

import shortuuid
from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueType,
    ProviderConfig,
    ProviderError,
)
from music_assistant_models.enums import (
    EventType,
    ProviderFeature,
    ProviderType,
)

from music_assistant.constants import (
    CONF_ENTRY_LIBRARY_SYNC_ALBUM_TRACKS,
    CONF_ENTRY_LIBRARY_SYNC_ALBUMS,
    CONF_ENTRY_LIBRARY_SYNC_ARTISTS,
    CONF_ENTRY_LIBRARY_SYNC_AUDIOBOOKS,
    CONF_ENTRY_LIBRARY_SYNC_BACK,
    CONF_ENTRY_LIBRARY_SYNC_DELETIONS,
    CONF_ENTRY_LIBRARY_SYNC_PLAYLIST_TRACKS,
    CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS,
    CONF_ENTRY_LIBRARY_SYNC_PODCASTS,
    CONF_ENTRY_LIBRARY_SYNC_RADIOS,
    CONF_ENTRY_LIBRARY_SYNC_TRACKS,
    CONF_PLAYERS,
    CONF_PROVIDERS,
    DEFAULT_PROVIDER_CONFIG_ENTRIES,
)
from music_assistant.controllers.config.constants import BASE_KEYS, _ConfigValueT
from music_assistant.controllers.config.helpers import (
    _provider_status,
    _with_translation_owner,
)
from music_assistant.helpers.api import api_command
from music_assistant.helpers.util import load_provider_module
from music_assistant.models import ProviderModuleType
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant import MusicAssistant


LOGGER = logging.getLogger(__name__)


class ProviderConfigMixin:
    """Mixin providing provider configuration handling for the ConfigController."""

    # Type hints for attributes/methods provided by the class this mixin is used with
    if TYPE_CHECKING:
        mass: MusicAssistant

        @property
        def onboard_done(self) -> bool: ...  # noqa: D102

        def get(self, key: str, default: Any = None) -> Any: ...  # noqa: D102

        def set(self, key: str, value: Any, immediate: bool = False) -> None: ...  # noqa: D102

        def set_default(self, key: str, default_value: Any) -> None: ...  # noqa: D102

        def remove(self, key: str) -> None: ...  # noqa: D102

        def encrypt_string(self, str_value: str) -> str: ...  # noqa: D102

        async def set_onboard_complete(self) -> None: ...  # noqa: D102

    @api_command("config/providers", required_scope=Scope.CONFIG_PROVIDERS_READ)
    async def get_provider_configs(
        self,
        provider_type: ProviderType | None = None,
        provider_domain: str | None = None,
        include_values: bool = False,
    ) -> list[ProviderConfig]:
        """Return all known provider configurations, optionally filtered by ProviderType."""
        raw_values = self.get(CONF_PROVIDERS, {})
        prov_entries = {x.domain for x in self.mass.get_provider_manifests()}
        configs: list[ProviderConfig] = []
        for prov_conf in raw_values.values():
            if provider_type is not None and prov_conf["type"] != provider_type:
                continue
            if provider_domain is not None and prov_conf["domain"] != provider_domain:
                continue
            # guard for deleted providers
            if prov_conf["domain"] not in prov_entries:
                continue
            if include_values:
                # get_provider_config already stamps the derived status
                configs.append(await self.get_provider_config(prov_conf["instance_id"]))
                continue
            conf = cast("ProviderConfig", ProviderConfig.parse([], prov_conf))
            is_loaded = (
                self.mass.get_provider(conf.instance_id, return_unavailable=True) is not None
            )
            conf.status = _provider_status(conf, is_loaded)
            configs.append(conf)
        return configs

    @api_command("config/providers/get", required_scope=Scope.CONFIG_PROVIDERS_READ)
    async def get_provider_config(self, instance_id: str) -> ProviderConfig:
        """Return configuration for a single provider."""
        if raw_conf := self.get(f"{CONF_PROVIDERS}/{instance_id}", {}):
            config_entries = await self.get_provider_config_entries(
                raw_conf["domain"],
                instance_id=instance_id,
                values=raw_conf.get("values"),
            )
            for prov in self.mass.get_provider_manifests():
                if prov.domain == raw_conf["domain"]:
                    break
            else:
                msg = f"Unknown provider domain: {raw_conf['domain']}"
                raise KeyError(msg)
            conf = cast("ProviderConfig", ProviderConfig.parse(config_entries, raw_conf))
            is_loaded = self.mass.get_provider(instance_id, return_unavailable=True) is not None
            conf.status = _provider_status(conf, is_loaded)
            return conf
        msg = f"No config found for provider id {instance_id}"
        raise KeyError(msg)

    @overload
    async def get_provider_config_value(
        self,
        instance_id: str,
        key: str,
        *,
        default: _ConfigValueT,
        return_type: type[_ConfigValueT] = ...,
    ) -> _ConfigValueT: ...

    @overload
    async def get_provider_config_value(
        self,
        instance_id: str,
        key: str,
        *,
        default: ConfigValueType = ...,
        return_type: type[_ConfigValueT] = ...,
    ) -> _ConfigValueT: ...

    @overload
    async def get_provider_config_value(
        self,
        instance_id: str,
        key: str,
        *,
        default: ConfigValueType = ...,
        return_type: None = ...,
    ) -> ConfigValueType: ...

    @api_command("config/providers/get_value", required_scope=Scope.CONFIG_PROVIDERS_READ)
    async def get_provider_config_value(
        self,
        instance_id: str,
        key: str,
        *,
        default: ConfigValueType = None,
        return_type: type[_ConfigValueT | ConfigValueType] | None = None,
    ) -> _ConfigValueT | ConfigValueType:
        """
        Return single configentry value for a provider.

        :param instance_id: The provider instance ID.
        :param key: The config key to retrieve.
        :param default: Optional default value to return if key is not found.
        :param return_type: Optional type hint for type inference (e.g., str, int, bool).
            Note: This parameter is used purely for static type checking and does not
            perform runtime type validation. Callers are responsible for ensuring the
            specified type matches the actual config value type.
        """
        # prefer stored value so we don't have to retrieve all config entries every time
        if (raw_value := self.get_raw_provider_config_value(instance_id, key)) is not None:
            return raw_value
        conf = await self.get_provider_config(instance_id)
        if key not in conf.values:
            if default is not None:
                return default
            msg = f"Config key {key} not found for provider {instance_id}"
            raise KeyError(msg)
        return (
            conf.values[key].value
            if conf.values[key].value is not None
            else conf.values[key].default_value
        )

    @api_command("config/providers/get_entries", required_scope=Scope.CONFIG_PROVIDERS_READ)
    async def get_provider_config_entries(
        self,
        provider_domain: str,
        instance_id: str | None = None,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """
        Return Config entries to setup/configure a provider.

        provider_domain: (mandatory) domain of the provider.
        instance_id: id of an existing provider instance (None for new instance setup).
        action: [optional] action key called from config entries UI.
        values: the (intermediate) raw values for config entries sent with the action.
        """
        # lookup provider manifest and module
        prov_mod: ProviderModuleType | None
        for manifest in self.mass.get_provider_manifests():
            if manifest.domain == provider_domain:
                try:
                    prov_mod = await load_provider_module(provider_domain, manifest.requirements)
                except Exception as e:
                    msg = f"Failed to load provider module for {provider_domain}: {e}"
                    LOGGER.exception(msg)
                    return []
                break
        else:
            msg = f"Unknown provider domain: {provider_domain}"
            LOGGER.exception(msg)
            return []

        # add dynamic optional config entries that depend on features
        if instance_id and (provider := self.mass.get_provider(instance_id)):
            supported_features = provider.supported_features
        else:
            provider = None
            supported_features = getattr(prov_mod, "SUPPORTED_FEATURES", set())
        extra_entries = self._build_sync_entries(manifest, supported_features, provider)

        all_entries = [
            *DEFAULT_PROVIDER_CONFIG_ENTRIES,
            *extra_entries,
            *await prov_mod.get_config_entries(
                self.mass, instance_id=instance_id, action=action, values=values
            ),
        ]
        return _with_translation_owner(all_entries, f"provider.{provider_domain}", action, values)

    @api_command("config/providers/save", required_scope=Scope.CONFIG_PROVIDERS_WRITE)
    async def save_provider_config(
        self,
        provider_domain: str,
        values: dict[str, ConfigValueType],
        instance_id: str | None = None,
    ) -> ProviderConfig:
        """
        Save Provider(instance) Config.

        provider_domain: (mandatory) domain of the provider.
        values: the raw values for config entries that need to be stored/updated.
        instance_id: id of an existing provider instance (None for new instance setup).
        """
        if instance_id is not None:
            config = await self._update_provider_config(instance_id, values)
        else:
            config = await self._add_provider_config(provider_domain, values)
        # return full config, just in case
        return await self.get_provider_config(config.instance_id)

    @api_command("config/providers/remove", required_scope=Scope.CONFIG_PROVIDERS_WRITE)
    async def remove_provider_config(self, instance_id: str) -> None:
        """Remove ProviderConfig."""
        conf_key = f"{CONF_PROVIDERS}/{instance_id}"
        existing = self.get(conf_key)
        if not existing:
            msg = f"Provider {instance_id} does not exist"
            raise KeyError(msg)
        prov_manifest = self.mass.get_provider_manifest(existing["domain"])
        if prov_manifest.builtin:
            msg = f"Builtin provider {prov_manifest.name} can not be removed."
            raise RuntimeError(msg)
        self.remove(conf_key)
        await self.mass.unload_provider(instance_id, True)
        if existing["type"] == "music":
            # cleanup entries in library
            await self.mass.music.cleanup_provider(instance_id)
        if existing["type"] == "player":
            # all players should already be removed by now through unload_provider
            for player in list(self.mass.players):
                if player.provider.instance_id != instance_id:
                    continue
                self.mass.players.delete_player_config(player.player_id)
            # cleanup remaining player configs
            for key, player_conf in list(self.get(CONF_PLAYERS, {}).items()):
                if not isinstance(player_conf, dict):
                    continue
                if player_conf.get("provider") == instance_id:
                    self.remove(f"{CONF_PLAYERS}/{player_conf.get('player_id') or key}")

    async def remove_provider_config_value(self, instance_id: str, key: str) -> None:
        """Remove/reset single Provider config value."""
        conf_key = f"{CONF_PROVIDERS}/{instance_id}/values/{key}"
        existing = self.get(conf_key)
        if not existing:
            return
        self.remove(conf_key)

    def set_provider_default_name(self, instance_id: str, default_name: str) -> None:
        """Set (or update) the default name for a provider."""
        conf_key = f"{CONF_PROVIDERS}/{instance_id}/default_name"
        self.set(conf_key, default_name)

    def update_provider_last_error(self, instance_id: str, error: ProviderError | None) -> None:
        """
        Persist (or clear) a provider's last_error.

        Only writes if the provider config still exists; this avoids re-creating a
        config entry that was removed while a load was still in flight, which would
        leave a stub entry without a domain. See #5728.
        """
        conf_key = f"{CONF_PROVIDERS}/{instance_id}"
        if not self.get(conf_key):
            return
        self.set(f"{conf_key}/last_error", error.to_dict() if error else None)

    async def create_builtin_provider_config(self, provider_domain: str) -> None:
        """
        Create builtin ProviderConfig.

        This is meant as helper to create default configs for builtin/default providers.
        Called by the server initialization code which load all providers at startup.
        """
        for _ in await self.get_provider_configs(provider_domain=provider_domain):
            # return if there is already any config
            return
        for prov in self.mass.get_provider_manifests():
            if prov.domain == provider_domain:
                manifest = prov
                break
        else:
            msg = f"Unknown provider domain: {provider_domain}"
            raise KeyError(msg)
        config_entries = await self.get_provider_config_entries(provider_domain)
        if manifest.multi_instance:
            instance_id = f"{manifest.domain}--{shortuuid.random(8)}"
        else:
            instance_id = manifest.domain
        default_config = cast(
            "ProviderConfig",
            ProviderConfig.parse(
                config_entries,
                {
                    "type": manifest.type.value,
                    "domain": manifest.domain,
                    "instance_id": instance_id,
                    "name": manifest.name,
                    # note: this will only work for providers that do
                    # not have any required config entries or provide defaults
                    "values": {},
                },
            ),
        )
        default_config.validate()
        conf_key = f"{CONF_PROVIDERS}/{default_config.instance_id}"
        self.set_default(conf_key, default_config.to_raw())

    if TYPE_CHECKING:
        # Overload for when default is provided - return type matches default type
        @overload
        def get_raw_provider_config_value(
            self, provider_instance: str, key: str, default: _ConfigValueT
        ) -> _ConfigValueT: ...

        # Overload for when no default is provided - return ConfigValueType | None
        @overload
        def get_raw_provider_config_value(
            self, provider_instance: str, key: str, default: None = None
        ) -> ConfigValueType | None: ...

    def get_raw_provider_config_value(
        self, provider_instance: str, key: str, default: ConfigValueType = None
    ) -> ConfigValueType:
        """
        Return (raw) single config(entry) value for a provider.

        Note that this only returns the stored value without any validation or default.
        """
        return cast(
            "ConfigValueType",
            self.get(
                f"{CONF_PROVIDERS}/{provider_instance}/values/{key}",
                self.get(f"{CONF_PROVIDERS}/{provider_instance}/{key}", default),
            ),
        )

    def set_raw_provider_config_value(
        self,
        provider_instance: str,
        key: str,
        value: ConfigValueType,
        encrypted: bool = False,
        immediate: bool = False,
    ) -> None:
        """
        Set (raw) single config(entry) value for a provider.

        Note that this only stores the (raw) value without any validation or default.
        When immediate is set the value is flushed to disk right away instead of on the
        debounced save timer, so a critical value (e.g. a rotated auth token) is not lost
        if the process is killed within the debounce window.
        """
        if not self.get(f"{CONF_PROVIDERS}/{provider_instance}"):
            # only allow setting raw values if main entry exists
            msg = f"Invalid provider_instance: {provider_instance}"
            raise KeyError(msg)
        if encrypted:
            if not isinstance(value, str):
                msg = f"Cannot encrypt non-string value for key {key}"
                raise ValueError(msg)
            value = self.encrypt_string(value)
        if key in BASE_KEYS:
            self.set(f"{CONF_PROVIDERS}/{provider_instance}/{key}", value, immediate=immediate)
            return
        self.set(f"{CONF_PROVIDERS}/{provider_instance}/values/{key}", value, immediate=immediate)
        # also update the loaded provider's in-place config copy so object-local value
        # reads stay in sync with raw writes; include unavailable instances, since values
        # like a rotated auth token can be written while the provider is temporarily
        # unavailable and its copy must not lag behind the stored value
        if (provider := self.mass.get_provider(provider_instance, return_unavailable=True)) and (
            entry := provider.config.values.get(key)
        ):
            entry.value = value

    @api_command("config/providers/reload", required_scope=Scope.CONFIG_PROVIDERS_WRITE)
    async def _reload_provider(self, instance_id: str) -> None:
        """Reload provider."""
        try:
            config = await self.get_provider_config(instance_id)
        except KeyError:
            # Edge case: Provider was removed before we could reload it
            return
        await self.mass.load_provider_config(config)

    async def _update_provider_config(
        self, instance_id: str, values: dict[str, ConfigValueType]
    ) -> ProviderConfig:
        """Update ProviderConfig."""
        config = await self.get_provider_config(instance_id)
        changed_keys = config.update(values)
        prov_instance = self.mass.get_provider(instance_id)
        available = prov_instance.available if prov_instance else False
        if not changed_keys and (config.enabled == available):
            # no changes
            return config
        # validate the new config
        config.validate()
        # save the config first to prevent issues when the
        # provider wants to manipulate the config during load
        conf_key = f"{CONF_PROVIDERS}/{config.instance_id}"
        raw_conf = config.to_raw()
        self.set(conf_key, raw_conf)
        if config.enabled and prov_instance and available:
            # update config for existing/loaded provider instance
            await prov_instance.update_config(config, changed_keys)
            # push instance name to config (to persist it if it was autogenerated)
            if prov_instance.default_name != config.default_name:
                self.set_provider_default_name(
                    prov_instance.instance_id, prov_instance.default_name
                )
            if "name" in changed_keys:
                # signal providers updated so frontends refresh the provider name
                self.mass.signal_event(EventType.PROVIDERS_UPDATED, data=self.mass.get_providers())
        elif config.enabled:
            # provider is enabled but not available, try to load it
            await self.mass.load_provider_config(config)
        else:
            # disable provider
            prov_manifest = self.mass.get_provider_manifest(config.domain)
            if not prov_manifest.allow_disable:
                msg = "Provider can not be disabled."
                raise RuntimeError(msg)
            # also unload any other providers dependent of this provider
            for dep_prov in self.mass.providers:
                if dep_prov.manifest.depends_on == config.domain:
                    await self.mass.unload_provider(dep_prov.instance_id)
            await self.mass.unload_provider(config.instance_id)
            # For player providers, unload_provider should have removed all its players by now
        return config

    async def _add_provider_config(
        self,
        provider_domain: str,
        values: dict[str, ConfigValueType],
    ) -> ProviderConfig:
        """
        Add new Provider (instance).

        params:
        - provider_domain: domain of the provider for which to add an instance of.
        - values: the raw values for config entries.

        Returns: newly created ProviderConfig.
        """
        # lookup provider manifest and module
        for prov in self.mass.get_provider_manifests():
            if prov.domain == provider_domain:
                manifest = prov
                break
        else:
            msg = f"Unknown provider domain: {provider_domain}"
            raise KeyError(msg)
        if prov.depends_on:
            dep_configs = await self.get_provider_configs(provider_domain=prov.depends_on)
            if not any(dep_conf.enabled for dep_conf in dep_configs):
                msg = f"Provider {manifest.name} depends on {prov.depends_on}"
                raise ValueError(msg)
        # create new provider config with given values
        existing = {
            x.instance_id for x in await self.get_provider_configs(provider_domain=provider_domain)
        }
        # determine instance id based on previous configs
        if existing and not manifest.multi_instance:
            msg = f"Provider {manifest.name} does not support multiple instances"
            raise ValueError(msg)
        if manifest.multi_instance:
            instance_id = f"{manifest.domain}--{shortuuid.random(8)}"
        else:
            instance_id = manifest.domain
        # all checks passed, create config object
        config_entries = await self.get_provider_config_entries(
            provider_domain=provider_domain, instance_id=instance_id, values=values
        )
        config = cast(
            "ProviderConfig",
            ProviderConfig.parse(
                config_entries,
                {
                    "type": manifest.type.value,
                    "domain": manifest.domain,
                    "instance_id": instance_id,
                    "default_name": manifest.name,
                    "values": values,
                },
            ),
        )
        # validate the new config
        config.validate()
        # save the config first to prevent issues when the
        # provider wants to manipulate the config during load
        conf_key = f"{CONF_PROVIDERS}/{config.instance_id}"
        self.set(conf_key, config.to_raw())
        # try to load the provider
        try:
            await self.mass.load_provider_config(config)
        except Exception:
            # loading failed, remove config
            self.remove(conf_key)
            raise
        if not self.onboard_done:
            # mark onboard as complete as soon as the first provider is added
            await self.set_onboard_complete()
        if manifest.type == ProviderType.MUSIC:
            # correct any multi-instance provider mappings
            self.mass.music.queue_provider_mapping_correction_task()
        return config

    def _build_sync_entries(
        self,
        manifest: Any,
        supported_features: builtins.set[ProviderFeature],
        provider: Any,
    ) -> list[ConfigEntry]:
        """Build sync-related ConfigEntry list based on provider features."""
        if manifest.type != ProviderType.MUSIC:
            return []
        extra_entries: list[ConfigEntry] = []
        # library sync settings
        if ProviderFeature.LIBRARY_ARTISTS in supported_features:
            extra_entries.append(CONF_ENTRY_LIBRARY_SYNC_ARTISTS)
        if ProviderFeature.LIBRARY_ALBUMS in supported_features:
            extra_entries.append(CONF_ENTRY_LIBRARY_SYNC_ALBUMS)
            if provider and isinstance(provider, MusicProvider) and provider.is_streaming_provider:
                extra_entries.append(CONF_ENTRY_LIBRARY_SYNC_ALBUM_TRACKS)
        if ProviderFeature.LIBRARY_TRACKS in supported_features:
            extra_entries.append(CONF_ENTRY_LIBRARY_SYNC_TRACKS)
        if ProviderFeature.LIBRARY_PLAYLISTS in supported_features:
            extra_entries.append(CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS)
            if provider and isinstance(provider, MusicProvider) and provider.is_streaming_provider:
                extra_entries.append(CONF_ENTRY_LIBRARY_SYNC_PLAYLIST_TRACKS)
        if ProviderFeature.LIBRARY_AUDIOBOOKS in supported_features:
            extra_entries.append(CONF_ENTRY_LIBRARY_SYNC_AUDIOBOOKS)
        if ProviderFeature.LIBRARY_PODCASTS in supported_features:
            extra_entries.append(CONF_ENTRY_LIBRARY_SYNC_PODCASTS)
        if ProviderFeature.LIBRARY_RADIOS in supported_features:
            extra_entries.append(CONF_ENTRY_LIBRARY_SYNC_RADIOS)
        # sync export settings
        if supported_features.intersection(
            {
                ProviderFeature.LIBRARY_ARTISTS_EDIT,
                ProviderFeature.LIBRARY_ALBUMS_EDIT,
                ProviderFeature.LIBRARY_TRACKS_EDIT,
                ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
                ProviderFeature.LIBRARY_AUDIOBOOKS_EDIT,
                ProviderFeature.LIBRARY_PODCASTS_EDIT,
                ProviderFeature.LIBRARY_RADIOS_EDIT,
            }
        ):
            extra_entries.append(CONF_ENTRY_LIBRARY_SYNC_BACK)
        if provider and isinstance(provider, MusicProvider) and provider.is_streaming_provider:
            extra_entries.append(CONF_ENTRY_LIBRARY_SYNC_DELETIONS)
        return extra_entries
