"""
OneDrive File System Provider for Music Assistant.

All filesystem/sync/streaming logic lives in CloudFileSystemProvider; this
module only supplies the OneDrive-specific parts: OAuth2 auth (see auth.py),
the folder-path resolution and the three Graph API hooks.

Listing/metadata goes through HA's onedrive-personal-sdk, but downloads go
straight to Microsoft Graph so we can forward Range headers (needed for
seeking) and read the response headers - the SDK's download helper exposes
neither.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from urllib.parse import quote

from aiohttp import ClientError
from music_assistant_models.errors import (
    LoginFailed,
    ProviderUnavailableError,
    SetupFailedError,
)
from onedrive_personal_sdk.clients.client import OneDriveClient
from onedrive_personal_sdk.exceptions import AuthenticationError, OneDriveException
from onedrive_personal_sdk.models.items import Folder

from music_assistant.providers.filesystem_cloud.base import CloudFileSystemProvider

from .auth import MAOneDriveAuth
from .constants import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_FOLDER_ID,
    CONF_REFRESH_TOKEN,
    GRAPH_BASE_URL,
)

if TYPE_CHECKING:
    from aiohttp import ClientResponse
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.filesystem_cloud.base import RawItem


class OneDriveFileSystemProvider(CloudFileSystemProvider):
    """OneDrive File System Provider for Music Assistant."""

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
    ) -> None:
        """Initialize OneDrive FileSystem Provider."""
        # the configured "root" is a folder path; handle_async_init resolves it
        # to the Graph item ID everything else works off
        super().__init__(
            mass, manifest, config, cast("str", config.get_value(CONF_FOLDER_ID) or "root")
        )
        self.auth = MAOneDriveAuth(
            mass,
            config.instance_id,
            cast("str", config.get_value(CONF_CLIENT_ID)),
            cast("str", config.get_value(CONF_CLIENT_SECRET)),
            cast("str", config.get_value(CONF_REFRESH_TOKEN)),
        )
        # the SDK just needs a coroutine that returns a fresh access token
        self.client = OneDriveClient(self.auth.async_get_access_token, mass.http_session)
        self._root_folder_name: str | None = None

    @property
    def instance_name_postfix(self) -> str | None:
        """Return a (default) instance name postfix for this provider instance."""
        return self._root_folder_name

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # a single API call validates auth and (if configured) the folder path
        if self.root_folder_id == "root":
            try:
                await self.client.get_drive_item("root")
            except AuthenticationError as err:
                raise LoginFailed(f"OneDrive authentication failed: {err}") from err
            except OneDriveException as err:
                raise SetupFailedError(f"Unable to connect to OneDrive: {err}") from err
        else:
            await self._resolve_root_folder()
        await self._post_init()

    # ------------------------------------------------------------------
    # cloud API hooks
    # ------------------------------------------------------------------

    async def _api_list_children(self, folder_id: str) -> list[RawItem]:
        """List a OneDrive folder's children."""
        try:
            # the SDK follows pagination internally
            items = await self.client.list_drive_items(folder_id)
        except AuthenticationError as err:
            raise LoginFailed(f"OneDrive authentication failed: {err}") from err
        except OneDriveException as err:
            raise ProviderUnavailableError(f"OneDrive API error: {err}") from err
        out: list[RawItem] = []
        for item in items:
            if isinstance(item, Folder):
                out.append((item.id, item.name, True, "folder", item.size))
                continue
            # quickXorHash is a stable content hash; not every file has one,
            # so fall back to the size
            checksum = item.hashes.quick_xor_hash or str(item.size)
            out.append((item.id, item.name, False, checksum, item.size))
        return out

    async def _api_download_bytes(self, file_id: str) -> bytes:
        """Download a OneDrive file's full contents."""
        try:
            stream = await self.client.download_drive_item(file_id)
            return await stream.read()
        except AuthenticationError as err:
            raise LoginFailed(f"OneDrive authentication failed: {err}") from err
        except OneDriveException as err:
            raise ProviderUnavailableError(f"OneDrive API error: {err}") from err

    async def _api_download_response(self, file_id: str, headers: dict[str, str]) -> ClientResponse:
        """Open a streaming download for a OneDrive file."""
        # go direct to Graph instead of through the SDK so Range headers are
        # forwarded and the response headers stay available
        token = await self.auth.async_get_access_token()
        url = f"{GRAPH_BASE_URL}/me/drive/items/{file_id}/content"
        req_headers = {"Authorization": f"Bearer {token}", **headers}
        try:
            # Graph 302-redirects to a pre-signed download URL; aiohttp follows
            # it and drops the auth header on the cross-host hop
            return await self.mass.http_session.get(url, headers=req_headers)
        except ClientError as err:
            raise ProviderUnavailableError(f"OneDrive API error: {err}") from err

    async def _resolve_root_folder(self) -> None:
        """Resolve the configured folder path to the Graph item ID it maps to."""
        path = self.root_folder_id.strip("/")
        token = await self.auth.async_get_access_token()
        # the SDK's /items/{id} syntax only takes item IDs, so address the
        # folder by path via Graph's root-relative syntax
        url = f"{GRAPH_BASE_URL}/me/drive/root:/{quote(path)}"
        try:
            async with self.mass.http_session.get(
                url, headers={"Authorization": f"Bearer {token}"}
            ) as resp:
                if resp.status in (401, 403):
                    raise LoginFailed(f"OneDrive authentication failed: {await resp.text()}")
                if resp.status == 404:
                    raise SetupFailedError(f"Folder '{path}' not found in your OneDrive")
                resp.raise_for_status()
                item = await resp.json()
        except ClientError as err:
            raise SetupFailedError(f"Unable to connect to OneDrive: {err}") from err
        if "folder" not in item:
            raise SetupFailedError(f"OneDrive item '{path}' is not a folder")
        self.root_folder_id = item["id"]
        self._root_folder_name = item["name"]
