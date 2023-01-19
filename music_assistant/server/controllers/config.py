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
        self._data: Dict[str, Any] = {}
        self.filename = os.path.join(self.mass.storage_path, "settings.json")
        self._timer_handle: asyncio.TimerHandle | None = None

    async def setup(self) -> None:
        """Async initialize of controller."""
        await self._load()
        LOGGER.debug("Started.")

    async def stop(self) -> None:
        """Handle logic on server stop."""
        if not self._timer_handle:
            # no point in forcing a save when there are no changes pending
            return
        await self.async_save()
        LOGGER.debug("Stopped.")

    def get(self, key: str, default: Any = None) -> Any:
        """Get value(s) for a specific key/path in persistent storage."""
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
                if cur_value == value:
                    # no need to save if value did not change
                    return
                self._data[subkey] = value
                self.save()
            else:
                parent.setdefault(subkey, {})
                parent = parent[subkey]

    def remove(
        self,
        key: str,
    ) -> None:
        """Remove value(s) for a specific key/path in persistent storage."""
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

    async def _load(self) -> None:
        """Load data from persistent storage."""
        assert not self._data, "Already loaded"

        for filename in self.filename, f"{self.filename}.backup":
            try:
                _filename = os.path.join(self.mass.storage_path, filename)
                async with aiofiles.open(_filename, "r", encoding="utf-8") as _file:
                    self._data = json_loads(_file.read())
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
