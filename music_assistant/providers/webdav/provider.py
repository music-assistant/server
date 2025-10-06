"""WebDAV File System Provider for Music Assistant."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, cast
from urllib.parse import quote, unquote, urlparse, urlunparse

import aiohttp
from music_assistant_models.enums import MediaType, StreamType
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ProviderUnavailableError,
    SetupFailedError,
)
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemType,
)
from music_assistant_models.streamdetails import MultiPartPath, StreamDetails

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME, VERBOSE_LOG_LEVEL
from music_assistant.providers.filesystem_local import LocalFileSystemProvider
from music_assistant.providers.filesystem_local.constants import (
    CACHE_CATEGORY_AUDIOBOOK_CHAPTERS,
    PLAYLIST_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    TRACK_EXTENSIONS,
)
from music_assistant.providers.filesystem_local.helpers import FileSystemItem

from .constants import (
    CONF_CONTENT_TYPE,
    CONF_URL,
    CONF_VERIFY_SSL,
    MAX_CONCURRENT_TASKS,
    WEBDAV_TIMEOUT,
)
from .helpers import build_webdav_url, webdav_propfind, webdav_test_connection

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
        self._session: aiohttp.ClientSession | None = None
        self.media_content_type = cast("str", config.get_value(CONF_CONTENT_TYPE))

    @property
    def instance_name_postfix(self) -> str | None:
        """Return a (default) instance name postfix for this provider instance."""
        try:
            parsed = urlparse(self.base_url)
            if parsed.path and parsed.path != "/":
                return PurePosixPath(parsed.path).name
            return parsed.netloc
        except Exception:
            return "WebDAV"

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session with proper authentication."""
        if self._session and not self._session.closed:
            return self._session

        auth = None
        if self.username:
            auth = aiohttp.BasicAuth(self.username, self.password or "")

        connector = aiohttp.TCPConnector(ssl=self.verify_ssl)

        self._session = aiohttp.ClientSession(
            auth=auth,
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=WEBDAV_TIMEOUT),
        )

        return self._session

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

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        if self._session and not self._session.closed:
            await self._session.close()
        await super().unload(is_removed)

    def get_absolute_path(self, file_path: str) -> str:
        """Return authenticated WebDAV URL for given file path."""
        return self._build_authenticated_url(file_path)

    async def exists(self, file_path: str) -> bool:
        """Return bool if this WebDAV resource exists."""
        if not file_path:
            return False

        try:
            webdav_url = build_webdav_url(self.base_url, file_path)
            session = await self._get_session()
            items = await webdav_propfind(session, webdav_url, depth=0)
            return len(items) > 0 or webdav_url.rstrip("/") == self.base_url.rstrip("/")
        except (LoginFailed, SetupFailedError):
            raise
        except aiohttp.ClientError as err:
            self.logger.debug(f"WebDAV client error during exists check for {file_path}: {err}")
            return False
        except Exception as err:
            self.logger.debug(f"WebDAV exists check failed for {file_path}: {err}")
            return False

    async def resolve(self, file_path: str) -> FileSystemItem:
        """Resolve WebDAV path to FileSystemItem with authenticated URL."""
        webdav_url = build_webdav_url(self.base_url, file_path)
        session = await self._get_session()

        try:
            items = await webdav_propfind(session, webdav_url, depth=0)
            if not items:
                if webdav_url.rstrip("/") == self.base_url.rstrip("/"):
                    return FileSystemItem(
                        filename="",
                        relative_path="",
                        absolute_path=self._build_authenticated_url(""),
                        is_dir=True,
                    )
                raise MediaNotFoundError(f"WebDAV resource not found: {file_path}")

            webdav_item = items[0]

            # Return FileSystemItem with authenticated URL - this is the key!
            # The parent class will use this URL for async_parse_tags()
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
        except Exception as err:
            raise MediaNotFoundError(f"Failed to resolve WebDAV path {file_path}: {err}") from err

    async def _scandir(self, path: str) -> list[FileSystemItem]:
        """List WebDAV directory contents."""
        webdav_url = build_webdav_url(self.base_url, path)
        session = await self._get_session()

        try:
            webdav_items = await webdav_propfind(session, webdav_url, depth=1)
            filesystem_items: list[FileSystemItem] = []

            for webdav_item in webdav_items:
                # Skip recycle bin
                if "#recycle" in webdav_item.name.lower():
                    continue

                decoded_href = unquote(webdav_item.href)
                decoded_base_url = unquote(self.base_url)

                parsed_webdav_url = urlparse(webdav_url)
                webdav_path = parsed_webdav_url.path.rstrip("/")

                # Skip the directory itself
                if decoded_href.rstrip("/") == webdav_path:
                    continue

                if decoded_href.startswith(decoded_base_url):
                    relative_path = decoded_href[len(decoded_base_url) :].strip("/")
                else:
                    decoded_name = unquote(webdav_item.name)
                    relative_path = (
                        str(PurePosixPath(path) / decoded_name) if path else decoded_name
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
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                f"Failed to list WebDAV directory {path}: {err}",
            )
            return []

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse this provider's items."""
        if self.media_content_type == "podcasts":
            return await self.mass.music.podcasts.library_items(provider=self.instance_id)
        if self.media_content_type == "audiobooks":
            return await self.mass.music.audiobooks.library_items(provider=self.instance_id)

        items: list[MediaItemType | ItemMapping | BrowseFolder] = []
        item_path = path.split("://", 1)[1] if "://" in path else ""

        try:
            filesystem_items = await self._scandir(item_path)

            for item in filesystem_items:
                if not item.is_dir and ("." not in item.filename or not item.ext):
                    continue

                if item.is_dir:
                    items.append(
                        BrowseFolder(
                            item_id=item.relative_path,
                            provider=self.instance_id,
                            path=f"{self.instance_id}://{item.relative_path}",
                            name=item.filename,
                            is_playable=True,
                        )
                    )
                elif item.ext in TRACK_EXTENSIONS:
                    items.append(
                        ItemMapping(
                            media_type=MediaType.TRACK,
                            item_id=item.relative_path,
                            provider=self.instance_id,
                            name=item.filename,
                        )
                    )
                elif item.ext in PLAYLIST_EXTENSIONS:
                    items.append(
                        ItemMapping(
                            media_type=MediaType.PLAYLIST,
                            item_id=item.relative_path,
                            provider=self.instance_id,
                            name=item.filename,
                        )
                    )
        except Exception as err:
            self.logger.error(f"Failed to browse WebDAV path {item_path}: {err}")

        return items

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return the content details for the given media when it will be streamed."""
        # For audiobooks, check if multi-file and use MultiPartPath
        if media_type == MediaType.AUDIOBOOK:
            file_item = await self.resolve(item_id)

            if file_item.is_dir:
                # Check cache for multi-file chapters
                file_based_chapters = await self.cache.get(
                    key=item_id,
                    provider=self.instance_id,
                    category=CACHE_CATEGORY_AUDIOBOOK_CHAPTERS,
                )

                if file_based_chapters:
                    # Multi-file audiobook - use MultiPartPath list
                    audiobook = await self.mass.music.audiobooks.get_library_item_by_prov_id(
                        item_id, self.instance_id
                    )
                    if audiobook is None:
                        raise MediaNotFoundError(f"Audiobook not found: {item_id}")

                    prov_mapping = next(
                        (x for x in audiobook.provider_mappings if x.item_id == item_id), None
                    )
                    audio_format = prov_mapping.audio_format if prov_mapping else AudioFormat()

                    # Build MultiPartPath list for core's get_multi_file_stream
                    multi_parts = [
                        MultiPartPath(
                            path=self._build_authenticated_url(chapter_path),
                            duration=duration,
                        )
                        for chapter_path, duration in file_based_chapters
                    ]

                    return StreamDetails(
                        provider=self.instance_id,
                        item_id=item_id,
                        audio_format=audio_format,
                        media_type=MediaType.AUDIOBOOK,
                        stream_type=StreamType.HTTP,  # Not CUSTOM - let core handle it!
                        duration=audiobook.duration,
                        path=multi_parts,  # List of MultiPartPath
                        can_seek=True,
                        allow_seek=True,
                    )

        # All other cases (single files, tracks, podcasts) use parent implementation
        return await super().get_stream_details(item_id, media_type)

    async def _scan_recursive(
        self,
        path: str,
        cur_filenames: set[str],
        file_checksums: dict[str, str],
        import_as_favorite: bool,
    ) -> None:
        """Recursively scan WebDAV directory with concurrent processing."""
        try:
            items = await self._scandir(path)

            # Separate directories and files
            dirs = [item for item in items if item.is_dir]
            files = [item for item in items if not item.is_dir]

            # Limit concurrent directory scans
            dir_semaphore = asyncio.Semaphore(3)

            async def scan_dir_limited(item: FileSystemItem) -> None:
                async with dir_semaphore:
                    await self._scan_recursive(
                        item.relative_path, cur_filenames, file_checksums, import_as_favorite
                    )

            dir_tasks = [scan_dir_limited(item) for item in dirs]

            # Process files concurrently with semaphore
            semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

            async def process_with_semaphore(item: FileSystemItem) -> None:
                async with semaphore:
                    if item.ext not in SUPPORTED_EXTENSIONS:
                        return

                    prev_checksum = file_checksums.get(item.relative_path)

                    # Call parent's synchronous _process_item in a thread
                    # item.absolute_path already has authenticated URL from resolve()
                    if await asyncio.to_thread(
                        self._process_item, item, prev_checksum, import_as_favorite
                    ):
                        cur_filenames.add(item.relative_path)

            file_tasks = [process_with_semaphore(item) for item in files]

            # Run all tasks concurrently
            all_tasks = dir_tasks + file_tasks
            results = await asyncio.gather(*all_tasks, return_exceptions=True)

            # Log any errors
            for result in results:
                if isinstance(result, Exception) and not isinstance(
                    result, (LoginFailed, SetupFailedError, ProviderUnavailableError)
                ):
                    self.logger.warning(f"Error during scan: {result}")

        except (LoginFailed, SetupFailedError, ProviderUnavailableError):
            raise
        except aiohttp.ClientError as err:
            self.logger.warning(f"WebDAV client error scanning path {path}: {err}")
        except Exception as err:
            self.logger.warning(f"Failed to scan WebDAV path {path}: {err}")

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
