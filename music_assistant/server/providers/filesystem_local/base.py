"""Filesystem musicprovider support for MusicAssistant."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

import cchardet
import xmltodict

from music_assistant.common.helpers.util import parse_title_and_version
from music_assistant.common.models.config_entries import (
    ConfigEntry,
    ConfigEntryType,
    ConfigValueOption,
)
from music_assistant.common.models.enums import ExternalID, ProviderFeature, StreamType
from music_assistant.common.models.errors import MediaNotFoundError, MusicAssistantError
from music_assistant.common.models.media_items import (
    Album,
    Artist,
    AudioFormat,
    BrowseFolder,
    ContentType,
    ImageType,
    ItemMapping,
    MediaItemImage,
    MediaItemType,
    MediaType,
    Playlist,
    ProviderMapping,
    SearchResults,
    Track,
)
from music_assistant.common.models.streamdetails import StreamDetails
from music_assistant.constants import (
    DB_TABLE_ALBUM_ARTISTS,
    DB_TABLE_ALBUM_TRACKS,
    DB_TABLE_ALBUMS,
    DB_TABLE_ARTISTS,
    DB_TABLE_PROVIDER_MAPPINGS,
    DB_TABLE_TRACK_ARTISTS,
    VARIOUS_ARTISTS_NAME,
)
from music_assistant.server.controllers.cache import use_cache
from music_assistant.server.controllers.music import DB_SCHEMA_VERSION
from music_assistant.server.helpers.compare import compare_strings
from music_assistant.server.helpers.playlists import parse_m3u, parse_pls
from music_assistant.server.helpers.tags import parse_tags, split_items
from music_assistant.server.models.music_provider import MusicProvider

from .helpers import get_parentdir

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


CONF_MISSING_ALBUM_ARTIST_ACTION = "missing_album_artist_action"

CONF_ENTRY_MISSING_ALBUM_ARTIST = ConfigEntry(
    key=CONF_MISSING_ALBUM_ARTIST_ACTION,
    type=ConfigEntryType.STRING,
    label="Action when a track is missing the Albumartist ID3 tag",
    default_value="various_artists",
    help_link="https://music-assistant.io/music-providers/filesystem/#tagging-files",
    required=False,
    options=(
        ConfigValueOption("Use Track artist(s)", "track_artist"),
        ConfigValueOption("Use Various Artists", "various_artists"),
        ConfigValueOption("Use Folder name (if possible)", "folder_name"),
    ),
)

TRACK_EXTENSIONS = (
    "mp3",
    "m4a",
    "m4b",
    "mp4",
    "flac",
    "wav",
    "ogg",
    "aiff",
    "wma",
    "dsf",
    "opus",
)
PLAYLIST_EXTENSIONS = ("m3u", "pls", "m3u8")
SUPPORTED_EXTENSIONS = TRACK_EXTENSIONS + PLAYLIST_EXTENSIONS
IMAGE_EXTENSIONS = ("jpg", "jpeg", "JPG", "JPEG", "png", "PNG", "gif", "GIF")
SEEKABLE_FILES = (ContentType.MP3, ContentType.WAV, ContentType.FLAC)
IGNORE_DIRS = ("recycle", "Recently-Snaphot")

SUPPORTED_FEATURES = (
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
)


@dataclass
class FileSystemItem:
    """Representation of an item (file or directory) on the filesystem.

    - filename: Name (not path) of the file (or directory).
    - path: Relative path to the item on this filesystem provider.
    - absolute_path: Absolute (provider dependent) path to this item.
    - is_file: Boolean if item is file (not directory or symlink).
    - is_dir: Boolean if item is directory (not file).
    - checksum: Checksum for this path (usually last modified time).
    - file_size : File size in number of bytes or None if unknown (or not a file).
    - local_path: Optional local accessible path to this (file)item, supported by ffmpeg.
    """

    filename: str
    path: str
    absolute_path: str
    is_file: bool
    is_dir: bool
    checksum: str
    file_size: int | None = None
    local_path: str | None = None

    @property
    def ext(self) -> str | None:
        """Return file extension."""
        try:
            return self.filename.rsplit(".", 1)[1]
        except IndexError:
            return None

    @property
    def name(self) -> str:
        """Return file name (without extension)."""
        return self.filename.rsplit(".", 1)[0]


class FileSystemProviderBase(MusicProvider):
    """Base Implementation of a musicprovider for files.

    Reads ID3 tags from file and falls back to parsing filename.
    Optionally reads metadata from nfo files and images in folder structure <artist>/<album>.
    Supports m3u files only for playlists.
    Supports having URI's from streaming providers within m3u playlist.
    """

    write_access: bool = False

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        if self.write_access:
            return (
                *SUPPORTED_FEATURES,
                ProviderFeature.PLAYLIST_CREATE,
                ProviderFeature.PLAYLIST_TRACKS_EDIT,
            )
        return SUPPORTED_FEATURES

    @abstractmethod
    async def listdir(
        self, path: str, recursive: bool = False
    ) -> AsyncGenerator[FileSystemItem, None]:
        """List contents of a given provider directory/path.

        Parameters
        ----------
            - path: path of the directory (relative or absolute) to list contents of.
              Empty string for provider's root.
            - recursive: If True will recursively keep unwrapping
              subdirectories (scandir equivalent).

        Returns:
        -------
            AsyncGenerator yielding FileSystemItem objects.

        """
        yield

    @abstractmethod
    async def resolve(self, file_path: str) -> FileSystemItem:
        """Resolve (absolute or relative) path to FileSystemItem."""

    @abstractmethod
    async def exists(self, file_path: str) -> bool:
        """Return bool is this FileSystem musicprovider has given file/dir."""

    @abstractmethod
    async def read_file_content(self, file_path: str, seek: int = 0) -> AsyncGenerator[bytes, None]:
        """Yield (binary) contents of file in chunks of bytes."""

    @abstractmethod
    async def write_file_content(self, file_path: str, data: bytes) -> None:
        """Write entire file content as bytes (e.g. for playlists)."""

    ##############################################
    # DEFAULT/GENERIC IMPLEMENTATION BELOW
    # should normally not be needed to override

    @property
    def is_streaming_provider(self) -> bool:
        """
        Return True if the provider is a streaming provider.

        This literally means that the catalog is not the same as the library contents.
        For local based providers (files, plex), the catalog is the same as the library content.
        It also means that data is if this provider is NOT a streaming provider,
        data cross instances is unique, the catalog and library differs per instance.

        Setting this to True will only query one instance of the provider for search and lookups.
        Setting this to False will query all instances of this provider for search and lookups.
        """
        return False

    async def search(
        self,
        search_query: str,
        media_types=list[MediaType] | None,
        limit: int = 5,
    ) -> SearchResults:
        """Perform search on this file based musicprovider."""
        result = SearchResults()
        # searching the filesystem is slow and unreliable,
        # so instead we just query the db...
        query = "provider_mappings.provider_instance = :provider_instance "
        params = {
            "provider_instance": self.instance_id,
        }
        if media_types is None or MediaType.TRACK in media_types:
            result.tracks = await self.mass.music.tracks._get_library_items_by_query(
                search=search_query, extra_query=query, extra_query_params=params, limit=limit
            )

        if media_types is None or MediaType.ALBUM in media_types:
            result.albums = await self.mass.music.albums._get_library_items_by_query(
                search=search_query, extra_query=query, extra_query_params=params, limit=limit
            )

        if media_types is None or MediaType.ARTIST in media_types:
            result.artists = await self.mass.music.artists._get_library_items_by_query(
                search=search_query, extra_query=query, extra_query_params=params, limit=limit
            )
        if media_types is None or MediaType.PLAYLIST in media_types:
            result.playlists = await self.mass.music.playlists._get_library_items_by_query(
                search=search_query, extra_query=query, extra_query_params=params, limit=limit
            )
        return result

    async def browse(self, path: str, offset: int, limit: int) -> list[MediaItemType]:
        """Browse this provider's items.

        :param path: The path to browse, (e.g. provid://artists).
        """
        if offset:
            # we do not support pagination
            return []
        items: list[MediaItemType] = []
        item_path = path.split("://", 1)[1]
        if not item_path:
            item_path = ""
        async for item in self.listdir(item_path, recursive=False):
            if not item.is_dir and ("." not in item.filename or not item.ext):
                # skip system files and files without extension
                continue

            if item.is_dir:
                items.append(
                    BrowseFolder(
                        item_id=item.path,
                        provider=self.instance_id,
                        path=f"{self.instance_id}://{item.path}",
                        name=item.filename,
                    )
                )
            elif item.ext in TRACK_EXTENSIONS:
                items.append(
                    ItemMapping(
                        media_type=MediaType.TRACK,
                        item_id=item.path,
                        provider=self.instance_id,
                        name=item.filename,
                    )
                )
            elif item.ext in PLAYLIST_EXTENSIONS:
                items.append(
                    ItemMapping(
                        media_type=MediaType.PLAYLIST,
                        item_id=item.path,
                        provider=self.instance_id,
                        name=item.filename,
                    )
                )
        return items

    async def sync_library(self, media_types: tuple[MediaType, ...]) -> None:
        """Run library sync for this provider."""
        file_checksums: dict[str, str] = {}
        query = (
            f"SELECT provider_item_id, details FROM {DB_TABLE_PROVIDER_MAPPINGS} "
            f"WHERE provider_instance = '{self.instance_id}' "
            "AND media_type in ('track', 'playlist')"
        )
        for db_row in await self.mass.music.database.get_rows_from_query(query, limit=0):
            file_checksums[db_row["provider_item_id"]] = str(db_row["details"])
        # find all music files in the music directory and all subfolders
        # we work bottom up, as-in we derive all info from the tracks
        cur_filenames = set()
        prev_filenames = set(file_checksums.keys())
        async for item in self.listdir("", recursive=True):
            if "." not in item.filename or not item.ext:
                # skip system files and files without extension
                continue

            if item.ext not in SUPPORTED_EXTENSIONS:
                # unsupported file extension
                continue

            cur_filenames.add(item.path)
            try:
                # continue if the item did not change (checksum still the same)
                if item.checksum == file_checksums.get(item.path):
                    continue
                self.logger.debug("Processing: %s", item.path)
                if item.ext in TRACK_EXTENSIONS:
                    # add/update track to db
                    # note that filesystem items are always overwriting existing info
                    # when they are detected as changed
                    track = await self._parse_track(item)
                    await self.mass.music.tracks.add_item_to_library(
                        track, metadata_lookup=False, overwrite_existing=True
                    )
                elif item.ext in PLAYLIST_EXTENSIONS:
                    playlist = await self.get_playlist(item.path)
                    # add/update] playlist to db
                    playlist.metadata.cache_checksum = item.checksum
                    # playlist is always favorite
                    playlist.favorite = True
                    await self.mass.music.playlists.add_item_to_library(
                        playlist, metadata_lookup=False, overwrite_existing=True
                    )
            except Exception as err:  # pylint: disable=broad-except
                # we don't want the whole sync to crash on one file so we catch all exceptions here
                self.logger.error(
                    "Error processing %s - %s",
                    item.path,
                    str(err),
                    exc_info=err if self.logger.isEnabledFor(logging.DEBUG) else None,
                )

        # work out deletions
        deleted_files = prev_filenames - cur_filenames
        await self._process_deletions(deleted_files)

        # process orphaned albums and artists
        await self._process_orphaned_albums_and_artists()

    async def _process_orphaned_albums_and_artists(self) -> None:
        """Process deletion of orphaned albums and artists."""
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
            if ext not in SUPPORTED_EXTENSIONS:
                # unsupported file extension
                continue

            if ext in PLAYLIST_EXTENSIONS:
                controller = self.mass.music.get_controller(MediaType.PLAYLIST)
            else:
                controller = self.mass.music.get_controller(MediaType.TRACK)

            if library_item := await controller.get_library_item_by_prov_id(
                file_path, self.instance_id
            ):
                if library_item.media_type == MediaType.TRACK:
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
                await self.mass.music.artists.remove_item_from_library(album_id)

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        db_artist = await self.mass.music.artists.get_library_item_by_prov_id(
            prov_artist_id, self.instance_id
        )
        if db_artist is None:
            msg = f"Artist not found: {prov_artist_id}"
            raise MediaNotFoundError(msg)
        if await self.exists(prov_artist_id):
            # if path exists on disk allow parsing full details to allow refresh of metadata
            return await self._parse_artist(db_artist.name, artist_path=prov_artist_id)
        return db_artist

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        for track in await self.get_album_tracks(prov_album_id):
            for prov_mapping in track.provider_mappings:
                if prov_mapping.provider_instance == self.instance_id:
                    full_track = await self.get_track(prov_mapping.item_id)
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
        return await self._parse_track(file_item)

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        if not await self.exists(prov_playlist_id):
            msg = f"Playlist path does not exist: {prov_playlist_id}"
            raise MediaNotFoundError(msg)

        file_item = await self.resolve(prov_playlist_id)
        playlist = Playlist(
            item_id=file_item.path,
            provider=self.instance_id,
            name=file_item.name,
            provider_mappings={
                ProviderMapping(
                    item_id=file_item.path,
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
        checksum = f"{DB_SCHEMA_VERSION}.{file_item.checksum}"
        playlist.metadata.cache_checksum = checksum
        return playlist

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

    async def get_playlist_tracks(
        self, prov_playlist_id: str, offset: int, limit: int
    ) -> list[Track]:
        """Get playlist tracks."""
        result: list[Track] = []
        if not await self.exists(prov_playlist_id):
            msg = f"Playlist path does not exist: {prov_playlist_id}"
            raise MediaNotFoundError(msg)

        _, ext = prov_playlist_id.rsplit(".", 1)
        try:
            # get playlist file contents
            playlist_data = b""
            async for chunk in self.read_file_content(prov_playlist_id):
                playlist_data += chunk
            encoding_details = await asyncio.to_thread(cchardet.detect, playlist_data)
            playlist_data = playlist_data.decode(encoding_details["encoding"] or "utf-8")

            if ext in ("m3u", "m3u8"):
                playlist_lines = parse_m3u(playlist_data)
            else:
                playlist_lines = parse_pls(playlist_data)

            playlist_lines = playlist_lines[offset : offset + limit]

            for line_no, playlist_line in enumerate(playlist_lines):
                if track := await self._parse_playlist_line(
                    playlist_line.path, os.path.dirname(prov_playlist_id)
                ):
                    track.position = offset + line_no
                    result.append(track)

        except Exception as err:  # pylint: disable=broad-except
            self.logger.warning(
                "Error while parsing playlist %s: %s",
                prov_playlist_id,
                str(err),
                exc_info=err if self.logger.isEnabledFor(10) else None,
            )
        return result

    async def _parse_playlist_line(self, line: str, playlist_path: str) -> Track | None:
        """Try to parse a track from a playlist line."""
        try:
            # if a relative path was given in an upper level from the playlist,
            # try to resolve it
            for parentpart in ("../", "..\\"):
                while line.startswith(parentpart):
                    if len(playlist_path) < 3:
                        break  # guard
                    playlist_path = parentpart[:-3]
                    line = line[3:]

            # try to resolve the filename
            for filename in (line, os.path.join(playlist_path, line)):
                with contextlib.suppress(FileNotFoundError):
                    item = await self.resolve(filename)
                    return await self._parse_track(item)

        except MusicAssistantError as err:
            self.logger.warning("Could not parse uri/file %s to track: %s", line, str(err))
            return None

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add track(s) to playlist."""
        if not await self.exists(prov_playlist_id):
            msg = f"Playlist path does not exist: {prov_playlist_id}"
            raise MediaNotFoundError(msg)
        playlist_data = b""
        async for chunk in self.read_file_content(prov_playlist_id):
            playlist_data += chunk
        encoding_details = await asyncio.to_thread(cchardet.detect, playlist_data)
        playlist_data = playlist_data.decode(encoding_details["encoding"] or "utf-8")
        for file_path in prov_track_ids:
            track = await self.get_track(file_path)
            playlist_data += f"\n#EXTINF:{track.duration or 0},{track.name}\n{file_path}\n"

        # write playlist file (always in utf-8)
        await self.write_file_content(prov_playlist_id, playlist_data.encode("utf-8"))

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
        if not await self.exists(prov_playlist_id):
            msg = f"Playlist path does not exist: {prov_playlist_id}"
            raise MediaNotFoundError(msg)
        _, ext = prov_playlist_id.rsplit(".", 1)
        # get playlist file contents
        playlist_data = b""
        async for chunk in self.read_file_content(prov_playlist_id):
            playlist_data += chunk
        encoding_details = await asyncio.to_thread(cchardet.detect, playlist_data)
        playlist_data = playlist_data.decode(encoding_details["encoding"] or "utf-8")
        # get current contents first
        if ext in ("m3u", "m3u8"):
            playlist_items = parse_m3u(playlist_data)
        else:
            playlist_items = parse_pls(playlist_data)
        # remove items by index
        for i in sorted(positions_to_remove, reverse=True):
            del playlist_items[i]
        # build new playlist data
        new_playlist_data = "#EXTM3U\n"
        for item in playlist_items:
            new_playlist_data += f"\n#EXTINF:{item.length or 0},{item.title}\n{item.path}\n"
        await self.write_file_content(prov_playlist_id, new_playlist_data.encode("utf-8"))

    async def create_playlist(self, name: str) -> Playlist:
        """Create a new playlist on provider with given name."""
        # creating a new playlist on the filesystem is as easy
        # as creating a new (empty) file with the m3u extension...
        # filename = await self.resolve(f"{name}.m3u")
        filename = f"{name}.m3u"
        await self.write_file_content(filename, b"#EXTM3U\n")
        return await self.get_playlist(filename)

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        library_item = await self.mass.music.tracks.get_library_item_by_prov_id(
            item_id, self.instance_id
        )
        if library_item is None:
            # this could be a file that has just been added, try parsing it
            file_item = await self.resolve(item_id)
            if not (library_item := await self._parse_track(file_item)):
                msg = f"Item not found: {item_id}"
                raise MediaNotFoundError(msg)

        prov_mapping = next(x for x in library_item.provider_mappings if x.item_id == item_id)
        file_item = await self.resolve(item_id)

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=prov_mapping.audio_format,
            media_type=MediaType.TRACK,
            stream_type=StreamType.LOCAL_FILE if file_item.local_path else StreamType.CUSTOM,
            duration=library_item.duration,
            size=file_item.file_size,
            data=file_item,
            path=file_item.local_path,
            can_seek=prov_mapping.audio_format.content_type in SEEKABLE_FILES,
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Return the audio stream for the provider item."""
        if seek_position:
            assert streamdetails.duration, "Duration required for seek requests"
            assert streamdetails.size, "Filesize required for seek requests"
            seek_bytes = int((streamdetails.size / streamdetails.duration) * seek_position)
        else:
            seek_bytes = 0

        async for chunk in self.read_file_content(streamdetails.item_id, seek_bytes):
            yield chunk

    async def resolve_image(self, path: str) -> str | bytes:
        """
        Resolve an image from an image path.

        This either returns (a generator to get) raw bytes of the image or
        a string with an http(s) URL or local path that is accessible from the server.
        """
        file_item = await self.resolve(path)
        if file_item.local_path:
            return file_item.local_path
        return file_item.absolute_path

    async def _parse_track(self, file_item: FileSystemItem) -> Track:
        """Get full track details by id."""
        # ruff: noqa: PLR0915, PLR0912

        # parse tags
        input_file = file_item.local_path or self.read_file_content(file_item.absolute_path)
        tags = await parse_tags(input_file, file_item.file_size)
        name, version = parse_title_and_version(tags.title, tags.version)
        track = Track(
            item_id=file_item.path,
            provider=self.instance_id,
            name=name,
            sort_name=tags.title_sort,
            version=version,
            provider_mappings={
                ProviderMapping(
                    item_id=file_item.path,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(
                        content_type=ContentType.try_parse(tags.format),
                        sample_rate=tags.sample_rate,
                        bit_depth=tags.bits_per_sample,
                        channels=tags.channels,
                        bit_rate=tags.bit_rate,
                    ),
                    details=file_item.checksum,
                )
            },
            disc_number=tags.disc,
            track_number=tags.track,
        )

        if isrc_tags := tags.isrc:
            for isrsc in isrc_tags:
                track.external_ids.add((ExternalID.ISRC, isrsc))

        if acoustid := tags.get("acoustidid"):
            track.external_ids.add((ExternalID.ACOUSTID, acoustid))

        # album
        if tags.album:
            # work out if we have an album and/or disc folder
            # disc_dir is the folder level where the tracks are located
            # this may be a separate disc folder (Disc 1, Disc 2 etc) underneath the album folder
            # or this is an album folder with the disc attached
            disc_dir = get_parentdir(file_item.path, f"disc {tags.disc or 1}")
            album_dir = get_parentdir(disc_dir or file_item.path, tags.album)

            # album artist(s)
            album_artists = []
            if tags.album_artists:
                for index, album_artist_str in enumerate(tags.album_artists):
                    # work out if we have an artist folder
                    artist_dir = get_parentdir(album_dir, album_artist_str, 1)
                    artist = await self._parse_artist(album_artist_str, artist_path=artist_dir)
                    if not artist.mbid:
                        with contextlib.suppress(IndexError):
                            artist.mbid = tags.musicbrainz_albumartistids[index]
                    # album artist sort name
                    with contextlib.suppress(IndexError):
                        artist.sort_name = tags.album_artist_sort_names[index]
                    album_artists.append(artist)
            else:
                # album artist tag is missing, determine fallback
                fallback_action = self.config.get_value(CONF_MISSING_ALBUM_ARTIST_ACTION)
                if fallback_action == "folder_name" and album_dir:
                    possible_artist_folder = os.path.dirname(album_dir)
                    self.logger.warning(
                        "%s is missing ID3 tag [albumartist], using foldername %s as fallback",
                        file_item.path,
                        possible_artist_folder,
                    )
                    album_artist_str = possible_artist_folder.rsplit(os.sep)[-1]
                    album_artists = [await self._parse_artist(name=album_artist_str)]
                # fallback to track artists (if defined by user)
                elif fallback_action == "track_artist":
                    self.logger.warning(
                        "%s is missing ID3 tag [albumartist], using track artist(s) as fallback",
                        file_item.path,
                    )
                    album_artists = [
                        await self._parse_artist(name=track_artist_str)
                        for track_artist_str in tags.artists
                    ]
                # all other: fallback to various artists
                else:
                    self.logger.warning(
                        "%s is missing ID3 tag [albumartist], using %s as fallback",
                        file_item.path,
                        VARIOUS_ARTISTS_NAME,
                    )
                    album_artists = [await self._parse_artist(name=VARIOUS_ARTISTS_NAME)]

            track.album = await self._parse_album(
                tags.album,
                album_dir,
                disc_dir,
                artists=album_artists,
                barcode=tags.barcode,
            )

        # track artist(s)
        for index, track_artist_str in enumerate(tags.artists):
            # reuse album artist details if possible
            if track.album and (
                album_artist := next(
                    (x for x in track.album.artists if x.name == track_artist_str), None
                )
            ):
                artist = album_artist
            else:
                artist = await self._parse_artist(track_artist_str)
            if not artist.mbid:
                with contextlib.suppress(IndexError):
                    artist.mbid = tags.musicbrainz_artistids[index]
            # artist sort name
            with contextlib.suppress(IndexError):
                artist.sort_name = tags.artist_sort_names[index]
            track.artists.append(artist)

        # handle embedded cover image
        if tags.has_cover_image:
            # we do not actually embed the image in the metadata because that would consume too
            # much space and bandwidth. Instead we set the filename as value so the image can
            # be retrieved later in realtime.
            track.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=file_item.path,
                    provider=self.instance_id,
                    remotely_accessible=False,
                )
            ]

        if track.album and not track.album.metadata.images:
            # set embedded cover on album if it does not have one yet
            track.album.metadata.images = track.metadata.images
        # copy album image from track (only if the album itself doesn't have an image)
        # this deals with embedded images from filesystem providers
        if track.album and not track.album.image and track.image:
            track.album.metadata.images = [track.image]

        # parse other info
        track.duration = tags.duration or 0
        track.metadata.genres = set(tags.genres)
        track.disc_number = tags.disc
        track.track_number = tags.track
        track.metadata.copyright = tags.get("copyright")
        track.metadata.lyrics = tags.lyrics
        explicit_tag = tags.get("itunesadvisory")
        if explicit_tag is not None:
            track.metadata.explicit = explicit_tag == "1"
        track.mbid = tags.musicbrainz_recordingid
        track.metadata.chapters = tags.chapters
        if track.album:
            if not track.album.mbid:
                track.album.mbid = tags.musicbrainz_releasegroupid
            if not track.album.year:
                track.album.year = tags.year
            track.album.album_type = tags.album_type
            track.album.metadata.explicit = track.metadata.explicit
        # set checksum to invalidate any cached listings
        track.metadata.cache_checksum = file_item.checksum
        if track.album:
            # use track checksum for album(artists) too
            track.album.metadata.cache_checksum = track.metadata.cache_checksum
            for artist in track.album.artists:
                artist.metadata.cache_checksum = track.metadata.cache_checksum

        return track

    async def _parse_artist(
        self,
        name: str | None = None,
        artist_path: str | None = None,
        sort_name: str | None = None,
    ) -> Artist | None:
        """Lookup metadata in Artist folder."""
        assert name or artist_path
        if not artist_path:
            # check if we have an existing item
            async for item in self.mass.music.artists.iter_library_items(search=name):
                if not compare_strings(name, item.name):
                    continue
                for prov_mapping in item.provider_mappings:
                    if prov_mapping.provider_instance == self.instance_id:
                        artist_path = prov_mapping.url
                        break
                if artist_path:
                    break
            else:
                # check if we have an artist folder for this artist at root level
                if await self.exists(name):
                    artist_path = name
                elif await self.exists(name.title()):
                    artist_path = name.title()
                else:
                    # use fake artist path as item id which is just the name
                    artist_path = name

        if not name:
            name = artist_path.split(os.sep)[-1]

        artist = Artist(
            item_id=artist_path,
            provider=self.instance_id,
            name=name,
            sort_name=sort_name,
            provider_mappings={
                ProviderMapping(
                    item_id=artist_path,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=artist_path,
                )
            },
        )

        if not await self.exists(artist_path):
            # return basic object if there is no dedicated artist folder
            return artist

        nfo_file = os.path.join(artist_path, "artist.nfo")
        if await self.exists(nfo_file):
            # found NFO file with metadata
            # https://kodi.wiki/view/NFO_files/Artists
            data = b""
            async for chunk in self.read_file_content(nfo_file):
                data += chunk
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
        if images := await self._get_local_images(artist_path):
            artist.metadata.images = images

        return artist

    async def _parse_album(
        self,
        name: str | None,
        album_path: str | None,
        disc_path: str | None,
        artists: list[Artist],
        barcode: str | None = None,
        sort_name: str | None = None,
    ) -> Album | None:
        """Lookup metadata in Album folder."""
        assert name or album_path
        # create fake path if needed
        if not album_path and artists:
            album_path = artists[0].name + os.sep + name
        elif not album_path:
            album_path = name

        if not name:
            name = album_path.split(os.sep)[-1]

        album = Album(
            item_id=album_path,
            provider=self.instance_id,
            name=name,
            sort_name=sort_name,
            artists=artists,
            provider_mappings={
                ProviderMapping(
                    item_id=album_path,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=album_path,
                )
            },
        )
        if barcode:
            album.external_ids.add((ExternalID.BARCODE, barcode))

        if not await self.exists(album_path):
            # return basic object if there is no dedicated album folder
            return album

        for folder_path in (disc_path, album_path):
            if not folder_path:
                continue
            nfo_file = os.path.join(folder_path, "album.nfo")
            if await self.exists(nfo_file):
                # found NFO file with metadata
                # https://kodi.wiki/view/NFO_files/Artists
                data = b""
                async for chunk in self.read_file_content(nfo_file):
                    data += chunk
                info = await asyncio.to_thread(xmltodict.parse, data)
                info = info["album"]
                album.name = info.get("title", info.get("name", name))
                if sort_name := info.get("sortname"):
                    album.sort_name = sort_name
                if mbid := info.get("musicbrainzreleasegroupid"):
                    album.mbid = mbid
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
            if images := await self._get_local_images(folder_path):
                if album.metadata.images is None:
                    album.metadata.images = images
                else:
                    album.metadata.images += images

        return album

    @use_cache(120)
    async def _get_local_images(self, folder: str) -> list[MediaItemImage]:
        """Return local images found in a given folderpath."""
        images = []
        async for item in self.listdir(folder):
            if "." not in item.path or item.is_dir:
                continue
            for ext in IMAGE_EXTENSIONS:
                if item.ext != ext:
                    continue
                # try match on filename = one of our imagetypes
                if item.name in ImageType:
                    images.append(
                        MediaItemImage(
                            type=ImageType(item.name),
                            path=item.path,
                            provider=self.instance_id,
                            remotely_accessible=False,
                        )
                    )
                    continue
                # try alternative names for thumbs
                for filename in ("folder", "cover", "albumart", "artist"):
                    if item.name.lower().startswith(filename):
                        images.append(
                            MediaItemImage(
                                type=ImageType.THUMB,
                                path=item.path,
                                provider=self.instance_id,
                                remotely_accessible=False,
                            )
                        )
                        break
        return images
