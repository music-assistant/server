"""
Async Yandex Disk API wrapper built on the yadisk library.

Owns a yadisk ``AsyncClient`` bound to Music Assistant's shared aiohttp session
(never closing it) and exposes the small surface the ``CloudFileSystemProvider``
hooks need: folder listing, small-file download, and a streaming download
response that honours HTTP Range.

Access tokens come from :class:`~provider.auth.MAYandexDiskAuth`, which refreshes
them with Yandex as needed; a rejected refresh surfaces as ``LoginFailed``. The
Yandex Disk REST API is path-addressed, so resource paths (``disk:/...``) double
as the opaque "file id" the base class passes around.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiohttp
import yadisk
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ProviderUnavailableError,
)
from yadisk.exceptions import (
    PathNotFoundError,
    TooManyRequestsError,
    UnauthorizedError,
    YaDiskError,
)
from yadisk.sessions.aiohttp_session import AIOHTTPSession

if TYPE_CHECKING:
    from music_assistant import MusicAssistant
    from music_assistant.providers.filesystem_cloud.base import RawItem

    from .auth import MAYandexDiskAuth

# fields requested per resource to keep listings slim
_FIELDS = ("name", "path", "type", "size", "md5", "modified")


class _SharedAIOHTTPSession(AIOHTTPSession):
    """AIOHTTPSession that reuses MA's shared ClientSession and never closes it."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """
        Wrap an existing session without taking ownership of it.

        :param session: Music Assistant's shared aiohttp ClientSession.
        """
        # deliberately skip AIOHTTPSession.__init__ (it creates its own session)
        self._session = session

    async def close(self) -> None:
        """No-op: Music Assistant owns the shared session's lifecycle."""


def _to_raw_item(resource: object) -> RawItem:
    """
    Map a yadisk resource object to the base's ``RawItem`` tuple.

    :param resource: A yadisk (Async)ResourceObject.
    :returns: ``(id, name, is_dir, checksum, size, metadata_token)`` where id is
        the disk path.
    """
    is_dir = getattr(resource, "type", None) == "dir"
    checksum = "" if is_dir else str(getattr(resource, "md5", None) or "unknown")
    size = None if is_dir else getattr(resource, "size", None)
    metadata_token = str(getattr(resource, "modified", None) or "") or None
    return (
        str(getattr(resource, "path", "")),
        str(getattr(resource, "name", "")),
        is_dir,
        checksum,
        size,
        metadata_token,
    )


class YandexDiskApi:
    """Thin async facade over yadisk for the filesystem provider."""

    def __init__(self, mass: MusicAssistant, auth: MAYandexDiskAuth) -> None:
        """
        Initialise the API wrapper.

        :param mass: The MusicAssistant instance (for its shared http session).
        :param auth: The auth helper that supplies fresh access tokens.
        """
        self.mass = mass
        self._auth = auth
        self._client = yadisk.AsyncClient(
            token="",
            session=_SharedAIOHTTPSession(mass.http_session),
        )

    async def validate(self) -> None:
        """
        Verify the credentials are accepted by Yandex Disk.

        :raises LoginFailed: The token is missing or rejected.
        :raises ProviderUnavailableError: A transient failure reaching Yandex.
        """
        await self._refresh_client_token()
        try:
            if not await self._client.check_token():
                raise LoginFailed("Yandex Disk token was rejected; re-authorize")
        except UnauthorizedError as err:
            raise LoginFailed(f"Yandex Disk token was rejected: {err}") from err
        except YaDiskError as err:
            raise ProviderUnavailableError(f"Yandex Disk API error: {err}") from err

    async def list_children(self, folder_path: str) -> list[RawItem]:
        """
        List a folder's children (yadisk auto-paginates).

        :param folder_path: Disk path of the folder (``disk:/...``).
        :returns: One ``RawItem`` per child.
        """
        await self._refresh_client_token()
        try:
            items: list[RawItem] = []
            async for entry in self._client.listdir(folder_path, fields=list(_FIELDS)):
                items.append(_to_raw_item(entry))
            return items
        except UnauthorizedError as err:
            raise LoginFailed(f"Yandex Disk token was rejected: {err}") from err
        except PathNotFoundError as err:
            raise MediaNotFoundError(f"Yandex Disk folder not found: {folder_path}") from err
        except TooManyRequestsError as err:
            raise ProviderUnavailableError(f"Yandex Disk rate limited: {err}") from err
        except YaDiskError as err:
            raise ProviderUnavailableError(f"Yandex Disk API error: {err}") from err

    async def download_bytes(self, file_path: str) -> bytes:
        """
        Download a small file's full contents (nfo/m3u/lrc/images).

        :param file_path: Disk path of the file.
        :returns: The file contents.
        """
        link = await self._download_link(file_path)
        try:
            async with self.mass.http_session.get(link) as resp:
                resp.raise_for_status()
                return await resp.read()
        except aiohttp.ClientError as err:
            raise ProviderUnavailableError(f"Yandex Disk download failed: {err}") from err

    async def download_response(
        self, file_path: str, headers: dict[str, str]
    ) -> aiohttp.ClientResponse:
        """
        Open a streaming download for a file (fresh pre-signed href per call).

        :param file_path: Disk path of the file.
        :param headers: Request headers (may include ``Range`` for seeking).
        :returns: An open aiohttp response; the caller closes it.
        """
        link = await self._download_link(file_path)
        try:
            # pre-signed downloader href: no Authorization header needed
            return await self.mass.http_session.get(link, headers=headers)
        except aiohttp.ClientError as err:
            raise ProviderUnavailableError(f"Yandex Disk stream failed: {err}") from err

    async def exists_dir(self, path: str) -> bool:
        """
        Return True if *path* exists and is a directory.

        :param path: Disk path to check.
        :returns: Whether the path is an existing directory.
        """
        await self._refresh_client_token()
        try:
            return await self._client.is_dir(path)
        except UnauthorizedError as err:
            raise LoginFailed(f"Yandex Disk token was rejected: {err}") from err
        except YaDiskError as err:
            raise ProviderUnavailableError(f"Yandex Disk API error: {err}") from err

    async def close(self) -> None:
        """Release the yadisk client (does not close MA's shared session)."""
        await self._client.close()

    async def _refresh_client_token(self) -> None:
        """Set a currently-valid access token on the yadisk client."""
        self._client.token = await self._auth.async_get_access_token()

    async def _download_link(self, file_path: str) -> str:
        """Fetch a fresh, short-lived pre-signed download href for *file_path*."""
        await self._refresh_client_token()
        try:
            return await self._client.get_download_link(file_path)
        except UnauthorizedError as err:
            raise LoginFailed(f"Yandex Disk token was rejected: {err}") from err
        except PathNotFoundError as err:
            raise MediaNotFoundError(f"Yandex Disk file not found: {file_path}") from err
        except TooManyRequestsError as err:
            raise ProviderUnavailableError(f"Yandex Disk rate limited: {err}") from err
        except YaDiskError as err:
            raise ProviderUnavailableError(f"Yandex Disk API error: {err}") from err
