"""
Google Drive File System Provider for Music Assistant.

All filesystem/sync/streaming logic lives in CloudFileSystemProvider; this
module only supplies the Google-specific parts: OAuth2 auth (see auth.py),
the folder-ID config check and the three Drive API hooks.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast

from aiohttp import ClientError
from google_drive_api.api import GoogleDriveApi
from google_drive_api.exceptions import AuthException, GoogleDriveApiError
from music_assistant_models.errors import (
    LoginFailed,
    ProviderUnavailableError,
    SetupFailedError,
)

from music_assistant.providers.filesystem_cloud.base import CloudFileSystemProvider

from .auth import MAGoogleDriveAuth
from .constants import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_FOLDER_ID,
    CONF_REFRESH_TOKEN,
    FOLDER_MIME_TYPE,
)

if TYPE_CHECKING:
    from collections.abc import Generator

    from aiohttp import ClientResponse
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.filesystem_cloud.base import RawItem

# fields we ask Google to return for each file
_FILE_FIELDS = "id, name, mimeType, size, modifiedTime"


class GoogleDriveFileSystemProvider(CloudFileSystemProvider):
    """Google Drive File System Provider for Music Assistant."""

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
    ) -> None:
        """Initialize Google Drive FileSystem Provider."""
        # the "root" for this provider is a Google Drive folder ID
        super().__init__(
            mass, manifest, config, cast("str", config.get_value(CONF_FOLDER_ID) or "root")
        )
        self.auth = MAGoogleDriveAuth(
            mass,
            cast("str", config.get_value(CONF_CLIENT_ID)),
            cast("str", config.get_value(CONF_CLIENT_SECRET)),
            cast("str", config.get_value(CONF_REFRESH_TOKEN)),
        )
        self.api = GoogleDriveApi(self.auth)
        self._root_folder_name: str | None = None

    @property
    def instance_name_postfix(self) -> str | None:
        """Return a (default) instance name postfix for this provider instance."""
        return self._root_folder_name

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # verify auth works early so setup fails clearly if creds are wrong
        try:
            await self.api.get_user(params={"fields": "user(emailAddress)"})
        except AuthException as err:
            raise LoginFailed(f"Google Drive authentication failed: {err}") from err
        except GoogleDriveApiError as err:
            raise SetupFailedError(f"Unable to connect to Google Drive: {err}") from err
        # verify the configured folder ID early: users tend to enter the folder
        # name here, but Drive only accepts the opaque ID from the folder URL
        if self.root_folder_id != "root":
            try:
                meta = await self._get_file_meta(self.root_folder_id, "id, name, mimeType")
            except GoogleDriveApiError as err:
                msg = (
                    f"Drive folder ID '{self.root_folder_id}' not found. Use the ID from the "
                    "folder URL (drive.google.com/drive/folders/<ID>), not the folder name."
                )
                raise SetupFailedError(msg) from err
            if meta.get("mimeType") != FOLDER_MIME_TYPE:
                msg = f"Drive item '{self.root_folder_id}' is not a folder."
                raise SetupFailedError(msg)
            self._root_folder_name = cast("str | None", meta.get("name"))
        await self._post_init()

    # ------------------------------------------------------------------
    # cloud API hooks
    # ------------------------------------------------------------------

    async def _api_list_children(self, folder_id: str) -> list[RawItem]:
        """List a Drive folder's children via the files.list API, following pagination."""
        items: list[RawItem] = []
        page_token: str | None = None
        with _translate_errors():
            while True:
                params = {
                    "q": f"'{folder_id}' in parents and trashed = false",
                    "fields": f"nextPageToken, files({_FILE_FIELDS})",
                    "pageSize": 1000,
                }
                if page_token:
                    params["pageToken"] = page_token
                result = await self.api.list_files(params=params)
                for f in result.get("files", []):
                    items.append(
                        (
                            f["id"],
                            f["name"],
                            f.get("mimeType") == FOLDER_MIME_TYPE,
                            f.get("modifiedTime", "unknown"),
                            int(f["size"]) if f.get("size") else None,
                        )
                    )
                page_token = result.get("nextPageToken")
                if not page_token:
                    break
        return items

    async def _api_download_bytes(self, file_id: str) -> bytes:
        """Download a Drive file's full contents."""
        with _translate_errors():
            resp = await self.api.get_file_content(file_id)
            data: bytes = await resp.read()
        return data

    async def _api_download_response(self, file_id: str, headers: dict[str, str]) -> ClientResponse:
        """Open a streaming download for a Drive file."""
        with _translate_errors():
            return await self.api.get_file_content(file_id, headers=headers)

    async def _get_file_meta(self, file_id: str, fields: str) -> dict[str, Any]:
        """
        Fetch a single file's metadata by (Drive) ID.

        The library has no get-single-file helper, so we use its auth object
        (same approach the library uses internally for get_user).
        """
        url = f"https://www.googleapis.com/drive/v3/files/{file_id}"
        result: dict[str, Any] = await self.auth.get_json(url, params={"fields": fields})
        return result


@contextmanager
def _translate_errors() -> Generator[None]:
    """Translate Drive client errors into MA errors."""
    try:
        yield
    except AuthException as err:
        raise LoginFailed(f"Google Drive authentication failed: {err}") from err
    except (GoogleDriveApiError, ClientError) as err:
        raise ProviderUnavailableError(f"Google Drive API error: {err}") from err
