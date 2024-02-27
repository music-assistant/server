"""Filesystem musicprovider support for MusicAssistant."""

from __future__ import annotations

import asyncio
import contextlib
import os
from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

import cchardet
import xmltodict

from music_assistant.common.helpers.util import create_sort_name, parse_title_and_version
from music_assistant.common.models.config_entries import (
    ConfigEntry,
    ConfigEntryType,
    ConfigValueOption,
)
from music_assistant.common.models.enums import ExternalID, ProviderFeature
from music_assistant.common.models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    MusicAssistantError,
)
from music_assistant.common.models.media_items import (
    Album,
    AlbumTrack,
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
    PlaylistTrack,
    ProviderMapping,
    SearchResults,
    StreamDetails,
    Track,
)
from music_assistant.constants import VARIOUS_ARTISTS_NAME
from music_assistant.server.controllers.cache import use_cache
from music_assistant.server.controllers.music import DB_SCHEMA_VERSION
from music_assistant.server.helpers.compare import compare_strings
from music_assistant.server.helpers.playlists import parse_m3u, parse_pls
from music_assistant.server.helpers.tags import parse_tags, split_items
from music_assistant.server.models.music_provider import MusicProvider

from .helpers import get_parentdir

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant.server.providers.musicbrainz import MusicbrainzProvider

CONF_MISSING_ALBUM_ARTIST_ACTION = "missing_album_artist_action"

