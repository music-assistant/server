"""The files the server keeps of the community catalog: the copy of the index and the state."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from music_assistant.controllers.community_catalog.constants import INDEX_SCHEMA, STATE_SCHEMA
from music_assistant.controllers.community_catalog.models import CatalogIndex, CatalogState
from music_assistant.helpers.json import json_dumps, json_loads

if TYPE_CHECKING:
    import logging

_INDEX_FILE: Final = "index.json"
_STATE_FILE: Final = "state.json"


def parse_index(data: Any, line: str | None = None) -> CatalogIndex:
    """
    Return the index in a parsed JSON document.

    :param data: The parsed document.
    :param line: The line the index must be for, any line when omitted.
    :raises ValueError: The document is not an index of the format and line this server reads.
    :raises LookupError: A field of the index is missing.
    """
    schema = data.get("schema") if isinstance(data, dict) else None
    if schema != INDEX_SCHEMA:
        msg = f"the document has schema {schema}, this server reads {INDEX_SCHEMA}"
        raise ValueError(msg)
    index = CatalogIndex.from_dict(data)
    if line is not None and index.server_line != line:
        msg = f"the index is for line {index.server_line}, this server reads {line}"
        raise ValueError(msg)
    return index


class CatalogStore:
    """
    The kept copy of the index and the state of the community catalog, and their only writer.

    Callers serialize the writes.
    """

    def __init__(self, folder: str, logger: logging.Logger) -> None:
        """
        Initialize the store; nothing is read until load is called.

        :param folder: The folder that holds the files, created by the first write.
        :param logger: The logger to report a file that cannot be read or written on.
        """
        self.logger = logger
        self.index: CatalogIndex | None = None
        self.state = CatalogState()
        self._folder = Path(folder)

    async def load(self) -> None:
        """Read the kept copy and the state; what is missing or unreadable reads as empty."""
        self.index, self.state = await asyncio.to_thread(self._read)

    async def keep_index(self, content: bytes, index: CatalogIndex, etag: str | None) -> None:
        """
        Use a fetched index and keep a copy of it with its ETag.

        An index that cannot be written is used all the same; the ETag then stays the one of
        the kept copy and a warning is logged.

        :param content: The index as fetched.
        :param index: The parsed index.
        :param etag: The ETag the registry sent with the index.
        """
        self.index = index
        state = replace(self.state, index_etag=etag)
        try:
            await asyncio.to_thread(self._write, content, state)
        except OSError as err:
            self.logger.warning("Could not keep a copy of the community catalog: %s", err)
        else:
            self.state = state

    async def save_state(self, state: CatalogState) -> None:
        """
        Write the state and use it.

        :param state: The new state.
        :raises OSError: The state cannot be written; the state in use stays as it was.
        """
        await asyncio.to_thread(self._write, None, state)
        self.state = state

    def _read(self) -> tuple[CatalogIndex | None, CatalogState]:
        """Return the kept copy and the state (blocking)."""
        index, state = None, CatalogState()
        try:
            data = self._read_file(_STATE_FILE)
            # a missing state, or one of another format, reads as empty
            if isinstance(data, dict) and data.get("schema") == STATE_SCHEMA:
                state = CatalogState.from_dict(data)
            if (data := self._read_file(_INDEX_FILE)) is not None:
                index = parse_index(data)
        except (OSError, ValueError, LookupError) as err:
            self.logger.warning("Could not read the kept copy of the community catalog: %s", err)
        return index, state

    def _read_file(self, name: str) -> Any:
        """
        Return the parsed content of a file in the folder, or None when it does not exist (blocking).

        :param name: The name of the file.
        """
        try:
            return json_loads((self._folder / name).read_bytes())
        except FileNotFoundError:
            return None

    def _write(self, index: bytes | None, state: CatalogState) -> None:
        """
        Write the state and, when given, the kept copy, each replacing its file at once (blocking).

        :param index: The content of the kept copy, None to leave it as it is.
        :param state: The state.
        """
        self._folder.mkdir(parents=True, exist_ok=True)
        if index is not None:
            _write_file(self._folder / _INDEX_FILE, index)
        _write_file(self._folder / _STATE_FILE, json_dumps(state.to_dict()).encode())


def _write_file(path: Path, content: bytes) -> None:
    """
    Replace a file at once, so a crash never leaves it half written (blocking).

    :param path: The file to replace.
    :param content: The new content.
    """
    temp_path = path.with_name(f"{path.name}.tmp")
    with temp_path.open("wb") as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())
    temp_path.replace(path)
