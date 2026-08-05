"""Logic to handle storage of persistent (configuration) settings."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import aiofiles
from cryptography.fernet import Fernet, InvalidToken
from music_assistant_models import config_entries
from music_assistant_models.errors import InvalidDataError

from music_assistant.constants import (
    CONF_ENCRYPTION_KEY,
    CONF_ENCRYPTION_KEY_MIGRATED,
    CONF_ONBOARD_DONE,
    CONF_SERVER_ID,
    ENCRYPT_SUFFIX,
)
from music_assistant.controllers.config.constants import DEFAULT_SAVE_DELAY
from music_assistant.controllers.config.core import CoreConfigMixin
from music_assistant.controllers.config.dsp import DSPConfigMixin
from music_assistant.controllers.config.flows import SetupFlowMixin
from music_assistant.controllers.config.migrations import (
    migrate,
    migrate_hass_engine_selection,
    migrate_nfs_subfolder_into_export_path,
    migrate_provider_setup_data,
)
from music_assistant.controllers.config.players import PlayerConfigMixin
from music_assistant.controllers.config.providers import ProviderConfigMixin
from music_assistant.controllers.config.queues import PlayerQueueConfigMixin
from music_assistant.helpers.json import (
    JSON_DECODE_EXCEPTIONS,
    async_json_dumps,
    async_json_loads,
    json_loads,
)

if TYPE_CHECKING:
    from music_assistant import MusicAssistant

LOGGER = logging.getLogger(__name__)


class ConfigController(
    ProviderConfigMixin,
    PlayerConfigMixin,
    PlayerQueueConfigMixin,
    DSPConfigMixin,
    CoreConfigMixin,
    SetupFlowMixin,
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
        self._save_requested = 0
        self._save_written = 0
        self._save_lock = asyncio.Lock()
        self._disk_lock = threading.Lock()

    async def setup(self) -> None:
        """Async initialize of controller."""
        await self._load()
        self.initialized = True
        # create default server ID if needed
        self.set_default(CONF_SERVER_ID, uuid4().hex)
        self._init_encryption()
        config_entries.ENCRYPT_CALLBACK = self.encrypt_string
        config_entries.DECRYPT_CALLBACK = self.decrypt_string
        # one-off: move pre-setup-flow provider values into (encrypted) setup_data.
        # runs here, after encryption is initialized, so string values are encrypted
        # at rest (the migrate() pass in _load() runs before encryption is available).
        setup_data_migrated = migrate_provider_setup_data(self._data, self.encrypt_string)
        # one-off: fold a stored NFS subfolder into its export path. Same phase and reason as
        # above, and after it so a legacy install's keys have landed in setup_data by now.
        # TODO: remove after 2.10 release
        nfs_subfolder_migrated = migrate_nfs_subfolder_into_export_path(
            self._data, self.encrypt_string, self.decrypt_string
        )
        if setup_data_migrated or nfs_subfolder_migrated:
            self.save(immediate=True)
        # one-off: hand the Home Assistant plugin's former single TTS/AI entity choice over to
        # the providers that select their own engine now. Runs here for the same reason: the
        # ai_radio selection lands in its encrypted setup_data.
        if migrate_hass_engine_selection(self._data, self.encrypt_string):
            self.save(immediate=True)
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
        if self._timer_handle is not None:
            self._timer_handle.cancel()
            self._timer_handle = None
        if self._save_written != self._save_requested:
            # the latest change never made it to disk: its save is either still waiting
            # out the debounce delay or was cancelled on stop, so write it here
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

    def set(self, key: str, value: Any, immediate: bool = False) -> None:
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
        self.save(immediate=immediate)

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

        self._save_requested += 1
        if immediate:
            self.mass.loop.create_task(self._async_save())
        else:
            # schedule the save for later
            self._timer_handle = self.mass.loop.call_later(DEFAULT_SAVE_DELAY, self._start_save)

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

    def _init_encryption(self) -> None:
        """Set up encryption for SECURE_STRING config values."""
        self._fernet = self._load_or_create_encryption_key()
        if not self.get(CONF_ENCRYPTION_KEY_MIGRATED):
            self._migrate_legacy_secrets()
            self.set(CONF_ENCRYPTION_KEY_MIGRATED, True)

    def _load_or_create_encryption_key(self) -> Fernet:
        """Return the stored encryption key, generating a new one if it is absent or invalid."""
        encryption_key: Any = self.get(CONF_ENCRYPTION_KEY, "")
        if isinstance(encryption_key, str) and encryption_key:
            try:
                return Fernet(encryption_key.encode())
            except ValueError:
                LOGGER.warning("Stored encryption key is invalid; generating a new one")
                self.set(CONF_ENCRYPTION_KEY_MIGRATED, False)
        encryption_key = Fernet.generate_key().decode()
        self.set(CONF_ENCRYPTION_KEY, encryption_key)
        return Fernet(encryption_key.encode())

    def _migrate_legacy_secrets(self) -> None:
        """One-time re-encryption of secrets that were encrypted with the server_id-derived key."""
        server_id: str = self.get(CONF_SERVER_ID)
        assert server_id
        legacy_fernet = Fernet(base64.urlsafe_b64encode(server_id.encode()[:32]))
        migrated = self._rotate_encrypted_values(self._data, legacy_fernet)
        if migrated:
            LOGGER.info("Re-encrypted %s secret(s) with the dedicated encryption key", migrated)
            self.save(immediate=True)

    def _rotate_encrypted_values(self, node: Any, legacy_fernet: Fernet) -> int:
        """Recursively re-encrypt legacy-encrypted values, returning the count."""
        assert self._fernet is not None
        count = 0
        values = node.items() if isinstance(node, dict) else enumerate(node)
        for key, value in values:
            if isinstance(value, (dict, list)):
                count += self._rotate_encrypted_values(value, legacy_fernet)
            elif isinstance(value, str) and value.startswith(ENCRYPT_SUFFIX):
                token = value[len(ENCRYPT_SUFFIX) :].encode()
                try:
                    decrypted = legacy_fernet.decrypt(token)
                except InvalidToken:
                    continue
                node[key] = ENCRYPT_SUFFIX + self._fernet.encrypt(decrypted).decode()
                count += 1
        return count

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

    def _start_save(self) -> None:
        """Start the save task, called by the save timer."""
        self._timer_handle = None
        self.mass.create_task(self._async_save)

    async def _async_save(self) -> None:
        """Save persistent data to disk."""
        async with self._save_lock:
            # remember which change we are about to write: anything requested after this
            # point is not part of it, and must leave the settings marked as unsaved
            requested = self._save_requested
            json_data = await async_json_dumps(self._data, indent=True)
            await asyncio.to_thread(self._save_to_disk, json_data)
            self._save_written = requested
        LOGGER.debug("Saved data to persistent storage")

    def _save_to_disk(self, json_data: str) -> None:
        """Atomically write the settings file to disk, rotating the previous one to backup."""
        # cancelling a save does not stop the worker thread it already handed the write
        # to, so _save_lock is released while this is still running. guard the file
        # itself here, in the thread that actually writes it, or a second writer would
        # race this one over the same temp file and leave no settings at all
        with self._disk_lock:
            filename = Path(self.filename)
            filename_temp = Path(f"{self.filename}.tmp")
            with filename_temp.open("w", encoding="utf-8") as _file:
                _file.write(json_data)
                _file.flush()
                # fsync so a power failure can not leave a zero-length file behind (#5716)
                os.fsync(_file.fileno())
            with contextlib.suppress(FileNotFoundError, *JSON_DECODE_EXCEPTIONS):
                # only rotate a parseable file to the backup, so a corrupt
                # (crash leftover) file can never clobber a possibly good backup
                json_loads(filename.read_bytes())
                filename.replace(f"{self.filename}.backup")
            filename_temp.replace(filename)
            # best effort: fsync the directory as well so the renames themselves
            # survive a power failure (not supported on all platforms/filesystems)
            with contextlib.suppress(OSError):
                dir_fd = os.open(os.path.dirname(self.filename), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
