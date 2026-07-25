"""WebDAV File System Provider for Music Assistant."""

from __future__ import annotations

from dataclasses import asdict
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
from music_assistant.controllers.tasks.context import update_current_task_progress_text
from music_assistant.helpers.tags import get_embedded_image
from music_assistant.providers.filesystem_local import LocalFileSystemProvider
from music_assistant.providers.filesystem_local.constants import (
    CONF_ENTRY_IGNORE_ALBUM_PLAYLISTS,
    SUPPORTED_EXTENSIONS,
)
from music_assistant.providers.filesystem_local.helpers import FileSystemItem

from .constants import CONF_CONTENT_TYPE, CONF_URL, CONF_VERIFY_SSL
from .helpers import WebDAVItem, build_webdav_url, webdav_propfind, webdav_test_connection

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant


class WebDAVFileSystemProvider(LocalFileSystemProvider):
    """WebDAV File System Provider for Music Assistant."""

    # WebDAV servers often struggle with 16 parallel tag-parse GETs
    _SYNC_CONCURRENCY = 4

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
    ) -> None:
        """Initialize WebDAV FileSystem Provider."""
        # the base path (WebDAV URL) is resolved from the setup data below, which needs
        # the initialized instance, so hand the base class a placeholder and set it after
        super().__init__(mass, manifest, config, base_path="")
        self.base_url = cast("str", self.get_setup_value(CONF_URL)).rstrip("/")
        self.base_path = self.base_url
        self.username = cast("str | None", self.get_setup_value(CONF_USERNAME))
        self.password = cast("str | None", self.get_setup_value(CONF_PASSWORD))
        self.verify_ssl = cast("bool", self.get_setup_value(CONF_VERIFY_SSL))
        self.media_content_type = cast("str", self.get_setup_value(CONF_CONTENT_TYPE))

    @property
    def instance_name_postfix(self) -> str | None:
        """Return a (default) instance name postfix for this provider instance."""
        parsed = urlparse(self.base_url)
        if parsed.path and parsed.path != "/":
            return PurePosixPath(parsed.path).name
        return parsed.netloc

    @property
    def _auth_header(self) -> str | None:
        """Return the WebDAV Authorization header value, or None when no credentials are set."""
        if self.username:
            return aiohttp.encode_basic_auth(self.username, self.password or "")
        return None

    @property
    def _session(self) -> aiohttp.ClientSession:
        """Get the appropriate HTTP session based on SSL verification setting."""
        return self.mass.http_session if self.verify_ssl else self.mass.http_session_no_ssl

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        session = self._session
        await webdav_test_connection(
            session,
            self.base_url,
            self.username,
            self.password,
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

    async def exists(self, file_path: str) -> bool:
        """Check if WebDAV resource exists."""
        if not file_path:
            return False
        file_path = self._normalize_path(file_path)
        webdav_url = build_webdav_url(self.base_url, file_path)
        session = self._session
        try:
            items = await webdav_propfind(
                session, webdav_url, depth=0, auth_header=self._auth_header
            )
            return len(items) > 0 or webdav_url.rstrip("/") == self.base_url.rstrip("/")
        except LoginFailed, SetupFailedError, ProviderUnavailableError:
            raise
        except aiohttp.ClientError:
            return False

    async def resolve(self, file_path: str) -> FileSystemItem:
        """Resolve WebDAV path to FileSystemItem."""
        webdav_url = build_webdav_url(self.base_url, file_path)
        session = self._session

        items = await webdav_propfind(session, webdav_url, depth=0, auth_header=self._auth_header)
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

    async def _scandir(self, path: str) -> list[FileSystemItem]:
        """List WebDAV directory contents with caching."""
        cache_key = f"scandir_{path}"
        # bypass the cache during sync so edits are picked up immediately;
        # the fresh result is still written back for subsequent browse/exists calls
        if not self.sync_running:
            if cached := await self.cache.get(
                key=cache_key,
                provider=self.instance_id,
                category=0,
            ):
                return [FileSystemItem(**item) for item in cached]

        path = self._normalize_path(path)
        webdav_url = build_webdav_url(self.base_url, path)
        session = self._session

        webdav_items = await webdav_propfind(
            session, webdav_url, depth=1, auth_header=self._auth_header
        )
        filesystem_items = self._convert_webdav_items(webdav_items, path)

        await self.cache.set(
            key=cache_key,
            data=[asdict(item) for item in filesystem_items],
            provider=self.instance_id,
            category=0,
            expiration=300,
        )
        return filesystem_items

    async def _read_file(self, path: str) -> bytes:
        """Read file contents over HTTP."""
        webdav_url = build_webdav_url(self.base_url, path)
        session = self._session
        auth_header = self._auth_header
        headers = {"Authorization": auth_header} if auth_header else None
        async with session.get(webdav_url, headers=headers) as resp:
            if resp.status != 200:
                raise MediaNotFoundError(f"File not found: {path}")
            return await resp.read()

    def _convert_webdav_items(
        self,
        webdav_items: list[WebDAVItem],
        scan_path: str,
    ) -> list[FileSystemItem]:
        """Convert WebDAV items to FileSystemItems."""
        base_path = urlparse(self.base_url).path.rstrip("/")
        result: list[FileSystemItem] = []

        for item in webdav_items:
            # Skip recycle bins
            if "#recycle" in item.name.lower():
                continue

            decoded_href = unquote(item.href)
            if decoded_href.startswith(("http://", "https://")):
                # Extract the path by hand: urlparse would treat ; ? # in the path as
                # params/query/fragment and corrupt names containing those characters.
                after_scheme = decoded_href.split("://", 1)[1]
                href_path = after_scheme[after_scheme.find("/") :] if "/" in after_scheme else ""
            else:
                href_path = decoded_href

            # Calculate relative path
            if href_path.startswith(base_path):
                relative_path = href_path[len(base_path) :].strip("/")
            else:
                decoded_name = unquote(item.name)
                relative_path = (
                    str(PurePosixPath(scan_path) / decoded_name) if scan_path else decoded_name
                )

            # Skip the directory being scanned itself (a depth-1 PROPFIND returns it too).
            # Comparing on the resolved relative path is reliable even when the name holds
            # characters a URL parser treats specially (e.g. ; ? #), which would otherwise
            # make the directory list itself and recurse endlessly.
            if relative_path == scan_path:
                continue

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
        session = self._session
        auth_header = self._auth_header
        headers = {"Authorization": auth_header} if auth_header else None
        async with session.get(webdav_url, headers=headers) as resp:
            if resp.status != 200:
                raise MediaNotFoundError(f"Image not found: {path}")
            return await resp.read()

    async def _enumerate_files_for_sync(
        self,
        *,
        file_checksums: dict[str, str],
        cue_file_checksums: dict[str, str],
        cur_filenames: set[str],
        items_to_process: list[tuple[FileSystemItem, str | None]],
        unchanged_cue_items: list[FileSystemItem],
        cue_stems: set[str],
        root_scan_errors: list[OSError],
    ) -> None:
        """Walk the WebDAV tree via PROPFIND and populate the sync buckets."""
        ignore_album_playlists = self.media_content_type == "music" and bool(
            self.config.get_value(CONF_ENTRY_IGNORE_ALBUM_PLAYLISTS.key)
        )
        # mutable counter for the nested coroutine
        scanned = [0]
        # guard against directory cycles (e.g. server-side symlink loops) so a single
        # bad path can never exhaust the recursion limit and abort the whole sync
        visited: set[str] = set()

        async def _walk(path: str, is_root: bool) -> None:
            if path in visited:
                return
            visited.add(path)
            try:
                items = await self._scandir(path)
            except LoginFailed, SetupFailedError, ProviderUnavailableError:
                raise
            except aiohttp.ClientError as err:
                # only a root-level failure aborts the sync; subdir failures
                # are logged and skipped, matching the local-filesystem walker
                if is_root:
                    root_scan_errors.append(OSError(str(err)))
                else:
                    self.logger.warning("WebDAV error scanning %s: %s", path, err)
                return
            for item in items:
                if item.is_dir:
                    await _walk(item.relative_path, is_root=False)
                    continue
                if item.ext not in SUPPORTED_EXTENSIONS:
                    continue
                scanned[0] += 1
                if scanned[0] % 500 == 0:
                    update_current_task_progress_text(f"Scanning files: {scanned[0]} found")
                self._classify_scan_item(
                    item,
                    file_checksums=file_checksums,
                    cue_file_checksums=cue_file_checksums,
                    cur_filenames=cur_filenames,
                    items_to_process=items_to_process,
                    unchanged_cue_items=unchanged_cue_items,
                    cue_stems=cue_stems,
                    ignore_album_playlists=ignore_album_playlists,
                )

        await _walk("", is_root=True)

    def _get_chapter_path(self, relative_path: str) -> str:
        """Return authenticated WebDAV URL for a chapter file."""
        return self._build_authenticated_url(relative_path)
