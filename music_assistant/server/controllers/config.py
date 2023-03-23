"""Logic to handle storage of persistent (configuration) settings."""
from __future__ import annotations

import asyncio
import base64
import logging
import os
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
    ConfigEntryValue,
    ConfigUpdate,
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
        await self.async_save()
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
            for prov in self.mass.get_available_providers():
                if prov.domain != raw_conf["domain"]:
                    continue
                prov_mod = await get_provider_module(prov.domain)
                prov_config_entries = await prov_mod.get_config_entries(self.mass, prov)
                config_entries = DEFAULT_PROVIDER_CONFIG_ENTRIES + prov_config_entries
                return ProviderConfig.parse(config_entries, raw_conf)
        raise KeyError(f"No config found for provider id {instance_id}")

    @api_command("config/providers/update")
    async def update_provider_config(self, instance_id: str, update: ConfigUpdate) -> None:
        """Update ProviderConfig."""
        config = await self.get_provider_config(instance_id)
        changed_keys = config.update(update)
        available = prov.available if (prov := self.mass.get_provider(instance_id)) else False
        if not changed_keys and (config.enabled == available):
            # no changes
            return
        # try to load the provider first to catch errors before we save it.
        if config.enabled:
            await self.mass.load_provider(config)
        else:
            await self.mass.unload_provider(config.instance_id)
        # load succeeded, save new config
        conf_key = f"{CONF_PROVIDERS}/{instance_id}"
        self.set(conf_key, config.to_raw())

    @api_command("config/providers/add")
    async def add_provider_config(
        self, provider_domain: str, config: ProviderConfig | None = None
    ) -> ProviderConfig:
        """Add new Provider (instance) Config Flow."""
        if not config:
            return await self._get_default_provider_config(provider_domain)
        # if provider config is provided, the frontend wants to submit a new provider instance
        # based on the earlier created template config.
        # try to load the provider first to catch errors before we save it.
        await self.mass.load_provider(config)
        # config provided and load success, storeconfig
        conf_key = f"{CONF_PROVIDERS}/{config.instance_id}"
        self.set(conf_key, config.to_raw())
        return config

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

    @api_command("config/providers/reload")
    async def reload_provider(self, instance_id: str) -> None:
        """Reload provider."""
        config = await self.get_provider_config(instance_id)
        await self.mass.load_provider(config)

    @api_command("config/players")
    def get_player_configs(self, provider: str | None = None) -> list[PlayerConfig]:
        """Return all known player configurations, optionally filtered by provider domain."""
        return [
            self.get_player_config(player_id)
            for player_id, raw_conf in self.get(CONF_PLAYERS).items()
            if (provider in (None, raw_conf["provider"]))
        ]

    @api_command("config/players/get")
    def get_player_config(self, player_id: str) -> PlayerConfig:
        """Return configuration for a single player."""
        if raw_conf := self.get(f"{CONF_PLAYERS}/{player_id}"):
            if prov := self.mass.get_provider(raw_conf["provider"]):
                prov_entries = prov.get_player_config_entries(player_id)
            else:
                prov_entries = tuple()
                raw_conf["available"] = False
                raw_conf["name"] = (
                    raw_conf.get("name") or raw_conf.get("default_name") or raw_conf["player_id"]
                )
            entries = DEFAULT_PLAYER_CONFIG_ENTRIES + prov_entries
            return PlayerConfig.parse(entries, raw_conf)
        raise KeyError(f"No config found for player id {player_id}")

    @api_command("config/players/get_value")
    def get_player_config_value(self, player_id: str, key: str) -> ConfigEntryValue:
        """Return single configentry value for a player."""
        conf = self.get(f"{CONF_PLAYERS}/{player_id}")
        if not conf:
            player = self.mass.players.get(player_id)
            if not player:
                raise PlayerUnavailableError(f"Player {player_id} is not available")
            conf = {"provider": player.provider, "player_id": player_id, "values": {}}
        prov = self.mass.get_provider(conf["provider"])
        entries = DEFAULT_PLAYER_CONFIG_ENTRIES + prov.get_player_config_entries(player_id)
        for entry in entries:
            if entry.key == key:
                return ConfigEntryValue.parse(entry, conf["values"].get(key))
        raise KeyError(f"ConfigEntry {key} is invalid")

    @api_command("config/players/update")
    def update_player_config(self, player_id: str, update: ConfigUpdate) -> None:
        """Update PlayerConfig."""
        config = self.get_player_config(player_id)
        changed_keys = config.update(update)

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
        try:
            player = self.mass.players.get(config.player_id)
            player.enabled = config.enabled
            self.mass.players.update(config.player_id)
        except PlayerUnavailableError:
            pass

        # signal player provider that the config changed
        try:
            if provider := self.mass.get_provider(config.provider):
                assert isinstance(provider, PlayerProvider)
                provider.on_player_config_changed(config, changed_keys)
        except PlayerUnavailableError:
            pass

    @api_command("config/players/create")
    async def create_player_config(
        self, provider_domain: str, config: PlayerConfig | None = None
    ) -> PlayerConfig:
        """Register a new Player(config) if the provider supports this."""
        provider: PlayerProvider = self.mass.get_provider(provider_domain)
        return await provider.create_player_config(config)

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
        # config does not yet exist, create a default one
        default_config = await self._get_default_provider_config(provider_domain)
        conf_key = f"{CONF_PROVIDERS}/{default_config.instance_id}"
        self.set(conf_key, default_config.to_raw())

    async def _get_default_provider_config(self, provider_domain: str) -> ProviderConfig:
        """
        Return default/empty ProviderConfig.

        This is intended to be used as helper method to add a new provider,
        and it performs some quick sanity checks as well as handling the
        instance_id generation.
        """
        # lookup provider manifest
        for prov in self.mass.get_available_providers():
            if prov.domain == provider_domain:
                manifest = prov
                break
        else:
            raise KeyError(f"Unknown provider domain: {provider_domain}")

        # determine instance id based on previous configs
        existing = {
            x.instance_id for x in await self.get_provider_configs(provider_domain=provider_domain)
        }

        if existing and not manifest.multi_instance:
            raise ValueError(f"Provider {manifest.name} does not support multiple instances")

        if len(existing) == 0:
            instance_id = provider_domain
            name = manifest.name
        else:
            instance_id = f"{provider_domain}{len(existing)+1}"
            name = f"{manifest.name} {len(existing)+1}"

        # all checks passed, return a default config
        prov_mod = await get_provider_module(provider_domain)
        config_entries = await prov_mod.get_config_entries(self.mass, manifest)
        return ProviderConfig.parse(
            DEFAULT_PROVIDER_CONFIG_ENTRIES + config_entries,
            {
                "type": manifest.type.value,
                "domain": manifest.domain,
                "instance_id": instance_id,
                "name": name,
                "values": {},
            },
        )

    async def _load(self) -> None:
        """Load data from persistent storage."""
        assert not self._data, "Already loaded"

        for filename in self.filename, f"{self.filename}.backup":
            try:
                _filename = os.path.join(self.mass.storage_path, filename)
                async with aiofiles.open(_filename, "r", encoding="utf-8") as _file:
                    self._data = json_loads(await _file.read())
                    return
            except FileNotFoundError:
                pass
            except JSON_DECODE_EXCEPTIONS:  # pylint: disable=catching-non-exception
                LOGGER.error("Error while reading persistent storage file %s", filename)
            else:
                LOGGER.debug("Loaded persistent settings from %s", filename)
        LOGGER.debug("Started with empty storage: No persistent storage file found.")

    def save(self, immediate: bool = False) -> None:
        """Schedule save of data to disk."""
        if self._timer_handle is not None:
            self._timer_handle.cancel()
            self._timer_handle = None

        if immediate:
            self.mass.loop.create_task(self.async_save())
        else:
            # schedule the save for later
            self._timer_handle = self.mass.loop.call_later(
                DEFAULT_SAVE_DELAY, self.mass.create_task, self.async_save
            )

    async def async_save(self):
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

    def encrypt_string(self, str_value: str) -> str:
        """Encrypt a (password)string with Fernet."""
        if str_value.startswith(ENCRYPT_SUFFIX):
            return str_value
        return ENCRYPT_SUFFIX + self._fernet.encrypt(str_value.encode()).decode()

    def decrypt_string(self, encrypted_str: str) -> str:
        """Decrypt a (password)string with Fernet."""
        if not encrypted_str.startswith(ENCRYPT_SUFFIX):
            return encrypted_str
        encrypted_str = encrypted_str.replace(ENCRYPT_SUFFIX, "")
        try:
            return self._fernet.decrypt(encrypted_str.encode()).decode()
        except InvalidToken as err:
            raise InvalidDataError("Password decryption failed") from err
