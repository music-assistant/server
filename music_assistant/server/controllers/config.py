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
from music_assistant.common.models.config_entries import ProviderConfig
from music_assistant.common.models.enums import EventType, ProviderType
from music_assistant.server.helpers.api import api_command
from music_assistant.constants import CONF_PROVIDERS

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

    @api_command("config/providers/all")
    def get_provider_configs(
        self, prov_type: ProviderType | None = None
    ) -> list[ProviderConfig]:
        """Return all known provider configurations, optionally filtered by ProviderType."""
        raw_values: dict[str, dict] = self.get(CONF_PROVIDERS, {})
        return [
            ProviderConfig.from_dict(x)
            for x in raw_values.values()
            if (prov_type is None or x["type"] == prov_type)
        ]

    @api_command("config/providers/get")
    def get_provider_config(self, instance_id: str) -> list[ProviderConfig]:
        """Return configuration for a single provider."""
        if raw_value := self.get(f"{CONF_PROVIDERS}/{instance_id}"):
            return ProviderConfig.from_dict(raw_value)
        raise KeyError(f"No config found for provider id {instance_id}")

    @api_command("config/providers/set")
    def set_provider_config(self, config: ProviderConfig) -> None:
        """Create or update ProviderConfig."""
        conf_key = f"{CONF_PROVIDERS}/{config.instance_id}"
        existing = self.get(conf_key)
        config_dict = config.to_dict()
        if existing == config_dict:
            # no changes
            return
        self.set(conf_key, config.to_dict())
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
