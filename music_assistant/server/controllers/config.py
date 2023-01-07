"""Logic to handle storage of persistent data."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any, Dict

from ..helpers.json import JSON_DECODE_EXCEPTIONS, json_dumps, json_loads

if TYPE_CHECKING:
    from ..server import MusicAssistantServer

LOGGER = logging.getLogger(__name__)
DEFAULT_SAVE_DELAY = 120


class StorageController:
    """Controller that handles storage of persistent data."""

    def __init__(self, mass: "MusicAssistantServer") -> None:
        """Initialize storage controller."""
        self.mass = mass
        self._data: Dict[str, Any] = {}
        self._timer_handle: asyncio.TimerHandle | None = None

    @property
    def filename(self) -> str:
        """Return full path to (fabric-specific) storage file."""
        return os.path.join(
            self.server.storage_path,
            f"{self.server.device_controller.compressed_fabric_id}.json",
        )

    async def start(self) -> None:
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

    def get(self, key: str, default: Any = None, subkey: str | None = None) -> Any:
        """Get data from specific (sub)key."""
        if subkey:
            # we provide support for (1-level) nested dict
            return self._data.get(key, {}).get(subkey, default)
        return self._data.get(key, default)

    def set(
        self,
        key: str,
        value: Any,
        subkey: str | None = None,
        force: bool = False,
    ) -> None:
        """Set a (sub)value in persistent storage."""
        if not force and self.get(key, subkey=subkey) == value:
            # no need to save if value did not change
            return
        if subkey:
            # we provide support for (1-level) nested dict
            self._data.setdefault(key, {})
            self._data[key][subkey] = value
        else:
            self._data[key] = value
        self.save(force)

    def remove(
        self,
        key: str,
        subkey: str | None = None,
    ) -> None:
        """Remove a (sub)value in persistent storage."""
        if subkey:
            # we provide support for (1-level) nested dict
            self._data.setdefault(key, {})
            self._data[key].pop(subkey)
        else:
            self._data.pop(key)
        self.save(True)

    def __getitem__(self, key: str) -> Any:
        """Get data from specific key."""
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        """Set a value in persistent storage."""
        self.set(key, value)

    async def _load(self) -> None:
        """Load data from persistent storage."""
        assert not self._data, "Already loaded"

        def _load() -> dict:
            for filename in self.filename, f"{self.filename}.backup":
                try:
                    _filename = os.path.join(self.server.storage_path, filename)
                    with open(_filename, "r", encoding="utf-8") as _file:
                        return json_loads(_file.read())
                except FileNotFoundError:
                    pass
                except JSON_DECODE_EXCEPTIONS:  # pylint: disable=catching-non-exception
                    LOGGER.error(
                        "Error while reading persistent storage file %s", filename
                    )
                else:
                    LOGGER.debug("Loaded persistent settings from %s", filename)
            LOGGER.debug(
                "Started with empty storage: No persistent storage file found."
            )
            return {}

        loop = asyncio.get_running_loop()
        self._data = await loop.run_in_executor(None, _load)

    def save(self, immediate: bool = False) -> None:
        """Schedule save of data to disk."""
        if self._timer_handle is not None:
            self._timer_handle.cancel()
            self._timer_handle = None

        if immediate:
            self.server.loop.create_task(self.async_save())
        else:
            # schedule the save for later
            self._timer_handle = self.server.loop.call_later(
                DEFAULT_SAVE_DELAY, self.server.loop.create_task, self.async_save()
            )

    async def async_save(self):
        """Save persistent data to disk."""

        def do_save():
            filename_backup = f"{self.filename}.backup"
            # make backup before we write a new file
            if os.path.isfile(self.filename):
                if os.path.isfile(filename_backup):
                    os.remove(filename_backup)
                os.rename(self.filename, filename_backup)

            with open(self.filename, "w", encoding="utf-8") as _file:
                _file.write(json_dumps(self._data))
            LOGGER.debug("Saved data to persistent storage")

        await self.server.loop.run_in_executor(None, do_save)
