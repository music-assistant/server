"""
Base class for cloud-storage filesystem providers (Google Drive, OneDrive, ...).

Extends LocalFileSystemProvider with path <-> cloud-file-ID resolution, API-backed
directory listings and streaming through a dynamic MA URL, so short-lived cloud
auth tokens stay fresh. Concrete providers implement the _api_* hooks and their
own auth/setup.
"""

from __future__ import annotations

import posixpath
import time
from dataclasses import replace
from typing import TYPE_CHECKING, cast
from urllib.parse import quote

from aiohttp import ClientError, web
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ProviderUnavailableError,
)

from music_assistant.controllers.tasks.context import update_current_task_progress_text
from music_assistant.helpers.tags import get_embedded_image
from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.filesystem_local import LocalFileSystemProvider
from music_assistant.providers.filesystem_local.constants import (
    AUDIOBOOK_EXTENSIONS,
    CONF_CONTENT_TYPE,
    CONF_ENTRY_CONTENT_TYPE,
    CONF_ENTRY_IGNORE_ALBUM_PLAYLISTS,
    PODCAST_EPISODE_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    TRACK_EXTENSIONS,
    WALK_EXTENSIONS,
)
from music_assistant.providers.filesystem_local.helpers import FileSystemItem, ScanErrors

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aiohttp import ClientResponse
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.setup_flow import SetupSession

# (id, name, is_dir, checksum, size, metadata_token) as returned by _api_list_children.
# metadata_token is an optional higher-precision token (e.g. a stronger hash a provider
# also computes) used only to detect a metadata file (NFO/image) changing; it never
# substitutes for checksum, which stays whatever it always was for imported media.
RawItem = tuple[str, str, bool, str, int | None, str | None]

# extensions the stream route will serve; playlists/cue/images are read
# server-side and never fetched over HTTP, so audio is all it needs to proxy
AUDIO_STREAM_EXTENSIONS = TRACK_EXTENSIONS | AUDIOBOOK_EXTENSIONS | PODCAST_EPISODE_EXTENSIONS

# config keys shared by the cloud filesystem providers, all collected by the setup flow
CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_FOLDER_ID = "folder_id"


async def run_cloud_setup(
    session: SetupSession,
    authorize: Callable[[SetupSession, str, str], Awaitable[str]],
) -> None:
    """
    Drive the setup flow shared by the cloud filesystem providers.

    Collects the content type, OAuth client credentials and root folder, runs the
    provider-specific OAuth ``authorize`` step for a refresh token and persists it all.

    :param session: The setup session driving the flow.
    :param authorize: Provider-specific coroutine that runs the OAuth consent for the given
        (client_id, client_secret) and returns the resulting refresh token.
    """
    setup_data = dict(session.context.setup_data)
    # a secure value is never echoed back into a flow step, so on reconfigure the user may
    # leave the client secret blank to reuse the previously stored one
    stored_secret = str(session.context.setup_data.get(CONF_CLIENT_SECRET) or "")
    errors: dict[str, str] | None = None
    while True:
        entries = [
            replace(entry, value=setup_data.get(entry.key, entry.value))
            for entry in _cloud_setup_entries(has_stored_secret=bool(stored_secret))
        ]
        submitted = await session.form(entries, step_id="user", errors=errors)
        setup_data.update(submitted)
        client_id = str(setup_data.get(CONF_CLIENT_ID) or "")
        client_secret = str(setup_data.get(CONF_CLIENT_SECRET) or "") or stored_secret
        setup_data[CONF_CLIENT_SECRET] = client_secret
        # a blank secret on a retry means "the one just tried", not the original stored one
        stored_secret = client_secret
        try:
            if not client_secret:
                raise SetupFlowError("A client secret is required", translation_key="required")
            setup_data[CONF_REFRESH_TOKEN] = await authorize(session, client_id, client_secret)
            await session.finish(setup_data)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}


def read_setup_value(
    mass: MusicAssistant, config: ProviderConfig, key: str, default: ConfigValueType = None
) -> ConfigValueType:
    """
    Read a setup_data value from a config not yet attached to a provider instance.

    Mirrors Provider.get_setup_value for the __init__ window (before super().__init__),
    decrypting strings and reading through to legacy config values for pre-flow installs.

    :param mass: The MusicAssistant instance.
    :param config: The provider config being loaded.
    :param key: The setup data key to read.
    :param default: Value to return when the key is not present anywhere.
    """
    value = config.setup_data.get(key)
    if value is not None:
        return mass.config.decrypt_string(value) if isinstance(value, str) else value
    return config.get_value(key, default)


def _cloud_setup_entries(*, has_stored_secret: bool) -> tuple[ConfigEntry, ...]:
    """Return the config entries collected by the shared cloud setup form."""
    return (
        CONF_ENTRY_CONTENT_TYPE,
        ConfigEntry(key=CONF_CLIENT_ID, type=ConfigEntryType.STRING, required=True),
        ConfigEntry(
            key=CONF_CLIENT_SECRET,
            type=ConfigEntryType.SECURE_STRING,
            # optional on reconfigure (a stored secret can be reused), required on first setup
            required=not has_stored_secret,
        ),
        ConfigEntry(
            key=CONF_FOLDER_ID, type=ConfigEntryType.STRING, required=False, default_value="root"
        ),
    )


