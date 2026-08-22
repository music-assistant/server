"""Filesystem musicprovider support for MusicAssistant."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import os.path
import posixpath
import urllib.parse
from collections.abc import AsyncGenerator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast
from xml.parsers.expat import ExpatError

import aiofiles
import shortuuid
import xmltodict
from aiofiles.os import wrap
from music_assistant_models.enums import (
    ContentType,
    EventType,
    ExternalID,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    MusicAssistantError,
    SetupFailedError,
)
from music_assistant_models.helpers import create_safe_string
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
    Playlist,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    SearchResults,
    SoundEffect,
    Track,
    UniqueList,
    is_track,
)
from music_assistant_models.streamdetails import MultiPartPath, StreamDetails

from music_assistant.constants import (
    CONF_PATH,
    DB_TABLE_ALBUM_ARTISTS,
    DB_TABLE_ALBUM_TRACKS,
    DB_TABLE_ALBUMS,
    DB_TABLE_ARTISTS,
    DB_TABLE_PROVIDER_MAPPINGS,
    DB_TABLE_TRACK_ARTISTS,
    VARIOUS_ARTISTS_MBID,
    VARIOUS_ARTISTS_NAME,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.controllers.tasks.context import (
    report_current_task_failure,
    update_current_task_progress_from_index,
    update_current_task_progress_text,
)
from music_assistant.helpers import lyrics
from music_assistant.helpers.compare import compare_strings
from music_assistant.helpers.json import SerializableType, json_loads
from music_assistant.helpers.playlists import parse_m3u, parse_pls
from music_assistant.helpers.tags import AudioTags, async_parse_tags, clean_mbid, split_items
from music_assistant.helpers.util import (
    TaskManager,
    detect_charset,
    parse_title_and_version,
    try_parse_int,
)
from music_assistant.models.music_provider import MusicProvider

from .constants import (
    AUDIOBOOK_EXTENSIONS,
    AVAILABILITY_PROBE_INTERVAL,
    CACHE_CATEGORY_ALBUM_INFO,
    CACHE_CATEGORY_ARTIST_INFO,
    CACHE_CATEGORY_AUDIOBOOK_CHAPTERS,
    CACHE_CATEGORY_FOLDER_IMAGES,
    CACHE_CATEGORY_PODCAST_EPISODES,
    CACHE_CATEGORY_PODCAST_METADATA,
    CACHE_CATEGORY_SOUND_EFFECTS,
    CONF_CONTENT_TYPE,
    CONF_ENTRY_CONTENT_TYPE,
    CONF_ENTRY_IGNORE_ALBUM_PLAYLISTS,
    CONF_ENTRY_LIBRARY_SYNC_AUDIOBOOKS,
    CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS,
    CONF_ENTRY_LIBRARY_SYNC_PODCASTS,
    CONF_ENTRY_LIBRARY_SYNC_TRACKS,
    CONF_ENTRY_MISSING_ALBUM_ARTIST,
    CONF_ENTRY_PROPAGATE_GENRES,
    CUE_EXTENSIONS,
    DEFAULT_AUDIOBOOK_PODCAST_GENRE,
    IMAGE_EXTENSIONS,
    PARTIAL_LISTING_CACHE_EXPIRATION,
    PLAYLIST_EXTENSIONS,
    PODCAST_EPISODE_EXTENSIONS,
    SOUND_EFFECT_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    TRACK_EXTENSIONS,
    IsChapterFile,
    content_type_config_entry,
)
from .cue import (
    CueSheetHandler,
    cue_metadata_checksum,
    cue_referenced_audio_stem,
    make_cue_track_id,
    parse_cue_track_id,
)
from .helpers import (
    FileSystemItem,
    ScanErrors,
    get_absolute_path,
    get_album_dir,
    get_artist_dir,
    get_folder_signature,
    get_relative_path,
    recursive_iter,
    sorted_scandir,
)
from .parsers import parse_album_nfo

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType
    from music_assistant.providers.musicbrainz import MusicbrainzProvider


isdir = wrap(os.path.isdir)
isfile = wrap(os.path.isfile)
ismount = wrap(os.path.ismount)
exists = wrap(os.path.exists)
makedirs = wrap(os.makedirs)

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return LocalFileSystemProvider(mass, manifest, config)


class LocalFileSystemProvider(MusicProvider):
    """
    Implementation of a musicprovider for (local) files.

    Reads ID3 tags from file and falls back to parsing filename.
    Optionally reads metadata from nfo files and images in folder structure <artist>/<album>.
    Supports m3u files for playlists.
    """

    # parallel workers per sync; subclasses lower this for slower transports
    _SYNC_CONCURRENCY: ClassVar[int] = 16
    _sync_tracks: bool = True
    _sync_playlists: bool = True

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        base_path: str | None = None,
    ) -> None:
        """Initialize MusicProvider."""
        super().__init__(mass, manifest, config, SUPPORTED_FEATURES)
        # subclasses (NFS/SMB/...) mount elsewhere and pass their own base_path;
        # the plain local provider reads its scan directory from the setup data
        self.base_path: str = (
            base_path if base_path is not None else cast("str", self.get_setup_value(CONF_PATH))
        )
        self.write_access: bool = False
        self.sync_running: bool = False
        self.media_content_type = cast(
            "str", self.get_setup_value(CONF_CONTENT_TYPE, CONF_ENTRY_CONTENT_TYPE.default_value)
        )
        self._cue = CueSheetHandler(self)

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        # content type and path are collected by the setup flow; surface the (immutable)
        # content type read-only so the sync options' depends_on chains still resolve
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
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        base_features = {*SUPPORTED_FEATURES}
        if self.media_content_type == "audiobooks":
            return {ProviderFeature.LIBRARY_AUDIOBOOKS, *base_features}
        if self.media_content_type == "podcasts":
            return {ProviderFeature.LIBRARY_PODCASTS, *base_features}
        if self.media_content_type == "sound_effects":
            # sound effects are live-fetched content, never synced into the library
            return {ProviderFeature.SOUND_EFFECTS, *base_features}
        music_features = {
            ProviderFeature.LIBRARY_ALBUMS,
            ProviderFeature.LIBRARY_ARTISTS,
            ProviderFeature.LIBRARY_TRACKS,
            ProviderFeature.LIBRARY_PLAYLISTS,
            *base_features,
        }
        if self.write_access:
            music_features.add(ProviderFeature.PLAYLIST_TRACKS_EDIT)
            music_features.add(ProviderFeature.PLAYLIST_CREATE)
        return music_features

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return False

    @property
    def instance_name_postfix(self) -> str | None:
        """Return a (default) instance name postfix for this provider instance."""
        return Path(self.base_path).name

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        if not await isdir(self.base_path):
            msg = f"Music Directory {self.base_path} does not exist"
            raise SetupFailedError(
                msg,
                translation_key="music_directory_not_found",
                translation_owner=self.translation_owner,
                translation_args=[self.base_path],
            )
        await self.check_write_access()

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        self._cancel_availability_probe()
        # a check that already started runs as a task under the same id, and it would
        # otherwise keep talking to storage this unload is in the middle of tearing down
        self.mass.cancel_task(self._availability_probe_id)

    async def get_diagnostics(self) -> dict[str, SerializableType]:
        """Return diagnostics info for this provider to include in diagnostics reports."""
        return {
            "sync_running": self.sync_running,
            "write_access": self.write_access,
            "content_type": self.media_content_type,
        }

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType] | None,
        limit: int = 5,
    ) -> SearchResults:
        """Perform search on this file based musicprovider."""
        result = SearchResults()
        # searching the filesystem is slow and unreliable,
        # so instead we just query the db...
        if media_types is None or MediaType.TRACK in media_types:
            result.tracks = await self.mass.music.tracks.get_library_items_by_query(
                search=search_query, provider_filter=[self.instance_id], limit=limit
            )

        if media_types is None or MediaType.ALBUM in media_types:
            result.albums = await self.mass.music.albums.get_library_items_by_query(
                search=search_query,
                provider_filter=[self.instance_id],
                limit=limit,
            )

        if media_types is None or MediaType.ARTIST in media_types:
            result.artists = await self.mass.music.artists.get_library_items_by_query(
                search=search_query,
                provider_filter=[self.instance_id],
                limit=limit,
            )
        if media_types is None or MediaType.PLAYLIST in media_types:
            result.playlists = await self.mass.music.playlists.get_library_items_by_query(
                search=search_query,
                provider_filter=[self.instance_id],
                limit=limit,
            )
        if media_types is None or MediaType.AUDIOBOOK in media_types:
            result.audiobooks = await self.mass.music.audiobooks.get_library_items_by_query(
                search=search_query,
                provider_filter=[self.instance_id],
                limit=limit,
            )
        if media_types is None or MediaType.PODCAST in media_types:
            result.podcasts = await self.mass.music.podcasts.get_library_items_by_query(
                search=search_query,
                provider_filter=[self.instance_id],
                limit=limit,
            )
        return result

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse this provider's items.

        :param path: The path to browse, (e.g. provid://artists).
        """
        # for audiobooks and podcasts we just return all library items
        if self.media_content_type == "podcasts":
            return await self.mass.music.podcasts.library_items(
                provider=self.instance_id, summary=False
            )
        if self.media_content_type == "audiobooks":
            return await self.mass.music.audiobooks.library_items(
                provider=self.instance_id, summary=False
            )
        items: list[MediaItemType | ItemMapping | BrowseFolder] = []
        item_path = path.split("://", 1)[1]
        if not item_path:
            item_path = ""
        scanned = await self._scandir(item_path)
        # expand CUE sheets into per-track entries and hide the companion audio;
        # synthetic ids match those minted during sync so get_track resolves them
        cue_stems: set[str] = set()
        if self.media_content_type == "music":
            for item in scanned:
                if item.ext not in CUE_EXTENSIONS:
                    continue
                cue_stems.add(item.absolute_path.rsplit(".", 1)[0])
                try:
                    cue_sheet = await self._cue.load_cue_sheet(item)
                except InvalidDataError as err:
                    self.logger.warning("Unable to parse CUE sheet %s: %s", item.relative_path, err)
                    continue
                # also hide the audio file named in the CUE (may differ from its stem)
                if companion_stem := cue_referenced_audio_stem(item, cue_sheet):
                    cue_stems.add(companion_stem)
                for cue_track in cue_sheet.tracks:
                    items.append(
                        ItemMapping(
                            media_type=MediaType.TRACK,
                            item_id=make_cue_track_id(item.relative_path, cue_track.number),
                            provider=self.instance_id,
                            name=cue_track.title or f"Track {cue_track.number}",
                        )
                    )
        for item in scanned:
            if not item.is_dir and ("." not in item.filename or not item.ext):
                # skip system files and files without extension
                continue

            if item.is_dir:
                items.append(
                    BrowseFolder(
                        item_id=item.relative_path,
                        provider=self.instance_id,
                        path=f"{self.instance_id}://{item.relative_path}",
                        name=item.filename,
                        # mark folder as playable, assuming it contains tracks underneath
                        is_playable=True,
                    )
                )
            elif item.ext in TRACK_EXTENSIONS:
                if item.absolute_path.rsplit(".", 1)[0] in cue_stems:
                    continue
                items.append(
                    ItemMapping(
                        media_type=(
                            MediaType.SOUND_EFFECT
                            if self.media_content_type == "sound_effects"
                            else MediaType.TRACK
                        ),
                        item_id=item.relative_path,
                        provider=self.instance_id,
                        name=item.filename,
                    )
                )
            elif item.ext in PLAYLIST_EXTENSIONS and self.media_content_type == "music":
                items.append(
                    ItemMapping(
                        media_type=MediaType.PLAYLIST,
                        item_id=item.relative_path,
                        provider=self.instance_id,
                        name=item.filename,
                    )
                )
        if self.media_content_type == "music":
            track_indexes = [
                index
                for index, item in enumerate(items)
                if isinstance(item, ItemMapping) and item.media_type == MediaType.TRACK
            ]
            library_tracks = await asyncio.gather(
                *(
                    self.mass.music.tracks.get_library_item_by_prov_id(
                        items[index].item_id, self.instance_id
                    )
                    for index in track_indexes
                )
            )
            for index, library_track in zip(track_indexes, library_tracks, strict=True):
                if library_track:
                    items[index] = library_track
        return items

    async def sync_library(self, media_type: MediaType) -> None:
        """Run library sync for this provider."""
        if media_type in (MediaType.ARTIST, MediaType.ALBUM):
            # artists and albums are synced as part of track sync
            return
        if self.media_content_type == "sound_effects":
            # sound effects are live-fetched content, never synced into the library
            return
        # check if any sync options are enabled for this content type
        # the filesystem provider processes all file types in one scan,
        # so we can return early if nothing needs syncing
        if self.media_content_type == "music":
            self._sync_tracks = bool(self.config.get_value(CONF_ENTRY_LIBRARY_SYNC_TRACKS.key))
            self._sync_playlists = bool(
                self.config.get_value(CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS.key)
            )
            if not self._sync_tracks and not self._sync_playlists:
                return
        elif self.media_content_type == "audiobooks":
            if not self.config.get_value(CONF_ENTRY_LIBRARY_SYNC_AUDIOBOOKS.key):
                return
        elif self.media_content_type == "podcasts":
            if not self.config.get_value(CONF_ENTRY_LIBRARY_SYNC_PODCASTS.key):
                return
        assert self.mass.music.database
        if self.sync_running:
            self.logger.warning("Library sync already running for %s", self.name)
            return
        file_checksums: dict[str, str] = {}
        # NOTE: we always run a scan of the entire library, as we need to detect changes
        # we ignore any given mediatype(s) and just scan all supported files
        query = (
            f"SELECT provider_item_id, details FROM {DB_TABLE_PROVIDER_MAPPINGS} "
            f"WHERE provider_instance = '{self.instance_id}' "
            f"AND media_type in ('track', 'playlist', 'audiobook', 'podcast_episode')"
        )
        for db_row in await self.mass.music.database.get_rows_from_query(query, limit=0):
            file_checksums[db_row["provider_item_id"]] = str(db_row["details"])
        # provider_mappings stores synthetic per-track ids for CUE sheets, not the
        # CUE path, so collect every track checksum per path for the scan classifier
        cue_file_checksums: dict[str, set[str]] = {}
        for prov_item_id, checksum in file_checksums.items():
            parsed = parse_cue_track_id(prov_item_id)
            if parsed is not None:
                cue_file_checksums.setdefault(parsed[0], set()).add(checksum)
        # find all supported files in the base directory and all subfolders
        # we work bottom up, as-in we derive all info from the tracks
        cur_filenames: set[str] = set()
        prev_filenames = set(file_checksums.keys())

        items_to_process: list[tuple[FileSystemItem, str | None]] = []
        unchanged_cue_items: list[FileSystemItem] = []
        # absolute paths of every CUE sheet in this scan with the ".cue" stripped,
        # used for O(1) companion-CUE lookups per audio file
        cue_stems: set[str] = set()
        # collects the errors raised while walking the tree; any error means the
        # scan is incomplete, a fatal one means the provider is unreachable
        scan_errors = ScanErrors()

        self.sync_running = True
        try:
            await self._enumerate_files_for_sync(
                file_checksums=file_checksums,
                cue_file_checksums=cue_file_checksums,
                cur_filenames=cur_filenames,
                items_to_process=items_to_process,
                unchanged_cue_items=unchanged_cue_items,
                cue_stems=cue_stems,
                scan_errors=scan_errors,
            )
            if scan_errors.fatal:
                # the storage is gone, so reading the files collected before it went
                # away would only add a timeout each
                self.logger.error("Aborting sync for %s: %s", self.name, scan_errors.fatal)
                report_current_task_failure("Sync aborted: filesystem unavailable during scan")
                self._set_available(False)
                return
            # a CUE may name an audio file other than its own; hide that companion too
            if self.media_content_type == "music":
                for cue_item in (
                    *unchanged_cue_items,
                    *(item for item, _ in items_to_process if item.ext in CUE_EXTENSIONS),
                ):
                    try:
                        cue_sheet = await self._cue.load_cue_sheet(cue_item)
                    except InvalidDataError:
                        continue
                    if companion_stem := cue_referenced_audio_stem(cue_item, cue_sheet):
                        cue_stems.add(companion_stem)
            # drop CUE companion audio: absorbed into CUE tracks and not tracked in
            # provider_mappings, so they would otherwise flag as changed every sync
            items_to_process = [
                (item, prev)
                for item, prev in items_to_process
                if not (
                    item.ext in TRACK_EXTENSIONS
                    and item.absolute_path.rsplit(".", 1)[0] in cue_stems
                )
            ]
            # register synthetic track IDs for unchanged CUE files so the
            # deletion pass does not treat them as removed
            for cue_item in unchanged_cue_items:
                try:
                    cue_sheet = await self._cue.load_cue_sheet(cue_item)
                except InvalidDataError as err:
                    self.logger.warning(
                        "Unable to parse CUE sheet %s: %s", cue_item.relative_path, err
                    )
                    continue
                for cue_track in cue_sheet.tracks:
                    cur_filenames.add(make_cue_track_id(cue_item.relative_path, cue_track.number))
            total_items = len(items_to_process)
            self.logger.info(
                "Found %d changed/new items to process for %s",
                total_items,
                self.name,
            )

            # _SYNC_CONCURRENCY caps parallelism per provider (NFS/SMB/WebDAV friendly)
            processed_count = 0

            async def _process(item: FileSystemItem, prev_checksum: str | None) -> None:
                nonlocal processed_count
                if await self._process_item_async(
                    item, prev_checksum, cur_filenames, cue_stems, prev_filenames
                ):
                    cur_filenames.add(item.relative_path)
                processed_count += 1
                if processed_count % 50 == 0 or processed_count == total_items:
                    update_current_task_progress_from_index(
                        processed_count,
                        total_items,
                        f"Processed {processed_count}/{total_items} files",
                    )

            async with TaskManager(self.mass, self._SYNC_CONCURRENCY) as tm:
                for item, prev_checksum in items_to_process:
                    await tm.create_task_with_limit(_process(item, prev_checksum))
        finally:
            self.sync_running = False

        # do not run deletions on a clean but empty scan of a previously non-empty library
        # (wrong share mounted, empty backup mount, ...)
        if prev_filenames and not cur_filenames:
            self.logger.error(
                "Aborting sync for %s: scan found no files but %d were previously indexed",
                self.name,
                len(prev_filenames),
            )
            report_current_task_failure(
                f"Sync aborted: scan found no files but {len(prev_filenames)} "
                "were previously indexed"
            )
            return

        # a scan that skipped folders or files is incomplete: what it missed is still
        # there, so deleting it from the library would throw away valid content
        if scan_errors.incomplete:
            summary = scan_errors.describe()
            self.logger.warning("Skipping deletions for %s: %s", self.name, summary)
            report_current_task_failure(f"Deletions skipped: {summary}")
        else:
            deleted_files = prev_filenames - cur_filenames
            await self._process_deletions(deleted_files)
            await self._process_orphaned_albums_and_artists()

        # flag provider as available again if an earlier sync had marked it down
        self._set_available(True)

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        db_artist = await self.mass.music.artists.get_library_item_by_prov_id(
            prov_artist_id, self.instance_id
        )
        if not db_artist:
            # this may happen if the artist is not in the db yet
            # e.g. when browsing the filesystem
            if await self.exists(prov_artist_id):
                return await self._parse_artist(prov_artist_id, artist_path=prov_artist_id)
            return await self._parse_artist(prov_artist_id)

        # prov_artist_id is either an actual (relative) path or a name (as fallback)
        safe_artist_name = create_safe_string(prov_artist_id, lowercase=False, replace_space=False)
        if await self.exists(prov_artist_id):
            artist_path = prov_artist_id
        elif await self.exists(safe_artist_name):
            artist_path = safe_artist_name
        else:
            for prov_mapping in db_artist.provider_mappings:
                if prov_mapping.provider_instance != self.instance_id:
                    continue
                if prov_mapping.url:
                    artist_path = prov_mapping.url
                    break
            else:
                # this is an artist without an actual path on disk
                # return the info we already have in the db
                return db_artist
        return await self._parse_artist(
            db_artist.name,
            sort_name=db_artist.sort_name,
            mbid=db_artist.mbid,
            artist_path=artist_path,
        )

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        parsed_cue_paths: set[str] = set()
        for track in await self.get_album_tracks(prov_album_id):
            for prov_mapping in track.provider_mappings:
                if prov_mapping.provider_instance != self.instance_id:
                    continue
                if parsed := parse_cue_track_id(prov_mapping.item_id):
                    # every track from the same CUE shares the same album; only parse once
                    if parsed[0] in parsed_cue_paths:
                        continue
                    parsed_cue_paths.add(parsed[0])
                    cue_item = await self.resolve(parsed[0])
                    for cue_track in await self._cue.parse_tracks(cue_item):
                        if isinstance(cue_track.album, Album):
                            return cue_track.album
                    continue
                file_item = await self.resolve(prov_mapping.item_id)
                tags = await async_parse_tags(file_item.absolute_path, file_item.file_size)
                full_track = await self._parse_track(file_item, tags)
                assert isinstance(full_track.album, Album)
                return full_track.album
        msg = f"Album not found: {prov_album_id}"
        raise MediaNotFoundError(msg)

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        # ruff: noqa: PLR0915
        if parsed := parse_cue_track_id(prov_track_id):
            cue_item = await self.resolve(parsed[0])
            for cue_track in await self._cue.parse_tracks(cue_item):
                if cue_track.item_id == prov_track_id:
                    return cue_track
            msg = f"CUE track not found: {prov_track_id}"
            raise MediaNotFoundError(msg)

        if not await self.exists(prov_track_id):
            msg = f"Track path does not exist: {prov_track_id}"
            raise MediaNotFoundError(msg)

        file_item = await self.resolve(prov_track_id)
        tags = await async_parse_tags(file_item.absolute_path, file_item.file_size)
        return await self._parse_track(file_item, tags=tags, full_album_metadata=True)

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get (full) podcast episode details by id."""
        if not await self.exists(prov_episode_id):
            msg = f"Episode path does not exist: {prov_episode_id}"
            raise MediaNotFoundError(msg)
        file_item = await self.resolve(prov_episode_id)
        tags = await async_parse_tags(file_item.absolute_path, file_item.file_size)
        return await self._parse_podcast_episode(file_item, tags=tags)

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        if not await self.exists(prov_playlist_id):
            msg = f"Playlist path does not exist: {prov_playlist_id}"
            raise MediaNotFoundError(msg)

        file_item = await self.resolve(prov_playlist_id)
        playlist = Playlist(
            item_id=file_item.relative_path,
            provider=self.instance_id,
            name=file_item.name,
            provider_mappings={
                ProviderMapping(
                    item_id=file_item.relative_path,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    details=file_item.checksum,
                    in_library=True,
                )
            },
        )
        playlist.is_editable = ProviderFeature.PLAYLIST_TRACKS_EDIT in self.supported_features
        # only playlists in the root are editable - all other are read only
        if "/" in prov_playlist_id or "\\" in prov_playlist_id:
            playlist.is_editable = False
        # we do not (yet) have support to edit/create pls playlists, only m3u files can be edited
        if file_item.ext == "pls":
            playlist.is_editable = False
        playlist.owner = self.name
        # Check for local image with the same basename
        if local_image := await self._get_playlist_local_image(file_item):
            playlist.metadata.images = UniqueList([local_image])
        return playlist

    async def get_audiobook(self, prov_audiobook_id: str) -> Audiobook:
        """Get full audiobook details by id."""
        # ruff: noqa: PLR0915
        if not await self.exists(prov_audiobook_id):
            msg = f"Audiobook path does not exist: {prov_audiobook_id}"
            raise MediaNotFoundError(msg)

        file_item = await self.resolve(prov_audiobook_id)
        tags = await async_parse_tags(file_item.absolute_path, file_item.file_size)
        return await self._parse_audiobook(file_item, tags=tags)

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get full podcast details by id."""
        async for episode in self.get_podcast_episodes(prov_podcast_id):
            assert isinstance(episode.podcast, Podcast)
            return episode.podcast
        msg = f"Podcast not found: {prov_podcast_id}"
        raise MediaNotFoundError(msg)

    async def get_sound_effect(self, prov_sound_effect_id: str) -> SoundEffect:
        """Get full sound effect details by id."""
        if not await self.exists(prov_sound_effect_id):
            msg = f"Sound effect path does not exist: {prov_sound_effect_id}"
            raise MediaNotFoundError(msg)
        file_item = await self.resolve(prov_sound_effect_id)
        return await self._get_or_parse_sound_effect(file_item)

    async def get_sound_effects(self) -> AsyncGenerator[SoundEffect]:
        """Get all sound effect items this provider offers."""

        def _walk() -> list[FileSystemItem]:
            return sorted(
                recursive_iter(
                    self.base_path, self.base_path, SOUND_EFFECT_EXTENSIONS, self.logger
                ),
                key=lambda x: x.relative_path,
            )

        for file_item in await asyncio.to_thread(_walk):
            yield await self._get_or_parse_sound_effect(file_item)

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id."""
        # filesystem items are always stored in db so we can query the database
        db_album = await self.mass.music.albums.get_library_item_by_prov_id(
            prov_album_id, self.instance_id
        )
        if db_album is None:
            msg = f"Album not found: {prov_album_id}"
            raise MediaNotFoundError(msg)
        album_tracks = await self.mass.music.albums.get_library_album_tracks(db_album.item_id)
        return [
            track
            for track in album_tracks
            if any(x.provider_instance == self.instance_id for x in track.provider_mappings)
        ]

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks."""
        result: list[Track] = []
        if page > 0:
            # paging not (yet) supported
            return result
        if not await self.exists(prov_playlist_id):
            msg = f"Playlist path does not exist: {prov_playlist_id}"
            raise MediaNotFoundError(msg)

        file_item = await self.resolve(prov_playlist_id)
        # We are using the checksum of the playlist file here to invalidate the cache
        # when a change has been made to the playlist file (ie track addition/deletion)
        cache_checksum = file_item.checksum

        cache_key = f"get_playlist_tracks.{prov_playlist_id}"
        cached_data = await self.mass.cache.get(
            cache_key,
            provider=self.instance_id,
            checksum=cache_checksum,
            category=0,
            base_class=Track,
        )
        if cached_data is not None:
            return cached_data  # type: ignore[no-any-return]

        _, ext = prov_playlist_id.rsplit(".", 1)
        try:
            # get playlist file contents
            playlist_data_raw = await self._read_file(prov_playlist_id)
            encoding = await detect_charset(playlist_data_raw)
            playlist_data = playlist_data_raw.decode(encoding, errors="replace")

            if ext in ("m3u", "m3u8"):
                playlist_lines = parse_m3u(playlist_data)
            else:
                playlist_lines = parse_pls(playlist_data)

            for idx, playlist_line in enumerate(playlist_lines, 1):
                if "#EXT" in playlist_line.path:
                    continue
                if track := await self._parse_playlist_line(
                    playlist_line.path, os.path.dirname(prov_playlist_id)
                ):
                    track.position = idx
                    result.append(track)

        except Exception as err:
            self.logger.warning(
                "Error while parsing playlist %s: %s",
                prov_playlist_id,
                str(err),
                exc_info=err if self.logger.isEnabledFor(10) else None,
            )

        await self.mass.cache.set(
            key=cache_key,
            data=[track.to_dict() for track in result],
            expiration=3600 * 24 * 365,  # File timestamp checksum handles invalidation
            provider=self.instance_id,
            checksum=cache_checksum,
            category=0,
        )

        return result

    async def get_podcast_episodes(self, prov_podcast_id: str) -> AsyncGenerator[PodcastEpisode]:
        """Get podcast episodes for given podcast id."""
        folder_items = [item for item in await self._scandir(prov_podcast_id) if not item.is_dir]
        episode_files = [x for x in folder_items if x.ext in PODCAST_EPISODE_EXTENSIONS]
        # artwork and metadata.json count towards the signature too, because the parse embeds
        # them into every episode. Case-insensitive, matching _get_podcast_metadata's exists()
        signature_files = [
            x
            for x in folder_items
            if x.ext in PODCAST_EPISODE_EXTENSIONS
            or x.ext in IMAGE_EXTENSIONS
            or x.filename.lower() == "metadata.json"
        ]
        cache_key = f"podcast_episodes.{prov_podcast_id}"
        cache_checksum = get_folder_signature(signature_files)
        if (
            cached_episodes := await self.mass.cache.get(
                cache_key,
                provider=self.instance_id,
                category=CACHE_CATEGORY_PODCAST_EPISODES,
                checksum=cache_checksum,
                base_class=PodcastEpisode,
            )
        ) is not None:
            for episode in cached_episodes:
                yield episode
            return

        # these caches have no checksum of their own, so drop them before parsing or the new
        # entry gets the values the signature just invalidated. Refill once, or every parse
        # task below misses at the same time and repeats the same scandir and file read
        for stale_category in (CACHE_CATEGORY_FOLDER_IMAGES, CACHE_CATEGORY_PODCAST_METADATA):
            await self.mass.cache.delete(
                prov_podcast_id, category=stale_category, provider=self.instance_id
            )
        await self._get_local_images(prov_podcast_id)
        await self._get_podcast_metadata(prov_podcast_id)

        # collected by index so the listing keeps scandir order, not parse completion order
        parsed: list[PodcastEpisode | None] = [None] * len(episode_files)

        async def _process_podcast_episode(index: int, item: FileSystemItem) -> None:
            try:
                tags = await async_parse_tags(item.absolute_path, item.file_size)
                parsed[index] = await self._parse_podcast_episode(item, tags)
            except MusicAssistantError as err:
                self.logger.warning(
                    "Could not parse uri/file %s to podcast episode: %s",
                    item.relative_path,
                    str(err),
                )

        # reuse the per-sync worker limit: the slowest filesystems to parse are exactly the
        # ones that lower it
        async with TaskManager(self.mass, self._SYNC_CONCURRENCY) as tm:
            for index, item in enumerate(episode_files):
                await tm.create_task_with_limit(_process_podcast_episode(index, item))

        episodes = [episode for episode in parsed if episode is not None]
        # cache an incomplete listing briefly rather than not at all, so one unreadable file
        # cannot make every request re-parse the whole folder
        complete = len(episodes) == len(episode_files)
        await self.mass.cache.set(
            key=cache_key,
            data=[episode.to_dict() for episode in episodes],
            # a complete listing is invalidated by the folder signature instead
            expiration=3600 * 24 * 365 if complete else PARTIAL_LISTING_CACHE_EXPIRATION,
            provider=self.instance_id,
            category=CACHE_CATEGORY_PODCAST_EPISODES,
            checksum=cache_checksum,
        )

        for episode in episodes:
            yield episode

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add track(s) to playlist."""
        if not await self.exists(prov_playlist_id):
            msg = f"Playlist path does not exist: {prov_playlist_id}"
            raise MediaNotFoundError(msg)
        playlist_filename = self.get_absolute_path(prov_playlist_id)
        async with aiofiles.open(playlist_filename, encoding="utf-8") as _file:
            playlist_data = await _file.read()
        for file_path in prov_track_ids:
            track = await self.get_track(file_path)
            playlist_data += f"\n#EXTINF:{track.duration or 0},{track.name}\n{file_path}\n"

        # write playlist file (always in utf-8)
        async with aiofiles.open(playlist_filename, "w", encoding="utf-8") as _file:
            await _file.write(playlist_data)

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
        if not await self.exists(prov_playlist_id):
            msg = f"Playlist path does not exist: {prov_playlist_id}"
            raise MediaNotFoundError(msg)
        _, ext = prov_playlist_id.rsplit(".", 1)
        # get playlist file contents
        playlist_filename = self.get_absolute_path(prov_playlist_id)
        async with aiofiles.open(playlist_filename, encoding="utf-8") as _file:
            playlist_data = await _file.read()
        # get current contents first
        if ext in ("m3u", "m3u8"):
            playlist_items = parse_m3u(playlist_data)
        else:
            playlist_items = parse_pls(playlist_data)
        # remove items by index
        for i in sorted(positions_to_remove, reverse=True):
            # position = index + 1
            del playlist_items[i - 1]
        # build new playlist data
        new_playlist_data = "#EXTM3U\n"
        for item in playlist_items:
            new_playlist_data += f"\n#EXTINF:{item.length or 0},{item.title}\n{item.path}\n"
        async with aiofiles.open(playlist_filename, "w", encoding="utf-8") as _file:
            await _file.write(new_playlist_data)

    async def create_playlist(self, name: str, media_types: set[MediaType]) -> Playlist:
        """Create a new playlist on provider with given name."""
        # creating a new playlist on the filesystem is as easy
        # as creating a new (empty) file with the m3u extension...
        # filename = await self.resolve(f"{name}.m3u")
        filename = f"{name}.m3u"
        playlist_filename = self.get_absolute_path(filename)
        async with aiofiles.open(playlist_filename, "w", encoding="utf-8") as _file:
            await _file.write("#EXTM3U\n")
        return await self.get_playlist(filename)

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        try:
            if media_type == MediaType.AUDIOBOOK:
                return await self._get_stream_details_for_audiobook(item_id)
            if media_type == MediaType.PODCAST_EPISODE:
                return await self._get_stream_details_for_podcast_episode(item_id)
            if media_type == MediaType.SOUND_EFFECT:
                return await self._get_stream_details_for_sound_effect(item_id)
            return await self._get_stream_details_for_track(item_id)
        except FileNotFoundError:
            self.logger.warning(
                "File not found for media item %s",
                item_id,
            )
            msg = f"Media file not found: {item_id}"
            raise MediaNotFoundError(msg)

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """Return the custom audio stream for the provider item."""
        # only CUE-derived tracks use StreamType.CUSTOM in this provider
        async for chunk in self._cue.get_audio_stream(streamdetails, seek_position):
            yield chunk

    async def resolve_image(self, path: str) -> str | bytes:
        """
        Resolve an image from an image path.

        This either returns (a generator to get) raw bytes of the image or
        a string with an http(s) URL or local path that is accessible from the server.
        """
        # drop the cache-busting suffix appended by _versioned_image_path
        try:
            file_item = await self.resolve(path.split("?cs=", 1)[0])
        except FileNotFoundError as err:
            # the referenced image file was removed from disk; surface a typed
            # not-found so the image layer treats it as a missing image
            raise MediaNotFoundError(f"Image not found: {path}") from err
        if file_item.is_dir:
            # handing the path back would have the image layer run an ffmpeg
            # embedded-artwork extraction on the directory before giving up
            raise MediaNotFoundError(f"Image path is a directory: {path}")
        return file_item.absolute_path

    async def check_write_access(self) -> None:
        """Perform check if we have write access."""
        # verify write access to determine we have playlist create/edit support
        # overwrite with provider specific implementation if needed
        temp_file_name = self.get_absolute_path(f"{shortuuid.random(8)}.txt")
        try:
            async with aiofiles.open(temp_file_name, "w") as _file:
                await _file.write("test")
            await asyncio.to_thread(os.remove, temp_file_name)
            self.write_access = True
        except Exception as err:
            self.logger.debug("Write access disabled: %s", str(err))

    async def resolve(self, file_path: str) -> FileSystemItem:
        """Resolve (absolute or relative) path to FileSystemItem."""
        absolute_path = self.get_absolute_path(file_path)

        def _create_item() -> FileSystemItem:
            if Path(absolute_path).is_dir():
                return FileSystemItem(
                    filename=Path(file_path).name,
                    relative_path=get_relative_path(self.base_path, file_path),
                    absolute_path=absolute_path,
                    is_dir=True,
                )
            stat_info = Path(absolute_path).stat(follow_symlinks=False)
            return FileSystemItem(
                filename=Path(file_path).name,
                relative_path=get_relative_path(self.base_path, file_path),
                absolute_path=absolute_path,
                is_dir=False,
                checksum=str(int(stat_info.st_mtime)),
                file_size=stat_info.st_size,
            )

        return await asyncio.to_thread(_create_item)

    async def exists(self, file_path: str) -> bool:
        """Return bool is this FileSystem musicprovider has given file/dir."""
        if not file_path:
            return False
        try:
            abs_path = self.get_absolute_path(file_path)
        except MediaNotFoundError:
            # a path that escapes the base directory simply does not exist here
            return False
        return bool(await exists(abs_path))

    def get_absolute_path(self, file_path: str) -> str:
        """Return absolute path for given file path."""
        return get_absolute_path(self.base_path, file_path)

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
    ) -> None:
        """
        Walk every supported file under the provider root and populate the sync buckets.

        Override in subclasses that cannot use a local ``os.scandir`` walk.
        Implementations must route each discovered file through
        :meth:`_classify_scan_item`, report every unreadable directory to
        ``scan_errors`` and stop the walk once it reports ``aborted``.

        :param file_checksums: Previously stored checksum per provider item id.
        :param cue_file_checksums: Previously stored track checksums keyed by CUE relative_path.
        :param cur_filenames: Receives the ids/paths present in this scan.
        :param items_to_process: Receives changed or new items to process.
        :param unchanged_cue_items: Receives CUE sheets whose checksum matches.
        :param cue_stems: Receives absolute paths (minus extension) of CUE sheets.
        :param scan_errors: Receives the errors raised while walking the tree.
        """
        ignore_album_playlists = self.media_content_type == "music" and bool(
            self.config.get_value(CONF_ENTRY_IGNORE_ALBUM_PLAYLISTS.key)
        )

        def _walk() -> None:
            for scanned, item in enumerate(
                recursive_iter(
                    self.base_path,
                    self.base_path,
                    SUPPORTED_EXTENSIONS,
                    self.logger,
                    scan_errors=scan_errors,
                ),
                start=1,
            ):
                if scanned % 500 == 0:
                    update_current_task_progress_text(f"Scanning files: {scanned} found")
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

        await asyncio.to_thread(_walk)

    def _classify_scan_item(
        self,
        item: FileSystemItem,
        *,
        file_checksums: dict[str, str],
        cue_file_checksums: dict[str, set[str]],
        cur_filenames: set[str],
        items_to_process: list[tuple[FileSystemItem, str | None]],
        unchanged_cue_items: list[FileSystemItem],
        cue_stems: set[str],
        ignore_album_playlists: bool,
    ) -> None:
        """
        Route a single scanned file into the correct sync bucket.

        :param item: The file to classify.
        :param file_checksums: Previously stored checksum per provider item id.
        :param cue_file_checksums: Previously stored track checksums keyed by CUE relative_path.
        :param cur_filenames: Receives the ids/paths present in this scan.
        :param items_to_process: Receives changed or new items to process.
        :param unchanged_cue_items: Receives CUE sheets whose checksum matches.
        :param cue_stems: Receives absolute paths (minus extension) of CUE sheets.
        :param ignore_album_playlists: When True, skip playlists nested inside
            album directories.
        """
        # a file this provider never imports gets no mapping, so it would flag as
        # changed on every sync; it is still on disk, so record it as present
        if not self._is_imported_file(item):
            cur_filenames.add(item.relative_path)
            return
        # skip playlists in album directories if configured
        if (
            item.ext in PLAYLIST_EXTENSIONS
            and ignore_album_playlists
            and len(item.relative_path.split("/")) > 2
        ):
            return
        is_cue = item.ext in CUE_EXTENSIONS and self.media_content_type == "music"
        item_checksum = item.checksum
        if is_cue:
            cue_stems.add(item.absolute_path.rsplit(".", 1)[0])
            item_checksum = cue_metadata_checksum(item.checksum)
            prev_checksums = cue_file_checksums.get(item.relative_path, set())
            prev_checksum = min(prev_checksums, default=None)
            checksum_matches = prev_checksums == {item_checksum}
        else:
            prev_checksum = file_checksums.get(item.relative_path)
            checksum_matches = item_checksum == prev_checksum
        if checksum_matches:
            # unchanged, just record it as still present
            cur_filenames.add(item.relative_path)
            if is_cue:
                unchanged_cue_items.append(item)
        else:
            items_to_process.append((item, prev_checksum))

    def _is_imported_file(self, item: FileSystemItem) -> bool:
        """Return True when this provider imports the given file into the library."""
        if self.media_content_type == "music":
            if item.ext in CUE_EXTENSIONS:
                return True
            if item.ext in TRACK_EXTENSIONS:
                return self._sync_tracks
            if item.ext in PLAYLIST_EXTENSIONS:
                return self._sync_playlists
            return False
        if self.media_content_type == "audiobooks":
            return item.ext in AUDIOBOOK_EXTENSIONS
        if self.media_content_type == "podcasts":
            return item.ext in PODCAST_EPISODE_EXTENSIONS
        return False

    def _set_available(self, available: bool) -> None:
        """Update the provider availability and notify listeners on change."""
        if self.available == available:
            return
        self.available = available
        if available:
            self._cancel_availability_probe()
        else:
            self._schedule_availability_probe()
        self.mass.signal_event(EventType.PROVIDERS_UPDATED, data=self.mass.get_providers())

    async def _is_reachable(self) -> bool:
        """Return whether the storage backing this provider can be read."""
        return bool(await isdir(self.base_path))

    @property
    def _availability_probe_id(self) -> str:
        """Return the timer id of this provider's reachability checks."""
        return f"filesystem_availability_probe_{self.instance_id}"

    def _schedule_availability_probe(self) -> None:
        """Arm the next reachability check."""
        self.mass.call_later(
            AVAILABILITY_PROBE_INTERVAL,
            self._probe_availability,
            task_id=self._availability_probe_id,
        )

    def _cancel_availability_probe(self) -> None:
        """Stop checking for the storage coming back."""
        self.mass.cancel_timer(self._availability_probe_id)

    async def _probe_availability(self) -> None:
        """Mark the provider available again once its storage can be read."""
        try:
            reachable = await self._is_reachable()
        except MusicAssistantError as err:
            # storage that is simply still gone, which is what this loop waits for
            self.logger.debug("%s is still unreachable: %s", self.name, err)
            reachable = False
        except Exception:
            # an unexpected failure must not end the loop, since it is what brings the
            # provider back, but it is a defect rather than an outage so it is logged loudly
            self.logger.exception("Reachability check for %s failed", self.name)
            reachable = False
        if self.unloading:
            # the provider was torn down while this check was running; re-arming here
            # would leave a timer firing against an instance nothing owns anymore
            return
        if reachable:
            self.logger.info("%s is reachable again", self.name)
            self._set_available(True)
            return
        self._schedule_availability_probe()

    async def _process_item_async(
        self,
        item: FileSystemItem,
        prev_checksum: str | None,
        cur_filenames: set[str] | None = None,
        cue_stems: set[str] | None = None,
        prev_filenames: set[str] | None = None,
    ) -> bool:
        """
        Process a single item asynchronously.

        :param item: The filesystem item to process.
        :param prev_checksum: Previous checksum from the database, or None for new items.
        :param cur_filenames: Set of current filenames being tracked (for CUE track IDs).
        :param cue_stems: Absolute paths (without extension) of CUE sheets in this scan,
            used to detect companion-CUE audio files without a filesystem stat.
        :param prev_filenames: The ids/paths the previous scan found, used to keep the
            ids of a CUE sheet that fails to parse.
        """
        try:
            self.logger.log(VERBOSE_LOG_LEVEL, "Processing: %s", item.relative_path)

            if prev_checksum is not None:
                # the file changed on disk: drop cached artwork derived from it
                # (thumbnails, source bytes, palette) so re-read embedded art is
                # served fresh, for both reference forms of the image path
                await self.mass.metadata.invalidate_image_cache(
                    self.instance_id, item.relative_path
                )
                await self.mass.metadata.invalidate_image_cache(
                    self.instance_id, self._versioned_image_path(item.relative_path, prev_checksum)
                )

            if item.ext in CUE_EXTENSIONS and self.media_content_type == "music":
                tracks = await self._cue.parse_tracks(item)
                for track in tracks:
                    track.favorite = False
                    await self.mass.music.tracks.add_item_to_library(
                        track, overwrite_existing=prev_checksum is not None
                    )
                    if cur_filenames is not None:
                        cur_filenames.add(track.item_id)
                return True

            if item.ext in TRACK_EXTENSIONS and self.media_content_type == "music":
                if not self._sync_tracks:
                    return False
                # skip audio files that have a companion CUE sheet
                if cue_stems is not None and item.absolute_path.rsplit(".", 1)[0] in cue_stems:
                    return False
                tags = await async_parse_tags(item.absolute_path, item.file_size)
                track = await self._parse_track(item, tags)
                track.favorite = False  # TODO: implement favorite status based on rating ?
                await self.mass.music.tracks.add_item_to_library(
                    track, overwrite_existing=prev_checksum is not None
                )
                return True

            if item.ext in AUDIOBOOK_EXTENSIONS and self.media_content_type == "audiobooks":
                tags = await async_parse_tags(item.absolute_path, item.file_size)
                try:
                    audiobook = await self._parse_audiobook(item, tags)
                except IsChapterFile:
                    return True
                await self.mass.music.audiobooks.add_item_to_library(
                    audiobook, overwrite_existing=prev_checksum is not None
                )
                return True

            if item.ext in PODCAST_EPISODE_EXTENSIONS and self.media_content_type == "podcasts":
                tags = await async_parse_tags(item.absolute_path, item.file_size)
                episode = await self._parse_podcast_episode(item, tags)
                assert isinstance(episode.podcast, Podcast)
                await self.mass.music.podcasts.add_item_to_library(
                    episode.podcast, overwrite_existing=prev_checksum is not None
                )
                return True

            if item.ext in PLAYLIST_EXTENSIONS and self.media_content_type == "music":
                if not self._sync_playlists:
                    return False
                playlist = await self.get_playlist(item.relative_path)
                await self.mass.music.playlists.add_item_to_library(
                    playlist, overwrite_existing=prev_checksum is not None
                )
                return True

        except Exception as err:
            # we don't want the whole sync to crash on one file so we catch all exceptions here
            self.logger.error(
                "Error processing %s - %s",
                item.relative_path,
                str(err),
                exc_info=err if self.logger.isEnabledFor(logging.DEBUG) else None,
            )
            report_current_task_failure(f"Failed to process {item.relative_path}: {err}")
            # the file is still on the storage, so keep it in the scan result:
            # leaving it out makes the deletion step treat it as removed
            self._keep_failed_item(item, cur_filenames, prev_filenames)
        return False

    def _keep_failed_item(
        self,
        item: FileSystemItem,
        cur_filenames: set[str] | None,
        prev_filenames: set[str] | None,
    ) -> None:
        """
        Keep an item that could not be processed in the scan result.

        :param item: The item that failed to process.
        :param cur_filenames: Receives the ids/paths present in this scan.
        :param prev_filenames: The ids/paths the previous scan found.
        """
        if cur_filenames is None:
            return
        cur_filenames.add(item.relative_path)
        if (
            item.ext not in CUE_EXTENSIONS
            or self.media_content_type != "music"
            or not prev_filenames
        ):
            return
        # a CUE sheet stands in for one id per track it describes and those cannot be
        # rebuilt without parsing it, so carry over the ids of the previous scan
        cur_filenames.update(
            item_id
            for item_id in prev_filenames
            if (parsed := parse_cue_track_id(item_id)) and parsed[0] == item.relative_path
        )

    async def _process_orphaned_albums_and_artists(self) -> None:
        """Process deletion of orphaned albums and artists."""
        assert self.mass.music.database
        # Remove albums without any tracks
        query = (
            f"SELECT item_id FROM {DB_TABLE_ALBUMS} "
            f"WHERE item_id not in ( SELECT album_id from {DB_TABLE_ALBUM_TRACKS}) "
            f"AND item_id in ( SELECT item_id from {DB_TABLE_PROVIDER_MAPPINGS} "
            f"WHERE provider_instance = '{self.instance_id}' and media_type = 'album' )"
        )
        for db_row in await self.mass.music.database.get_rows_from_query(
            query,
            limit=100000,
        ):
            await self.mass.music.albums.remove_item_from_library(db_row["item_id"])

        # Remove artists without any tracks or albums
        query = (
            f"SELECT item_id FROM {DB_TABLE_ARTISTS} "
            f"WHERE item_id not in "
            f"( select artist_id from {DB_TABLE_TRACK_ARTISTS} "
            f"UNION SELECT artist_id from {DB_TABLE_ALBUM_ARTISTS} ) "
            f"AND item_id in ( SELECT item_id from {DB_TABLE_PROVIDER_MAPPINGS} "
            f"WHERE provider_instance = '{self.instance_id}' and media_type = 'artist' )"
        )
        for db_row in await self.mass.music.database.get_rows_from_query(
            query,
            limit=100000,
        ):
            await self.mass.music.artists.remove_item_from_library(db_row["item_id"])

    async def _process_deletions(self, deleted_files: set[str]) -> None:
        """Process all deletions."""
        # process deleted tracks/playlists
        album_ids = set()
        artist_ids = set()
        for file_path in deleted_files:
            if parse_cue_track_id(file_path) is not None and self.media_content_type == "music":
                controller = self.mass.music.get_controller(MediaType.TRACK)
            elif "." not in file_path:
                continue
            else:
                _, ext = file_path.rsplit(".", 1)
                if ext in PODCAST_EPISODE_EXTENSIONS and self.media_content_type == "podcasts":
                    controller = self.mass.music.get_controller(MediaType.PODCAST_EPISODE)
                elif ext in AUDIOBOOK_EXTENSIONS and self.media_content_type == "audiobooks":
                    controller = self.mass.music.get_controller(MediaType.AUDIOBOOK)
                elif ext in PLAYLIST_EXTENSIONS and self.media_content_type == "music":
                    controller = self.mass.music.get_controller(MediaType.PLAYLIST)
                elif ext in TRACK_EXTENSIONS and self.media_content_type == "music":
                    controller = self.mass.music.get_controller(MediaType.TRACK)
                else:
                    # unsupported file extension?
                    continue

            if library_item := await controller.get_library_item_by_prov_id(
                file_path, self.instance_id
            ):
                if is_track(library_item):
                    if library_item.album:
                        album_ids.add(library_item.album.item_id)
                        # need to fetch the library album to resolve the itemmapping
                        db_album = await self.mass.music.albums.get_library_item(
                            library_item.album.item_id
                        )
                        for artist in db_album.artists:
                            artist_ids.add(artist.item_id)
                    for artist in library_item.artists:
                        artist_ids.add(artist.item_id)
                await controller.remove_item_from_library(library_item.item_id)
        # check if any albums need to be cleaned up
        for album_id in album_ids:
            if not await self.mass.music.albums.tracks(album_id, "library"):
                await self.mass.music.albums.remove_item_from_library(album_id)
        # check if any artists need to be cleaned up
        for artist_id in artist_ids:
            artist_albums = await self.mass.music.artists.albums(artist_id, "library")
            artist_tracks = await self.mass.music.artists.tracks(artist_id, "library")
            if not (artist_albums or artist_tracks):
                await self.mass.music.artists.remove_item_from_library(artist_id)

    async def _get_playlist_local_image(self, file_item: FileSystemItem) -> MediaItemImage | None:
        """Return a local image alongside the playlist file (matching basename) if any."""
        cache_key = f"playlist_image.{file_item.relative_path}"
        cached = await self.cache.get(
            key=cache_key,
            provider=self.instance_id,
            category=CACHE_CATEGORY_FOLDER_IMAGES,
            base_class=MediaItemImage,
        )
        if cached is not None:
            return cached[0] if cached else None
        try:
            folder_files = await self._scandir(file_item.relative_parent_path)
        except OSError, MusicAssistantError:
            return None
        target = file_item.name.lower()
        result: MediaItemImage | None = None
        for item in folder_files:
            if item.is_dir or not item.ext:
                continue
            if item.ext.lower() not in IMAGE_EXTENSIONS:
                continue
            if item.name.lower() != target:
                continue
            result = MediaItemImage(
                type=ImageType.THUMB,
                path=item.relative_path,
                provider=self.instance_id,
                remotely_accessible=False,
            )
            break
        await self.cache.set(
            key=cache_key,
            data=[result.to_dict()] if result is not None else [],
            provider=self.instance_id,
            category=CACHE_CATEGORY_FOLDER_IMAGES,
            expiration=120,
        )
        return result

    async def _parse_playlist_line(self, line: str, playlist_path: str) -> Track | None:
        """Try to parse a track from a playlist line."""
        try:
            line = line.replace("file://", "").strip()
            # try to resolve the filename (both normal and url decoded):
            # - relative to the playlist folder (normpath resolves parent .. references)
            # - as-is: an absolute path, or relative to our base path
            # candidates stay relative so subclasses with virtual paths (cloud,
            # webdav) resolve them too, instead of leaking the server CWD
            for _line in (line, urllib.parse.unquote(line)):
                if playlist_path:
                    normalized = posixpath.normpath(f"{playlist_path}/{_line}")
                    with contextlib.suppress(FileNotFoundError, MediaNotFoundError):
                        file_item = await self.resolve(normalized)
                        tags = await async_parse_tags(file_item.absolute_path, file_item.file_size)
                        return await self._parse_track(file_item, tags)
                with contextlib.suppress(FileNotFoundError, MediaNotFoundError):
                    file_item = await self.resolve(_line)
                    tags = await async_parse_tags(file_item.absolute_path, file_item.file_size)
                    return await self._parse_track(file_item, tags)
            # all attempts failed
            raise MediaNotFoundError("Invalid path/uri")

        except MusicAssistantError as err:
            self.logger.warning("Could not parse %s to track: %s", line, str(err))

        return None

    @staticmethod
    def _versioned_image_path(relative_path: str, checksum: str | None) -> str:
        """Append the file checksum so the image cache busts when the file is replaced."""
        if checksum:
            return f"{relative_path}?cs={checksum}"
        return relative_path

    @staticmethod
    def _codec_type_from_tags(tags: AudioTags) -> ContentType:
        """
        Return the actual audio codec detected by ffprobe, if any.

        WAV/AIFF containers can embed a non-PCM bitstream e.g. DTS 5.1, intended
        for SPDIF/HDMI passthrough) while declaring PCM in the fmt chunk. Forcing
        ffmpeg to decode with the real codec, learned from the library scan probe,
        which uses a much larger read window than the streaming ffmpeg pass, avoids
        relying on ffmpeg's own content-sniffing heuristic at stream time, which can
        miss the embedded bitstream if it's located far into a large multi-track file.
        Falls back to UNKNOWN if the probe didn't report anything usable.
        """
        if tags.raw and (streams := tags.raw.get("streams")):
            if codec_name := streams[0].get("codec_name"):
                return ContentType.try_parse(codec_name)
        return ContentType.UNKNOWN

    async def _parse_track(
        self, file_item: FileSystemItem, tags: AudioTags, full_album_metadata: bool = False
    ) -> Track:
        """Parse full track details from file tags."""
        # ruff: noqa: PLR0915
        name, version = parse_title_and_version(tags.title, tags.version)
        track = Track(
            item_id=file_item.relative_path,
            provider=self.instance_id,
            name=name,
            sort_name=tags.title_sort,
            version=version,
            provider_mappings={
                ProviderMapping(
                    item_id=file_item.relative_path,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(
                        content_type=ContentType.try_parse(file_item.ext or tags.format),
                        codec_type=self._codec_type_from_tags(tags),
                        sample_rate=tags.sample_rate,
                        bit_depth=tags.bits_per_sample,
                        channels=tags.channels,
                        bit_rate=tags.bit_rate,
                    ),
                    details=file_item.checksum,
                    in_library=True,
                )
            },
            disc_number=tags.disc or 0,
            track_number=tags.track or 0,
            date_added=(
                datetime.fromtimestamp(file_item.created_at, tz=UTC)
                if file_item.created_at
                else None
            ),
        )

        if isrc_tags := tags.isrc:
            for isrsc in isrc_tags:
                track.external_ids.add((ExternalID.ISRC, isrsc))

        if acoustid := tags.get("acoustid"):
            track.external_ids.add((ExternalID.ACOUSTID, acoustid))

        # album
        album = track.album = (
            await self._parse_album(
                track_path=file_item.relative_path,
                track_tags=tags,
                track_created_at=file_item.created_at,
            )
            if tags.album
            else None
        )

        # track artist(s)
        resolved_track_artists = await self._resolve_artists_with_mbids(
            tags.artists,
            tags.musicbrainz_artistids,
            tags.artist_sort_names,
            log_label="ARTISTS tag",
        )
        for name, mbid, sort_name in resolved_track_artists:
            # prefer the existing album artist object when it's the same artist
            if album_artist_match := self._match_album_artist(album, name, mbid):
                track.artists.append(album_artist_match)
                continue
            artist = await self._parse_artist(name, sort_name=sort_name, mbid=mbid)
            track.artists.append(artist)

        # handle embedded cover image
        if tags.has_cover_image:
            # we do not actually embed the image in the metadata because that would consume too
            # much space and bandwidth. Instead we set the filename as value so the image can
            # be retrieved later in realtime.
            track.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=file_item.relative_path,
                        provider=self.instance_id,
                        remotely_accessible=False,
                    )
                ]
            )

        # copy (embedded) album image from track (if the album itself doesn't have an image)
        if album and not album.image and track.image:
            album.metadata.images = UniqueList([track.image])

        # parse other info
        track.duration = int(tags.duration or 0)
        track.metadata.genres = set(tags.genres)
        if tags.disc:
            track.disc_number = tags.disc
        if tags.track:
            track.track_number = tags.track
        track.metadata.copyright = tags.get("copyright")
        track.metadata.lyrics = tags.lyrics
        track.metadata.grouping = tags.get("grouping")
        track.metadata.description = tags.get("comment")
        explicit_tag = tags.get("itunesadvisory")
        if explicit_tag is not None:
            track.metadata.explicit = explicit_tag == "1"
        if recording_mbid := clean_mbid(tags.musicbrainz_recordingid, tags.filename):
            track.mbid = recording_mbid

        # handle (optional) loudness measurement tag(s)
        if tags.track_loudness is not None:
            self.mass.create_task(
                self.mass.streams.audio_analysis.set_track_loudness(
                    track.item_id,
                    self.instance_id,
                    tags.track_loudness,
                    tags.track_album_loudness,
                )
            )

        # possible lrclib metadata
        # synced lyrics are saved as "filename.lrc" by lrcget alongside
        # the actual file location - just change the file extension
        assert file_item.ext is not None  # for type checking
        lrc_path = f"{file_item.relative_path.removesuffix(file_item.ext)}lrc"
        if await self.exists(lrc_path):
            try:
                raw = await self._read_file(lrc_path)
                track.metadata.lrc_lyrics = raw.decode("utf-8")
            except Exception as err:
                self.logger.warning(
                    "Failed to read lyrics file %s: %s",
                    lrc_path,
                    str(err),
                )
        elif syn_lyrics := tags.synchronized_lyrics:
            track.metadata.lrc_lyrics = lyrics.convert_to_lrc_lyrics(syn_lyrics)

        return track

    async def _resolve_artists_with_mbids(
        self,
        parsed_names: tuple[str, ...],
        mbids: tuple[str, ...],
        sort_names: tuple[str, ...],
        log_label: str,
    ) -> list[tuple[str, str | None, str | None]]:
        """
        Return ``(name, mbid, sort_name)`` triples for a track's or album's artists.

        When the parsed name count and the MBID count disagree, canonical names
        are looked up from MusicBrainz; otherwise the tag-parsed names are used.

        :param parsed_names: Tag-parsed artist names.
        :param mbids: MusicBrainz artist IDs from the tag.
        :param sort_names: Sort names from the corresponding *sort tag.
        :param log_label: Tag name used in warning messages (e.g. "ARTISTS tag").
        """

        def _sort_name(index: int) -> str | None:
            return sort_names[index] if index < len(sort_names) else None

        def _from_tags() -> list[tuple[str, str | None, str | None]]:
            return [
                (
                    name,
                    mbids[i] if i < len(mbids) else None,
                    _sort_name(i),
                )
                for i, name in enumerate(parsed_names)
            ]

        if not mbids or len(parsed_names) == len(mbids):
            return _from_tags()

        mb_provider = cast("MusicbrainzProvider | None", self.mass.get_provider("musicbrainz"))
        if mb_provider is None:
            self.logger.warning(
                "%s count (%d) doesn't match MBID count (%d) and MusicBrainz "
                "provider is not loaded; using tag-parsed names: %s",
                log_label,
                len(parsed_names),
                len(mbids),
                parsed_names,
            )
            return _from_tags()

        mb_results = await mb_provider.resolve_artists_from_mbids(mbids)
        # counts disagree, so positional fallback to a tag name is unreliable;
        # drop any MBID whose lookup failed (already logged per-MBID)
        resolved: list[tuple[str, str | None, str | None]] = [
            mb_result for mb_result in mb_results if mb_result is not None
        ]
        if not resolved:
            self.logger.warning(
                "%s count (%d) didn't match MBID count (%d) and every MusicBrainz "
                "lookup failed; falling back to tag-parsed names: %s",
                log_label,
                len(parsed_names),
                len(mbids),
                parsed_names,
            )
            return _from_tags()
        self.logger.info(
            "%s count (%d) didn't match MBID count (%d); resolved canonical names "
            "via MusicBrainz: %s",
            log_label,
            len(parsed_names),
            len(mbids),
            [r[0] for r in resolved],
        )
        return resolved

    def _match_album_artist(
        self, album: Album | None, name: str, mbid: str | None
    ) -> Artist | ItemMapping | None:
        """
        Return an existing album artist representing the same artist, if any.

        Matches on MusicBrainz ID when available (names may differ when only one
        side was resolved against MusicBrainz), otherwise on exact name.

        :param album: The track's album, if known.
        :param name: Resolved track-artist name.
        :param mbid: Resolved track-artist MusicBrainz ID, if any.
        """
        if not album:
            return None
        return next(
            (x for x in album.artists if (mbid and x.mbid == mbid) or x.name == name),
            None,
        )

    async def _parse_artist(
        self,
        name: str,
        album_dir: str | None = None,
        sort_name: str | None = None,
        mbid: str | None = None,
        artist_path: str | None = None,
    ) -> Artist:
        """Parse full (album) Artist."""
        if not artist_path:
            # we need to hunt for the artist (metadata) path on disk
            # this can either be relative to the album path or at root level
            # check if we have an artist folder for this artist at root level
            safe_artist_name = create_safe_string(name, lowercase=False, replace_space=False)
            if await self.exists(name):
                artist_path = name
            elif await self.exists(safe_artist_name):
                artist_path = safe_artist_name
            elif album_dir and (foldermatch := get_artist_dir(name, album_dir=album_dir)):
                # try to find (album)artist folder based on album path
                artist_path = foldermatch
            else:
                # check if we have an existing item to retrieve the artist path
                async for item in self.mass.music.artists.iter_library_items(
                    search=name, provider=self.instance_id
                ):
                    if not compare_strings(name, item.name):
                        continue
                    for prov_mapping in item.provider_mappings:
                        if prov_mapping.provider_instance != self.instance_id:
                            continue
                        if prov_mapping.url:
                            artist_path = prov_mapping.url
                            break
                    if artist_path:
                        break

        # prefer (short lived) cache for a bit more speed
        if artist_path and (
            cache := await self.cache.get(
                key=artist_path,
                provider=self.instance_id,
                category=CACHE_CATEGORY_ARTIST_INFO,
                base_class=Artist,
            )
        ):
            return cache  # type: ignore[no-any-return]

        prov_artist_id = artist_path or name
        artist = Artist(
            item_id=prov_artist_id,
            provider=self.instance_id,
            name=name,
            sort_name=sort_name,
            provider_mappings={
                ProviderMapping(
                    item_id=prov_artist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=artist_path,
                    in_library=True,
                )
            },
        )
        if mbid := clean_mbid(mbid, f"tags of artist {name}"):
            artist.mbid = mbid
        if not artist_path or not await self.exists(artist_path):
            return artist

        # grab additional metadata within the Artist's folder
        nfo_file = os.path.join(artist_path, "artist.nfo")
        if await self.exists(nfo_file):
            # found NFO file with metadata
            # https://kodi.wiki/view/NFO_files/Artists
            try:
                data = (await self._read_file(nfo_file)).decode("utf-8")
                info = await asyncio.to_thread(xmltodict.parse, data)
                info = info["artist"]
                artist.name = info.get("title", info.get("name", name))
                if sort_name := info.get("sortname"):
                    artist.sort_name = sort_name
                if mbid := clean_mbid(info.get("musicbrainzartistid"), nfo_file):
                    artist.mbid = mbid
                if description := info.get("biography"):
                    artist.metadata.description = description
                if genre := info.get("genre"):
                    artist.metadata.genres = set(split_items(genre))
            except (ExpatError, KeyError) as err:
                self.logger.warning(
                    "Failed to parse artist NFO file %s: %s",
                    nfo_file,
                    str(err),
                )
        # find local images
        if images := await self._get_local_images(artist_path, extra_thumb_names=("artist",)):
            artist.metadata.images = UniqueList(images)

        await self.cache.set(
            key=artist_path,
            data=artist.to_dict(),
            provider=self.instance_id,
            category=CACHE_CATEGORY_ARTIST_INFO,
            expiration=120,
        )

        return artist

    async def _parse_audiobook(self, file_item: FileSystemItem, tags: AudioTags) -> Audiobook:
        """
        Parse Audiobook details from file tags.

        Audiobooks can be single files with embedded chapters or multiple files per folder.
        Only the first file (by track number or alphabetically) is processed as the audiobook.
        """
        # Skip files that aren't the first chapter.
        # A file carrying its own embedded chapter markers is a standalone audiobook,
        # so it should never be treated as a chapter file of another book.
        track_tag = tags.tags.get("track")
        if track_tag:
            track_num = try_parse_int(str(track_tag).split("/")[0], None)
            if track_num and track_num > 1 and not tags.chapters:
                raise IsChapterFile
        elif not tags.chapters:
            # No track tag and no embedded chapters -
            # assume part of a multi-file audiobook, only process the first file alphabetically
            items = await self._scandir(file_item.relative_parent_path)
            # Sort by filename for alphabetical ordering
            items.sort(key=lambda x: x.filename.lower())
            for item in items:
                if item.is_dir or item.ext not in AUDIOBOOK_EXTENSIONS:
                    continue
                if item.absolute_path != file_item.absolute_path:
                    raise IsChapterFile
                break

        # For multi-file audiobooks, album tag is the book name, title is the chapter name
        if tags.album:
            book_name = tags.album
            sort_name = tags.album_sort
        elif (title := tags.tags.get("title")) and tags.track is None:
            book_name = title
            sort_name = tags.title_sort
        else:
            # file(s) without tags, use foldername
            book_name = file_item.parent_name
            sort_name = None

        # collect all chapters
        total_duration, chapters = await self._get_chapters_for_audiobook(file_item, tags)

        audio_book = Audiobook(
            item_id=file_item.relative_path,
            provider=self.instance_id,
            name=book_name,
            sort_name=sort_name,
            version=tags.version,
            duration=total_duration or int(tags.duration or 0),
            provider_mappings={
                ProviderMapping(
                    item_id=file_item.relative_path,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(
                        content_type=ContentType.try_parse(file_item.ext or tags.format),
                        codec_type=self._codec_type_from_tags(tags),
                        sample_rate=tags.sample_rate,
                        bit_depth=tags.bits_per_sample,
                        channels=tags.channels,
                        bit_rate=tags.bit_rate,
                    ),
                    details=file_item.checksum,
                    in_library=True,
                )
            },
        )
        audio_book.metadata.chapters = chapters

        # handle embedded cover image
        if tags.has_cover_image:
            # we do not actually embed the image in the metadata because that would consume too
            # much space and bandwidth. Instead we set the filename as value so the image can
            # be retrieved later in realtime.
            audio_book.metadata.add_image(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=self._versioned_image_path(file_item.relative_path, file_item.checksum),
                    provider=self.instance_id,
                    remotely_accessible=False,
                )
            )

        # parse other info
        audio_book.authors.set(tags.writers or tags.album_artists or tags.artists)
        audio_book.metadata.genres = (
            set(tags.genres) if tags.genres else {DEFAULT_AUDIOBOOK_PODCAST_GENRE}
        )
        audio_book.metadata.copyright = tags.get("copyright")
        audio_book.metadata.lyrics = tags.lyrics
        audio_book.metadata.description = tags.get("comment")
        explicit_tag = tags.get("itunesadvisory")
        if explicit_tag is not None:
            audio_book.metadata.explicit = explicit_tag == "1"
        if recording_mbid := clean_mbid(tags.musicbrainz_recordingid, tags.filename):
            audio_book.mbid = recording_mbid

        # try to fetch additional metadata from the folder
        if not audio_book.image or not audio_book.metadata.description:
            # try to get an image by traversing files in the same folder
            for _item in await self._scandir(file_item.relative_parent_path):
                if "." not in _item.relative_path or _item.is_dir:
                    continue
                if _item.ext in IMAGE_EXTENSIONS and not audio_book.image:
                    audio_book.metadata.add_image(
                        MediaItemImage(
                            type=ImageType.THUMB,
                            path=self._versioned_image_path(_item.relative_path, _item.checksum),
                            provider=self.instance_id,
                            remotely_accessible=False,
                        )
                    )
                if _item.ext == "txt" and not audio_book.metadata.description:
                    # try to parse a description from a text file
                    try:
                        raw = await self._read_file(_item.relative_path)
                        audio_book.metadata.description = raw.decode("utf-8")
                    except Exception as err:
                        self.logger.warning(
                            "Could not read description from file %s: %s",
                            _item.relative_path,
                            str(err),
                        )

        # handle (optional) loudness measurement tag(s)
        if tags.track_loudness is not None:
            self.mass.create_task(
                self.mass.streams.audio_analysis.set_track_loudness(
                    audio_book.item_id,
                    self.instance_id,
                    tags.track_loudness,
                    tags.track_album_loudness,
                    media_type=MediaType.AUDIOBOOK,
                )
            )
        return audio_book

    async def _parse_podcast_episode(
        self, file_item: FileSystemItem, tags: AudioTags
    ) -> PodcastEpisode:
        """Parse full PodcastEpisode details from file tags."""
        # ruff: noqa: PLR0915
        podcast_name = tags.album or file_item.parent_name
        podcast_path = file_item.relative_parent_path
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
                        codec_type=self._codec_type_from_tags(tags),
                        sample_rate=tags.sample_rate,
                        bit_depth=tags.bits_per_sample,
                        channels=tags.channels,
                        bit_rate=tags.bit_rate,
                    ),
                    details=file_item.checksum,
                    in_library=True,
                )
            },
            position=tags.track or 0,
            duration=try_parse_int(tags.duration) or 0,
            podcast=Podcast(
                item_id=podcast_path,
                provider=self.instance_id,
                name=podcast_name,
                sort_name=tags.album_sort,
                publisher=tags.tags.get("publisher"),
                provider_mappings={
                    ProviderMapping(
                        item_id=podcast_path,
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                        in_library=True,
                    )
                },
            ),
        )
        # handle embedded cover image
        if tags.has_cover_image:
            # we do not actually embed the image in the metadata because that would consume too
            # much space and bandwidth. Instead we set the filename as value so the image can
            # be retrieved later in realtime.
            episode.metadata.add_image(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=file_item.relative_path,
                    provider=self.instance_id,
                    remotely_accessible=False,
                )
            )
        # parse other info
        episode.metadata.genres = (
            set(tags.genres) if tags.genres else {DEFAULT_AUDIOBOOK_PODCAST_GENRE}
        )
        episode.metadata.copyright = tags.get("copyright")
        episode.metadata.lyrics = tags.lyrics
        episode.metadata.description = tags.get("comment")
        explicit_tag = tags.get("itunesadvisory")
        if explicit_tag is not None:
            episode.metadata.explicit = explicit_tag == "1"

        # handle (optional) chapters
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

        # try to fetch additional Podcast metadata from the folder
        assert isinstance(episode.podcast, Podcast)
        if images := await self._get_local_images(file_item.relative_parent_path):
            episode.podcast.metadata.images = images
        if metadata := await self._get_podcast_metadata(file_item.relative_parent_path):
            if title := metadata.get("title"):
                episode.podcast.name = title
            if sort_name := metadata.get("sorttitle"):
                episode.podcast.sort_name = sort_name
            if description := metadata.get("description"):
                episode.podcast.metadata.description = description
            if genres := metadata.get("genres"):
                episode.podcast.metadata.genres = set(genres)
            if publisher := metadata.get("publisher"):
                episode.podcast.publisher = publisher
            if image := metadata.get("imageURL"):
                episode.podcast.metadata.add_image(
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                )
        # copy (embedded) image from episode (or vice versa)
        if not episode.podcast.image and episode.image:
            episode.podcast.metadata.add_image(episode.image)
        elif not episode.image and episode.podcast.image:
            episode.metadata.add_image(episode.podcast.image)
        # ensure podcast has a default genre if none set
        if not episode.podcast.metadata.genres:
            episode.podcast.metadata.genres = {DEFAULT_AUDIOBOOK_PODCAST_GENRE}

        # handle (optional) loudness measurement tag(s)
        if tags.track_loudness is not None:
            self.mass.create_task(
                self.mass.streams.audio_analysis.set_track_loudness(
                    episode.item_id,
                    self.instance_id,
                    tags.track_loudness,
                    tags.track_album_loudness,
                    media_type=MediaType.PODCAST_EPISODE,
                )
            )
        return episode

    async def _parse_sound_effect(self, file_item: FileSystemItem, tags: AudioTags) -> SoundEffect:
        """Parse full sound effect details from file tags."""
        sound_effect = SoundEffect(
            item_id=file_item.relative_path,
            provider=self.instance_id,
            name=tags.title,
            sort_name=tags.title_sort,
            duration=int(tags.duration or 0),
            provider_mappings={
                ProviderMapping(
                    item_id=file_item.relative_path,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(
                        content_type=ContentType.try_parse(file_item.ext or tags.format),
                        codec_type=self._codec_type_from_tags(tags),
                        sample_rate=tags.sample_rate,
                        bit_depth=tags.bits_per_sample,
                        channels=tags.channels,
                        bit_rate=tags.bit_rate,
                    ),
                    details=file_item.checksum,
                    in_library=True,
                )
            },
        )
        sound_effect.metadata.description = tags.get("comment")
        # handle embedded cover image
        if tags.has_cover_image:
            # we do not actually embed the image in the metadata because that would consume too
            # much space and bandwidth. Instead we set the filename as value so the image can
            # be retrieved later in realtime.
            sound_effect.metadata.add_image(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=file_item.relative_path,
                    provider=self.instance_id,
                    remotely_accessible=False,
                )
            )
        return sound_effect

    async def _get_or_parse_sound_effect(self, file_item: FileSystemItem) -> SoundEffect:
        """Return the (cached) SoundEffect for the given file, parsing tags when needed."""
        cache_key = f"sound_effect.{file_item.relative_path}"
        cached_data: SoundEffect | None = await self.cache.get(
            cache_key,
            provider=self.instance_id,
            checksum=file_item.checksum,
            category=CACHE_CATEGORY_SOUND_EFFECTS,
            base_class=SoundEffect,
        )
        if cached_data is not None:
            return cached_data
        tags = await async_parse_tags(file_item.absolute_path, file_item.file_size)
        sound_effect = await self._parse_sound_effect(file_item, tags)
        await self.cache.set(
            cache_key,
            sound_effect.to_dict(),
            expiration=3600 * 24 * 365,  # File timestamp checksum handles invalidation
            provider=self.instance_id,
            checksum=file_item.checksum,
            category=CACHE_CATEGORY_SOUND_EFFECTS,
        )
        return sound_effect

    async def _parse_album(
        self, track_path: str, track_tags: AudioTags, track_created_at: int | None = None
    ) -> Album:
        """
        Parse Album metadata from Track tags.

        :param track_path: Path to the track file.
        :param track_tags: Audio tags from the track.
        :param track_created_at: Creation timestamp of the track file (Unix epoch).
        """
        assert track_tags.album
        # work out if we have an album and/or disc folder
        # track_dir is the folder level where the tracks are located
        # this may be a separate disc folder (Disc 1, Disc 2 etc) underneath the album folder
        # or this is an album folder with the disc attached
        track_dir = os.path.dirname(track_path)
        album_dir = get_album_dir(track_dir, track_tags.album)

        if album_dir and (
            cache := await self.cache.get(
                key=album_dir,
                provider=self.instance_id,
                category=CACHE_CATEGORY_ALBUM_INFO,
                base_class=Album,
            )
        ):
            return cache  # type: ignore[no-any-return]

        # album artist(s)
        album_artists: UniqueList[Artist | ItemMapping] = UniqueList()
        if track_tags.album_artists:
            resolved_album_artists = await self._resolve_artists_with_mbids(
                track_tags.album_artists,
                track_tags.musicbrainz_albumartistids,
                track_tags.album_artist_sort_names,
                log_label="ALBUMARTIST tag",
            )
            for name, mbid, sort_name in resolved_album_artists:
                artist = await self._parse_artist(
                    name, album_dir=album_dir, sort_name=sort_name, mbid=mbid
                )
                album_artists.append(artist)
        else:
            # album artist tag is missing, determine fallback
            fallback_action = self.config.get_value(CONF_ENTRY_MISSING_ALBUM_ARTIST.key)
            if fallback_action == "folder_name" and album_dir:
                possible_artist_folder = os.path.dirname(album_dir)
                self.logger.warning(
                    "%s is missing ID3 tag [albumartist], using foldername %s as fallback",
                    track_path,
                    possible_artist_folder,
                )
                album_artist_str = Path(possible_artist_folder).name
                album_artists = UniqueList(
                    [await self._parse_artist(name=album_artist_str, album_dir=album_dir)]
                )
            # fallback to track artists (if defined by user)
            elif fallback_action == "track_artist":
                self.logger.warning(
                    "%s is missing ID3 tag [albumartist], using track artist(s) as fallback",
                    track_path,
                )
                album_artists = UniqueList(
                    [
                        await self._parse_artist(name=track_artist_str, album_dir=album_dir)
                        for track_artist_str in track_tags.artists
                    ]
                )
            # all other: fallback to various artists
            else:
                self.logger.warning(
                    "%s is missing ID3 tag [albumartist], using %s as fallback",
                    track_path,
                    VARIOUS_ARTISTS_NAME,
                )
                album_artists = UniqueList(
                    [await self._parse_artist(name=VARIOUS_ARTISTS_NAME, mbid=VARIOUS_ARTISTS_MBID)]
                )

        if album_dir:  # noqa: SIM108
            # prefer the path as id
            item_id = album_dir
        else:
            # create fake item_id based on artist + album
            item_id = album_artists[0].name + os.sep + track_tags.album

        name, version = parse_title_and_version(track_tags.album)
        album = Album(
            item_id=item_id,
            provider=self.instance_id,
            name=name,
            version=version,
            sort_name=track_tags.album_sort,
            artists=album_artists,
            provider_mappings={
                ProviderMapping(
                    item_id=item_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=album_dir,
                    in_library=True,
                )
            },
            date_added=(
                datetime.fromtimestamp(track_created_at, tz=UTC) if track_created_at else None
            ),
        )
        if track_tags.barcode:
            album.external_ids.add((ExternalID.BARCODE, track_tags.barcode))

        if album_mbid := clean_mbid(track_tags.musicbrainz_albumid, track_tags.filename):
            album.mbid = album_mbid
        if releasegroup_mbid := clean_mbid(
            track_tags.musicbrainz_releasegroupid, track_tags.filename
        ):
            album.add_external_id(ExternalID.MB_RELEASEGROUP, releasegroup_mbid)
        if track_tags.year:
            album.year = track_tags.year
        album.album_type = track_tags.album_type

        # hunt for additional metadata and images in the folder structure
        if not album_dir:
            return album

        for folder_path in (track_dir, album_dir):
            if not folder_path or not await self.exists(folder_path):
                continue
            nfo_file = os.path.join(folder_path, "album.nfo")
            if await self.exists(nfo_file):
                # found NFO file with metadata
                # https://kodi.wiki/view/NFO_files/Artists
                try:
                    data = (await self._read_file(nfo_file)).decode("utf-8")
                    info = await asyncio.to_thread(xmltodict.parse, data)
                    parse_album_nfo(album, info["album"], nfo_file)
                except (ExpatError, KeyError) as err:
                    self.logger.warning(
                        "Failed to parse album NFO file %s: %s",
                        nfo_file,
                        str(err),
                    )

            # find local images
            if images := await self._get_local_images(folder_path, extra_thumb_names=("album",)):
                if album.metadata.images is None:
                    album.metadata.images = UniqueList(images)
                else:
                    album.metadata.images += images

        await self.cache.set(
            key=album_dir,
            data=album.to_dict(),
            provider=self.instance_id,
            category=CACHE_CATEGORY_ALBUM_INFO,
            expiration=120,
        )
        return album

    async def _get_local_images(
        self, folder: str, extra_thumb_names: tuple[str, ...] | None = None
    ) -> UniqueList[MediaItemImage]:
        """Return local images found in a given folderpath."""
        if (
            cache := await self.cache.get(
                key=folder,
                provider=self.instance_id,
                category=CACHE_CATEGORY_FOLDER_IMAGES,
                base_class=MediaItemImage,
            )
        ) is not None:
            return UniqueList(cache)
        if extra_thumb_names is None:
            extra_thumb_names = ()
        images: UniqueList[MediaItemImage] = UniqueList()
        folder_files = await self._scandir(folder)
        for item in folder_files:
            if "." not in item.relative_path or item.is_dir or not item.ext:
                continue
            if item.ext.lower() not in IMAGE_EXTENSIONS:
                continue
            # try match on filename = one of our imagetypes
            if item.name.lower() in ImageType:
                images.append(
                    MediaItemImage(
                        type=ImageType(item.name),
                        path=item.relative_path,
                        provider=self.instance_id,
                        remotely_accessible=False,
                    )
                )

        # try alternative names for thumbs
        extra_thumb_names = ("folder", "cover", *extra_thumb_names)
        for item in folder_files:
            if "." not in item.relative_path or item.is_dir or not item.ext:
                continue
            if item.ext.lower() not in IMAGE_EXTENSIONS:
                continue
            if item.name.lower() not in extra_thumb_names:
                continue
            images.append(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=item.relative_path,
                    provider=self.instance_id,
                    remotely_accessible=False,
                )
            )

        await self.cache.set(
            key=folder,
            data=[img.to_dict() for img in images],
            provider=self.instance_id,
            category=CACHE_CATEGORY_FOLDER_IMAGES,
            expiration=120,
        )
        return images

    async def _get_stream_details_for_track(self, item_id: str) -> StreamDetails:
        """Return the streamdetails for a track/song."""
        if parse_cue_track_id(item_id) is not None:
            return await self._cue.get_stream_details(item_id)

        library_item = await self.mass.music.tracks.get_library_item_by_prov_id(
            item_id, self.instance_id
        )
        if library_item is None:
            # this could be a file that has just been added, try parsing it
            file_item = await self.resolve(item_id)
            tags = await async_parse_tags(file_item.absolute_path, file_item.file_size)
            if not (library_item := await self._parse_track(file_item, tags)):
                msg = f"Item not found: {item_id}"
                raise MediaNotFoundError(msg)

        prov_mapping = next(x for x in library_item.provider_mappings if x.item_id == item_id)
        file_item = await self.resolve(item_id)

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=prov_mapping.audio_format,
            media_type=MediaType.TRACK,
            stream_type=StreamType.LOCAL_FILE,
            duration=library_item.duration,
            size=file_item.file_size,
            data=file_item,
            path=file_item.absolute_path,
            can_seek=True,
            allow_seek=True,
        )

    async def _get_stream_details_for_podcast_episode(self, item_id: str) -> StreamDetails:
        """Return the streamdetails for a podcast episode."""
        # podcasts episodes are never stored in the library so we need to parse the file
        file_item = await self.resolve(item_id)
        tags = await async_parse_tags(file_item.absolute_path, file_item.file_size)
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.try_parse(file_item.ext or tags.format),
                codec_type=self._codec_type_from_tags(tags),
                sample_rate=tags.sample_rate,
                bit_depth=tags.bits_per_sample,
                channels=tags.channels,
                bit_rate=tags.bit_rate,
            ),
            media_type=MediaType.PODCAST_EPISODE,
            stream_type=StreamType.LOCAL_FILE,
            duration=try_parse_int(tags.duration or 0),
            size=file_item.file_size,
            data=file_item,
            path=file_item.absolute_path,
            allow_seek=True,
            can_seek=True,
        )

    async def _get_stream_details_for_sound_effect(self, item_id: str) -> StreamDetails:
        """Return the streamdetails for a sound effect."""
        # sound effects are never stored in the library so we parse the file,
        # served from cache unless the file changed on disk
        file_item = await self.resolve(item_id)
        sound_effect = await self._get_or_parse_sound_effect(file_item)
        prov_mapping = next(x for x in sound_effect.provider_mappings if x.item_id == item_id)
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=prov_mapping.audio_format,
            media_type=MediaType.SOUND_EFFECT,
            stream_type=StreamType.LOCAL_FILE,
            duration=sound_effect.duration,
            size=file_item.file_size,
            data=file_item,
            path=file_item.absolute_path,
            allow_seek=True,
            can_seek=True,
        )

    async def _get_stream_details_for_audiobook(self, item_id: str) -> StreamDetails:
        """Return the streamdetails for an audiobook."""
        library_item = await self.mass.music.audiobooks.get_library_item_by_prov_id(
            item_id, self.instance_id
        )
        if library_item is None:
            # this could be a file that has just been added, try parsing it
            file_item = await self.resolve(item_id)
            tags = await async_parse_tags(file_item.absolute_path, file_item.file_size)
            if not (library_item := await self._parse_audiobook(file_item, tags)):
                msg = f"Item not found: {item_id}"
                raise MediaNotFoundError(msg)

        prov_mapping = next(x for x in library_item.provider_mappings if x.item_id == item_id)
        file_item = await self.resolve(item_id)
        duration = library_item.duration
        file_based_chapters: list[tuple[str, float]] | None = await self.cache.get(
            key=file_item.relative_path,
            provider=self.instance_id,
            category=CACHE_CATEGORY_AUDIOBOOK_CHAPTERS,
        )
        if file_based_chapters is None:
            # no cache available for this audiobook, we need to parse the chapters
            tags = await async_parse_tags(file_item.absolute_path, file_item.file_size)
            await self._parse_audiobook(file_item, tags)
            file_based_chapters = await self.cache.get(
                key=file_item.relative_path,
                provider=self.instance_id,
                category=CACHE_CATEGORY_AUDIOBOOK_CHAPTERS,
            )

        if file_based_chapters:
            # this is a multi-file audiobook
            return StreamDetails(
                provider=self.instance_id,
                item_id=item_id,
                audio_format=prov_mapping.audio_format,
                media_type=MediaType.AUDIOBOOK,
                stream_type=StreamType.LOCAL_FILE,
                duration=duration,
                path=[
                    MultiPartPath(
                        path=self._get_chapter_path(chapter_path),
                        duration=chapter_duration,
                    )
                    for chapter_path, chapter_duration in file_based_chapters
                ],
                allow_seek=True,
            )

        # regular single-file streaming, simply let ffmpeg deal with the file directly
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=prov_mapping.audio_format,
            media_type=MediaType.AUDIOBOOK,
            stream_type=StreamType.LOCAL_FILE,
            duration=library_item.duration,
            size=file_item.file_size,
            data=file_item,
            path=file_item.absolute_path,
            allow_seek=True,
            can_seek=True,
        )

    def _get_chapter_path(self, relative_path: str) -> str:
        """Return absolute path for a chapter file. Override for network storage."""
        return self.get_absolute_path(relative_path)

    async def _get_chapters_for_audiobook(
        self, audiobook_file_item: FileSystemItem, tags: AudioTags
    ) -> tuple[int, list[MediaItemChapter]]:
        """
        Return chapters for an audiobook.

        Chapter sources in order of preference:
        1. Multiple files with track tags - sorted by track number
        2. Single file with embedded chapters - use embedded chapter markers
        3. Multiple files without track tags - sorted alphabetically (fallback)
        """
        chapters: list[MediaItemChapter] = []
        all_chapter_files: list[tuple[str, float]] = []
        total_duration = 0.0

        # Scan folder for chapter files, separating tagged from untagged
        chapter_file_items: list[tuple[FileSystemItem, AudioTags]] = []
        untagged_file_items: list[tuple[FileSystemItem, AudioTags]] = []

        items = await self._scandir(audiobook_file_item.relative_parent_path)
        # Sort by filename for consistent alphabetical ordering
        items.sort(key=lambda x: x.filename.lower())

        for item in items:
            if "." not in item.relative_path or item.is_dir:
                continue
            if item.ext not in AUDIOBOOK_EXTENSIONS:
                continue
            item_tags = await async_parse_tags(item.absolute_path, item.file_size)
            if not (tags.album == item_tags.album or (item_tags.tags.get("title") is None)):
                continue
            if item_tags.tags.get("track") is None:
                untagged_file_items.append((item, item_tags))
            else:
                chapter_file_items.append((item, item_tags))

        # Determine chapter source
        use_embedded = False
        use_alphabetical = False

        if len(chapter_file_items) > 1:
            chapter_file_items.sort(key=lambda x: (x[1].disc or 0, x[1].track or 0))
        elif len(chapter_file_items) <= 1 and tags.chapters:
            use_embedded = True
        elif len(untagged_file_items) > 1:
            use_alphabetical = True
            chapter_file_items = untagged_file_items
            self.logger.info(
                "Audiobook files have no track tags, using alphabetical order: %s",
                tags.album,
            )

        if use_embedded:
            chapters = [
                MediaItemChapter(
                    position=chapter.chapter_id,
                    name=chapter.title or f"Chapter {chapter.chapter_id}",
                    start=chapter.position_start,
                    end=chapter.position_end,
                )
                for chapter in tags.chapters
            ]
            total_duration = try_parse_int(tags.duration) or 0
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "Audiobook '%s': %d embedded chapters, duration=%d",
                tags.album,
                len(chapters),
                int(total_duration),
            )
        else:
            for position, (chapter_item, chapter_tags) in enumerate(chapter_file_items, start=1):
                if chapter_tags.duration is None:
                    self.logger.warning(
                        "Chapter file has no duration, skipping: %s",
                        chapter_item.relative_path,
                    )
                    continue
                self.logger.debug("Chapter filename: %s", chapter_item.relative_path)
                chapters.append(
                    MediaItemChapter(
                        position=position,
                        name=chapter_tags.title,
                        start=total_duration,
                        end=total_duration + chapter_tags.duration,
                    )
                )
                all_chapter_files.append(
                    (
                        chapter_item.relative_path,
                        chapter_tags.duration,
                    )
                )
                total_duration += chapter_tags.duration
            sort_method = "alphabetical" if use_alphabetical else "track"
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "Audiobook '%s': %d files (%s order), duration=%d",
                tags.album,
                len(chapters),
                sort_method,
                int(total_duration),
            )
        # Cache chapter files for streaming
        await self.cache.set(
            key=audiobook_file_item.relative_path,
            data=all_chapter_files,
            provider=self.instance_id,
            category=CACHE_CATEGORY_AUDIOBOOK_CHAPTERS,
        )
        return int(total_duration), chapters

    async def _get_podcast_metadata(self, podcast_folder: str) -> dict[str, Any]:
        """Return metadata for a podcast."""
        if (
            cache := await self.cache.get(
                key=podcast_folder,
                provider=self.instance_id,
                category=CACHE_CATEGORY_PODCAST_METADATA,
            )
        ) is not None:
            return cast("dict[str, Any]", cache)
        data: dict[str, Any] = {}
        metadata_file = os.path.join(podcast_folder, "metadata.json")
        if await self.exists(metadata_file):
            # found json file with metadata
            raw = await self._read_file(metadata_file)
            data.update(json_loads(raw.decode("utf-8")))
        await self.cache.set(
            key=podcast_folder,
            data=data,
            provider=self.instance_id,
            category=CACHE_CATEGORY_PODCAST_METADATA,
        )
        return data

    async def _scandir(self, path: str) -> list[FileSystemItem]:
        """List directory contents in natural sort order."""
        # raw scandir order depends on the underlying filesystem (e.g. hash order
        # on ext4) so sort to make browse and folder playback order deterministic
        abs_path = self.get_absolute_path(path)
        return await asyncio.to_thread(sorted_scandir, self.base_path, abs_path, sort=True)

    async def _read_file(self, path: str) -> bytes:
        """Read file contents. Override for network storage."""
        async with aiofiles.open(self.get_absolute_path(path), mode="rb") as f:
            return cast("bytes", await f.read())
