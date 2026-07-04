"""
Base class for cloud-storage filesystem providers (Google Drive, OneDrive, ...).

Extends LocalFileSystemProvider with path <-> cloud-file-ID resolution, API-backed
directory listings and streaming through a dynamic MA URL, so short-lived cloud
auth tokens stay fresh. Concrete providers implement the _api_* hooks and their
own auth/setup.
"""

from __future__ import annotations

import posixpath
from typing import TYPE_CHECKING
from urllib.parse import quote

from aiohttp import ClientError, web
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ProviderUnavailableError,
)

from music_assistant.controllers.tasks.context import update_current_task_progress_text
from music_assistant.helpers.tags import get_embedded_image
from music_assistant.providers.filesystem_local import LocalFileSystemProvider
from music_assistant.providers.filesystem_local.constants import (
    CONF_ENTRY_IGNORE_ALBUM_PLAYLISTS,
    SUPPORTED_EXTENSIONS,
)
from music_assistant.providers.filesystem_local.helpers import FileSystemItem

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiohttp import ClientResponse
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant

# (id, name, is_dir, checksum, size) as returned by _api_list_children
RawItem = tuple[str, str, bool, str, int | None]


class CloudFileSystemProvider(LocalFileSystemProvider):
    """Base class for filesystem providers backed by a cloud storage API."""

    # cloud APIs generally struggle with the default 16 parallel tag-parse downloads
    _SYNC_CONCURRENCY = 4

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        root_folder_id: str,
    ) -> None:
        """
        Initialize the cloud filesystem provider.

        :param root_folder_id: The cloud provider's opaque ID of the root folder to serve.
        """
        # base_path is unused for us, but the parent expects something
        super().__init__(mass, manifest, config, root_folder_id)
        self.root_folder_id = root_folder_id
        self._unregister_stream_route: Callable[[], None] | None = None
        # per-folder listing cache: folder path -> {child name -> (cloud id, item)};
        # every path->id lookup is answered from here, so sibling probes by the
        # inherited logic (artwork, lyrics, playlists) cost no extra API calls
        self._dir_cache: dict[str, dict[str, tuple[str, FileSystemItem]]] = {}

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        if self._unregister_stream_route is not None:
            self._unregister_stream_route()

    async def resolve(self, file_path: str) -> FileSystemItem:
        """Resolve a relative path to a FileSystemItem."""
        file_path = self._normalize_path(file_path)
        if entry := await self._lookup(file_path):
            return entry[1]
        raise MediaNotFoundError(f"Cloud path not found: {file_path}")

    async def exists(self, file_path: str) -> bool:
        """Check if a cloud file/folder exists."""
        if not file_path:
            return False
        try:
            return await self._lookup(self._normalize_path(file_path)) is not None
        except ProviderUnavailableError, MediaNotFoundError:
            return False

    async def resolve_image(self, path: str) -> str | bytes:
        """Return raw image bytes for a cloud image file or embedded cover art."""
        # drop the cache-busting suffix the parent appends for embedded images
        path = path.split("?cs=", 1)[0]
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext in SUPPORTED_EXTENSIONS:
            # audio file: extract the embedded art with ffmpeg over our stream URL
            if img_data := await get_embedded_image(self._stream_url(path)):
                return img_data
            raise MediaNotFoundError(f"No embedded image found: {path}")
        return await self._read_file(path)

    # ------------------------------------------------------------------
    # API hooks (implemented by the concrete cloud provider); hooks must
    # translate client library errors into MA errors: ProviderUnavailableError
    # for API/transport failures, LoginFailed for authentication problems
    # ------------------------------------------------------------------

    async def _api_list_children(self, folder_id: str) -> list[RawItem]:
        """
        List the children of a cloud folder, following pagination if needed.

        :param folder_id: The cloud provider's opaque folder ID.
        :return: One (id, name, is_dir, checksum, size) tuple per child.
        """
        raise NotImplementedError

    async def _api_download_bytes(self, file_id: str) -> bytes:
        """
        Download a (small) cloud file's full contents.

        :param file_id: The cloud provider's opaque file ID.
        """
        raise NotImplementedError

    async def _api_download_response(self, file_id: str, headers: dict[str, str]) -> ClientResponse:
        """
        Open a streaming download for a cloud file.

        :param file_id: The cloud provider's opaque file ID.
        :param headers: Extra request headers to forward (e.g. Range for seeking).
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # initialization helpers
    # ------------------------------------------------------------------

    async def _post_init(self) -> None:
        """Complete common initialization; call at the end of handle_async_init."""
        self._register_stream_route()

    def _register_stream_route(self) -> None:
        """Register the dynamic route that proxies cloud downloads with fresh auth."""
        self._unregister_stream_route = self.mass.streams.register_dynamic_route(
            f"/{self.instance_id}_stream", self._handle_stream_request
        )

    # ------------------------------------------------------------------
    # filesystem hooks (these are what the parent calls)
    # ------------------------------------------------------------------

    async def _scandir(self, path: str) -> list[FileSystemItem]:
        """
        List the children of a cloud folder.

        `path` is the relative path of the folder ("" means this provider's root).
        """
        path = self._normalize_path(path)
        folder_id = await self._resolve_id(path)
        children: dict[str, tuple[str, FileSystemItem]] = {}
        items: list[FileSystemItem] = []
        for raw in await self._api_list_children(folder_id):
            # slashes in cloud file names would corrupt our path scheme
            name = raw[1].replace("/", "_")
            if name in children:
                # some clouds (e.g. Google Drive) allow duplicate names in a folder; paths can't
                self.logger.warning(
                    "Duplicate name '%s' in folder '%s' - ignoring all but the first",
                    name,
                    path or "(root)",
                )
                continue
            item = self._to_item(raw, path, name)
            children[name] = (raw[0], item)
            items.append(item)
        self._dir_cache[path] = children
        return items

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
        """Walk the cloud folder tree via the API and populate the sync buckets."""
        ignore_album_playlists = self.media_content_type == "music" and bool(
            self.config.get_value(CONF_ENTRY_IGNORE_ALBUM_PLAYLISTS.key)
        )
        # mutable counter for the nested coroutine
        scanned = [0]
        # a cloud folder may be reachable twice (e.g. Drive multi-parent), so
        # guard against re-visiting
        visited: set[str] = set()

        async def _walk(path: str, is_root: bool) -> None:
            if path in visited:
                return
            visited.add(path)
            try:
                items = await self._scandir(path)
            except ProviderUnavailableError as err:
                # only a root-level failure aborts the sync; subfolder failures
                # are logged and skipped, matching the local-filesystem walker
                if is_root:
                    root_scan_errors.append(OSError(str(err)))
                else:
                    self.logger.warning("Error scanning folder %s: %s", path, err)
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

    async def _read_file(self, path: str) -> bytes:
        """Download a (small text) file's bytes: nfo, m3u, lrc, etc."""
        file_id = await self._resolve_id(self._normalize_path(path))
        try:
            return await self._api_download_bytes(file_id)
        except ProviderUnavailableError as err:
            raise MediaNotFoundError(f"Unable to read cloud file {path}: {err}") from err

    def _get_chapter_path(self, relative_path: str) -> str:
        """Return the streamable URL for an audiobook chapter file."""
        return self._stream_url(relative_path)

    # ------------------------------------------------------------------
    # streaming
    # ------------------------------------------------------------------

    def _stream_url(self, path: str) -> str:
        """Build the MA-hosted URL that proxies this cloud file."""
        base = f"{self.mass.streams.base_url}/{self.instance_id}_stream"
        return f"{base}?path={quote(path)}"

    async def _handle_stream_request(self, request: web.Request) -> web.StreamResponse:
        """
        Proxy a cloud download through MA, adding a fresh auth header.

        Because this runs per request, the token is always valid - so even a
        multi-hour audiobook can't outlive it.
        """
        path = request.query.get("path")
        if not path:
            raise web.HTTPBadRequest(text="Missing path")
        try:
            file_id = await self._resolve_id(path)
        except MediaNotFoundError as err:
            raise web.HTTPNotFound(text=str(err)) from err
        # forward Range header so players can seek
        headers = {}
        if rng := request.headers.get("Range"):
            headers["Range"] = rng
        try:
            cloud_resp = await self._api_download_response(file_id, headers)
        except (ProviderUnavailableError, LoginFailed) as err:
            raise web.HTTPBadGateway(text=str(err)) from err

        response = web.StreamResponse(status=cloud_resp.status)
        # copy content-type / length / range headers back to the player
        for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
            if h in cloud_resp.headers:
                response.headers[h] = cloud_resp.headers[h]
        try:
            await response.prepare(request)
            async for chunk in cloud_resp.content.iter_chunked(64 * 1024):
                await response.write(chunk)
            await response.write_eof()
        except ConnectionError:
            # client hung up early (e.g. ffmpeg closes as soon as it has read
            # the tags); perfectly normal, not an error
            self.logger.debug("Client disconnected while streaming %s", path)
        except ClientError as err:
            # the cloud side dropped mid-transfer
            self.logger.warning("Cloud download interrupted for %s: %s", path, err)
        finally:
            # abort the cloud download so we don't keep pulling unneeded bytes
            cloud_resp.close()
        return response

    # ------------------------------------------------------------------
    # path resolution helpers
    # ------------------------------------------------------------------

    def _normalize_path(self, path: str) -> str:
        """Normalize a relative path (collapse ./.. segments from playlist entries)."""
        path = path.strip("/")
        if path:
            path = posixpath.normpath(path)
            if path == ".":
                path = ""
        return path

    async def _lookup(self, path: str) -> tuple[str, FileSystemItem] | None:
        """
        Return the cached (cloud id, item) tuple for a relative path, if it exists.

        Lists the parent folder (once) on a cache miss.
        """
        if not path:
            return None
        parent, _, name = path.rpartition("/")
        if (children := self._dir_cache.get(parent)) is None:
            await self._scandir(parent)
            children = self._dir_cache.get(parent, {})
        return children.get(name)

    async def _resolve_id(self, path: str) -> str:
        """Resolve a relative path to its cloud file ID."""
        if not path:
            return self.root_folder_id
        if entry := await self._lookup(path):
            return entry[0]
        raise MediaNotFoundError(f"Cloud path not found: {path}")

    def _to_item(self, raw: RawItem, parent_path: str, name: str) -> FileSystemItem:
        """Convert a raw API listing entry to a FileSystemItem."""
        _, _, is_dir, checksum, size = raw
        relative_path = f"{parent_path}/{name}" if parent_path else name
        return FileSystemItem(
            filename=name,
            relative_path=relative_path,
            # absolute_path is what the parent hands to the tag parser (ffmpeg);
            # point it at our streaming URL so tags are read over HTTP with a
            # fresh token - no temp download needed. Folders don't stream.
            absolute_path="" if is_dir else self._stream_url(relative_path),
            is_dir=is_dir,
            checksum=checksum,
            file_size=size,
        )