class CloudFileSystemProvider(LocalFileSystemProvider):
    """Base class for filesystem providers backed by a cloud storage API."""

    # cloud APIs generally struggle with the default 16 parallel tag-parse downloads
    _SYNC_CONCURRENCY = 4
    # how long a folder listing may be served from cache; keeps interactive
    # browsing snappy (no API round trip per click). Library syncs always fetch
    # fresh listings, so new cloud content is never missed because of this.
    _DIR_CACHE_TTL = 300

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
        # the content type is collected by the setup flow (setup_data); the parent reads it
        # from the legacy config values, so re-resolve it setup-data-aware (read-through keeps
        # pre-flow installs working)
        self.media_content_type = cast(
            "str", self.get_setup_value(CONF_CONTENT_TYPE, CONF_ENTRY_CONTENT_TYPE.default_value)
        )
        self.root_folder_id = root_folder_id
        self._unregister_stream_route: Callable[[], None] | None = None
        # per-folder listing cache: folder path -> {child name -> (cloud id, item)};
        # every path->id lookup is answered from here, so sibling probes by the
        # inherited logic (artwork, lyrics, playlists) cost no extra API calls
        self._dir_cache: dict[str, dict[str, tuple[str, FileSystemItem]]] = {}
        # monotonic deadline per folder path until which _scandir may serve the
        # cached listing; path->id lookups deliberately never expire (IDs are stable)
        self._dir_cache_expiry: dict[str, float] = {}

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        await super().unload(is_removed)
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
        :return: One (id, name, is_dir, checksum, size, metadata_token) tuple per child.
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

    async def _is_reachable(self) -> bool:
        """Return whether the cloud storage can be read."""
        # this provider has no local path to stat, so ask the API for the root listing;
        # an outage (or expired credentials) surfaces as a raised error
        await self._scandir("", use_cache=False)
        return True

    async def _scandir(self, path: str, use_cache: bool = True) -> list[FileSystemItem]:
        """
        List the children of a cloud folder.

        `path` is the relative path of the folder ("" means this provider's root).
        `use_cache` allows serving a recent cached listing; pass False to force
        a fresh fetch from the cloud API.
        """
        path = self._normalize_path(path)
        # serve recently fetched listings from cache so browsing back and forth
        # through folders doesn't cost an API round trip per click
        if (
            use_cache
            and (cached := self._dir_cache.get(path)) is not None
            and time.monotonic() < self._dir_cache_expiry.get(path, 0)
        ):
            return [entry[1] for entry in cached.values()]
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
        self._dir_cache_expiry[path] = time.monotonic() + self._DIR_CACHE_TTL
        return items

    async def _enumerate_files_for_sync(
        self,
        *,
        file_checksums: dict[str, str],
        cue_file_checksums: dict[str, set[str]],
        cur_filenames: set[str],
        items_to_process: list[tuple[FileSystemItem, str | None]],
        unchanged_cue_items: list[FileSystemItem],
        cue_stems: set[str],
        scan_errors: ScanErrors,
        metadata_files: list[FileSystemItem],
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
                # always fetch fresh during a sync so new cloud content is
                # picked up no matter how recently a folder was browsed
                items = await self._scandir(path, use_cache=False)
            except ProviderUnavailableError as err:
                # a root-level failure aborts the sync right away, subfolder failures only
                # once too many happen in a row, matching the local-filesystem walker
                if not is_root:
                    self.logger.warning("Error scanning folder %s: %s", path, err)
                scan_errors.record_dir_error(err, is_root=is_root, path=path)
                return
            scan_errors.record_dir_read()
            for item in items:
                if item.is_dir:
                    await _walk(item.relative_path, is_root=False)
                    if scan_errors.aborted:
                        return
                    continue
                if item.ext not in WALK_EXTENSIONS:
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
                    metadata_files=metadata_files,
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
        path = self._normalize_path(request.query.get("path") or "")
        if not path:
            raise web.HTTPBadRequest(text="Missing path")
        # the streamserver is unauthenticated: only proxy audio files so this route
        # can't be used to download arbitrary files from the cloud account
        # (same 404 as a missing file, so blocked paths are indistinguishable)
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext not in AUDIO_STREAM_EXTENSIONS:
            raise web.HTTPNotFound(text="File not found")
        try:
            file_id = await self._resolve_id(path)
        except MediaNotFoundError as err:
            self.logger.debug("Cloud stream path not found: %s (%s)", path, err)
            raise web.HTTPNotFound(text="File not found") from err
        # forward Range header so players can seek
        headers = {}
        if rng := request.headers.get("Range"):
            headers["Range"] = rng
        try:
            cloud_resp = await self._api_download_response(file_id, headers)
        except (ProviderUnavailableError, LoginFailed) as err:
            self.logger.warning("Cloud provider unavailable while streaming %s: %s", path, err)
            raise web.HTTPBadGateway(text="Upstream provider unavailable") from err

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
        _, _, is_dir, checksum, size, metadata_token = raw
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
            metadata_token=metadata_token,
        )
