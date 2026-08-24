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

from music_assistant.providers.filesystem_cloud.base import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_FOLDER_ID,
    CONF_REFRESH_TOKEN,
    CloudFileSystemProvider,
    read_setup_value,
)
from music_assistant.providers.filesystem_local.constants import (
    CONF_CONTENT_TYPE,
    CONF_ENTRY_CONTENT_TYPE,
    CONF_ENTRY_IGNORE_ALBUM_PLAYLISTS,
    CONF_ENTRY_LIBRARY_SYNC_AUDIOBOOKS,
    CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS,
    CONF_ENTRY_LIBRARY_SYNC_PODCASTS,
    CONF_ENTRY_LIBRARY_SYNC_TRACKS,
    CONF_ENTRY_MISSING_ALBUM_ARTIST,
    CONF_ENTRY_PROPAGATE_GENRES,
    content_type_config_entry,
)

from .auth import MAOneDriveAuth
from .constants import GRAPH_BASE_URL

if TYPE_CHECKING:
    from aiohttp import ClientResponse
    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
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
        # the configured "root" is a folder path; handle_async_init resolves it to the Graph
        # item ID everything else works off. Read it setup-data-aware here since the instance
        # (and self.get_setup_value) does not exist yet
        super().__init__(
            mass,
            manifest,
            config,
            cast("str", read_setup_value(mass, config, CONF_FOLDER_ID) or "root"),
        )
        self.auth = MAOneDriveAuth(
            mass,
            config.instance_id,
            cast("str", self.get_setup_value(CONF_CLIENT_ID)),
            cast("str", self.get_setup_value(CONF_CLIENT_SECRET)),
            cast("str", self.get_setup_value(CONF_REFRESH_TOKEN)),
        )
        # the SDK just needs a coroutine that returns a fresh access token
        self.client = OneDriveClient(self.auth.async_get_access_token, mass.http_session)
        self._root_folder_name: str | None = None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """
        Return Config entries to setup this provider.

        Credentials, the content type and root folder are collected by the setup flow (see
        setup_flow.py); only the genuine sync options are configurable here.
        """
        # the content type is set by the setup flow; surface it read-only so the sync
        # options' depends_on chains still resolve
        content_type = str(
            self.get_setup_value(CONF_CONTENT_TYPE, CONF_ENTRY_CONTENT_TYPE.default_value)
        )
        return (
            content_type_config_entry(content_type),
            CONF_ENTRY_MISSING_ALBUM_ARTIST,
            CONF_ENTRY_IGNORE_ALBUM_PLAYLISTS,
            CONF_ENTRY_LIBRARY_SYNC_TRACKS,
            CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS,
            CONF_ENTRY_LIBRARY_SYNC_PODCASTS,
            CONF_ENTRY_LIBRARY_SYNC_AUDIOBOOKS,
            CONF_ENTRY_PROPAGATE_GENRES,
        )

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
            # prefer a real content hash; quickXorHash is not always present (e.g. some
            # business accounts only compute SHA1/SHA256), and no hash at all is possible for
            # very large or still-processing files, in which case the size is the last resort
            checksum = (
                item.hashes.quick_xor_hash
                or item.hashes.sha256_hash
                or item.hashes.sha1_hash
                or str(item.size)
            )
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
