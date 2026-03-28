"""WebDAV File System Provider for Music Assistant."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import TYPE_CHECKING, cast
from urllib.parse import quote, unquote, urlparse, urlunparse

import aiohttp
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ProviderUnavailableError,
    SetupFailedError,
)

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.helpers.tags import get_embedded_image
from music_assistant.providers.filesystem_local import LocalFileSystemProvider
from music_assistant.providers.filesystem_local.constants import SUPPORTED_EXTENSIONS
from music_assistant.providers.filesystem_local.helpers import FileSystemItem

from .constants import CONF_CONTENT_TYPE, CONF_URL, CONF_VERIFY_SSL
from .helpers import WebDAVItem, build_webdav_url, webdav_propfind, webdav_test_connection

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant


class WebDAVFileSystemProvider(LocalFileSystemProvider):
    """WebDAV File System Provider for Music Assistant."""

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
    ) -> None:
        """Initialize WebDAV FileSystem Provider."""
        base_url = cast("str", config.get_value(CONF_URL)).rstrip("/")
        super().__init__(mass, manifest, config, base_url)
        self.base_url = base_url
        self.username = cast("str | None", config.get_value(CONF_USERNAME))
        self.password = cast("str | None", config.get_value(CONF_PASSWORD))
        self.verify_ssl = cast("bool", config.get_value(CONF_VERIFY_SSL))
        self.media_content_type = cast("str", config.get_value(CONF_CONTENT_TYPE))

    @property
    def instance_name_postfix(self) -> str | None:
        """Return a (default) instance name postfix for this provider instance."""
        parsed = urlparse(self.base_url)
        if parsed.path and parsed.path != "/":
            return PurePosixPath(parsed.path).name
        return parsed.netloc

    @property
    def _auth(self) -> aiohttp.BasicAuth | None:
        """Get BasicAuth for WebDAV requests."""
        if self.username:
            return aiohttp.BasicAuth(self.username, self.password or "")
        return None

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        await webdav_test_connection(
            self.base_url,
            self.username,
            self.password,
            self.verify_ssl,
            timeout=10,
        )
        self.write_access = False

    def _build_authenticated_url(self, file_path: str) -> str:
        """Build authenticated WebDAV URL with properly encoded credentials."""
        webdav_url = build_webdav_url(self.base_url, file_path)
        if not (self.username and self.password):
            return webdav_url

        parsed = urlparse(webdav_url)
        encoded_username = quote(self.username, safe="")
        encoded_password = quote(self.password, safe="")
        netloc = f"{encoded_username}:{encoded_password}@{parsed.netloc}"
        return urlunparse(
            (parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)
        )

    def _normalize_path(self, path: str) -> str:
        """Convert absolute URL to relative path if needed."""
        if path.startswith("http"):
            parsed = urlparse(path)
            base_parsed = urlparse(self.base_url)
            return parsed.path[len(base_parsed.path) :].strip("/")
        return path

    async def _exists_impl(self, file_path: str) -> bool:
        """Check if WebDAV resource exists."""
        if not file_path:
            return False
        file_path = self._normalize_path(file_path)
        webdav_url = build_webdav_url(self.base_url, file_path)
        session = self.mass.http_session if self.verify_ssl else self.mass.http_session_no_ssl
        try:
            items = await webdav_propfind(session, webdav_url, depth=0, auth=self._auth)
            return len(items) > 0 or webdav_url.rstrip("/") == self.base_url.rstrip("/")
        except (LoginFailed, SetupFailedError, ProviderUnavailableError):
            raise
        except aiohttp.ClientError:
            return False

    async def _resolve_impl(self, file_path: str) -> FileSystemItem:
        """Resolve WebDAV path to FileSystemItem."""
        webdav_url = build_webdav_url(self.base_url, file_path)
        session = self.mass.http_session if self.verify_ssl else self.mass.http_session_no_ssl

        items = await webdav_propfind(session, webdav_url, depth=0, auth=self._auth)
        if not items:
            # Handle root directory case
            if webdav_url.rstrip("/") == self.base_url.rstrip("/"):
                return FileSystemItem(
                    filename="",
                    relative_path="",
                    absolute_path=self._build_authenticated_url(file_path),
                    is_dir=True,
                )
            raise MediaNotFoundError(f"WebDAV resource not found: {file_path}")

        webdav_item = items[0]
        return FileSystemItem(
            filename=PurePosixPath(file_path).name or webdav_item.name,
            relative_path=file_path,
            absolute_path=self._build_authenticated_url(file_path),
            is_dir=webdav_item.is_dir,
            checksum=webdav_item.last_modified or "unknown",
            file_size=webdav_item.size,
        )

    async def _scandir_impl(self, path: str) -> list[FileSystemItem]:
        """List WebDAV directory contents with caching."""
        cache_key = f"scandir_{path}"
        if cached := await self.cache.get(
            key=cache_key,
            provider=self.instance_id,
            category=0,
        ):
            return cast("list[FileSystemItem]", cached)

        path = self._normalize_path(path)
        webdav_url = build_webdav_url(self.base_url, path)
        session = self.mass.http_session if self.verify_ssl else self.mass.http_session_no_ssl

        webdav_items = await webdav_propfind(session, webdav_url, depth=1, auth=self._auth)
        filesystem_items = self._convert_webdav_items(webdav_items, webdav_url, path)

        await self.cache.set(
            key=cache_key,
            data=filesystem_items,
            provider=self.instance_id,
            category=0,
            expiration=300,
        )
        return filesystem_items

    def _convert_webdav_items(
        self,
        webdav_items: list[WebDAVItem],
        webdav_url: str,
        scan_path: str,
    ) -> list[FileSystemItem]:
        """Convert WebDAV items to FileSystemItems."""
        base_path = urlparse(self.base_url).path.rstrip("/")
        current_path = urlparse(webdav_url).path.rstrip("/")
        result: list[FileSystemItem] = []

        for item in webdav_items:
            # Skip recycle bins
            if "#recycle" in item.name.lower():
                continue

            decoded_href = unquote(item.href)
            href_path = (
                urlparse(decoded_href).path if decoded_href.startswith("http") else decoded_href
            )

            # Skip the directory itself
            if href_path.rstrip("/") == current_path:
                continue

            # Calculate relative path
            if href_path.startswith(base_path):
                relative_path = href_path[len(base_path) :].strip("/")
            else:
                decoded_name = unquote(item.name)
                relative_path = (
                    str(PurePosixPath(scan_path) / decoded_name) if scan_path else decoded_name
                )

            result.append(
                FileSystemItem(
                    filename=unquote(item.name),
                    relative_path=relative_path,
                    absolute_path=self._build_authenticated_url(relative_path),
                    is_dir=item.is_dir,
                    checksum=item.last_modified or "unknown",
                    file_size=item.size,
                )
            )
        return result

    async def resolve_image(self, path: str) -> str | bytes:
        """Resolve image path to actual image data or URL."""
        # Check if this is an audio file with embedded image
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext in SUPPORTED_EXTENSIONS:
            # Use authenticated URL for ffmpeg to extract embedded image
            auth_url = self._build_authenticated_url(path)
            if img_data := await get_embedded_image(auth_url):
                return img_data
            raise MediaNotFoundError(f"No embedded image found: {path}")

        # For actual image files, fetch the raw bytes
        webdav_url = build_webdav_url(self.base_url, path)
        session = self.mass.http_session if self.verify_ssl else self.mass.http_session_no_ssl
        async with session.get(webdav_url, auth=self._auth) as resp:
            if resp.status != 200:
                raise MediaNotFoundError(f"Image not found: {path}")
            return await resp.read()

    async def _sync_library_impl(self, file_checksums: dict[str, str]) -> set[str]:
        """Scan and process all WebDAV files using async PROPFIND.

        :param file_checksums: Dict mapping relative paths to their previous checksums.
        :returns: Set of current filenames that were successfully processed.
        """
        cur_filenames: set[str] = set()
        self.sync_running = True
        try:
            await self._scan_recursive("", cur_filenames, file_checksums)
        finally:
            self.sync_running = False
        return cur_filenames

    async def _scan_recursive(
        self,
        path: str,
        cur_filenames: set[str],
        file_checksums: dict[str, str],
    ) -> None:
        """Recursively scan WebDAV directory."""
        try:
            items = await self._scandir_impl(path)
        except (LoginFailed, SetupFailedError, ProviderUnavailableError):
            raise
        except aiohttp.ClientError as err:
            self.logger.warning("WebDAV error scanning %s: %s", path, err)
            return

        for item in items:
            if item.is_dir:
                await self._scan_recursive(item.relative_path, cur_filenames, file_checksums)
            else:
                prev_checksum = file_checksums.get(item.relative_path)
                if await self._process_item_async(item, prev_checksum):
                    cur_filenames.add(item.relative_path)
