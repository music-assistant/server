"""WebDAV File System Provider for Music Assistant."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Sequence
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, cast
from urllib.parse import quote, unquote, urlparse, urlunparse

import aiohttp
from music_assistant_models.enums import ContentType, ImageType, MediaType, StreamType
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ProviderUnavailableError,
    SetupFailedError,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemChapter,
    MediaItemImage,
    MediaItemType,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import (
    CONF_PASSWORD,
    CONF_USERNAME,
    DB_TABLE_PROVIDER_MAPPINGS,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.helpers.tags import AudioTags, async_parse_tags
from music_assistant.providers.filesystem_local import LocalFileSystemProvider
from music_assistant.providers.filesystem_local.constants import CACHE_CATEGORY_AUDIOBOOK_CHAPTERS
from music_assistant.providers.filesystem_local.helpers import FileSystemItem

from .constants import (
    AUDIOBOOK_EXTENSIONS,
    CONF_CONTENT_TYPE,
    CONF_URL,
    CONF_VERIFY_SSL,
    IMAGE_EXTENSIONS,
    MAX_CONCURRENT_TASKS,
    PLAYLIST_EXTENSIONS,
    PODCAST_EPISODE_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    TRACK_EXTENSIONS,
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
        self.media_content_type = cast("str", config.get_value(CONF_CONTENT_TYPE))
        self.sync_running: bool = False
        self.processed_audiobook_folders: set[str] = set()

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

    @property
    def _auth(self) -> aiohttp.BasicAuth | None:
        """Get BasicAuth for WebDAV requests."""
        if self.username:
            return aiohttp.BasicAuth(self.username, self.password or "")
        return None

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        try:
            session = self.mass.http_session if self.verify_ssl else self.mass.http_session_no_ssl
            await webdav_test_connection(
                session,
                self.base_url,
                auth=self._auth,
                timeout=10,
            )
        except (LoginFailed, SetupFailedError):
            raise
        except Exception as err:
            raise SetupFailedError(f"WebDAV connection failed: {err}") from err

        self.write_access = False

    async def exists(self, file_path: str) -> bool:
        """Return bool if this WebDAV resource exists."""
        if not file_path:
            return False

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

    async def resolve(self, file_path: str) -> FileSystemItem:
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
                        absolute_path=webdav_url,
                        is_dir=True,
                    )
                raise MediaNotFoundError(f"WebDAV resource not found: {file_path}")

            webdav_item = items[0]

            return FileSystemItem(
                filename=PurePosixPath(file_path).name or webdav_item.name,
                relative_path=file_path,
                absolute_path=webdav_url,
                is_dir=webdav_item.is_dir,
                checksum=webdav_item.last_modified or "unknown",
                file_size=webdav_item.size,
            )

        except MediaNotFoundError:
            raise
        except Exception as err:
            raise MediaNotFoundError(f"Failed to resolve WebDAV path {file_path}: {err}") from err

    def get_absolute_path(self, file_path: str) -> str:
        """Return WebDAV URL for given file path."""
        return build_webdav_url(self.base_url, file_path)

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

    async def get_item_by_uri(self, uri: str) -> MediaItemType:
        """Get a single media item by URI."""
        item_path = uri.split("://", 1)[1] if "://" in uri else uri
        file_item = await self.resolve(item_path)

        # Folders aren't returned by get_item_by_uri - MA handles them differently
        if file_item.is_dir:
            raise MediaNotFoundError(f"Cannot get media item for directory: {item_path}")

        # Determine type from extension and return appropriate item
        ext = (
            PurePosixPath(file_item.filename).suffix.lstrip(".").lower()
            if "." in file_item.filename
            else None
        )

        if ext in TRACK_EXTENSIONS:
            return await self.get_track(item_path)

        if ext in PLAYLIST_EXTENSIONS:
            raise NotImplementedError("Playlist retrieval not yet implemented")

        if ext in AUDIOBOOK_EXTENSIONS and self.media_content_type == "audiobooks":
            raise NotImplementedError("Audiobook retrieval not yet implemented")

        if ext in PODCAST_EPISODE_EXTENSIONS and self.media_content_type == "podcasts":
            raise NotImplementedError("Podcast episode retrieval not yet implemented")

        raise MediaNotFoundError(f"Unsupported file type: {file_item.filename}")

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        file_item = await self.resolve(prov_track_id)
        auth_url = self._build_authenticated_url(prov_track_id)
        tags = await async_parse_tags(auth_url, file_item.file_size)
        return await self._parse_track(file_item, tags, full_album_metadata=True)

    async def get_album(self, prov_album_id: str) -> Album:
        """Get album by id - reconstructed from tracks."""
        items = await self._scandir(prov_album_id)
        for item in items:
            if item.ext in TRACK_EXTENSIONS:
                track = await self.get_track(item.relative_path)
                if track.album and isinstance(track.album, Album):
                    album = track.album
                    # Scan for folder images on every get_album call
                    folder_images = await self._get_local_images(prov_album_id)
                    if folder_images:
                        album.metadata.images = folder_images
                    return album
        raise MediaNotFoundError(f"No tracks found in album path: {prov_album_id}")

    async def get_audiobook(self, prov_audiobook_id: str) -> Audiobook:
        """Get full audiobook details by id."""
        file_item = await self.resolve(prov_audiobook_id)

        if file_item.is_dir:
            items = await self._scandir(prov_audiobook_id)
            audiobook_files = [f for f in items if not f.is_dir and f.ext in AUDIOBOOK_EXTENSIONS]
            if not audiobook_files:
                raise MediaNotFoundError(f"No audiobook files found in folder: {prov_audiobook_id}")

            sorted_files = sorted(audiobook_files, key=lambda x: x.filename)
            file_item = sorted_files[0]
            file_path = file_item.relative_path
        else:
            file_path = prov_audiobook_id

        auth_url = self._build_authenticated_url(file_path)
        tags = await async_parse_tags(auth_url, file_item.file_size)
        audiobook = self._build_audiobook_from_tags(file_item, tags)  # Use custom builder

        if file_item.relative_path != prov_audiobook_id:
            audiobook.item_id = prov_audiobook_id

        return audiobook

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get full podcast details by id."""
        async for episode in self.get_podcast_episodes(prov_podcast_id):
            assert isinstance(episode.podcast, Podcast)
            return episode.podcast
        raise MediaNotFoundError(f"Podcast not found: {prov_podcast_id}")

    async def get_podcast_episodes(
        self,
        prov_podcast_id: str,
    ) -> AsyncGenerator[PodcastEpisode, None]:
        """Get all episodes for a podcast."""
        items = await self._scandir(prov_podcast_id)
        episode_files = [f for f in items if not f.is_dir and f.ext in PODCAST_EPISODE_EXTENSIONS]

        for episode_file in sorted(episode_files, key=lambda x: x.filename):
            auth_url = self._build_authenticated_url(episode_file.relative_path)
            tags = await async_parse_tags(auth_url, episode_file.file_size)
            episode = self._build_podcast_episode_from_tags(
                episode_file, tags
            )  # Use custom builder
            yield episode

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get full podcast episode details by id."""
        file_item = await self.resolve(prov_episode_id)
        auth_url = self._build_authenticated_url(prov_episode_id)
        tags = await async_parse_tags(auth_url, file_item.file_size)
        episode = self._build_podcast_episode_from_tags(file_item, tags)

        # Add folder images to podcast
        podcast_folder = str(PurePosixPath(prov_episode_id).parent)
        folder_images = await self._get_local_images(podcast_folder)

        assert isinstance(episode.podcast, Podcast)
        if folder_images:
            episode.podcast.metadata.images = folder_images

        return episode

    async def sync_library(self, media_type: MediaType, import_as_favorite: bool = False) -> None:
        """Run library sync for WebDAV provider."""
        if self.sync_running:
            self.logger.warning(f"Library sync already running for {self.name}")
            return

        self.logger.info(f"Started library sync for WebDAV provider {self.name}")
        self.sync_running = True
        self.processed_audiobook_folders.clear()

        try:
            assert self.mass.music.database
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

    async def _get_audiobook_chapters_from_cache(
        self, item_id: str, file_item: FileSystemItem
    ) -> list[tuple[str, float]] | None:
        """Get audiobook chapters from cache, rebuilding if necessary."""
        file_based_chapters = await self.cache.get(
            key=item_id,
            provider=self.instance_id,
            category=CACHE_CATEGORY_AUDIOBOOK_CHAPTERS,
        )

        if file_based_chapters is None and file_item.is_dir:
            self.logger.debug(f"Cache miss for audiobook chapters, re-parsing: {item_id}")
            items = await self._scandir(item_id)
            audiobook_files = [f for f in items if not f.is_dir and f.ext in AUDIOBOOK_EXTENSIONS]

            if audiobook_files:
                await self._process_multifile_audiobook(item_id, audiobook_files, None)
                file_based_chapters = cast(
                    "list[tuple[str, float]] | None",
                    await self.cache.get(
                        key=item_id,
                        provider=self.instance_id,
                        category=CACHE_CATEGORY_AUDIOBOOK_CHAPTERS,
                    ),
                )
        return cast("list[tuple[str, float]] | None", file_based_chapters)

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

    def _calculate_start_chapter(
        self, chapters_data: list[tuple[str, float]], seek_position: int
    ) -> int:
        """Calculate which chapter to start from based on seek position."""
        # Define a small tolerance margin (e.g., 100 milliseconds)
        # This handles cases where seek_position is slightly less than the chapter's start time
        tolerance_seconds = 2.0

        if seek_position <= 0:
            return 0

        accumulated_duration = 0.0

        for i, (_, chapter_duration) in enumerate(chapters_data):
            chapter_end_time = accumulated_duration + chapter_duration

            # If the seek is within TOLERANCE of the next chapter's start time,
            # treat it as the next chapter's start time.

            # This is the time when chapter i ends, and chapter i+1 begins.

            if seek_position >= chapter_end_time - tolerance_seconds:
                # If seek_position is at or just before the chapter end time (within tolerance),
                # we treat it as seeking to the NEXT chapter.
                accumulated_duration = chapter_end_time
                continue

            # Otherwise, the seek position is clearly within the bounds of chapter i.
            return i

        # Seek position beyond total duration - start from last chapter
        return max(0, len(chapters_data) - 1)

    async def _scandir(self, path: str) -> list[FileSystemItem]:
        """List WebDAV directory contents."""
        webdav_url = build_webdav_url(self.base_url, path)
        session = self.mass.http_session if self.verify_ssl else self.mass.http_session_no_ssl

        try:
            webdav_items = await webdav_propfind(session, webdav_url, depth=1, auth=self._auth)
            filesystem_items: list[FileSystemItem] = []

            for webdav_item in webdav_items:
                # SKIP RECYCLE BIN
                if "#recycle" in webdav_item.name.lower():
                    continue
                decoded_href = unquote(webdav_item.href)
                decoded_base_url = unquote(self.base_url)

                # Extract path from full URL for comparison
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
                        absolute_path=webdav_item.href,
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

            # ADD SEMAPHORE FOR DIRECTORY SCANNING
            dir_semaphore = asyncio.Semaphore(3)  # Limit concurrent directory scans

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
                    prev_checksum = file_checksums.get(item.relative_path)
                    if await self._process_webdav_item(item, prev_checksum, import_as_favorite):
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

    async def _process_webdav_item(
        self,
        item: FileSystemItem,
        prev_checksum: str | None,
        import_as_favorite: bool,
    ) -> bool:
        """Process a single WebDAV item during library sync."""
        if item.is_dir:
            return False

        if not item.ext or item.ext not in SUPPORTED_EXTENSIONS:
            return False

        try:
            self.logger.log(VERBOSE_LOG_LEVEL, f"Processing WebDAV item: {item.relative_path}")

            # Skip if unchanged
            if item.checksum == prev_checksum:
                return True

            # Process based on media type
            if item.ext in TRACK_EXTENSIONS and self.media_content_type == "music":
                await self._process_track(item, prev_checksum)
                return True

            if item.ext in AUDIOBOOK_EXTENSIONS and self.media_content_type == "audiobooks":
                # Check if this is a multi-file audiobook folder
                folder_path = str(PurePosixPath(item.relative_path).parent)

                # Skip if we've already processed this folder
                if folder_path in self.processed_audiobook_folders:
                    return True

                # Scan folder to see if there are multiple audiobook files
                folder_items = await self._scandir(folder_path)
                audiobook_files = [
                    f for f in folder_items if not f.is_dir and f.ext in AUDIOBOOK_EXTENSIONS
                ]

                if len(audiobook_files) > 1:
                    # Multi-file audiobook - process entire folder
                    await self._process_multifile_audiobook(
                        folder_path, audiobook_files, prev_checksum
                    )
                    self.processed_audiobook_folders.add(folder_path)
                else:
                    # Single file audiobook
                    await self._process_audiobook(item, prev_checksum)
                return True

            if item.ext in PODCAST_EPISODE_EXTENSIONS and self.media_content_type == "podcasts":
                await self._process_podcast(item, prev_checksum)
                return True

        except Exception as err:
            self.logger.error(
                f"Error processing WebDAV item {item.relative_path}: {err}",
                exc_info=self.logger.isEnabledFor(10),
            )
        return False

    async def _process_track(self, item: FileSystemItem, prev_checksum: str | None) -> None:
        """Process a track item."""
        try:
            auth_url = self._build_authenticated_url(item.relative_path)
            tags = await async_parse_tags(auth_url, item.file_size)
            track = await self._parse_track(item, tags)

            # Add folder images to album if present
            if track.album and isinstance(track.album, Album):
                album_path = str(PurePosixPath(item.relative_path).parent)
                folder_images = await self._get_local_images(album_path)
                if folder_images and not track.album.metadata.images:
                    track.album.metadata.images = folder_images

                # Add folder images to album artists
                for artist in track.album.artists:
                    if isinstance(artist, Artist) and not artist.metadata.images:
                        artist_mapping = next(
                            (
                                m
                                for m in artist.provider_mappings
                                if m.provider_instance == self.instance_id
                            ),
                            None,
                        )
                        if artist_mapping and artist_mapping.url:
                            artist_images = await self._get_local_images(
                                artist_mapping.url, extra_thumb_names=("artist",)
                            )
                            if artist_images:
                                artist.metadata.images = artist_images

            # Also add images to track artists
            for artist in track.artists:
                if isinstance(artist, Artist) and not artist.metadata.images:
                    artist_mapping = next(
                        (
                            m
                            for m in artist.provider_mappings
                            if m.provider_instance == self.instance_id
                        ),
                        None,
                    )
                    if artist_mapping and artist_mapping.url:
                        artist_images = await self._get_local_images(
                            artist_mapping.url, extra_thumb_names=("artist",)
                        )
                        if artist_images:
                            artist.metadata.images = artist_images

            await self.mass.music.tracks.add_item_to_library(
                track, overwrite_existing=prev_checksum is not None
            )
        except (LoginFailed, SetupFailedError, ProviderUnavailableError):
            raise
        except aiohttp.ClientError as err:
            self.logger.error(
                f"Connection error processing WebDAV track {item.relative_path}: {err}"
            )
        except Exception as err:
            self.logger.error(f"Failed to process WebDAV track {item.relative_path}: {err}")

    async def _process_audiobook(self, item: FileSystemItem, prev_checksum: str | None) -> None:
        """Process an audiobook item."""
        try:
            auth_url = self._build_authenticated_url(item.relative_path)
            tags = await async_parse_tags(auth_url, item.file_size)
            audiobook = self._build_audiobook_from_tags(item, tags)  # Use custom builder
            await self.mass.music.audiobooks.add_item_to_library(
                audiobook, overwrite_existing=prev_checksum is not None
            )
        except (LoginFailed, SetupFailedError, ProviderUnavailableError):
            raise
        except aiohttp.ClientError as err:
            self.logger.error(
                f"Connection error processing WebDAV audiobook {item.relative_path}: {err}"
            )
        except Exception as err:
            self.logger.error(f"Failed to process WebDAV audiobook {item.relative_path}: {err}")

    async def _process_multifile_audiobook(
        self,
        folder_path: str,
        audiobook_files: list[FileSystemItem],
        prev_checksum: str | None,
    ) -> None:
        """Process a multi-file audiobook folder."""
        try:
            sorted_files = sorted(audiobook_files, key=lambda x: x.filename)
            first_file = sorted_files[0]

            auth_url = self._build_authenticated_url(first_file.relative_path)
            tags = await async_parse_tags(auth_url, first_file.file_size)

            # Use custom builder instead of parent's _parse_audiobook
            audiobook = self._build_audiobook_from_tags(first_file, tags)

            # Override item_id to be the folder
            audiobook.item_id = folder_path
            audiobook.provider_mappings = {
                ProviderMapping(
                    item_id=folder_path,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            }

            # Build chapters from all files
            chapters: list[MediaItemChapter] = []
            chapter_files: list[tuple[str, float]] = []
            total_duration = 0.0

            for idx, file_item in enumerate(sorted_files, start=1):
                file_auth_url = self._build_authenticated_url(file_item.relative_path)
                file_tags = await async_parse_tags(file_auth_url, file_item.file_size)
                duration = file_tags.duration or 0

                chapters.append(
                    MediaItemChapter(
                        position=idx,
                        name=file_tags.title or file_item.filename,
                        start=total_duration,
                        end=total_duration + duration,
                    )
                )
                chapter_files.append((file_item.relative_path, duration))
                total_duration += duration

            audiobook.duration = int(total_duration)
            audiobook.metadata.chapters = chapters

            # Cache chapter files for streaming lookup
            await self.cache.set(
                key=folder_path,
                data=chapter_files,
                provider=self.instance_id,
                category=CACHE_CATEGORY_AUDIOBOOK_CHAPTERS,
            )

            # Add folder images
            folder_images = await self._get_local_images(folder_path)
            if folder_images:
                audiobook.metadata.images = folder_images

            await self.mass.music.audiobooks.add_item_to_library(
                audiobook, overwrite_existing=prev_checksum is not None
            )

        except (LoginFailed, SetupFailedError, ProviderUnavailableError):
            raise
        except aiohttp.ClientError as err:
            self.logger.error(
                f"Connection error processing multi-file audiobook {folder_path}: {err}"
            )
        except Exception as err:
            self.logger.error(f"Failed to process multi-file audiobook {folder_path}: {err}")

    async def _process_podcast(self, item: FileSystemItem, prev_checksum: str | None) -> None:
        """Process a podcast episode item."""
        try:
            auth_url = self._build_authenticated_url(item.relative_path)
            tags = await async_parse_tags(auth_url, item.file_size)
            episode = self._build_podcast_episode_from_tags(item, tags)  # Use custom builder

            podcast_folder = str(PurePosixPath(item.relative_path).parent)
            folder_images = await self._get_local_images(podcast_folder)

            assert isinstance(episode.podcast, Podcast)
            if folder_images:
                episode.podcast.metadata.images = folder_images

            await self.mass.music.podcasts.add_item_to_library(
                episode.podcast, overwrite_existing=prev_checksum is not None
            )
        except (LoginFailed, SetupFailedError, ProviderUnavailableError):
            raise
        except aiohttp.ClientError as err:
            self.logger.error(
                f"Connection error processing WebDAV podcast {item.relative_path}: {err}"
            )
        except Exception as err:
            self.logger.error(f"Failed to process WebDAV podcast {item.relative_path}: {err}")

    async def _get_local_images(
        self,
        item_path: str,
        extra_thumb_names: tuple[str, ...] | None = None,
    ) -> UniqueList[MediaItemImage]:
        """Get images from WebDAV folder."""
        try:
            # Scan the WebDAV directory for image files
            items = await self._scandir(item_path)
            images: list[MediaItemImage] = []

            # Standard image filenames to look for
            standard_names = {"folder", "cover", "albumart", "album", "front"}
            if extra_thumb_names:
                standard_names.update(extra_thumb_names)

            for item in items:
                if item.is_dir or not item.ext:
                    continue

                if item.ext in IMAGE_EXTENSIONS:
                    # Check if it's a standard cover image name
                    name_lower = item.filename.lower().rsplit(".", 1)[0]
                    if name_lower in standard_names:
                        webdav_url = build_webdav_url(self.base_url, item.relative_path)
                        images.append(
                            MediaItemImage(
                                type=ImageType.THUMB,
                                path=webdav_url,
                                provider=self.instance_id,
                                remotely_accessible=False,
                            )
                        )

            return UniqueList(images)
        except Exception as err:
            self.logger.debug(f"Failed to get images from {item_path}: {err}")
            return UniqueList()

    async def resolve_image(self, path: str) -> str | bytes:
        """Resolve image path to actual image data or URL."""
        webdav_url = build_webdav_url(self.base_url, path)
        session = self.mass.http_session if self.verify_ssl else self.mass.http_session_no_ssl

        async with session.get(webdav_url, auth=self._auth) as resp:
            if resp.status != 200:
                raise MediaNotFoundError(f"Image not found: {path}")
            return await resp.read()

    async def _get_stream_details_for_track(self, item_id: str) -> StreamDetails:
        """Return the streamdetails for a track/song."""
        library_item = await self.mass.music.tracks.get_library_item_by_prov_id(
            item_id, self.instance_id
        )
        if library_item is None:
            file_item = await self.resolve(item_id)
            auth_url = self._build_authenticated_url(item_id)
            tags = await async_parse_tags(auth_url, file_item.file_size)
            library_item = await self._parse_track(file_item, tags)

        prov_mapping = next(x for x in library_item.provider_mappings if x.item_id == item_id)
        file_item = await self.resolve(item_id)
        auth_url = self._build_authenticated_url(item_id)

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=prov_mapping.audio_format,
            media_type=MediaType.TRACK,
            stream_type=StreamType.HTTP,  # CRITICAL: HTTP not LOCAL_FILE
            duration=library_item.duration,
            size=file_item.file_size,
            path=auth_url,  # CRITICAL: authenticated URL
            can_seek=True,
            allow_seek=True,
        )

    async def _get_stream_details_for_podcast_episode(self, item_id: str) -> StreamDetails:
        """Return the streamdetails for a podcast episode."""
        file_item = await self.resolve(item_id)
        auth_url = self._build_authenticated_url(item_id)
        tags = await async_parse_tags(auth_url, file_item.file_size)

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.try_parse(file_item.ext or tags.format),
                sample_rate=tags.sample_rate,
                bit_depth=tags.bits_per_sample,
                channels=tags.channels,
                bit_rate=tags.bit_rate,
            ),
            media_type=MediaType.PODCAST_EPISODE,
            stream_type=StreamType.HTTP,
            duration=int(tags.duration or 0),
            size=file_item.file_size,
            path=auth_url,
            allow_seek=True,
            can_seek=True,
        )

    async def _get_stream_details_for_audiobook(self, item_id: str) -> StreamDetails:
        """Return the streamdetails for an audiobook."""
        library_item = await self.mass.music.audiobooks.get_library_item_by_prov_id(
            item_id, self.instance_id
        )
        if library_item is None:
            file_item = await self.resolve(item_id)
            auth_url = self._build_authenticated_url(item_id)
            tags = await async_parse_tags(auth_url, file_item.file_size)
            library_item = self._build_audiobook_from_tags(file_item, tags)

        prov_mapping = next(x for x in library_item.provider_mappings if x.item_id == item_id)
        file_item = await self.resolve(item_id)

        # Check for multi-file audiobook
        file_based_chapters = await self._get_audiobook_chapters_from_cache(item_id, file_item)

        if file_based_chapters:
            # Multi-file audiobook - use CUSTOM stream type
            chapter_urls = [self._build_authenticated_url(ch[0]) for ch in file_based_chapters]
            return StreamDetails(
                provider=self.instance_id,
                item_id=item_id,
                audio_format=prov_mapping.audio_format,
                media_type=MediaType.AUDIOBOOK,
                stream_type=StreamType.CUSTOM,  # CUSTOM not MULTI_FILE
                duration=library_item.duration,
                data={  # data dict, not path list
                    "chapters": chapter_urls,
                    "chapters_data": file_based_chapters,
                },
                allow_seek=True,
                can_seek=True,
            )

        # Single file audiobook
        auth_url = self._build_authenticated_url(item_id)
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=prov_mapping.audio_format,
            media_type=MediaType.AUDIOBOOK,
            stream_type=StreamType.HTTP,
            duration=library_item.duration,
            size=file_item.file_size,
            path=auth_url,
            can_seek=True,
            allow_seek=True,
        )

    def _build_audiobook_from_tags(self, file_item: FileSystemItem, tags: AudioTags) -> Audiobook:
        """Build audiobook from tags without filesystem operations."""
        book_name = tags.album or tags.title

        audiobook = Audiobook(
            item_id=file_item.relative_path,
            provider=self.instance_id,
            name=book_name,
            sort_name=tags.album_sort or tags.title_sort,
            version=tags.version,
            duration=int(tags.duration or 0),
            provider_mappings={
                ProviderMapping(
                    item_id=file_item.relative_path,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(
                        content_type=ContentType.try_parse(file_item.ext or tags.format),
                        sample_rate=tags.sample_rate,
                        bit_depth=tags.bits_per_sample,
                        channels=tags.channels,
                        bit_rate=tags.bit_rate,
                    ),
                    details=file_item.checksum,
                )
            },
        )

        # Set authors and metadata
        audiobook.authors.set(tags.writers or tags.album_artists or tags.artists)
        audiobook.metadata.genres = set(tags.genres)
        audiobook.metadata.copyright = tags.get("copyright")
        audiobook.metadata.description = tags.get("comment")

        if tags.musicbrainz_recordingid:
            audiobook.mbid = tags.musicbrainz_recordingid

        # Handle embedded chapters
        if tags.chapters:
            audiobook.metadata.chapters = [
                MediaItemChapter(
                    position=chapter.chapter_id,
                    name=chapter.title or f"Chapter {chapter.chapter_id}",
                    start=chapter.position_start,
                    end=chapter.position_end,
                )
                for chapter in tags.chapters
            ]

        # Handle embedded cover image
        if tags.has_cover_image:
            audiobook.metadata.add_image(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=file_item.relative_path,
                    provider=self.instance_id,
                    remotely_accessible=False,
                )
            )

        return audiobook

    def _build_podcast_episode_from_tags(
        self, file_item: FileSystemItem, tags: AudioTags
    ) -> PodcastEpisode:
        """Build podcast episode from tags without filesystem operations."""
        podcast_name = tags.album or PurePosixPath(file_item.relative_path).parent.name
        podcast_path = str(PurePosixPath(file_item.relative_path).parent)

        episode = PodcastEpisode(
            item_id=file_item.relative_path,
            provider=self.instance_id,
            name=tags.title,
            sort_name=tags.title_sort,
            provider_mappings={
                ProviderMapping(
                    item_id=file_item.relative_path,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(
                        content_type=ContentType.try_parse(file_item.ext or tags.format),
                        sample_rate=tags.sample_rate,
                        bit_depth=tags.bits_per_sample,
                        channels=tags.channels,
                        bit_rate=tags.bit_rate,
                    ),
                    details=file_item.checksum,
                )
            },
            position=tags.track or 0,
            duration=int(tags.duration or 0),
            podcast=Podcast(
                item_id=podcast_path,
                provider=self.instance_id,
                name=podcast_name,
                sort_name=tags.album_sort,
                publisher=tags.get("publisher"),
                total_episodes=0,
                provider_mappings={
                    ProviderMapping(
                        item_id=podcast_path,
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                    )
                },
            ),
        )

        # Set metadata
        episode.metadata.genres = set(tags.genres)
        episode.metadata.copyright = tags.get("copyright")
        episode.metadata.description = tags.get("comment")

        # Handle chapters
        if tags.chapters:
            episode.metadata.chapters = [
                MediaItemChapter(
                    position=chapter.chapter_id,
                    name=chapter.title or f"Chapter {chapter.chapter_id}",
                    start=chapter.position_start,
                    end=chapter.position_end,
                )
                for chapter in tags.chapters
            ]

        # Handle embedded cover
        if tags.has_cover_image:
            episode.metadata.add_image(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=file_item.relative_path,
                    provider=self.instance_id,
                    remotely_accessible=False,
                )
            )

        return episode

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Get audio stream for WebDAV items."""
        if streamdetails.media_type == MediaType.AUDIOBOOK and isinstance(streamdetails.data, dict):
            async for chunk in self._stream_multifile_audiobook(streamdetails, seek_position):
                yield chunk
        else:
            raise NotImplementedError("Use HTTP stream type for single files")

    async def _stream_multifile_audiobook(
        self, streamdetails: StreamDetails, seek_position: int
    ) -> AsyncGenerator[bytes, None]:
        """Stream multi-file audiobook chapters."""
        chapter_urls = streamdetails.data.get("chapters", [])
        chapters_data = streamdetails.data.get("chapters_data", [])
        start_chapter = self._calculate_start_chapter(chapters_data, seek_position)

        chapters_yielded = False
        for i in range(start_chapter, len(chapter_urls)):
            try:
                async with self.mass.http_session.get(chapter_urls[i]) as response:
                    response.raise_for_status()
                    async for chunk in response.content.iter_chunked(8192):
                        chapters_yielded = True
                        yield chunk
            except Exception as e:
                self.logger.exception(f"Chapter {i + 1} streaming failed: {e}")
                continue

        if not chapters_yielded:
            raise MediaNotFoundError(
                f"Failed to stream any chapters for audiobook {streamdetails.item_id}"
            )
