"""Logic to handle storage of persistent (configuration) settings."""
from __future__ import annotations

import asyncio
import base64
import logging
import os
from contextlib import suppress
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import aiofiles
from aiofiles.os import wrap
from cryptography.fernet import Fernet, InvalidToken

from music_assistant.common.helpers.json import JSON_DECODE_EXCEPTIONS, json_dumps, json_loads
from music_assistant.common.models import config_entries
from music_assistant.common.models.config_entries import (
    DEFAULT_PLAYER_CONFIG_ENTRIES,
    DEFAULT_PROVIDER_CONFIG_ENTRIES,
    ConfigEntry,
    ConfigValueType,
    PlayerConfig,
    ProviderConfig,
)
from music_assistant.common.models.enums import EventType, ProviderType
from music_assistant.common.models.errors import InvalidDataError, PlayerUnavailableError
from music_assistant.constants import CONF_PLAYERS, CONF_PROVIDERS, CONF_SERVER_ID, ENCRYPT_SUFFIX
from music_assistant.server.helpers.api import api_command
from music_assistant.server.helpers.util import get_provider_module
from music_assistant.server.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from music_assistant.server.server import MusicAssistant

LOGGER = logging.getLogger(__name__)
DEFAULT_SAVE_DELAY = 5


isfile = wrap(os.path.isfile)
remove = wrap(os.remove)
rename = wrap(os.rename)


