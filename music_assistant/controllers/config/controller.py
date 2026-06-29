"""Logic to handle storage of persistent (configuration) settings."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import aiofiles
from aiofiles.os import wrap
from cryptography.fernet import Fernet, InvalidToken
from music_assistant_models import config_entries
from music_assistant_models.errors import InvalidDataError

from music_assistant.constants import (
    CONF_ONBOARD_DONE,
    CONF_SERVER_ID,
    ENCRYPT_SUFFIX,
)
from music_assistant.controllers.config.constants import DEFAULT_SAVE_DELAY
from music_assistant.controllers.config.core import CoreConfigMixin
from music_assistant.controllers.config.dsp import DSPConfigMixin
from music_assistant.controllers.config.migrations import migrate
from music_assistant.controllers.config.players import PlayerConfigMixin
from music_assistant.controllers.config.providers import ProviderConfigMixin
from music_assistant.controllers.config.queues import PlayerQueueConfigMixin
from music_assistant.helpers.json import JSON_DECODE_EXCEPTIONS, async_json_dumps, async_json_loads

if TYPE_CHECKING:
    from music_assistant import MusicAssistant

LOGGER = logging.getLogger(__name__)

isfile = wrap(os.path.isfile)
remove = wrap(os.remove)
rename = wrap(os.rename)


class ConfigController(
    ProviderConfigMixin,
    PlayerConfigMixin,
    PlayerQueueConfigMixin,
    DSPConfigMixin,
    CoreConfigMixin,
):
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
        if not self.onboard_done:
            self.mass.register_api_command(
                "config/onboard_complete",
                self.set_onboard_complete,
                authenticated=True,
                alias=True,  # hide from public API docs
            )
        LOGGER.debug("Started.")

    @property
    def onboard_done(self) -> bool:
        """Return True if onboarding is done."""
        return bool(self.get(CONF_ONBOARD_DONE, False))

    async def set_onboard_complete(self) -> None:
        """
        Mark onboarding as complete.

        This is called by the frontend after the user has completed the onboarding wizard.
        Only available when onboarding is not yet complete.
        """
        if self.onboard_done:
            msg = "Onboarding already completed"
            raise InvalidDataError(msg)

        self.set(CONF_ONBOARD_DONE, True)
        self.save(immediate=True)
        LOGGER.info("Onboarding completed")

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
            if subkey not in parent:
                # requesting subkey from a non existing parent
                return default
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
                parent[subkey] = value
            else:
                parent.setdefault(subkey, {})
                parent = parent[subkey]
        self.save()

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

    def save(self, immediate: bool = False) -> None:
        """Schedule save of data to disk."""
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
        assert self._fernet is not None
        return ENCRYPT_SUFFIX + self._fernet.encrypt(str_value.encode()).decode()

    def decrypt_string(self, encrypted_str: str) -> str:
        """Decrypt a (password)string with Fernet."""
        if not encrypted_str:
            return encrypted_str
        if not encrypted_str.startswith(ENCRYPT_SUFFIX):
            return encrypted_str
        assert self._fernet is not None
        try:
            return self._fernet.decrypt(encrypted_str.replace(ENCRYPT_SUFFIX, "").encode()).decode()
        except InvalidToken as err:
            msg = "Password decryption failed"
            raise InvalidDataError(msg) from err

    async def _load(self) -> None:
        """Load data from persistent storage."""
        assert not self._data, "Already loaded"

        for filename in (self.filename, f"{self.filename}.backup"):
            try:
                async with aiofiles.open(filename, encoding="utf-8") as _file:
                    self._data = await async_json_loads(await _file.read())
                    LOGGER.debug("Loaded persistent settings from %s", filename)
                    if await migrate(self._data):
                        await self._async_save()
                    return
            except FileNotFoundError:
                pass
            except JSON_DECODE_EXCEPTIONS:
                LOGGER.exception("Error while reading persistent storage file %s", filename)
        LOGGER.debug("Started with empty storage: No persistent storage file found.")

    async def _async_save(self) -> None:
        """Save persistent data to disk."""
        filename_backup = f"{self.filename}.backup"
        # make backup before we write a new file
        if await isfile(self.filename):
            with contextlib.suppress(FileNotFoundError):
                await remove(filename_backup)
            await rename(self.filename, filename_backup)

        async with aiofiles.open(self.filename, "w", encoding="utf-8") as _file:
            await _file.write(await async_json_dumps(self._data, indent=True))
        LOGGER.debug("Saved data to persistent storage")
