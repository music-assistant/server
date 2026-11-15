"""The requests for the index of the community catalog."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, Final

from aiohttp import ClientError, ClientTimeout, hdrs

from music_assistant.controllers.community_catalog.store import parse_index
from music_assistant.helpers.build_info import release_channel
from music_assistant.helpers.json import json_loads

if TYPE_CHECKING:
    import logging

    from music_assistant.controllers.community_catalog.store import CatalogStore
    from music_assistant.mass import MusicAssistant

INDEX_URL: Final = "https://music-assistant.github.io/community-catalog/v1/{line}.json"
_TIMEOUT: Final = ClientTimeout(total=10)


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
    """The requests that keep the kept copy of the catalog index current."""

    def __init__(self, mass: MusicAssistant, store: CatalogStore, logger: logging.Logger) -> None:
        """
        Initialize the client.

        :param mass: The Music Assistant instance.
        :param store: The store that holds the kept copy.
        :param logger: The logger to report a failed fetch on.
        """
        self.mass = mass
        self.logger = logger
        self.store = store

    async def fetch(self) -> bool:
        """
        Fetch the index of this server's line and keep it when it is valid.

        Any failure keeps the copy and logs a warning. Returns whether the index changed.
        """
        line = server_line(self.mass.version)
        url = INDEX_URL.format(line=line)
        headers: dict[str, str] = {}
        if self.store.index is not None and self.store.state.index_etag:
            headers[hdrs.IF_NONE_MATCH] = self.store.state.index_etag
        try:
            async with self.mass.http_session.get(
                url, headers=headers, timeout=_TIMEOUT
            ) as response:
                if response.status == HTTPStatus.NOT_MODIFIED:
                    return False
                response.raise_for_status()
                content = await response.read()
                etag = response.headers.get(hdrs.ETAG)
            index = parse_index(json_loads(content), line)
        except TimeoutError:
            self.logger.warning("Could not fetch the community catalog from %s: no answer", url)
            return False
        except (ClientError, ValueError, LookupError) as err:
            self.logger.warning("Could not fetch the community catalog from %s: %s", url, err)
            return False
        changed = index != self.store.index
        await self.store.keep_index(content, index, etag)
        return changed