class ConfigController:
    """Controller that handles storage of persistent configuration settings."""

    _fernet: Fernet | None = None

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize storage controller."""
        self.mass = mass
        self.initialized = False
        self._data: dict[str, Any] = {}
        self.filename = os.path.join(self.mass.storage_path, "settings.json")
        self._timer_handle: asyncio.TimerHandle | None = None
        self._value_cache: dict[str, ConfigValueType] = {}

    async def setup(self) -> None:
        """Async initialize of controller."""
        await self._load()
        self.initialized = True
        # create default server ID if needed (also used for encrypting passwords)
        self.set_default(CONF_SERVER_ID, uuid4().hex)
        server_id: str = self.get(CONF_SERVER_ID)
        assert server_id
        fernet_key = base64.urlsafe_b64encode(server_id.encode()[:32])
        self._fernet = Fernet(fernet_key)
        config_entries.ENCRYPT_CALLBACK = self.encrypt_string
        config_entries.DECRYPT_CALLBACK = self.decrypt_string

        LOGGER.debug("Started.")

    async def close(self) -> None:
        """Handle logic on server stop."""
        if not self._timer_handle:
            # no point in forcing a save when there are no changes pending
            return
        await self._async_save()
        LOGGER.debug("Stopped.")

    def get(self, key: str, default: Any = None) -> Any:
        """Get value(s) for a specific key/path in persistent storage."""
        assert self.initialized, "Not yet (async) initialized"
        # we support a multi level hierarchy by providing the key as path,
        # with a slash (/) as splitter. Sort that out here.
        parent = self._data
        subkeys = key.split("/")
        for index, subkey in enumerate(subkeys):
            if index == (len(subkeys) - 1):
                value = parent.get(subkey, default)
                if value is None:
                    # replace None with default
                    return default
                return value
            elif subkey not in parent:
                # requesting subkey from a non existing parent
                return default
            else:
                parent = parent[subkey]
        return default

    def set(self, key: str, value: Any) -> None:
        """Set value(s) for a specific key/path in persistent storage."""
        assert self.initialized, "Not yet (async) initialized"
        # we support a multi level hierarchy by providing the key as path,
        # with a slash (/) as splitter.
        parent = self._data
        subkeys = key.split("/")
        for index, subkey in enumerate(subkeys):
            if index == (len(subkeys) - 1):
                cur_value = parent.get(subkey)
                if cur_value == value:
                    # no need to save if value did not change
                    return
                parent[subkey] = value
                self.save()
            else:
                parent.setdefault(subkey, {})
                parent = parent[subkey]

    def set_default(self, key: str, default_value: Any) -> None:
        """Set default value(s) for a specific key/path in persistent storage."""
        assert self.initialized, "Not yet (async) initialized"
        cur_value = self.get(key, "__MISSING__")
        if cur_value == "__MISSING__":
            self.set(key, default_value)

    def remove(
        self,
        key: str,
    ) -> None:
        """Remove value(s) for a specific key/path in persistent storage."""
        assert self.initialized, "Not yet (async) initialized"
        parent = self._data
        subkeys = key.split("/")
        for index, subkey in enumerate(subkeys):
            if subkey not in parent:
                return
            if index == (len(subkeys) - 1):
                parent.pop(subkey)
            else:
                parent.setdefault(subkey, {})
                parent = parent[subkey]

        self.save()

    @api_command("config/providers")
    async def get_provider_configs(
        self,
        provider_type: ProviderType | None = None,
        provider_domain: str | None = None,
    ) -> list[ProviderConfig]:
        """Return all known provider configurations, optionally filtered by ProviderType."""
        raw_values: dict[str, dict] = self.get(CONF_PROVIDERS, {})
        prov_entries = {x.domain for x in self.mass.get_available_providers()}
        return [
            await self.get_provider_config(prov_conf["instance_id"])
            for prov_conf in raw_values.values()
            if (provider_type is None or prov_conf["type"] == provider_type)
            and (provider_domain is None or prov_conf["domain"] == provider_domain)
            # guard for deleted providers
            and prov_conf["domain"] in prov_entries
        ]

    @api_command("config/providers/get")
    async def get_provider_config(self, instance_id: str) -> ProviderConfig:
        """Return configuration for a single provider."""
        if raw_conf := self.get(f"{CONF_PROVIDERS}/{instance_id}", {}):
            config_entries = await self.get_provider_config_entries(
                raw_conf["domain"], instance_id=instance_id, values=raw_conf.get("values")
            )
            return ProviderConfig.parse(config_entries, raw_conf)
        raise KeyError(f"No config found for provider id {instance_id}")

    @api_command("config/providers/get_value")
    def get_provider_config_value(self, instance_id: str, key: str) -> ConfigValueType:
        """Return single configentry value for a provider."""
        cache_key = f"prov_conf_value_{instance_id}.{key}"
        if cached_value := self._value_cache.get(cache_key) is not None:
            return cached_value
        conf = self.get_provider_config(instance_id)
        val = (
            conf.values[key].value
            if conf.values[key].value is not None
            else conf.values[key].default_value
        )
        # store value in cache because this method can potentially be called very often
        self._value_cache[cache_key] = val
        return val

    @api_command("config/providers/get_entries")
    async def get_provider_config_entries(
        self,
        provider_domain: str,
        instance_id: str | None = None,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> tuple[ConfigEntry, ...]:
        """
        Return Config entries to setup/configure a provider.

        provider_domain: (mandatory) domain of the provider.
        instance_id: id of an existing provider instance (None for new instance setup).
        action: [optional] action key called from config entries UI.
        values: the (intermediate) raw values for config entries sent with the action.
        """
        # lookup provider manifest and module
        for prov in self.mass.get_available_providers():
            if prov.domain == provider_domain:
                prov_mod = await get_provider_module(provider_domain)
                break
        else:
            raise KeyError(f"Unknown provider domain: {provider_domain}")
        if values is None:
            values = self.get(f"{CONF_PROVIDERS}/{instance_id}/values", {}) if instance_id else {}
        return (
            await prov_mod.get_config_entries(
                self.mass, instance_id=instance_id, action=action, values=values
            )
            + DEFAULT_PROVIDER_CONFIG_ENTRIES
        )

    @api_command("config/providers/save")
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
        action: [optional] action key called from config entries UI.
        """
        if instance_id is not None:
            config = await self._update_provider_config(instance_id, values)
        else:
            config = await self._add_provider_config(provider_domain, values)
        # return full config, just in case
        return await self.get_provider_config(config.instance_id)

    @api_command("config/providers/remove")
    async def remove_provider_config(self, instance_id: str) -> None:
        """Remove ProviderConfig."""
        conf_key = f"{CONF_PROVIDERS}/{instance_id}"
        existing = self.get(conf_key)
        if not existing:
            raise KeyError(f"Provider {instance_id} does not exist")
        self.remove(conf_key)
        await self.mass.unload_provider(instance_id)
        if existing["type"] == "music":
            # cleanup entries in library
            await self.mass.music.cleanup_provider(instance_id)

    async def remove_provider_config_value(self, instance_id: str, key: str) -> None:
        """Remove/reset single Provider config value."""
        conf_key = f"{CONF_PROVIDERS}/{instance_id}/values/{key}"
        existing = self.get(conf_key)
        if not existing:
            return
        self.remove(conf_key)

    async def set_provider_config_value(
        self, instance_id: str, key: str, value: ConfigValueType
    ) -> None:
        """Set single ProviderConfig value."""
        conf_key = f"{CONF_PROVIDERS}/{instance_id}/values/{key}"
        self.set(conf_key, value)

    @api_command("config/providers/reload")
    async def reload_provider(self, instance_id: str, config: ProviderConfig | None) -> None:
        """Reload provider."""
        config = await self.get_provider_config(instance_id)
        await self._load_provider_config(config)

    @api_command("config/players")
    async def get_player_configs(self, provider: str | None = None) -> list[PlayerConfig]:
        """Return all known player configurations, optionally filtered by provider domain."""
        available_providers = {x.domain for x in self.mass.providers}
        return [
            await self.get_player_config(player_id)
            for player_id, raw_conf in self.get(CONF_PLAYERS).items()
            # filter out unavailable providers
            if raw_conf["provider"] in available_providers
            # optional provider filter
            and (provider in (None, raw_conf["provider"]))
        ]

    @api_command("config/players/get")
    async def get_player_config(self, player_id: str) -> PlayerConfig:
        """Return configuration for a single player."""
        if raw_conf := self.get(f"{CONF_PLAYERS}/{player_id}"):
            if prov := self.mass.get_provider(raw_conf["provider"]):
                prov_entries = await prov.get_player_config_entries(player_id)
                if player := self.mass.players.get(player_id, False):
                    raw_conf["default_name"] = player.display_name
            else:
                prov_entries = tuple()
                raw_conf["available"] = False
                raw_conf["name"] = raw_conf.get("name")
                raw_conf["default_name"] = raw_conf.get("default_name") or raw_conf["player_id"]
            prov_entries_keys = {x.key for x in prov_entries}
            # combine provider defined entries with default player config entries
            entries = prov_entries + tuple(
                x for x in DEFAULT_PLAYER_CONFIG_ENTRIES if x.key not in prov_entries_keys
            )
            return PlayerConfig.parse(entries, raw_conf)
        raise KeyError(f"No config found for player id {player_id}")

    @api_command("config/players/get_value")
    async def get_player_config_value(self, player_id: str, key: str) -> ConfigValueType:
        """Return single configentry value for a player."""
        cache_key = f"player_conf_value_{player_id}.{key}"
        if (cached_value := self._value_cache.get(cache_key)) and cached_value is not None:
            return cached_value
        conf = await self.get_player_config(player_id)
        val = (
            conf.values[key].value
            if conf.values[key].value is not None
            else conf.values[key].default_value
        )
        # store value in cache because this method can potentially be called very often
        self._value_cache[cache_key] = val
        return val

    def get_raw_player_config_value(
        self, player_id: str, key: str, default: ConfigValueType = None
    ) -> ConfigValueType:
        """
        Return (raw) single configentry value for a player.

        Note that this only returns the stored value without any validation or default.
        """
        return self.get(f"{CONF_PLAYERS}/{player_id}/values/{key}", default)

    @api_command("config/players/save")
    async def save_player_config(
        self, player_id: str, values: dict[str, ConfigValueType]
    ) -> PlayerConfig:
        """Save/update PlayerConfig."""
        config = await self.get_player_config(player_id)
        changed_keys = config.update(values)

        if not changed_keys:
            # no changes
            return

        conf_key = f"{CONF_PLAYERS}/{player_id}"
        self.set(conf_key, config.to_raw())
        # send config updated event
        self.mass.signal_event(
            EventType.PLAYER_CONFIG_UPDATED,
            object_id=config.player_id,
            data=config,
        )
        # signal update to the player manager
        with suppress(PlayerUnavailableError, AttributeError):
            player = self.mass.players.get(config.player_id)
            if config.enabled:
                player_prov = self.mass.players.get_player_provider(player_id)
                await player_prov.poll_player(player_id)
            player.enabled = config.enabled
            self.mass.players.update(config.player_id, force_update=True)

        # signal player provider that the config changed
        with suppress(PlayerUnavailableError):
            if provider := self.mass.get_provider(config.provider):
                provider.on_player_config_changed(config, changed_keys)
        # return full player config (just in case)
        return await self.get_player_config(player_id)

    @api_command("config/players/remove")
    async def remove_player_config(self, player_id: str) -> None:
        """Remove PlayerConfig."""
        conf_key = f"{CONF_PLAYERS}/{player_id}"
        existing = self.get(conf_key)
        if not existing:
            raise KeyError(f"Player {player_id} does not exist")
        self.remove(conf_key)
        if provider := self.mass.get_provider(existing["provider"]):
            assert isinstance(provider, PlayerProvider)
            provider.on_player_config_removed(player_id)

    def create_default_player_config(self, player_id: str, provider: str, name: str) -> None:
        """
        Create default/empty PlayerConfig.

        This is meant as helper to create default configs when a player is registered.
        Called by the player manager on player register.
        """
        # return early if the config already exists
        if self.get(f"{CONF_PLAYERS}/{player_id}"):
            # update default name if needed
            self.set(f"{CONF_PLAYERS}/{player_id}/default_name", name)
            return
        # config does not yet exist, create a default one
        conf_key = f"{CONF_PLAYERS}/{player_id}"
        default_conf = PlayerConfig(
            values={}, provider=provider, player_id=player_id, default_name=name
        )
        self.set(
            conf_key,
            default_conf.to_raw(),
        )

    async def create_default_provider_config(self, provider_domain: str) -> None:
        """
        Create default ProviderConfig.

        This is meant as helper to create default configs for default enabled providers.
        Called by the server initialization code which load all providers at startup.
        """
        for conf in await self.get_provider_configs(provider_domain=provider_domain):
            # return if there is already a config
            return
        for prov in self.mass.get_available_providers():
            if prov.domain == provider_domain:
                manifest = prov
                break
        else:
            raise KeyError(f"Unknown provider domain: {provider_domain}")
        config_entries = await self.get_provider_config_entries(provider_domain)
        default_config: ProviderConfig = ProviderConfig.parse(
            config_entries,
            {
                "type": manifest.type.value,
                "domain": manifest.domain,
                "instance_id": manifest.domain,
                "name": manifest.name,
                # note: this will only work for providers that do
                # not have any required config entries or provide defaults
                "values": {},
            },
        )
        default_config.validate()
        conf_key = f"{CONF_PROVIDERS}/{default_config.instance_id}"
        self.set(conf_key, default_config.to_raw())

    def save(self, immediate: bool = False) -> None:
        """Schedule save of data to disk."""
        self._value_cache = {}
        if self._timer_handle is not None:
            self._timer_handle.cancel()
            self._timer_handle = None

        if immediate:
            self.mass.loop.create_task(self._async_save())
        else:
            # schedule the save for later
            self._timer_handle = self.mass.loop.call_later(
                DEFAULT_SAVE_DELAY, self.mass.create_task, self._async_save
            )

    def encrypt_string(self, str_value: str) -> str:
        """Encrypt a (password)string with Fernet."""
        if str_value.startswith(ENCRYPT_SUFFIX):
            return str_value
        return ENCRYPT_SUFFIX + self._fernet.encrypt(str_value.encode()).decode()

    def decrypt_string(self, encrypted_str: str) -> str:
        """Decrypt a (password)string with Fernet."""
        if not encrypted_str:
            return encrypted_str
        if not encrypted_str.startswith(ENCRYPT_SUFFIX):
            return encrypted_str
        encrypted_str = encrypted_str.replace(ENCRYPT_SUFFIX, "")
        try:
            return self._fernet.decrypt(encrypted_str.encode()).decode()
        except InvalidToken as err:
            raise InvalidDataError("Password decryption failed") from err

    async def _load(self) -> None:
        """Load data from persistent storage."""
        assert not self._data, "Already loaded"

        for filename in self.filename, f"{self.filename}.backup":
            try:
                _filename = os.path.join(self.mass.storage_path, filename)
                async with aiofiles.open(_filename, "r", encoding="utf-8") as _file:
                    self._data = json_loads(await _file.read())
                    LOGGER.debug("Loaded persistent settings from %s", filename)
                    return
            except FileNotFoundError:
                pass
            except JSON_DECODE_EXCEPTIONS:  # pylint: disable=catching-non-exception
                LOGGER.error("Error while reading persistent storage file %s", filename)
        LOGGER.debug("Started with empty storage: No persistent storage file found.")

    async def _async_save(self):
        """Save persistent data to disk."""
        filename_backup = f"{self.filename}.backup"
        # make backup before we write a new file
        if await isfile(self.filename):
            if await isfile(filename_backup):
                await remove(filename_backup)
            await rename(self.filename, filename_backup)

        async with aiofiles.open(self.filename, "w", encoding="utf-8") as _file:
            await _file.write(json_dumps(self._data, indent=True))
        LOGGER.debug("Saved data to persistent storage")

    async def _update_provider_config(
        self, instance_id: str, values: dict[str, ConfigValueType]
    ) -> ProviderConfig:
        """Update ProviderConfig."""
        config = await self.get_provider_config(instance_id)
        changed_keys = config.update(values)
        # validate the new config
        config.validate()
        available = prov.available if (prov := self.mass.get_provider(instance_id)) else False
        if not changed_keys and (config.enabled == available):
            # no changes
            return config
        # try to load the provider first to catch errors before we save it.
        if config.enabled:
            await self._load_provider_config(config)
        else:
            # disable provider
            # also unload any other providers dependent of this provider
            for dep_prov in self.mass.providers:
                if dep_prov.manifest.depends_on == config.domain:
                    await self.mass.unload_provider(dep_prov.instance_id)
            await self.mass.unload_provider(config.instance_id)
        # load succeeded, save new config
        config.last_error = None
        conf_key = f"{CONF_PROVIDERS}/{instance_id}"
        self.set(conf_key, config.to_raw())
        return config

    async def _add_provider_config(
        self,
        provider_domain: str,
        values: dict[str, ConfigValueType],
    ) -> list[ConfigEntry] | ProviderConfig:
        """
        Add new Provider (instance).

        params:
        - provider_domain: domain of the provider for which to add an instance of.
        - values: the raw values for config entries.

        Returns: newly created ProviderConfig.
        """
        # lookup provider manifest and module
        for prov in self.mass.get_available_providers():
            if prov.domain == provider_domain:
                manifest = prov
                break
        else:
            raise KeyError(f"Unknown provider domain: {provider_domain}")
        # create new provider config with given values
        existing = {
            x.instance_id for x in await self.get_provider_configs(provider_domain=provider_domain)
        }
        # determine instance id based on previous configs
        if existing and not manifest.multi_instance:
            raise ValueError(f"Provider {manifest.name} does not support multiple instances")
        if len(existing) == 0:
            instance_id = provider_domain
            name = manifest.name
        else:
            instance_id = f"{provider_domain}{len(existing)+1}"
            name = f"{manifest.name} {len(existing)+1}"
        # all checks passed, create config object
        config_entries = await self.get_provider_config_entries(
            provider_domain=provider_domain, instance_id=instance_id, values=values
        )
        config: ProviderConfig = ProviderConfig.parse(
            config_entries,
            {
                "type": manifest.type.value,
                "domain": manifest.domain,
                "instance_id": instance_id,
                "name": name,
                "values": values,
            },
        )
        # validate the new config
        config.validate()
        # try to load the provider first to catch errors before we save it.
        await self.mass.load_provider(config)
        # the load was a success, store this config
        conf_key = f"{CONF_PROVIDERS}/{config.instance_id}"
        self.set(conf_key, config.to_raw())
        return config

    async def _load_provider_config(self, config: ProviderConfig) -> None:
        """Load given provider config."""
        # check if there are no other providers dependent of this provider
        deps = set()
        for dep_prov in self.mass.providers:
            if dep_prov.manifest.depends_on == config.domain:
                deps.add(dep_prov.instance_id)
                await self.mass.unload_provider(dep_prov.instance_id)
        # (re)load the provider
        await self.mass.load_provider(config)
        # reload any dependants
        for dep in deps:
            conf = await self.get_provider_config(dep)
            await self.mass.load_provider(conf)
