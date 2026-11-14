"""The index of the community catalog, and the copy of it that the server keeps."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from aiohttp import ClientError, ClientTimeout, hdrs

from music_assistant.controllers.community_catalog.constants import INDEX_SCHEMA, STATE_SCHEMA
from music_assistant.controllers.community_catalog.models import CatalogIndex, CatalogState
from music_assistant.helpers.build_info import release_channel
from music_assistant.helpers.json import json_dumps, json_loads

if TYPE_CHECKING:
    import logging

    from music_assistant.mass import MusicAssistant

INDEX_URL: Final = "https://music-assistant.github.io/community-catalog/v1/{line}.json"
_TIMEOUT: Final = ClientTimeout(total=10)
_INDEX_FILE: Final = "index.json"
_STATE_FILE: Final = "state.json"


def server_line(version: str) -> str:
    """
    Return the line of the catalog a server version reads: nightly, beta or <major>.<minor>.

    :param version: The version of the server, as in MusicAssistant.version.
    """
    if (channel := release_channel(version)) != "stable":
        return channel
    major, minor = version.split(".")[:2]
    return f"{major}.{minor}"


class IndexClient:
    """The kept copy of the catalog index, and the requests that keep it current."""

    def __init__(self, mass: MusicAssistant, folder: str, logger: logging.Logger) -> None:
        """
        Initialize the client; nothing is read until load is called.

        :param mass: The Music Assistant instance.
        :param folder: The folder that holds the kept copy, created by the first successful fetch.
        :param logger: The logger to report a failed fetch on.
        """
        self.mass = mass
        self.logger = logger
        self.index: CatalogIndex | None = None
        self.state = CatalogState()
        self._folder = Path(folder)

    async def load(self) -> None:
        """Read the kept copy and the state; what is missing or unreadable reads as empty."""
        self.index, self.state = await asyncio.to_thread(self._read)

    async def fetch(self) -> bool:
        """
        Fetch the index of this server's line and keep it when it is valid.

        Any failure keeps the copy and logs a warning; an index that cannot be written is used
        all the same. Returns whether the index changed.
        """
        line = server_line(self.mass.version)
        url = INDEX_URL.format(line=line)
        headers: dict[str, str] = {}
        if self.index is not None and self.state.index_etag:
            headers[hdrs.IF_NONE_MATCH] = self.state.index_etag
        try:
            async with self.mass.http_session.get(
                url, headers=headers, timeout=_TIMEOUT
            ) as response:
                if response.status == HTTPStatus.NOT_MODIFIED:
                    return False
                response.raise_for_status()
                content = await response.read()
                etag = response.headers.get(hdrs.ETAG)
            index = _parse_index(json_loads(content), line)
        except TimeoutError:
            self.logger.warning("Could not fetch the community catalog from %s: no answer", url)
            return False
        except (ClientError, ValueError, LookupError) as err:
            self.logger.warning("Could not fetch the community catalog from %s: %s", url, err)
            return False
        changed = index != self.index
        self.index = index
        state = replace(self.state, index_etag=etag)
        try:
            await asyncio.to_thread(self._write, content, state)
        except OSError as err:
            # the ETag stays the one of the kept copy, so the next fetch tries to keep it again
            self.logger.warning("Could not keep a copy of the community catalog: %s", err)
        else:
            self.state = state
        return changed

    def _read(self) -> tuple[CatalogIndex | None, CatalogState]:
        """Return the kept copy and the state (blocking)."""
        index, state = None, CatalogState()
        try:
            data = self._read_file(_STATE_FILE)
            # a missing state, or one of another format, reads as empty
            if isinstance(data, dict) and data.get("schema") == STATE_SCHEMA:
                state = CatalogState.from_dict(data)
            if (data := self._read_file(_INDEX_FILE)) is not None:
                index = _parse_index(data)
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

    def _write(self, content: bytes, state: CatalogState) -> None:
        """Write the kept copy and the state, each replacing its file at once (blocking)."""
        self._folder.mkdir(parents=True, exist_ok=True)
        _write_file(self._folder / _INDEX_FILE, content)
        _write_file(self._folder / _STATE_FILE, json_dumps(state.to_dict()).encode())


def _parse_index(data: Any, line: str | None = None) -> CatalogIndex:
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
