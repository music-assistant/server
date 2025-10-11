"""WebDAV File System Provider for Music Assistant."""

from __future__ import annotations

import asyncio
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

from music_assistant.constants import (
    CONF_PASSWORD,
    CONF_USERNAME,
    DB_TABLE_PROVIDER_MAPPINGS,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.providers.filesystem_local import LocalFileSystemProvider
from music_assistant.providers.filesystem_local.helpers import FileSystemItem

from .constants import CONF_CONTENT_TYPE, CONF_URL, CONF_VERIFY_SSL
from .helpers import build_webdav_url, webdav_propfind, webdav_test_connection

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import MediaType
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
        try:
            parsed = urlparse(self.base_url)
            if parsed.path and parsed.path != "/":
                return PurePosixPath(parsed.path).name
            return parsed.netloc
        except (ValueError, TypeError):
            return "Invalid URL"

    @property
    def _auth(self) -> aiohttp.BasicAuth | None:
        """Get BasicAuth for WebDAV requests."""
        if self.username:
            return aiohttp.BasicAuth(self.username, self.password or "")
        return None

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        try:
            await webdav_test_connection(
                self.base_url,
                self.username,
                self.password,
                self.verify_ssl,
                timeout=10,
            )
        except (LoginFailed, SetupFailedError):
            raise
        except Exception as err:
            raise SetupFailedError(f"WebDAV connection failed: {err}") from err

        self.write_access = False

    def _build_authenticated_url(self, file_path: str) -> str:
        """Build authenticated WebDAV URL with properly encoded credentials."""
        webdav_url = build_webdav_url(self.base_url, file_path)
        parsed = urlparse(webdav_url)

        if self.username and self.password:
            encoded_username = quote(self.username, safe="")
            encoded_password = quote(self.password, safe="")
            netloc = f"{encoded_username}:{encoded_password}@{parsed.netloc}"
            return urlunparse(
                (parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)
            )

        return webdav_url

    async def _exists_impl(self, file_path: str) -> bool:
        """Check if WebDAV resource exists."""
        if not file_path:
            return False
        # Handle case where absolute URL is passed
        if file_path.startswith("http"):
            parsed = urlparse(file_path)
            base_parsed = urlparse(self.base_url)
            file_path = parsed.path[len(base_parsed.path) :].strip("/")
        try:
            webdav_url = build_webdav_url(self.base_url, file_path)
            session = self.mass.http_session if self.verify_ssl else self.mass.http_session_no_ssl
            items = await webdav_propfind(session, webdav_url, depth=0, auth=self._auth)
            return len(items) > 0 or webdav_url.rstrip("/") == self.base_url.rstrip("/")
        except (LoginFailed, SetupFailedError):
            raise
        except aiohttp.ClientError as err:
            self.logger.debug(f"WebDAV client error during exists check for {file_path}: {err}")
            return False
        except Exception as err:
            self.logger.debug(f"WebDAV exists check failed for {file_path}: {err}")
            return False

    async def _resolve_impl(self, file_path: str) -> FileSystemItem:
        """Resolve WebDAV path to FileSystemItem."""
        webdav_url = build_webdav_url(self.base_url, file_path)
        session = self.mass.http_session if self.verify_ssl else self.mass.http_session_no_ssl

        try:
            items = await webdav_propfind(session, webdav_url, depth=0, auth=self._auth)
            if not items:
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

        except MediaNotFoundError:
            raise
        except (LoginFailed, SetupFailedError):
            raise
        except aiohttp.ClientError as err:
            raise MediaNotFoundError(f"Failed to resolve WebDAV path {file_path}: {err}") from err

    async def _scandir_impl(self, path: str) -> list[FileSystemItem]:
        """List WebDAV directory contents."""
        # Handle case where absolute URL is passed (from parent's code)
        if path.startswith("http"):
            parsed = urlparse(path)
            base_parsed = urlparse(self.base_url)
            path = parsed.path[len(base_parsed.path) :].strip("/")
            self.logger.debug(f"Converted absolute URL to relative path: {path}")

        self.logger.debug(f"Scanning WebDAV path: {path}")
        webdav_url = build_webdav_url(self.base_url, path)
        session = self.mass.http_session if self.verify_ssl else self.mass.http_session_no_ssl

        try:
            webdav_items = await webdav_propfind(session, webdav_url, depth=1, auth=self._auth)
            self.logger.debug(f"WebDAV returned {len(webdav_items)} items for {path}")  # ADD THIS

            filesystem_items: list[FileSystemItem] = []

            # Parse base path component for comparison
            base_parsed = urlparse(self.base_url)
            base_path = base_parsed.path.rstrip("/")

            for webdav_item in webdav_items:
                self.logger.debug(
                    f"Processing item: name={webdav_item.name}, "
                    f"href={webdav_item.href[:100]}, is_dir={webdav_item.is_dir}"
                )

                if "#recycle" in webdav_item.name.lower():
                    continue
                decoded_name = unquote(webdav_item.name)
                decoded_href = unquote(webdav_item.href)

                # If href is a full URL, extract just the path component
                if decoded_href.startswith("http"):
                    href_parsed = urlparse(decoded_href)
                    href_path = href_parsed.path
                else:
                    href_path = decoded_href

                # Skip the directory itself
                current_path = urlparse(webdav_url).path.rstrip("/")
                if href_path.rstrip("/") == current_path:
                    self.logger.debug(f"Skipping directory itself: {href_path}")

                    continue
                self.logger.debug(f"After skip check, processing: {webdav_item.name}")

                # Calculate relative path by stripping base path
                if href_path.startswith((base_path + "/", base_path)):
                    relative_path = href_path[len(base_path) :].strip("/")
                else:
                    # Fallback: construct from current path + name
                    relative_path = (
                        str(PurePosixPath(path) / decoded_name) if path else decoded_name
                    )
                self.logger.debug(
                    f"Item: {decoded_name}, href: {decoded_href[:80]}, "
                    f"relative_path: {relative_path}"
                )
                self.logger.debug(
                    f"Calculated relative_path: '{relative_path}' for {webdav_item.name}"
                )

                decoded_name = unquote(webdav_item.name)

                filesystem_items.append(
                    FileSystemItem(
                        filename=decoded_name,
                        relative_path=relative_path,
                        absolute_path=self._build_authenticated_url(relative_path),
                        is_dir=webdav_item.is_dir,
                        checksum=webdav_item.last_modified or "unknown",
                        file_size=webdav_item.size,
                    )
                )
                self.logger.debug(f"Added to filesystem_items: {decoded_name}")
            self.logger.debug(
                f"Parsed {len(filesystem_items)} filesystem items for {path}"
            )  # ADD THIS

            return filesystem_items

        except (LoginFailed, SetupFailedError, ProviderUnavailableError):
            raise
        except aiohttp.ClientError as err:
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                f"WebDAV client error listing directory {path}: {err}",
            )
            raise ProviderUnavailableError(f"WebDAV server connection failed: {err}") from err
        except Exception as err:
            self.logger.exception(f"Failed to list WebDAV directory {path}: {err}")

            return []

    async def resolve_image(self, path: str) -> str | bytes:
        """Resolve image path to actual image data or URL."""
        webdav_url = build_webdav_url(self.base_url, path)
        session = self.mass.http_session if self.verify_ssl else self.mass.http_session_no_ssl

        async with session.get(webdav_url, auth=self._auth) as resp:
            if resp.status != 200:
                raise MediaNotFoundError(f"Image not found: {path}")
            return await resp.read()

    async def sync_library(self, media_type: MediaType, import_as_favorite: bool = False) -> None:
        """Run library sync for WebDAV provider."""
        assert self.mass.music.database

        if self.sync_running:
            self.logger.warning(f"Library sync already running for {self.name}")
            return

        self.logger.info(f"Started library sync for WebDAV provider {self.name}")
        self.sync_running = True

        try:
            file_checksums: dict[str, str] = {}
            query = (
                f"SELECT provider_item_id, details FROM {DB_TABLE_PROVIDER_MAPPINGS} "
                f"WHERE provider_instance = '{self.instance_id}' "
                "AND media_type in ('track', 'playlist', 'audiobook', 'podcast_episode')"
            )
            for db_row in await self.mass.music.database.get_rows_from_query(query, limit=0):
                file_checksums[db_row["provider_item_id"]] = str(db_row["details"])

            cur_filenames: set[str] = set()
            prev_filenames: set[str] = set(file_checksums.keys())

            await self._scan_recursive("", cur_filenames, file_checksums, import_as_favorite)

            deleted_files = prev_filenames - cur_filenames
            await self._process_deletions(deleted_files)
            await self._process_orphaned_albums_and_artists()

        except (LoginFailed, SetupFailedError, ProviderUnavailableError) as err:
            self.logger.error(f"WebDAV library sync failed due to provider error: {err}")
            raise
        except aiohttp.ClientError as err:
            self.logger.error(f"WebDAV library sync failed due to connection error: {err}")
            raise ProviderUnavailableError(f"WebDAV server connection failed: {err}") from err
        except Exception as err:
            self.logger.error(f"WebDAV library sync failed with unexpected error: {err}")
            raise SetupFailedError(f"WebDAV library sync failed: {err}") from err
        finally:
            self.sync_running = False
            self.logger.info(f"Completed library sync for WebDAV provider {self.name}")

    async def _scan_recursive(
        self,
        path: str,
        cur_filenames: set[str],
        file_checksums: dict[str, str],
        import_as_favorite: bool,
    ) -> None:
        """Recursively scan WebDAV directory."""
        try:
            items = await self._scandir_impl(path)

            # Separate directories and files
            dirs = [item for item in items if item.is_dir]
            files = [item for item in items if not item.is_dir]

            # Process files in executor (blocking operation)
            for item in files:
                prev_checksum = file_checksums.get(item.relative_path)
                # Wrap _process_item in executor since it's blocking
                if await asyncio.to_thread(
                    self._process_item, item, prev_checksum, import_as_favorite
                ):
                    cur_filenames.add(item.relative_path)

            # Recurse into directories
            for dir_item in dirs:
                await self._scan_recursive(
                    dir_item.relative_path, cur_filenames, file_checksums, import_as_favorite
                )

        except (LoginFailed, SetupFailedError, ProviderUnavailableError):
            raise
        except aiohttp.ClientError as err:
            self.logger.warning(f"WebDAV client error scanning path {path}: {err}")
        except Exception as err:
            self.logger.warning(f"Failed to scan WebDAV path {path}: {err}")