CONF_ENTRY_MISSING_ALBUM_ARTIST = ConfigEntry(
    key=CONF_MISSING_ALBUM_ARTIST_ACTION,
    type=ConfigEntryType.STRING,
    label="Action when a track is missing the Albumartist ID3 tag",
    default_value="skip",
    description="Music Assistant prefers information stored in ID3 tags and only uses"
    " online sources for additional metadata. This means that the ID3 tags need to be "
    "accurate, preferably tagged with MusicBrainz Picard.",
    advanced=False,
    required=False,
    options=(
        ConfigValueOption("Skip track and log warning", "skip"),
        ConfigValueOption("Use Track artist(s)", "track_artist"),
        ConfigValueOption("Use Various Artists", "various_artists"),
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
    ProviderFeature.PLAYLIST_TRACKS_EDIT,
    ProviderFeature.PLAYLIST_CREATE,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
)


@dataclass
class FileSystemItem:
    """Representation of an item (file or directory) on the filesystem.

    - name: Name (not path) of the file (or directory).
    - path: Relative path to the item on this filesystem provider.
    - absolute_path: Absolute (provider dependent) path to this item.
    - is_file: Boolean if item is file (not directory or symlink).
    - is_dir: Boolean if item is directory (not file).
    - checksum: Checksum for this path (usually last modified time).
    - file_size : File size in number of bytes or None if unknown (or not a file).
    - local_path: Optional local accessible path to this (file)item, supported by ffmpeg.
    """

    name: str
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
            return self.name.rsplit(".", 1)[1]
        except IndexError:
            return None


class FileSystemProviderBase(MusicProvider):
    """Base Implementation of a musicprovider for files.

    Reads ID3 tags from file and falls back to parsing filename.
    Optionally reads metadata from nfo files and images in folder structure <artist>/<album>.
    Supports m3u files only for playlists.
    Supports having URI's from streaming providers within m3u playlist.
    """

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return SUPPORTED_FEATURES

    @abstractmethod
    async def async_setup(self) -> None:
        """Handle async initialization of the provider."""

    @abstractmethod
    async def listdir(
        self, path: str, recursive: bool = False
    ) -> AsyncGenerator[FileSystemItem, None]:
        """List contents of a given provider directory/path.

        Parameters
        ----------
            - path: path of the directory (relative or absolute) to list contents of.
              Empty string for provider's root.
            - recursive: If True will recursively keep unwrapping subdirectories (scandir equivalent).

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
        # instead we make some (slow) freaking queries to the db ;-)
        params = {
            "name": f"%{search_query}%",
            "provider_instance": f"%{self.instance_id}%",
        }
        # ruff: noqa: E501
        if media_types is None or MediaType.TRACK in media_types:
            query = (
                "WHERE tracks.name LIKE :name AND tracks.provider_mappings LIKE :provider_instance"
            )
            result.tracks = (
                await self.mass.music.tracks.library_items(
                    extra_query=query, extra_query_params=params
                )
            ).items
        if media_types is None or MediaType.ALBUM in media_types:
            query = "WHERE name LIKE :name AND provider_mappings LIKE :provider_instance"
            result.albums = (
                await self.mass.music.albums.library_items(
                    extra_query=query, extra_query_params=params
                )
            ).items
        if media_types is None or MediaType.ARTIST in media_types:
            query = "WHERE name LIKE :name AND provider_mappings LIKE :provider_instance"
            result.artists = (
                await self.mass.music.artists.library_items(
                    extra_query=query, extra_query_params=params
                )
            ).items
        if media_types is None or MediaType.PLAYLIST in media_types:
            query = "WHERE name LIKE :name AND provider_mappings LIKE :provider_instance"
            result.playlists = (
                await self.mass.music.playlists.library_items(
                    extra_query=query, extra_query_params=params
                )
            ).items
        return result

    async def browse(self, path: str) -> AsyncGenerator[MediaItemType, None]:
        """Browse this provider's items.

        :param path: The path to browse, (e.g. provid://artists).
        """
        item_path = path.split("://", 1)[1]
        if not item_path:
            item_path = ""
        async for item in self.listdir(item_path, recursive=False):
            if item.is_dir:
                yield BrowseFolder(
                    item_id=item.path,
                    provider=self.instance_id,
                    path=f"{self.instance_id}://{item.path}",
                    name=item.name,
                )
                continue

            if "." not in item.name or not item.ext:
                # skip system files and files without extension
                continue

            if item.ext in TRACK_EXTENSIONS:
                yield ItemMapping(
                    media_type=MediaType.TRACK,
                    item_id=item.path,
                    provider=self.instance_id,
                    name=item.name,
                )
                continue
            if item.ext in PLAYLIST_EXTENSIONS:
                yield ItemMapping(
                    media_type=MediaType.PLAYLIST,
                    item_id=item.path,
                    provider=self.instance_id,
                    name=item.name,
                )

    async def sync_library(self, media_types: tuple[MediaType, ...]) -> None:
        """Run library sync for this provider."""
        # first build a listing of all current items and their checksums
        prev_checksums = {}
        for ctrl in (self.mass.music.tracks, self.mass.music.playlists):
            async for db_item in ctrl.iter_library_items_by_prov_id(self.instance_id):
                file_name = next(
                    x.item_id
                    for x in db_item.provider_mappings
                    if x.provider_instance == self.instance_id
                )
                prev_checksums[file_name] = db_item.metadata.checksum

        # process all deleted (or renamed) files first
        cur_filenames = set()
        async for item in self.listdir("", recursive=True):
            if "." not in item.name or not item.ext:
                # skip system files and files without extension
                continue

            if item.ext not in SUPPORTED_EXTENSIONS:
                # unsupported file extension
                continue
            cur_filenames.add(item.path)
        # work out deletions
        deleted_files = set(prev_checksums.keys()) - cur_filenames
        await self._process_deletions(deleted_files)

        # find all music files in the music directory and all subfolders
        # we work bottom up, as-in we derive all info from the tracks
        async for item in self.listdir("", recursive=True):
            if "." not in item.name or not item.ext:
                # skip system files and files without extension
                continue

            if item.ext not in SUPPORTED_EXTENSIONS:
                # unsupported file extension
                continue

            try:
                # continue if the item did not change (checksum still the same)
                if item.checksum == prev_checksums.get(item.path):
                    continue

                if item.ext in TRACK_EXTENSIONS:
                    # add/update track to db
                    track = await self._parse_track(item)
                    await self.mass.music.tracks.add_item_to_library(track, metadata_lookup=False)
                elif item.ext in PLAYLIST_EXTENSIONS:
                    playlist = await self.get_playlist(item.path)
                    # add/update] playlist to db
                    playlist.metadata.checksum = item.checksum
                    # playlist is always in-library
                    playlist.favorite = True
                    await self.mass.music.playlists.add_item_to_library(
                        playlist, metadata_lookup=False
                    )
            except Exception as err:  # pylint: disable=broad-except
                # we don't want the whole sync to crash on one file so we catch all exceptions here
                self.logger.error("Error processing %s - %s", item.path, str(err))

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
            if not self.mass.music.albums.tracks(album_id, "library"):
                await self.mass.music.albums.remove_item_from_library(album_id)
        # check if any artists need to be cleaned up
        for artist_id in artist_ids:
            artist_albums = await self.mass.music.artists.albums(artist_id, "library")
            artist_tracks = self.mass.music.artists.tracks(artist_id, "library")
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
        # all data is originated from the actual files (tracks) so grab the data from there
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
            name=file_item.name.replace(f".{file_item.ext}", ""),
            provider_mappings={
                ProviderMapping(
                    item_id=file_item.path,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        playlist.is_editable = file_item.ext != "pls"  # can only edit m3u playlists
        playlist.owner = self.name
        checksum = f"{DB_SCHEMA_VERSION}.{file_item.checksum}"
        playlist.metadata.checksum = checksum
        return playlist

    async def get_album_tracks(self, prov_album_id: str) -> list[AlbumTrack]:
        """Get album tracks for given album id."""
        # filesystem items are always stored in db so we can query the database
        db_album = await self.mass.music.albums.get_library_item_by_prov_id(
            prov_album_id, self.instance_id
        )
        if db_album is None:
            msg = f"Album not found: {prov_album_id}"
            raise MediaNotFoundError(msg)
        album_tracks = await self.mass.music.albums.tracks(db_album.item_id, db_album.provider)
        return [
            track
            for track in album_tracks
            if any(x.provider_instance == self.instance_id for x in track.provider_mappings)
        ]

    async def get_playlist_tracks(
        self, prov_playlist_id: str
    ) -> AsyncGenerator[PlaylistTrack, None]:
        """Get playlist tracks for given playlist id."""
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
                playlist_lines = await parse_m3u(playlist_data)
            else:
                playlist_lines = await parse_pls(playlist_data)

            for line_no, playlist_line in enumerate(playlist_lines, 1):
                if media_item := await self._parse_playlist_line(
                    playlist_line, os.path.dirname(prov_playlist_id), line_no
                ):
                    yield media_item

        except Exception as err:  # pylint: disable=broad-except
            self.logger.warning(
                "Error while parsing playlist %s: %s",
                prov_playlist_id,
                str(err),
                exc_info=err if self.logger.isEnabledFor(10) else None,
            )

    async def _parse_playlist_line(
        self, line: str, playlist_path: str, position: int
    ) -> PlaylistTrack | None:
        """Try to parse a track from a playlist line."""
        try:
            if "://" in line:
                # handle as generic uri
                media_item = await self.mass.music.get_item_by_uri(line)
                if isinstance(media_item, Track):
                    return PlaylistTrack.from_dict({**media_item.to_dict(), "position": position})

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
                    return await self._parse_track(item, playlist_position=position)

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
        for uri in prov_track_ids:
            playlist_data += f"\n{uri}"

        # write playlist file (always in utf-8)
        await self.write_file_content(prov_playlist_id, playlist_data.encode("utf-8"))

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
        if not await self.exists(prov_playlist_id):
            msg = f"Playlist path does not exist: {prov_playlist_id}"
            raise MediaNotFoundError(msg)
        cur_lines = []
        _, ext = prov_playlist_id.rsplit(".", 1)

        # get playlist file contents
        playlist_data = b""
        async for chunk in self.read_file_content(prov_playlist_id):
            playlist_data += chunk
        encoding_details = await asyncio.to_thread(cchardet.detect, playlist_data)
        playlist_data = playlist_data.decode(encoding_details["encoding"] or "utf-8")

        if ext in ("m3u", "m3u8"):
            playlist_lines = await parse_m3u(playlist_data)
        else:
            playlist_lines = await parse_pls(playlist_data)

        for line_no, playlist_line in enumerate(playlist_lines, 1):
            if line_no not in positions_to_remove:
                cur_lines.append(playlist_line)

        new_playlist_data = "\n".join(cur_lines)
        # write playlist file (always in utf-8)
        await self.write_file_content(prov_playlist_id, new_playlist_data.encode("utf-8"))

    async def create_playlist(self, name: str) -> Playlist:
        """Create a new playlist on provider with given name."""
        # creating a new playlist on the filesystem is as easy
        # as creating a new (empty) file with the m3u extension...
        # filename = await self.resolve(f"{name}.m3u")
        filename = f"{name}.m3u"
        await self.write_file_content(filename, b"")
        return await self.get_playlist(filename)

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        library_item = await self.mass.music.tracks.get_library_item_by_prov_id(
            item_id, self.instance_id
        )
        if library_item is None:
            msg = f"Item not found: {item_id}"
            raise MediaNotFoundError(msg)

        prov_mapping = next(x for x in library_item.provider_mappings if x.item_id == item_id)
        file_item = await self.resolve(item_id)

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=prov_mapping.audio_format,
            media_type=MediaType.TRACK,
            duration=library_item.duration,
            size=file_item.file_size,
            direct=file_item.local_path,
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

    async def resolve_image(self, path: str) -> str | bytes | AsyncGenerator[bytes, None]:
        """
        Resolve an image from an image path.

        This either returns (a generator to get) raw bytes of the image or
        a string with an http(s) URL or local path that is accessible from the server.
        """
        file_item = await self.resolve(path)
        return file_item.local_path or self.read_file_content(file_item.absolute_path)

    async def _parse_track(
        self, file_item: FileSystemItem, playlist_position: int | None = None
    ) -> Track | AlbumTrack | PlaylistTrack:
        """Get full track details by id."""
        # ruff: noqa: PLR0915, PLR0912

        # parse tags
        input_file = file_item.local_path or self.read_file_content(file_item.absolute_path)
        tags = await parse_tags(input_file, file_item.file_size)
        name, version = parse_title_and_version(tags.title, tags.version)
        base_details = {
            "item_id": file_item.path,
            "provider": self.instance_id,
            "name": name,
            "version": version,
            "provider_mappings": {
                ProviderMapping(
                    item_id=file_item.path,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(
                        content_type=ContentType.try_parse(tags.format),
                        sample_rate=tags.sample_rate,
                        bit_depth=tags.bits_per_sample,
                        bit_rate=tags.bit_rate,
                    ),
                )
            },
        }
        if playlist_position is not None:
            track = PlaylistTrack(
                **base_details,
                position=playlist_position,
            )
        elif tags.album and tags.disc and tags.track:
            track = AlbumTrack(  # pylint: disable=missing-kwoa
                **base_details,
                disc_number=tags.disc,
                track_number=tags.track,
            )
        else:
            track = Track(
                **base_details,
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
                musicbrainz: MusicbrainzProvider = self.mass.get_provider("musicbrainz")
                assert musicbrainz
                # lookup track details on musicbrainz first
                if mb_search_details := await musicbrainz.search(
                    tags.artists[0], tags.album, tags.title, tags.version
                ):
                    # get full releasegroup details and get the releasegroup artist(s)
                    mb_details = await musicbrainz.get_releasegroup_details(mb_search_details[1].id)
                    for mb_artist in mb_details.artist_credit:
                        artist = await self._parse_artist(
                            mb_artist.artist.name, mb_artist.artist.sort_name
                        )
                        artist.mbid = mb_artist.artist.id
                        album_artists.append(artist)
                    if not tags.musicbrainz_recordingid:
                        tags.tags["musicbrainzrecordingid"] = mb_search_details[2].id
                    if not tags.musicbrainz_releasegroupid:
                        tags.tags["musicbrainzreleasegroupid"] = mb_search_details[1].id
                # fallback to various artists (if defined by user)
                elif fallback_action == "various_artists":
                    self.logger.warning(
                        "%s is missing ID3 tag [albumartist], using %s as fallback",
                        file_item.path,
                        VARIOUS_ARTISTS_NAME,
                    )
                    album_artists = [await self._parse_artist(name=VARIOUS_ARTISTS_NAME)]
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
                # fallback to just log error and add track without album
                else:
                    # default action is to skip the track
                    msg = "missing ID3 tag [albumartist]"
                    raise InvalidDataError(msg)

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
                MediaItemImage(type=ImageType.THUMB, path=file_item.path, provider=self.instance_id)
            ]

        if track.album and not track.album.metadata.images:
            # set embedded cover on album if it does not have one yet
            track.album.metadata.images = track.metadata.images

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
        track.metadata.checksum = file_item.checksum
        if track.album:
            # use track checksum for album(artists) too
            track.album.metadata.checksum = track.metadata.checksum
            for artist in track.album.artists:
                artist.metadata.checksum = track.metadata.checksum

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
            sort_name=sort_name or create_sort_name(name),
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
            sort_name=sort_name or create_sort_name(name),
            artists=artists,
            provider_mappings={
                ProviderMapping(
                    item_id=album_path,
                    provider_domain=self.instance_id,
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
                try:
                    images.append(
                        MediaItemImage(
                            type=ImageType(item.name),
                            path=item.path,
                            provider=self.instance_id,
                        )
                    )
                except ValueError:
                    for filename in ("folder", "cover", "albumart", "artist"):
                        if item.name.lower().startswith(filename):
                            images.append(
                                MediaItemImage(
                                    type=ImageType.THUMB,
                                    path=item.path,
                                    provider=self.instance_id,
                                )
                            )
                            break
        return images
