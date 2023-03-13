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
    ConfigEntryValue,
    ConfigUpdate,
    PlayerConfig,
    ProviderConfig,
)
from music_assistant.common.models.enums import EventType, ProviderType
from music_assistant.common.models.errors import (
    InvalidDataError,
    PlayerUnavailableError,
    ProviderUnavailableError,
)
from music_assistant.constants import CONF_PLAYERS, CONF_PROVIDERS, CONF_SERVER_ID, ENCRYPT_SUFFIX
from music_assistant.server.helpers.api import api_command
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
    def get_provider_configs(
        self,
        provider_type: ProviderType | None = None,
        provider_domain: str | None = None,
    ) -> list[ProviderConfig]:
        """Return all known provider configurations, optionally filtered by ProviderType."""
        raw_values: dict[str, dict] = self.get(CONF_PROVIDERS, {})
        prov_entries = {x.domain: x.config_entries for x in self.mass.get_available_providers()}
        return [
            ProviderConfig.parse(
                prov_entries[prov_conf["domain"]],
                prov_conf,
            )
            for prov_conf in raw_values.values()
            if (provider_type is None or prov_conf["type"] == provider_type)
            and (provider_domain is None or prov_conf["domain"] == provider_domain)
            # guard for deleted providers
            and prov_conf["domain"] in prov_entries
        ]

    @api_command("config/providers/get")
    def get_provider_config(self, instance_id: str) -> ProviderConfig:
        """Return configuration for a single provider."""
        if raw_conf := self.get(f"{CONF_PROVIDERS}/{instance_id}", {}):
            for prov in self.mass.get_available_providers():
                if prov.domain != raw_conf["domain"]:
                    continue
                return ProviderConfig.parse(
                    prov.config_entries,
                    raw_conf,
                )
        raise KeyError(f"No config found for provider id {instance_id}")

    @api_command("config/providers/update")
    def update_provider_config(
        self, instance_id: str, update: ConfigUpdate, skip_reload: bool = False
    ) -> None:
        """Update ProviderConfig."""
        config = self.get_provider_config(instance_id)
        changed_keys = config.update(update)

        if not changed_keys:
            # no changes
            return

        conf_key = f"{CONF_PROVIDERS}/{instance_id}"
        self.set(conf_key, config.to_raw())
        # (re)load provider
        if not skip_reload:
            updated_config = self.get_provider_config(config.instance_id)
            self.mass.create_task(self.mass.load_provider(updated_config))

    @api_command("config/providers/create")
    def create_provider_config(self, provider_domain: str) -> ProviderConfig:
        """Create default/empty ProviderConfig.

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
        existing = self.get_provider_configs(provider_domain=provider_domain)
        if existing and not manifest.multi_instance:
            raise ValueError(f"Provider {manifest.name} does not support multiple instances")

        count = len(existing)
        if count == 0:
            instance_id = provider_domain
            name = manifest.name
        else:
            instance_id = f"{provider_domain}{count+1}"
            name = f"{manifest.name} {count+1}"

        return ProviderConfig.parse(
            prov.config_entries,
            {
                "type": manifest.type.value,
                "domain": manifest.domain,
                "instance_id": instance_id,
                "name": name,
                "values": dict(),
            },
            allow_none=True,
        )

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

    @api_command("config/players")
    def get_player_configs(self, provider: str | None = None) -> list[PlayerConfig]:
        """Return all known player configurations, optionally filtered by provider domain."""
        result: dict[str, PlayerConfig] = {}
        # we use an intermediate dict to cover both edge cases:
        # - player does not yet have a config stored persistently
        # - player is disabled in config and not available

        # do all existing players first
        for player in self.mass.players:
            if provider is not None and player.provider != provider:
                continue
            result[player.player_id] = self.get_player_config(player.player_id)

        # add remaining configs that do have a config stored but are not (yet) available now
        raw_configs = self.get(CONF_PLAYERS, {})
        for player_id, raw_conf in raw_configs.items():
            if player_id in result:
                continue
            if provider is not None and raw_conf["provider"] != provider:
                continue
            try:
                prov = self.mass.get_provider(raw_conf["provider"])
                prov_entries = prov.get_player_config_entries(player_id)
            except (ProviderUnavailableError, PlayerUnavailableError):
                prov_entries = tuple()

            entries = DEFAULT_PLAYER_CONFIG_ENTRIES + prov_entries
            result[player.player_id] = PlayerConfig.parse(entries, raw_conf)

        return list(result.values())

    @api_command("config/players/get")
    def get_player_config(self, player_id: str) -> PlayerConfig:
        """Return configuration for a single player."""
        conf = self.get(f"{CONF_PLAYERS}/{player_id}")
        if not conf:
            player = self.mass.players.get(player_id, raise_unavailable=False)
            conf = {
                "provider": player.provider,
                "player_id": player_id,
                "enabled": player.enabled_by_default,
            }

        try:
            prov = self.mass.get_provider(conf["provider"])
            prov_entries = prov.get_player_config_entries(player_id)
        except (ProviderUnavailableError, PlayerUnavailableError):
            prov_entries = tuple()

        entries = DEFAULT_PLAYER_CONFIG_ENTRIES + prov_entries
        return PlayerConfig.parse(entries, conf)

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
            # copy playername to find back the playername if its disabled
            if not config.enabled and not config.name:
                config.name = player.display_name
                self.set(conf_key, config.to_raw())
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
