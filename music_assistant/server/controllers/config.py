"""Logic to handle storage of persistent (configuration) settings."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any, Dict

import aiofiles
from aiofiles.os import wrap

from music_assistant.common.helpers.json import (
    JSON_DECODE_EXCEPTIONS,
    json_dumps,
    json_loads,
)
from music_assistant.common.models.config_entries import (
    DEFAULT_PLAYER_CONFIG_ENTRIES,
    PlayerConfig,
    PlayerConfigSet,
    ProviderConfig,
    ProviderConfigSet,
)
from music_assistant.common.models.enums import EventType, ProviderType
from music_assistant.common.models.errors import PlayerUnavailableError
from music_assistant.constants import CONF_PLAYERS, CONF_PROVIDERS
from music_assistant.server.helpers.api import api_command

if TYPE_CHECKING:
    from ..server import MusicAssistant

LOGGER = logging.getLogger(__name__)
DEFAULT_SAVE_DELAY = 120

isfile = wrap(os.path.isfile)
remove = wrap(os.remove)
rename = wrap(os.rename)


class ConfigController:
    """Controller that handles storage of persistent configuration settings."""

    def __init__(self, mass: "MusicAssistant") -> None:
        """Initialize storage controller."""
        self.mass = mass
        self.initialized = False
        self._data: Dict[str, Any] = {}
        self.filename = os.path.join(self.mass.storage_path, "settings.json")
        self._timer_handle: asyncio.TimerHandle | None = None

    async def setup(self) -> None:
        """Async initialize of controller."""
        await self._load()
        self.initialized = True
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
                return parent.get(subkey, default)
            elif subkey not in parent:
                # requesting subkey from a non existing parent
                return default
            else:
                parent = parent[subkey]
        return default

    def set(
        self,
        key: str,
        value: Any,
    ) -> None:
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
        self, prov_type: ProviderType | None = None
    ) -> list[ProviderConfig]:
        """Return all known provider configurations, optionally filtered by ProviderType."""
        raw_values: dict[str, dict] = self.get(CONF_PROVIDERS, {})
        prov_entries = {
            x.domain: x.config_entries for x in self.mass.get_available_providers()
        }
        return [
            ProviderConfig.parse(prov_entries[prov_conf["domain"]], prov_conf)
            for prov_conf in raw_values.values()
            if (prov_type is None or prov_conf["type"] == prov_type)
        ]

    @api_command("config/providers/get")
    def get_provider_config(self, instance_id: str) -> ProviderConfig:
        """Return configuration for a single provider."""
        if raw_conf := self.get(f"{CONF_PROVIDERS}/{instance_id}", {}):
            for prov in self.mass.get_available_providers():
                if prov.domain != raw_conf["domain"]:
                    continue
                return ProviderConfig.parse(prov.config_entries, raw_conf)
        raise KeyError(f"No config found for provider id {instance_id}")

    @api_command("config/providers/set")
    def set_provider_config(self, config: ProviderConfig | ProviderConfigSet) -> None:
        """Create or update ProviderConfig."""
        conf_key = f"{CONF_PROVIDERS}/{config.instance_id}"
        existing = self.get(conf_key)
        config_dict = config.to_raw()
        if existing == config_dict:
            # no changes
            return
        self.set(conf_key, config_dict)
        # make sure we send a full object in the event
        if isinstance(config, ProviderConfigSet):
            config = self.get(f"{CONF_PROVIDERS}/{config.instance_id}")
        if existing:
            # existing provider updated
            self.mass.signal_event(
                EventType.PROVIDER_CONFIG_UPDATED,
                object_id=config.instance_id,
                data=config,
            )
        else:
            # new provider config added
            self.mass.signal_event(
                EventType.PROVIDER_CONFIG_CREATED,
                object_id=config.instance_id,
                data=config,
            )

    @api_command("config/players")
    def get_player_configs(self, provider: str | None = None) -> list[PlayerConfig]:
        """Return all known player configurations, optionally filtered by provider domain."""
        player_configs: dict[str, dict] = self.get(CONF_PLAYERS, {})
        # we build a list of all playerids to cover both edge cases:
        # - player does not yet have a config stored persistently
        # - player is disabled in config and not available
        all_player_ids = set(player_configs.keys())
        for player in self.mass.players:
            all_player_ids.add(player.player_id)
        return [self.get_player_config(x) for x in all_player_ids]

    @api_command("config/players/get")
    def get_player_config(self, player_id: str) -> PlayerConfig:
        """Return configuration for a single player."""
        conf = self.get(f"{CONF_PLAYERS}/{player_id}")
        if not conf:
            player = self.mass.players.get(player_id)
            if not player:
                raise PlayerUnavailableError(f"Player {player_id} is not available")
            conf = {"provider": player.provider, "player_id": player_id}
        prov = self.mass.get_provider(conf["provider"])
        entries = DEFAULT_PLAYER_CONFIG_ENTRIES + prov.get_player_config_entries(
            player_id
        )
        return PlayerConfig.parse(entries, conf)

    @api_command("config/players/set")
    def set_player_config(self, config: PlayerConfig | PlayerConfigSet) -> None:
        """Create or update PlayerConfig."""
        conf_key = f"{CONF_PLAYERS}/{config.player_id}"
        existing = self.get(conf_key)
        config_dict = config.to_raw()
        if existing == config_dict:
            # no changes
            return
        self.set(conf_key, config_dict)
        # make sure we send a full object in the event
        if isinstance(config, PlayerConfigSet):
            config = self.get(f"{CONF_PLAYERS}/{config.player_id}")
        # send config updated event
        self.mass.signal_event(
            EventType.PLAYER_CONFIG_UPDATED,
            object_id=config.player_id,
            data=config,
        )
        # signal update to the player manager
        # TODO: restart playback if player is playing?
        if player := self.mass.players.get(config.player_id):
            player.enabled = config.enabled
            self.mass.players.update(config.player_id)

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
                DEFAULT_SAVE_DELAY, self.mass.loop.create_task, self.async_save()
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
            await _file.write(json_dumps(self._data))
        LOGGER.debug("Saved data to persistent storage")
