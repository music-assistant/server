"""Filesystem musicprovider support for MusicAssistant."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import os.path
import time
import urllib.parse
from collections.abc import AsyncGenerator, Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import aiofiles
import shortuuid
import xmltodict
from aiofiles.os import wrap
from music_assistant_models.enums import (
    ContentType,
    ExternalID,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import MediaNotFoundError, MusicAssistantError, SetupFailedError
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemChapter,
    MediaItemImage,
    MediaItemTypeOrItemMapping,
    Playlist,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    SearchResults,
    Track,
    UniqueList,
    is_track,
)
from music_assistant_models.streamdetails import StreamDetails

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
from music_assistant.helpers.compare import compare_strings, create_safe_string
from music_assistant.helpers.json import json_loads
from music_assistant.helpers.playlists import parse_m3u, parse_pls
from music_assistant.helpers.tags import AudioTags, async_parse_tags, parse_tags, split_items
from music_assistant.helpers.util import (
    TaskManager,
    detect_charset,
    parse_title_and_version,
    try_parse_int,
)
from music_assistant.models.music_provider import MusicProvider

from .constants import (
    AUDIOBOOK_EXTENSIONS,
    CONF_ENTRY_CONTENT_TYPE,
    CONF_ENTRY_CONTENT_TYPE_READ_ONLY,
    CONF_ENTRY_IGNORE_ALBUM_PLAYLISTS,
    CONF_ENTRY_MISSING_ALBUM_ARTIST,
    CONF_ENTRY_PATH,
    IMAGE_EXTENSIONS,
    PLAYLIST_EXTENSIONS,
    PODCAST_EPISODE_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    TRACK_EXTENSIONS,
    IsChapterFile,
)
from .helpers import (
    IGNORE_DIRS,
    FileSystemItem,
    get_absolute_path,
    get_album_dir,
    get_artist_dir,
    get_relative_path,
    sorted_scandir,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


isdir = wrap(os.path.isdir)
isfile = wrap(os.path.isfile)
exists = wrap(os.path.exists)
makedirs = wrap(os.makedirs)
scandir = wrap(os.scandir)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    base_path = cast("str", config.get_value(CONF_PATH))
    return LocalFileSystemProvider(mass, manifest, config, base_path)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # ruff: noqa: ARG001
    if instance_id is None or values is None:
        return (
            CONF_ENTRY_CONTENT_TYPE,
            CONF_ENTRY_PATH,
            CONF_ENTRY_MISSING_ALBUM_ARTIST,
            CONF_ENTRY_IGNORE_ALBUM_PLAYLISTS,
        )
    return (
        CONF_ENTRY_PATH,
        CONF_ENTRY_CONTENT_TYPE_READ_ONLY,
        CONF_ENTRY_MISSING_ALBUM_ARTIST,
        CONF_ENTRY_IGNORE_ALBUM_PLAYLISTS,
    )


class LocalFileSystemProvider(MusicProvider):
    """
    Implementation of a musicprovider for (local) files.

    Reads ID3 tags from file and falls back to parsing filename.
    Optionally reads metadata from nfo files and images in folder structure <artist>/<album>.
    Supports m3u files for playlists.
    """

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        base_path: str,
    ) -> None:
        """Initialize MusicProvider."""
        super().__init__(mass, manifest, config)
        self.base_path: str = base_path
        self.write_access: bool = False
        self.sync_running: bool = False
        self.media_content_type = cast("str", config.get_value(CONF_ENTRY_CONTENT_TYPE.key))

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        base_features = {
            ProviderFeature.BROWSE,
            ProviderFeature.SEARCH,
        }
        if self.media_content_type == "audiobooks":
            return {ProviderFeature.LIBRARY_AUDIOBOOKS, *base_features}
        if self.media_content_type == "podcasts":
            return {ProviderFeature.LIBRARY_PODCASTS, *base_features}
        music_features = {
            ProviderFeature.LIBRARY_ARTISTS,
            ProviderFeature.LIBRARY_ALBUMS,
            ProviderFeature.LIBRARY_TRACKS,
            # for now, only support playlists for music files and not for podcasts or audiobooks
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
        return self.base_path.split(os.sep)[-1]

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        if not await isdir(self.base_path):
            msg = f"Music Directory {self.base_path} does not exist"
            raise SetupFailedError(msg)
        await self.check_write_access()

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
            result.tracks = await self.mass.music.tracks._get_library_items_by_query(
                search=search_query, provider=self.instance_id, limit=limit
            )

        if media_types is None or MediaType.ALBUM in media_types:
            result.albums = await self.mass.music.albums._get_library_items_by_query(
                search=search_query,
                provider=self.instance_id,
                limit=limit,
            )

        if media_types is None or MediaType.ARTIST in media_types:
            result.artists = await self.mass.music.artists._get_library_items_by_query(
                search=search_query,
                provider=self.instance_id,
                limit=limit,
            )
        if media_types is None or MediaType.PLAYLIST in media_types:
            result.playlists = await self.mass.music.playlists._get_library_items_by_query(
                search=search_query,
                provider=self.instance_id,
                limit=limit,
            )
        if media_types is None or MediaType.AUDIOBOOK in media_types:
            result.audiobooks = await self.mass.music.audiobooks._get_library_items_by_query(
                search=search_query,
                provider=self.instance_id,
                limit=limit,
            )
        if media_types is None or MediaType.PODCAST in media_types:
            result.podcasts = await self.mass.music.podcasts._get_library_items_by_query(
                search=search_query,
                provider=self.instance_id,
                limit=limit,
            )
        return result

    async def browse(self, path: str) -> Sequence[MediaItemTypeOrItemMapping]:
        """Browse this provider's items.

        :param path: The path to browse, (e.g. provid://artists).
        """
        # for audiobooks and podcasts we just return all library items
        if self.media_content_type == "podcasts":
            return await self.mass.music.podcasts.library_items(provider=self.instance_id)
        if self.media_content_type == "audiobooks":
            return await self.mass.music.audiobooks.library_items(provider=self.instance_id)
        items: list[MediaItemTypeOrItemMapping] = []
        item_path = path.split("://", 1)[1]
        if not item_path:
            item_path = ""
        abs_path = self.get_absolute_path(item_path)
        for item in await asyncio.to_thread(sorted_scandir, self.base_path, abs_path, sort=True):
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
        return items

    async def sync_library(self, media_type: MediaType) -> None:
        """Run library sync for this provider."""
        assert self.mass.music.database
        start_time = time.time()
        if self.sync_running:
            self.logger.warning("Library sync already running for %s", self.name)
            return
        self.logger.info(
            "Started Library sync for %s",
            self.name,
        )
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
        # find all supported files in the base directory and all subfolders
        # we work bottom up, as-in we derive all info from the tracks
        cur_filenames = set()
        prev_filenames = set(file_checksums.keys())

        # NOTE: we do the entire traversing of the directory structure, including parsing tags
        # in a single executor thread to save the overhead of having to spin up tons of tasks
        def listdir(path: str) -> Iterator[FileSystemItem]:
            """Recursively traverse directory entries."""
            for item in os.scandir(path):
                # ignore invalid filenames
                if item.name in IGNORE_DIRS or item.name.startswith((".", "_")):
                    continue
                if item.is_dir(follow_symlinks=False):
                    yield from listdir(item.path)
                elif item.is_file(follow_symlinks=False):
                    # skip files without extension
                    if "." not in item.name:
                        continue
                    ext = item.name.rsplit(".", 1)[1].lower()
                    if ext not in SUPPORTED_EXTENSIONS:
                        # skip unsupported file extension
                        continue
                    yield FileSystemItem.from_dir_entry(item, self.base_path)

        def run_sync() -> None:
            """Run the actual sync (in an executor job)."""
            self.sync_running = True
            try:
                for item in listdir(self.base_path):
                    prev_checksum = file_checksums.get(item.relative_path)
                    if self._process_item(item, prev_checksum):
                        cur_filenames.add(item.relative_path)
            finally:
                self.sync_running = False

        await asyncio.to_thread(run_sync)

        end_time = time.time()
        self.logger.info(
            "Library sync for %s completed in %.2f seconds",
            self.name,
            end_time - start_time,
        )
        # work out deletions
        deleted_files = prev_filenames - cur_filenames
        await self._process_deletions(deleted_files)

        # process orphaned albums and artists
        await self._process_orphaned_albums_and_artists()

    def _process_item(self, item: FileSystemItem, prev_checksum: str | None) -> bool:
        """Process a single item. NOT async friendly."""
        try:
            self.logger.log(VERBOSE_LOG_LEVEL, "Processing: %s", item.relative_path)

            # ignore playlists that are in album directories
            # we need to run this check early because the setting may have changed
            if (
                item.ext in PLAYLIST_EXTENSIONS
                and self.media_content_type == "music"
                and self.config.get_value(CONF_ENTRY_IGNORE_ALBUM_PLAYLISTS.key)
            ):
                # we assume this in a bit of a dumb way by just checking if the playlist
                # is more than 1 level deep in the directory structure
                if len(item.relative_path.split("/")) > 2:
                    return False

            # return early if the item did not change (checksum still the same)
            if item.checksum == prev_checksum:
                return True

            if item.ext in TRACK_EXTENSIONS and self.media_content_type == "music":
                # handle track item
                tags = parse_tags(item.absolute_path, item.file_size)

                async def process_track() -> None:
                    track = await self._parse_track(item, tags)
                    # add/update track to db
                    # note that filesystem items are always overwriting existing info
                    # when they are detected as changed
                    await self.mass.music.tracks.add_item_to_library(
                        track, overwrite_existing=prev_checksum is not None
                    )

                asyncio.run_coroutine_threadsafe(process_track(), self.mass.loop).result()
                return True

            if item.ext in AUDIOBOOK_EXTENSIONS and self.media_content_type == "audiobooks":
                # handle audiobook item
                tags = parse_tags(item.absolute_path, item.file_size)

                async def process_audiobook() -> None:
                    try:
                        audiobook = await self._parse_audiobook(item, tags)
                    except IsChapterFile:
                        return
                    # add/update audiobook to db
                    # note that filesystem items are always overwriting existing info
                    # when they are detected as changed
                    await self.mass.music.audiobooks.add_item_to_library(
                        audiobook, overwrite_existing=prev_checksum is not None
                    )

                asyncio.run_coroutine_threadsafe(process_audiobook(), self.mass.loop).result()
                return True

            if item.ext in PODCAST_EPISODE_EXTENSIONS and self.media_content_type == "podcasts":
                # handle podcast(episode) item
                tags = parse_tags(item.absolute_path, item.file_size)

                async def process_episode() -> None:
                    episode = await self._parse_podcast_episode(item, tags)
                    assert isinstance(episode.podcast, Podcast)
                    # add/update episode to db
                    # note that filesystem items are always overwriting existing info
                    # when they are detected as changed
                    await self.mass.music.podcasts.add_item_to_library(
                        episode.podcast, overwrite_existing=prev_checksum is not None
                    )

                asyncio.run_coroutine_threadsafe(process_episode(), self.mass.loop).result()
                return True

            if item.ext in PLAYLIST_EXTENSIONS and self.media_content_type == "music":
                # handle playlist item

                async def process_playlist() -> None:
                    playlist = await self.get_playlist(item.relative_path)
                    # add/update] playlist to db
                    playlist.cache_checksum = item.checksum
                    await self.mass.music.playlists.add_item_to_library(
                        playlist,
                        overwrite_existing=prev_checksum is not None,
                    )

                asyncio.run_coroutine_threadsafe(process_playlist(), self.mass.loop).result()
                return True

        except Exception as err:
            # we don't want the whole sync to crash on one file so we catch all exceptions here
            self.logger.error(
                "Error processing %s - %s",
                item.relative_path,
                str(err),
                exc_info=err if self.logger.isEnabledFor(logging.DEBUG) else None,
            )
        return False

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
        for track in await self.get_album_tracks(prov_album_id):
            for prov_mapping in track.provider_mappings:
                if prov_mapping.provider_instance == self.instance_id:
                    file_item = await self.resolve(prov_mapping.item_id)
                    tags = await async_parse_tags(file_item.absolute_path, file_item.file_size)
                    full_track = await self._parse_track(file_item, tags)
                    assert isinstance(full_track.album, Album)
                    return full_track.album
        msg = f"Album not found: {prov_album_id}"
        raise MediaNotFoundError(msg)

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        # ruff: noqa: PLR0915, PLR0912
        if not await self.exists(prov_track_id):
            msg = f"Track path does not exist: {prov_track_id}"
            raise MediaNotFoundError(msg)

        file_item = await self.resolve(prov_track_id)
        tags = await async_parse_tags(file_item.absolute_path, file_item.file_size)
        return await self._parse_track(file_item, tags=tags, full_album_metadata=True)

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
        checksum = str(file_item.checksum)
        playlist.cache_checksum = checksum
        return playlist

    async def get_audiobook(self, prov_audiobook_id: str) -> Audiobook:
        """Get full audiobook details by id."""
        # ruff: noqa: PLR0915, PLR0912
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

        _, ext = prov_playlist_id.rsplit(".", 1)
        try:
            # get playlist file contents
            playlist_filename = self.get_absolute_path(prov_playlist_id)
            async with aiofiles.open(playlist_filename, mode="rb") as _file:
                playlist_data_raw = await _file.read()
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
        return result

    async def get_podcast_episodes(
        self, prov_podcast_id: str
    ) -> AsyncGenerator[PodcastEpisode, None]:
        """Get podcast episodes for given podcast id."""
        episodes: list[PodcastEpisode] = []

        async def _process_podcast_episode(item: FileSystemItem) -> None:
            tags = await async_parse_tags(item.absolute_path, item.file_size)
            try:
                episode = await self._parse_podcast_episode(item, tags)
            except MusicAssistantError as err:
                self.logger.warning(
                    "Could not parse uri/file %s to podcast episode: %s",
                    item.relative_path,
                    str(err),
                )
            else:
                episodes.append(episode)

        async with TaskManager(self.mass, 25) as tm:
            for item in await asyncio.to_thread(sorted_scandir, self.base_path, prov_podcast_id):
                if "." not in item.relative_path or item.is_dir:
                    continue
                if item.ext not in PODCAST_EPISODE_EXTENSIONS:
                    continue
                tm.create_task(_process_podcast_episode(item))

        for episode in episodes:
            yield episode

    async def _parse_playlist_line(self, line: str, playlist_path: str) -> Track | None:
        """Try to parse a track from a playlist line."""
        try:
            line = line.replace("file://", "").strip()
            # try to resolve the filename (both normal and url decoded):
            # - as an absolute path
            # - relative to the playlist path
            # - relative to our base path
            # - relative to the playlist path with a leading slash
            for _line in (line, urllib.parse.unquote(line)):
                for filename in (
                    # try to resolve the line by resolving it against the (absolute) playlist path
                    # use the path.resolve step in between to auto-resolve parent item references
                    (Path(self.get_absolute_path(playlist_path)) / _line).resolve().as_posix(),
                    # try to resolve the line as a full absolute (or relative to music dir) path
                    _line,
                ):
                    with contextlib.suppress(FileNotFoundError):
                        file_item = await self.resolve(filename)
                        tags = await async_parse_tags(file_item.absolute_path, file_item.file_size)
                        return await self._parse_track(file_item, tags)
            # all attempts failed
            raise MediaNotFoundError("Invalid path/uri")

        except MusicAssistantError as err:
            self.logger.warning("Could not parse %s to track: %s", line, str(err))

        return None

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

    async def create_playlist(self, name: str) -> Playlist:
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
        if media_type == MediaType.AUDIOBOOK:
            return await self._get_stream_details_for_audiobook(item_id)
        if media_type == MediaType.PODCAST_EPISODE:
            return await self._get_stream_details_for_podcast_episode(item_id)
        return await self._get_stream_details_for_track(item_id)

    async def resolve_image(self, path: str) -> str | bytes:
        """
        Resolve an image from an image path.

        This either returns (a generator to get) raw bytes of the image or
        a string with an http(s) URL or local path that is accessible from the server.
        """
        file_item = await self.resolve(path)
        return file_item.absolute_path

    async def _parse_track(
        self, file_item: FileSystemItem, tags: AudioTags, full_album_metadata: bool = False
    ) -> Track:
        """Parse full track details from file tags."""
        # ruff: noqa: PLR0915, PLR0912
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
                        sample_rate=tags.sample_rate,
                        bit_depth=tags.bits_per_sample,
                        channels=tags.channels,
                        bit_rate=tags.bit_rate,
                    ),
                    details=file_item.checksum,
                )
            },
            disc_number=tags.disc or 0,
            track_number=tags.track or 0,
        )

        if isrc_tags := tags.isrc:
            for isrsc in isrc_tags:
                track.external_ids.add((ExternalID.ISRC, isrsc))

        if acoustid := tags.get("acoustid"):
            track.external_ids.add((ExternalID.ACOUSTID, acoustid))

        # album
        album = track.album = (
            await self._parse_album(track_path=file_item.relative_path, track_tags=tags)
            if tags.album
            else None
        )

        # track artist(s)
        for index, track_artist_str in enumerate(tags.artists):
            # prefer album artist if match
            if album and (
                album_artist_match := next(
                    (x for x in album.artists if x.name == track_artist_str), None
                )
            ):
                track.artists.append(album_artist_match)
                continue
            artist = await self._parse_artist(
                track_artist_str,
                sort_name=(
                    tags.artist_sort_names[index] if index < len(tags.artist_sort_names) else None
                ),
                mbid=(
                    tags.musicbrainz_artistids[index]
                    if index < len(tags.musicbrainz_artistids)
                    else None
                ),
            )
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
        track.metadata.description = tags.get("comment")
        explicit_tag = tags.get("itunesadvisory")
        if explicit_tag is not None:
            track.metadata.explicit = explicit_tag == "1"
        if tags.musicbrainz_recordingid:
            track.mbid = tags.musicbrainz_recordingid

        # handle (optional) loudness measurement tag(s)
        if tags.track_loudness is not None:
            self.mass.create_task(
                self.mass.music.set_loudness(
                    track.item_id,
                    self.instance_id,
                    tags.track_loudness,
                    tags.track_album_loudness,
                )
            )
        return track

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
                async for item in self.mass.music.artists.iter_library_items(search=name):
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
        cache_base_key = f"{self.instance_id}.artist"
        if artist_path and (cache := await self.cache.get(artist_path, base_key=cache_base_key)):
            return cast("Artist", cache)

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
                )
            },
        )
        if mbid:
            artist.mbid = mbid
        if not artist_path:
            return artist

        # grab additional metadata within the Artist's folder
        nfo_file = os.path.join(artist_path, "artist.nfo")
        if await self.exists(nfo_file):
            # found NFO file with metadata
            # https://kodi.wiki/view/NFO_files/Artists
            nfo_file = self.get_absolute_path(nfo_file)
            async with aiofiles.open(nfo_file) as _file:
                data = await _file.read()
            info = await asyncio.to_thread(xmltodict.parse, data)
            info = info["artist"]
            artist.name = info.get("title", info.get("name", name))
            if sort_name := info.get("sortname"):
                artist.sort_name = sort_name
            if mbid := info.get("musicbrainzartistid"):
                artist.mbid = mbid
            if description := info.get("biography"):
                artist.metadata.description = description
            if genre := info.get("genre"):
                artist.metadata.genres = set(split_items(genre))
        # find local images
        if images := await self._get_local_images(artist_path, extra_thumb_names=("artist",)):
            artist.metadata.images = UniqueList(images)

        await self.cache.set(artist_path, artist, base_key=cache_base_key, expiration=120)

        return artist

    async def _parse_audiobook(self, file_item: FileSystemItem, tags: AudioTags) -> Audiobook:
        """Parse full Audiobook details from file tags."""
        # an audiobook can either be a single file with chapters embedded in the file
        # or a folder with multiple files (each file being a chapter)
        # we only scrape all tags from the first file in the folder
        if tags.track and tags.track > 1:
            raise IsChapterFile
        # in case of a multi-file audiobook, the title is the chapter name
        # and the album is the actual audiobook name
        # so we prefer the album name as the audiobook name
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
                        sample_rate=tags.sample_rate,
                        bit_depth=tags.bits_per_sample,
                        channels=tags.channels,
                        bit_rate=tags.bit_rate,
                    ),
                    details=file_item.checksum,
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
                    path=file_item.relative_path,
                    provider=self.instance_id,
                    remotely_accessible=False,
                )
            )

        # parse other info
        audio_book.authors.set(tags.writers or tags.album_artists or tags.artists)
        audio_book.metadata.genres = set(tags.genres)
        audio_book.metadata.copyright = tags.get("copyright")
        audio_book.metadata.lyrics = tags.lyrics
        audio_book.metadata.description = tags.get("comment")
        explicit_tag = tags.get("itunesadvisory")
        if explicit_tag is not None:
            audio_book.metadata.explicit = explicit_tag == "1"
        if tags.musicbrainz_recordingid:
            audio_book.mbid = tags.musicbrainz_recordingid

        # try to fetch additional metadata from the folder
        if not audio_book.image or not audio_book.metadata.description:
            # try to get an image by traversing files in the same folder
            abs_path = self.get_absolute_path(file_item.parent_path)
            for _item in await asyncio.to_thread(sorted_scandir, self.base_path, abs_path):
                if "." not in _item.relative_path or _item.is_dir:
                    continue
                if _item.ext in IMAGE_EXTENSIONS and not audio_book.image:
                    audio_book.metadata.add_image(
                        MediaItemImage(
                            type=ImageType.THUMB,
                            path=_item.relative_path,
                            provider=self.instance_id,
                            remotely_accessible=False,
                        )
                    )
                if _item.ext == "txt" and not audio_book.metadata.description:
                    # try to parse a description from a text file
                    try:
                        async with aiofiles.open(_item.absolute_path, encoding="utf-8") as _file:
                            description = await _file.read()
                        audio_book.metadata.description = description
                    except Exception as err:
                        self.logger.warning(
                            "Could not read description from file %s: %s",
                            _item.relative_path,
                            str(err),
                        )

        # handle (optional) loudness measurement tag(s)
        if tags.track_loudness is not None:
            self.mass.create_task(
                self.mass.music.set_loudness(
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
        # ruff: noqa: PLR0915, PLR0912
        podcast_name = tags.album or file_item.parent_name
        podcast_path = get_relative_path(self.base_path, file_item.parent_path)
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
        episode.metadata.genres = set(tags.genres)
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
        if images := await self._get_local_images(file_item.parent_path):
            episode.podcast.metadata.images = images
        if metadata := await self._get_podcast_metadata(file_item.parent_path):
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

        # handle (optional) loudness measurement tag(s)
        if tags.track_loudness is not None:
            self.mass.create_task(
                self.mass.music.set_loudness(
                    episode.item_id,
                    self.instance_id,
                    tags.track_loudness,
                    tags.track_album_loudness,
                    media_type=MediaType.PODCAST_EPISODE,
                )
            )
        return episode

    async def _parse_album(self, track_path: str, track_tags: AudioTags) -> Album:
        """Parse Album metadata from Track tags."""
        assert track_tags.album
        # work out if we have an album and/or disc folder
        # track_dir is the folder level where the tracks are located
        # this may be a separate disc folder (Disc 1, Disc 2 etc) underneath the album folder
        # or this is an album folder with the disc attached
        track_dir = os.path.dirname(track_path)
        album_dir = get_album_dir(track_dir, track_tags.album)

        cache_base_key = f"{self.instance_id}.album"
        if album_dir and (cache := await self.cache.get(album_dir, base_key=cache_base_key)):
            return cast("Album", cache)

        # album artist(s)
        album_artists: UniqueList[Artist | ItemMapping] = UniqueList()
        if track_tags.album_artists:
            for index, album_artist_str in enumerate(track_tags.album_artists):
                artist = await self._parse_artist(
                    album_artist_str,
                    album_dir=album_dir,
                    sort_name=(
                        track_tags.album_artist_sort_names[index]
                        if index < len(track_tags.album_artist_sort_names)
                        else None
                    ),
                    mbid=(
                        track_tags.musicbrainz_albumartistids[index]
                        if index < len(track_tags.musicbrainz_albumartistids)
                        else None
                    ),
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
                album_artist_str = possible_artist_folder.rsplit(os.sep)[-1]
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
                )
            },
        )
        if track_tags.barcode:
            album.external_ids.add((ExternalID.BARCODE, track_tags.barcode))

        if track_tags.musicbrainz_albumid:
            album.mbid = track_tags.musicbrainz_albumid
        if track_tags.musicbrainz_releasegroupid:
            album.add_external_id(ExternalID.MB_RELEASEGROUP, track_tags.musicbrainz_releasegroupid)
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
                nfo_file = self.get_absolute_path(nfo_file)
                async with aiofiles.open(nfo_file) as _file:
                    data = await _file.read()
                info = await asyncio.to_thread(xmltodict.parse, data)
                info = info["album"]
                album.name = info.get("title", info.get("name", name))
                if sort_name := info.get("sortname"):
                    album.sort_name = sort_name
                if releasegroup_id := info.get("musicbrainzreleasegroupid"):
                    album.add_external_id(ExternalID.MB_RELEASEGROUP, releasegroup_id)
                if album_id := info.get("musicbrainzalbumid"):
                    album.add_external_id(ExternalID.MB_ALBUM, album_id)
                if mb_artist_id := info.get("musicbrainzalbumartistid"):
                    if album.artists and not album.artists[0].mbid:
                        album.artists[0].mbid = mb_artist_id
                if description := info.get("review"):
                    album.metadata.description = description
                if year := info.get("year"):
                    album.year = int(year)
                if genre := info.get("genre"):
                    album.metadata.genres = set(split_items(genre))
            # parse name/version
            album.name, album.version = parse_title_and_version(album.name)
            # find local images
            if images := await self._get_local_images(folder_path, extra_thumb_names=("album",)):
                if album.metadata.images is None:
                    album.metadata.images = UniqueList(images)
                else:
                    album.metadata.images += images
        await self.cache.set(album_dir, album, base_key=cache_base_key, expiration=120)
        return album

    async def _get_local_images(
        self, folder: str, extra_thumb_names: tuple[str, ...] | None = None
    ) -> UniqueList[MediaItemImage]:
        """Return local images found in a given folderpath."""
        cache_base_key = f"{self.lookup_key}.folderimages"
        if (cache := await self.cache.get(folder, base_key=cache_base_key)) is not None:
            return cast("UniqueList[MediaItemImage]", cache)
        if extra_thumb_names is None:
            extra_thumb_names = ()
        images: UniqueList[MediaItemImage] = UniqueList()
        abs_path = self.get_absolute_path(folder)
        folder_files = await asyncio.to_thread(sorted_scandir, self.base_path, abs_path, sort=False)
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

        await self.cache.set(folder, images, base_key=cache_base_key, expiration=120)
        return images

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

    async def resolve(
        self,
        file_path: str,
    ) -> FileSystemItem:
        """Resolve (absolute or relative) path to FileSystemItem."""
        absolute_path = self.get_absolute_path(file_path)

        def _create_item() -> FileSystemItem:
            if os.path.isdir(absolute_path):
                return FileSystemItem(
                    filename=os.path.basename(file_path),
                    relative_path=get_relative_path(self.base_path, file_path),
                    absolute_path=absolute_path,
                    is_dir=True,
                )
            stat = os.stat(absolute_path, follow_symlinks=False)
            return FileSystemItem(
                filename=os.path.basename(file_path),
                relative_path=get_relative_path(self.base_path, file_path),
                absolute_path=absolute_path,
                is_dir=False,
                checksum=str(int(stat.st_mtime)),
                file_size=stat.st_size,
            )

        # run in thread because strictly taken this may be blocking IO
        return await asyncio.to_thread(_create_item)

    async def exists(self, file_path: str) -> bool:
        """Return bool is this FileSystem musicprovider has given file/dir."""
        if not file_path:
            return False  # guard
        abs_path = self.get_absolute_path(file_path)
        return bool(await exists(abs_path))

    def get_absolute_path(self, file_path: str) -> str:
        """Return absolute path for given file path."""
        return get_absolute_path(self.base_path, file_path)

    async def _get_stream_details_for_track(self, item_id: str) -> StreamDetails:
        """Return the streamdetails for a track/song."""
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
        chapters_cache_key = f"{self.lookup_key}.audiobook.chapters"
        file_based_chapters: list[tuple[str, float]] | None = await self.cache.get(
            file_item.relative_path,
            base_key=chapters_cache_key,
        )
        if file_based_chapters is None:
            # no cache available for this audiobook, we need to parse the chapters
            tags = await async_parse_tags(file_item.absolute_path, file_item.file_size)
            await self._parse_audiobook(file_item, tags)
            file_based_chapters = await self.cache.get(
                file_item.relative_path,
                base_key=chapters_cache_key,
            )

        if file_based_chapters:
            # this is a multi-file audiobook
            return StreamDetails(
                provider=self.instance_id,
                item_id=item_id,
                audio_format=prov_mapping.audio_format,
                media_type=MediaType.AUDIOBOOK,
                stream_type=StreamType.MULTI_FILE,
                duration=duration,
                data=[self.get_absolute_path(x[0]) for x in file_based_chapters],
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

    async def _get_chapters_for_audiobook(
        self, audiobook_file_item: FileSystemItem, tags: AudioTags
    ) -> tuple[int, list[MediaItemChapter]]:
        """Return the chapters for an audiobook."""
        chapters: list[MediaItemChapter] = []
        all_chapter_files: list[tuple[str, float]] = []
        total_duration = 0.0
        if tags.chapters:
            # The chapters are embedded in the file tags
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
        else:
            # there could be multiple files for this audiobook in the same folder,
            # where each file is a portion/chapter of the audiobook
            # try to gather the chapters by traversing files in the same folder
            chapter_file_tags: list[AudioTags] = []
            abs_path = self.get_absolute_path(audiobook_file_item.parent_path)
            for item in await asyncio.to_thread(
                sorted_scandir, self.base_path, abs_path, sort=True
            ):
                if "." not in item.relative_path or item.is_dir:
                    continue
                if item.ext not in AUDIOBOOK_EXTENSIONS:
                    continue
                item_tags = await async_parse_tags(item.absolute_path, item.file_size)
                if not (tags.album == item_tags.album or (item_tags.tags.get("title") is None)):
                    continue
                if item_tags.track is None:
                    continue
                chapter_file_tags.append(item_tags)
            chapter_file_tags.sort(key=lambda x: x.track or 0)
            for chapter_tags in chapter_file_tags:
                assert chapter_tags.duration is not None
                chapters.append(
                    MediaItemChapter(
                        position=chapter_tags.track or 0,
                        name=chapter_tags.title,
                        start=total_duration,
                        end=total_duration + chapter_tags.duration,
                    )
                )
                all_chapter_files.append(
                    (
                        get_relative_path(self.base_path, chapter_tags.filename),
                        chapter_tags.duration,
                    )
                )
                total_duration += chapter_tags.duration

        # store chapter files in cache
        # for easy access from streamdetails
        await self.cache.set(
            audiobook_file_item.relative_path,
            all_chapter_files,
            base_key=f"{self.lookup_key}.audiobook.chapters",
        )
        return (int(total_duration), chapters)

    async def _get_podcast_metadata(self, podcast_folder: str) -> dict[str, Any]:
        """Return metadata for a podcast."""
        cache_base_key = f"{self.lookup_key}.podcastmetadata"
        if (cache := await self.cache.get(podcast_folder, base_key=cache_base_key)) is not None:
            return cast("dict[str, Any]", cache)
        data: dict[str, Any] = {}
        metadata_file = os.path.join(podcast_folder, "metadata.json")
        if await self.exists(metadata_file):
            # found json file with metadata
            metadata_file = self.get_absolute_path(metadata_file)
            async with aiofiles.open(metadata_file) as _file:
                data.update(json_loads(await _file.read()))
        await self.cache.set(podcast_folder, data, base_key=cache_base_key)
        return data
