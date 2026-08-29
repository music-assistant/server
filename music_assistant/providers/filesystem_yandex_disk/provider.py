"""
Yandex Disk filesystem provider.

All filesystem/sync/streaming logic lives in ``CloudFileSystemProvider``; this
module supplies only the Yandex-specific parts: the yadisk-backed API hooks,
root-path validation and auth wiring. The path-addressed Yandex Disk API uses
resource paths (``disk:/...``) as the opaque "file id" the base expects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.errors import SetupFailedError

from music_assistant.providers.filesystem_cloud.base import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_FOLDER_ID,
    CONF_REFRESH_TOKEN,
    CloudFileSystemProvider,
    read_setup_value,
)

from .api_client import YandexDiskApi
from .auth import MAYandexDiskAuth
from .constants import DISK_ROOT

if TYPE_CHECKING:
    import aiohttp
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.providers.filesystem_cloud.base import RawItem

_LEGACY_CONF_ROOT_PATH = "root_path"


class YandexDiskFileSystemProvider(CloudFileSystemProvider):
    """Yandex Disk filesystem provider for Music Assistant."""

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
    ) -> None:
        """
        Initialize the Yandex Disk provider.

        :param mass: The MusicAssistant instance.
        :param manifest: The provider manifest.
        :param config: The provider (instance) configuration.
        """
        legacy_root = config.get_value(_LEGACY_CONF_ROOT_PATH)
        folder_id = cast(
            "str",
            read_setup_value(mass, config, CONF_FOLDER_ID, legacy_root) or "root",
        )
        root_path = DISK_ROOT if folder_id == "root" else folder_id
        super().__init__(mass, manifest, config, root_path)
        auth = MAYandexDiskAuth(
            mass,
            cast("str", self.get_setup_value(CONF_CLIENT_ID) or ""),
            cast("str", self.get_setup_value(CONF_CLIENT_SECRET) or ""),
            cast("str", self.get_setup_value(CONF_REFRESH_TOKEN) or ""),
            lambda token: self._update_setup_data(
                CONF_REFRESH_TOKEN,
                token,
                immediate=True,
            ),
        )
        self.api = YandexDiskApi(mass, auth)

    async def handle_async_init(self) -> None:
        """Validate credentials and the configured root, then register routes."""
        # verify the token works early so setup fails clearly if it is bad
        await self.api.validate()
        # validate the configured root exists (skip for the whole-disk default)
        if self.root_folder_id not in ("", DISK_ROOT) and not await self.api.exists_dir(
            self.root_folder_id
        ):
            msg = f"Yandex Disk root path '{self.root_folder_id}' is not an existing folder."
            raise SetupFailedError(msg)
        await self._post_init()

    async def unload(self, is_removed: bool = False) -> None:
        """
        Unregister routes and release the API client.

        :param is_removed: Whether the provider instance is being deleted.
        """
        await super().unload(is_removed)
        await self.api.close()

    async def _api_list_children(self, folder_id: str) -> list[RawItem]:
        """
        List a Yandex Disk folder's children.

        :param folder_id: Disk path of the folder ("" means the disk root).
        :returns: One ``RawItem`` per child.
        """
        return await self.api.list_children(folder_id or DISK_ROOT)

    async def _api_download_bytes(self, file_id: str) -> bytes:
        """
        Download a small file's full contents.

        :param file_id: Disk path of the file.
        :returns: The file contents.
        """
        return await self.api.download_bytes(file_id)

    async def _api_download_response(
        self, file_id: str, headers: dict[str, str]
    ) -> aiohttp.ClientResponse:
        """
        Open a streaming download, forwarding any Range header.

        :param file_id: Disk path of the file.
        :param headers: Request headers (may include ``Range``).
        :returns: An open aiohttp response.
        """
        return await self.api.download_response(file_id, headers)
