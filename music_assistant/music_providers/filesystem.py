"""Filesystem musicprovider support for MusicAssistant."""
from __future__ import annotations

import asyncio
import logging
import os
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path
from time import time
from typing import AsyncGenerator, List, Optional, Set, Tuple

import aiofiles
import xmltodict
from aiofiles.os import wrap
from aiofiles.threadpool.binary import AsyncFileIO

from music_assistant.helpers.compare import compare_strings
from music_assistant.helpers.tags import FALLBACK_ARTIST, parse_tags, split_items
from music_assistant.helpers.util import create_safe_string, parse_title_and_version
from music_assistant.models.enums import MusicProviderFeature, ProviderType
from music_assistant.models.errors import MediaNotFoundError, MusicAssistantError
from music_assistant.models.media_items import (
    Album,
    AlbumType,
    Artist,
    BrowseFolder,
    ContentType,
    ImageType,
    MediaItemImage,
    MediaItemProviderId,
    MediaItemType,
    MediaQuality,
    MediaType,
    Playlist,
    Radio,
    StreamDetails,
    Track,
)
from music_assistant.models.music_provider import MusicProvider

TRACK_EXTENSIONS = ("mp3", "m4a", "mp4", "flac", "wav", "ogg", "aiff", "wma", "dsf")
PLAYLIST_EXTENSIONS = ("m3u",)
SUPPORTED_EXTENSIONS = TRACK_EXTENSIONS + PLAYLIST_EXTENSIONS
SCHEMA_VERSION = 17
LOGGER = logging.getLogger(__name__)

listdir = wrap(os.listdir)
isdir = wrap(os.path.isdir)
isfile = wrap(os.path.isfile)


async def scantree(path: str) -> AsyncGenerator[os.DirEntry, None]:
    """Recursively yield DirEntry objects for given directory."""

    def is_dir(entry: os.DirEntry) -> bool:
        return entry.is_dir(follow_symlinks=False)

    loop = asyncio.get_running_loop()
    for entry in await loop.run_in_executor(None, os.scandir, path):
        if entry.name.startswith("."):
            continue
        if await loop.run_in_executor(None, is_dir, entry):
            try:
                async for subitem in scantree(entry.path):
                    yield subitem
            except (OSError, PermissionError) as err:
                LOGGER.warning("Skip folder %s: %s", entry.path, str(err))
        else:
            yield entry


def get_parentdir(base_path: str, name: str) -> str | None:
    """Look for folder name in path (to find dedicated artist or album folder)."""
    parentdir = os.path.dirname(base_path)
    for _ in range(3):
        dirname = parentdir.rsplit(os.sep)[-1]
        if compare_strings(name, dirname, False):
            return parentdir
        parentdir = os.path.dirname(parentdir)
    return None


class FileSystemProvider(MusicProvider):
    """
    Implementation of a musicprovider for local files.

    Reads ID3 tags from file and falls back to parsing filename.
    Optionally reads metadata from nfo files and images in folder structure <artist>/<album>.
    Supports m3u files only for playlists.
    Supports having URI's from streaming providers within m3u playlist.
    """

    _attr_name = "Filesystem"
    _attr_type = ProviderType.FILESYSTEM_LOCAL

    @property
    def supported_features(self) -> Tuple[MusicProviderFeature]:
        """Return the features supported by this MusicProvider."""
        return (
            MusicProviderFeature.LIBRARY_ARTISTS,
            MusicProviderFeature.LIBRARY_ALBUMS,
            MusicProviderFeature.LIBRARY_TRACKS,
            MusicProviderFeature.LIBRARY_PLAYLISTS,
            MusicProviderFeature.PLAYLIST_TRACKS_EDIT,
            MusicProviderFeature.PLAYLIST_CREATE,
            MusicProviderFeature.BROWSE,
            MusicProviderFeature.SEARCH,
        )

    async def setup(self) -> bool:
        """Handle async initialization of the provider."""

        if not await isdir(self.config.path):
            raise MediaNotFoundError(
                f"Music Directory {self.config.path} does not exist"
            )

        return True

    async def search(
        self, search_query: str, media_types=Optional[List[MediaType]], limit: int = 5
    ) -> List[MediaItemType]:
        """Perform search on musicprovider."""
        result = []
        # searching the filesystem is slow and unreliable,
        # instead we make some (slow) freaking queries to the db ;-)
        params = {"name": f"%{search_query}%", "prov_type": f"%{self.type.value}%"}
        if media_types is None or MediaType.TRACK in media_types:
            query = "SELECT * FROM tracks WHERE name LIKE :name AND provider_ids LIKE :prov_type"
            tracks = await self.mass.music.tracks.get_db_items_by_query(query, params)
            result += tracks
        if media_types is None or MediaType.ALBUM in media_types:
            query = "SELECT * FROM albums WHERE name LIKE :name AND provider_ids LIKE :prov_type"
            albums = await self.mass.music.albums.get_db_items_by_query(query, params)
            result += albums
        if media_types is None or MediaType.ARTIST in media_types:
            query = "SELECT * FROM artists WHERE name LIKE :name AND provider_ids LIKE :prov_type"
            artists = await self.mass.music.artists.get_db_items_by_query(query, params)
            result += artists
        if media_types is None or MediaType.PLAYLIST in media_types:
            query = "SELECT * FROM playlists WHERE name LIKE :name AND provider_ids LIKE :prov_type"
            playlists = await self.mass.music.playlists.get_db_items_by_query(
                query, params
            )
            result += playlists
        return result

    async def browse(self, path: str) -> BrowseFolder:
        """
        Browse this provider's items.

            :param path: The path to browse, (e.g. provid://artists).
        """
        _, sub_path = path.split("://")
        if not sub_path:
            item_path = self.config.path
        else:
            item_path = os.path.join(self.config.path, sub_path)
        subitems = []
        for filename in await listdir(item_path):
            full_path: str = os.path.join(item_path, filename)
            rel_path = full_path.replace(self.config.path + os.sep, "")
            if await isdir(full_path):
                subitems.append(
                    BrowseFolder(
                        item_id=rel_path,
                        provider=self.type,
                        path=f"{self.id}://{rel_path}",
                        name=filename,
                    )
                )
                continue

            if "." not in filename or filename.startswith("."):
                # skip system files and files without extension
                continue

            _, ext = filename.rsplit(".", 1)

            if ext in TRACK_EXTENSIONS:
                if track := await self._parse_track(full_path):
                    subitems.append(track)
                continue
            if ext in PLAYLIST_EXTENSIONS:
                if playlist := await self._parse_playlist(full_path):
                    subitems.append(playlist)
                continue

        return BrowseFolder(
            item_id=sub_path,
            provider=self.type,
            path=path,
            name=path.split("://", 1)[-1],
            items=subitems,
        )

    async def sync_library(
        self, media_types: Optional[Tuple[MediaType]] = None
    ) -> None:
        """Run library sync for this provider."""
        cache_key = f"{self.id}.checksums"
        prev_checksums = await self.mass.cache.get(cache_key, SCHEMA_VERSION)
        save_checksum_interval = 0
        if prev_checksums is None:
            prev_checksums = {}

        # find all music files in the music directory and all subfolders
        # we work bottom up, as-in we derive all info from the tracks
        cur_checksums = {}
        async for entry in scantree(self.config.path):

            if "." not in entry.path or entry.path.startswith("."):
                # skip system files and files without extension
                continue

            _, ext = entry.path.rsplit(".", 1)
            if ext not in SUPPORTED_EXTENSIONS:
                # unsupported file extension
                continue

            try:
                # mtime is used as file checksum
                stat = await asyncio.get_running_loop().run_in_executor(
                    None, entry.stat
                )
                checksum = int(stat.st_mtime)
                cur_checksums[entry.path] = checksum
                if checksum == prev_checksums.get(entry.path):
                    continue

                if ext in TRACK_EXTENSIONS:
                    # add/update track to db
                    track = await self._parse_track(entry.path)
                    # if the track was edited on disk, always overwrite existing db details
                    overwrite_existing = entry.path in prev_checksums
                    await self.mass.music.tracks.add_db_item(
                        track, overwrite_existing=overwrite_existing
                    )
                elif ext in PLAYLIST_EXTENSIONS:
                    playlist = await self._parse_playlist(entry.path)
                    # add/update] playlist to db
                    playlist.metadata.checksum = checksum
                    await self.mass.music.playlists.add_db_item(playlist)
            except Exception as err:  # pylint: disable=broad-except
                # we don't want the whole sync to crash on one file so we catch all exceptions here
                self.logger.exception("Error processing %s - %s", entry.path, str(err))

            # save checksums every 100 processed items
            # this allows us to pickup where we leftoff when initial scan gets intterrupted
            if save_checksum_interval == 100:
                await self.mass.cache.set(cache_key, cur_checksums, SCHEMA_VERSION)
                save_checksum_interval = 0
            else:
                save_checksum_interval += 1

        # store (final) checksums in cache
        await self.mass.cache.set(cache_key, cur_checksums, SCHEMA_VERSION)
        # work out deletions
        deleted_files = set(prev_checksums.keys()) - set(cur_checksums.keys())
        await self._process_deletions(deleted_files)

    async def _process_deletions(self, deleted_files: Set[str]) -> None:
        """Process all deletions."""
        # process deleted tracks/playlists
        for file_path in deleted_files:

            if "." not in file_path or file_path.startswith("."):
                # skip system files and files without extension
                continue

            _, ext = file_path.rsplit(".", 1)
            if ext not in SUPPORTED_EXTENSIONS:
                # unsupported file extension
                continue

            item_id = self._get_item_id(file_path)

            if ext in PLAYLIST_EXTENSIONS:
                controller = self.mass.music.get_controller(MediaType.PLAYLIST)
            else:
                controller = self.mass.music.get_controller(MediaType.TRACK)

            if db_item := await controller.get_db_item_by_prov_id(item_id, self.type):
                await controller.remove_prov_mapping(db_item.item_id, self.id)

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        db_artist = await self.mass.music.artists.get_db_item_by_prov_id(
            provider_item_id=prov_artist_id, provider_id=self.id
        )
        if db_artist is None:
            raise MediaNotFoundError(f"Artist not found: {prov_artist_id}")
        itempath = await self._get_filepath(MediaType.ARTIST, prov_artist_id)
        if await self.exists(itempath):
            # if path exists on disk allow parsing full details to allow refresh of metadata
            return await self._parse_artist(db_artist.name, artist_path=itempath)
        return db_artist

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        db_album = await self.mass.music.albums.get_db_item_by_prov_id(
            provider_item_id=prov_album_id, provider_id=self.id
        )
        if db_album is None:
            raise MediaNotFoundError(f"Album not found: {prov_album_id}")
        itempath = await self._get_filepath(MediaType.ALBUM, prov_album_id)
        if await self.exists(itempath):
            # if path exists on disk allow parsing full details to allow refresh of metadata
            return await self._parse_album(db_album.name, itempath, db_album.artists)
        return db_album

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        itempath = await self._get_filepath(MediaType.TRACK, prov_track_id)
        return await self._parse_track(itempath)

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        itempath = await self._get_filepath(MediaType.PLAYLIST, prov_playlist_id)
        return await self._parse_playlist(itempath)

    async def get_album_tracks(self, prov_album_id: str) -> List[Track]:
        """Get album tracks for given album id."""
        # filesystem items are always stored in db so we can query the database
        db_album = await self.mass.music.albums.get_db_item_by_prov_id(
            prov_album_id, provider_id=self.id
        )
        if db_album is None:
            raise MediaNotFoundError(f"Album not found: {prov_album_id}")
        # TODO: adjust to json query instead of text search
        query = f"SELECT * FROM tracks WHERE albums LIKE '%\"{db_album.item_id}\"%'"
        query += f" AND provider_ids LIKE '%\"{self.type.value}\"%'"
        result = []
        for track in await self.mass.music.tracks.get_db_items_by_query(query):
            track.album = db_album
            if album_mapping := next(
                (x for x in track.albums if x.item_id == db_album.item_id), None
            ):
                track.disc_number = album_mapping.disc_number
                track.track_number = album_mapping.track_number
                result.append(track)
        return sorted(result, key=lambda x: (x.disc_number or 0, x.track_number or 0))

    async def get_playlist_tracks(self, prov_playlist_id: str) -> List[Track]:
        """Get playlist tracks for given playlist id."""
        result = []
        playlist_path = await self._get_filepath(MediaType.PLAYLIST, prov_playlist_id)
        if not await self.exists(playlist_path):
            raise MediaNotFoundError(f"Playlist path does not exist: {playlist_path}")
        playlist_base_path = Path(playlist_path).parent
        try:
            async with self.open_file(playlist_path, "r") as _file:
                for line_no, line in enumerate(await _file.readlines()):
                    line = urllib.parse.unquote(line.strip())
                    if line and not line.startswith("#"):
                        # TODO: add support for .pls playlist files
                        if media_item := await self._parse_playlist_line(
                            line, playlist_base_path
                        ):
                            # use the linenumber as position for easier deletions
                            media_item.position = line_no
                            result.append(media_item)
        except Exception as err:  # pylint: disable=broad-except
            self.logger.warning(
                "Error while parsing playlist %s", playlist_path, exc_info=err
            )
        return result

    async def _parse_playlist_line(
        self, line: str, playlist_path: str
    ) -> Track | Radio | None:
        """Try to parse a track from a playlist line."""
        try:
            # try to treat uri as filename first
            if await self.exists(line):
                file_path = await self.resolve(line)
                return await self._parse_track(file_path)
            # fallback to generic uri parsing
            return await self.mass.music.get_item_by_uri(line)
        except MusicAssistantError as err:
            self.logger.warning(
                "Could not parse uri/file %s to track: %s", line, str(err)
            )
            return None

    async def add_playlist_tracks(
        self, prov_playlist_id: str, prov_track_ids: List[str]
    ) -> None:
        """Add track(s) to playlist."""
        itempath = await self._get_filepath(MediaType.PLAYLIST, prov_playlist_id)
        if not await self.exists(itempath):
            raise MediaNotFoundError(f"Playlist path does not exist: {itempath}")
        async with self.open_file(itempath, "r") as _file:
            cur_data = await _file.read()
        async with self.open_file(itempath, "w") as _file:
            await _file.write(cur_data)
            for uri in prov_track_ids:
                await _file.write(f"\n{uri}")

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: Tuple[int]
    ) -> None:
        """Remove track(s) from playlist."""
        itempath = await self._get_filepath(MediaType.PLAYLIST, prov_playlist_id)
        if not await self.exists(itempath):
            raise MediaNotFoundError(f"Playlist path does not exist: {itempath}")
        cur_lines = []
        async with self.open_file(itempath, "r") as _file:
            for line_no, line in enumerate(await _file.readlines()):
                line = urllib.parse.unquote(line.strip())
                if line_no not in positions_to_remove:
                    cur_lines.append(line)
        async with self.open_file(itempath, "w") as _file:
            for uri in cur_lines:
                await _file.write(f"{uri}\n")

    async def create_playlist(self, name: str) -> Playlist:
        """Create a new playlist on provider with given name."""
        # creating a new playlist on the filesystem is as easy
        # as creating a new (empty) file with the m3u extension...
        filename = await self.resolve(f"{name}.m3u")
        async with self.open_file(filename, "w") as _file:
            await _file.write("\n")
        playlist = await self._parse_playlist(filename)
        db_playlist = await self.mass.music.playlists.add_db_item(playlist)
        return db_playlist

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        itempath = await self._get_filepath(MediaType.TRACK, item_id)
        if not await self.exists(itempath):
            raise MediaNotFoundError(f"Track path does not exist: {itempath}")

        metadata = await parse_tags(itempath)
        stat = await self.mass.loop.run_in_executor(None, os.stat, itempath)

        return StreamDetails(
            provider=self.type,
            item_id=item_id,
            content_type=ContentType.try_parse(metadata.format),
            media_type=MediaType.TRACK,
            duration=metadata.duration,
            size=stat.st_size,
            sample_rate=metadata.sample_rate,
            bit_depth=metadata.bits_per_sample,
            direct=itempath,
        )

    async def _parse_track(self, track_path: str) -> Track:
        """Try to parse a track from a filename by reading its tags."""

        if not await self.exists(track_path):
            raise MediaNotFoundError(f"Track path does not exist: {track_path}")

        track_item_id = self._get_item_id(track_path)

        # parse tags
        tags = await parse_tags(track_path)

        name, version = parse_title_and_version(tags.title)
        track = Track(
            item_id=track_item_id,
            provider=self.type,
            name=name,
            version=version,
        )

        # album
        if tags.album:
            # work out if we have an album folder
            album_dir = get_parentdir(track_path, tags.album)

            # album artist(s)
            if tags.album_artists:
                album_artists = []
                for index, album_artist_str in enumerate(tags.album_artists):
                    # work out if we have an artist folder
                    artist_dir = get_parentdir(track_path, album_artist_str)
                    artist = await self._parse_artist(
                        album_artist_str, artist_path=artist_dir
                    )
                    if not artist.musicbrainz_id:
                        try:
                            artist.musicbrainz_id = tags.musicbrainz_albumartistids[
                                index
                            ]
                        except IndexError:
                            pass
                    album_artists.append(artist)
            else:
                # always fallback to various artists as album artist if user did not tag album artist
                # ID3 tag properly because we must have an album artist
                self.logger.warning(
                    "%s is missing ID3 tag [albumartist], using %s as fallback",
                    track_path,
                    FALLBACK_ARTIST,
                )
                album_artists = [await self._parse_artist(name=FALLBACK_ARTIST)]

            track.album = await self._parse_album(
                tags.album,
                album_dir,
                artists=album_artists,
            )
        else:
            self.logger.warning("%s is missing ID3 tag [album]", track_path)

        # track artist(s)
        for index, track_artist_str in enumerate(tags.artists):
            # re-use album artist details if possible
            if track.album:
                if artist := next(
                    (x for x in track.album.artists if x.name == track_artist_str), None
                ):
                    track.artists.append(artist)
                    continue
            artist = await self._parse_artist(track_artist_str)
            if not artist.musicbrainz_id:
                try:
                    artist.musicbrainz_id = tags.musicbrainz_artistids[index]
                except IndexError:
                    pass
            track.artists.append(artist)

        # cover image - prefer album image, fallback to embedded
        if track.album and track.album.image:
            track.metadata.images = [
                MediaItemImage(ImageType.THUMB, track.album.image, True)
            ]
        elif tags.has_cover_image:
            # we do not actually embed the image in the metadata because that would consume too
            # much space and bandwidth. Instead we set the filename as value so the image can
            # be retrieved later in realtime.
            track.metadata.images = [MediaItemImage(ImageType.THUMB, track_path, True)]
            if track.album:
                # set embedded cover on album
                track.album.metadata.images = track.metadata.images

        # parse other info
        assert tags.duration, "Invalid duration"
        track.duration = tags.duration
        track.metadata.genres = tags.genres
        track.disc_number = tags.disc
        track.track_number = tags.track
        track.isrc = tags.get("isrc")
        track.metadata.copyright = tags.get("copyright")
        track.metadata.lyrics = tags.get("lyrics")
        track.musicbrainz_id = tags.musicbrainz_trackid
        if track.album:
            if not track.album.musicbrainz_id:
                track.album.musicbrainz_id = tags.musicbrainz_releasegroupid
            if not track.album.year:
                track.album.year = tags.year
            if not track.album.upc:
                track.album.upc = tags.get("barcode")
        # try to parse albumtype
        if track.album and track.album.album_type == AlbumType.UNKNOWN:
            album_type = tags.album_type
            if album_type and "compilation" in album_type:
                track.album.album_type = AlbumType.COMPILATION
            elif album_type and "single" in album_type:
                track.album.album_type = AlbumType.SINGLE
            elif album_type and "album" in album_type:
                track.album.album_type = AlbumType.ALBUM
            elif track.album.sort_name in track.sort_name:
                track.album.album_type = AlbumType.SINGLE

        # set checksum to invalidate any cached listings
        checksum_timestamp = str(int(time()))
        track.metadata.checksum = checksum_timestamp
        if track.album:
            track.album.metadata.checksum = checksum_timestamp
            for artist in track.album.artists:
                artist.metadata.checksum = checksum_timestamp

        quality_details = ""
        content_type = ContentType.try_parse(tags.format)
        quality_details = f"{int(tags.bit_rate / 1000)} kbps"
        if content_type == ContentType.MP3:
            quality = MediaQuality.LOSSY_MP3
        elif content_type == ContentType.OGG:
            quality = MediaQuality.LOSSY_OGG
        elif content_type == ContentType.AAC:
            quality = MediaQuality.LOSSY_AAC
        elif content_type == ContentType.M4A:
            quality = MediaQuality.LOSSY_M4A
        elif content_type.is_lossless():
            quality = MediaQuality.LOSSLESS
            quality_details = f"{tags.sample_rate / 1000} Khz / {tags.bit_rate} bit"
            if tags.sample_rate > 192000:
                quality = MediaQuality.LOSSLESS_HI_RES_4
            elif tags.sample_rate > 96000:
                quality = MediaQuality.LOSSLESS_HI_RES_3
            elif tags.sample_rate > 48000:
                quality = MediaQuality.LOSSLESS_HI_RES_2
            elif tags.bits_per_sample > 16:
                quality = MediaQuality.LOSSLESS_HI_RES_1
        else:
            quality = MediaQuality.UNKNOWN

        track.add_provider_id(
            MediaItemProviderId(
                item_id=track_item_id,
                prov_type=self.type,
                prov_id=self.id,
                quality=quality,
                details=quality_details,
                url=track_path,
            )
        )
        return track

    async def _parse_artist(
        self,
        name: Optional[str] = None,
        artist_path: Optional[str] = None,
    ) -> Artist | None:
        """Lookup metadata in Artist folder."""
        assert name or artist_path
        if not artist_path:
            # create fake path
            artist_path = os.path.join(self.config.path, name)

        artist_item_id = self._get_item_id(artist_path)
        if not name:
            name = artist_path.split(os.sep)[-1]

        artist = Artist(
            artist_item_id,
            self.type,
            name,
            provider_ids={
                MediaItemProviderId(artist_item_id, self.type, self.id, url=artist_path)
            },
        )

        if not await self.exists(artist_path):
            # return basic object if there is no dedicated artist folder
            return artist

        nfo_file = os.path.join(artist_path, "artist.nfo")
        if await self.exists(nfo_file):
            # found NFO file with metadata
            # https://kodi.wiki/view/NFO_files/Artists
            async with self.open_file(nfo_file, "r") as _file:
                data = await _file.read()
            info = await self.mass.loop.run_in_executor(None, xmltodict.parse, data)
            info = info["artist"]
            artist.name = info.get("title", info.get("name", name))
            if sort_name := info.get("sortname"):
                artist.sort_name = sort_name
            if musicbrainz_id := info.get("musicbrainzartistid"):
                artist.musicbrainz_id = musicbrainz_id
            if descripton := info.get("biography"):
                artist.metadata.description = descripton
            if genre := info.get("genre"):
                artist.metadata.genres = set(split_items(genre))
        # find local images
        images = []
        for _path in await self.mass.loop.run_in_executor(
            None, os.scandir, artist_path
        ):
            if "." not in _path.path or _path.is_dir():
                continue
            filename, ext = _path.path.rsplit(os.sep, 1)[-1].split(".", 1)
            if ext not in ("jpg", "png"):
                continue
            try:
                images.append(MediaItemImage(ImageType(filename), _path.path, True))
            except ValueError:
                if "folder" in filename:
                    images.append(MediaItemImage(ImageType.THUMB, _path.path, True))
                elif "Artist" in filename:
                    images.append(MediaItemImage(ImageType.THUMB, _path.path, True))
        if images:
            artist.metadata.images = images

        return artist

    async def _parse_album(
        self, name: Optional[str], album_path: Optional[str], artists: List[Artist]
    ) -> Album | None:
        """Lookup metadata in Album folder."""
        assert (name or album_path) and artists
        if not album_path:
            # create fake path
            album_path = os.path.join(self.config.path, artists[0].name, name)

        album_item_id = self._get_item_id(album_path)
        if not name:
            name = album_path.split(os.sep)[-1]

        album = Album(
            album_item_id,
            self.type,
            name,
            artists=artists,
            provider_ids={
                MediaItemProviderId(album_item_id, self.type, self.id, url=album_path)
            },
        )

        if not await self.exists(album_path):
            # return basic object if there is no dedicated album folder
            return album

        nfo_file = os.path.join(album_path, "album.nfo")
        if await self.exists(nfo_file):
            # found NFO file with metadata
            # https://kodi.wiki/view/NFO_files/Artists
            async with self.open_file(nfo_file) as _file:
                data = await _file.read()
            info = await self.mass.loop.run_in_executor(None, xmltodict.parse, data)
            info = info["album"]
            album.name = info.get("title", info.get("name", name))
            if sort_name := info.get("sortname"):
                album.sort_name = sort_name
            if musicbrainz_id := info.get("musicbrainzreleasegroupid"):
                album.musicbrainz_id = musicbrainz_id
            if mb_artist_id := info.get("musicbrainzalbumartistid"):
                if album.artist and not album.artist.musicbrainz_id:
                    album.artist.musicbrainz_id = mb_artist_id
            if description := info.get("review"):
                album.metadata.description = description
            if year := info.get("year"):
                album.year = int(year)
            if genre := info.get("genre"):
                album.metadata.genres = set(split_items(genre))
        # parse name/version
        album.name, album.version = parse_title_and_version(album.name)

        # find local images
        images = []
        async for _path in scantree(album_path):
            if "." not in _path.path or _path.is_dir():
                continue
            filename, ext = _path.path.rsplit(os.sep, 1)[-1].split(".", 1)
            if ext not in ("jpg", "png"):
                continue
            try:
                images.append(MediaItemImage(ImageType(filename), _path.path, True))
            except ValueError:
                if "folder" in filename:
                    images.append(MediaItemImage(ImageType.THUMB, _path.path, True))
                elif "AlbumArt" in filename:
                    images.append(MediaItemImage(ImageType.THUMB, _path.path, True))
        if images:
            album.metadata.images = images

        return album

    async def _parse_playlist(self, playlist_path: str) -> Playlist:
        """Parse playlist from file."""
        playlist_item_id = self._get_item_id(playlist_path)

        if not await self.exists(playlist_path):
            raise MediaNotFoundError(f"Playlist path does not exist: {playlist_path}")

        name = playlist_path.split(os.sep)[-1].replace(".m3u", "")

        playlist = Playlist(playlist_item_id, provider=self.type, name=name)
        playlist.is_editable = True
        # playlist is always in-library
        playlist.in_library = True
        playlist.add_provider_id(
            MediaItemProviderId(
                item_id=playlist_item_id,
                prov_type=self.type,
                prov_id=self.id,
                url=playlist_path,
            )
        )
        playlist.owner = self._attr_name
        getmtime = wrap(os.path.getmtime)
        mtime = await getmtime(playlist_path)
        checksum = f"{SCHEMA_VERSION}.{int(mtime)}"
        playlist.metadata.checksum = checksum
        return playlist

    async def exists(self, file_path: str) -> bool:
        """Return bool is this FileSystem musicprovider has given file/dir."""
        if not file_path:
            return False  # guard
        file_path = await self.resolve(file_path)
        if self.config.path not in file_path:
            # additional guard (needed for files within m3u files)
            return False
        _exists = wrap(os.path.exists)
        return await _exists(file_path)

    @asynccontextmanager
    async def open_file(self, file_path: str, mode="rb") -> AsyncFileIO:
        """Return (async) handle to given file."""
        # ensure we have a full path and not relative
        if self.config.path not in file_path:
            file_path = os.path.join(self.config.path, file_path)
        file_path = await self.resolve(file_path)
        async with aiofiles.open(file_path, mode) as _file:
            yield _file

    async def resolve(self, file_path: str) -> str:
        """Resolve local accessible file."""
        # remote file locations should return a tempfile here so this is future proofing
        if self.config.path not in file_path:
            file_path = os.path.join(self.config.path, file_path)
        return file_path

    async def _get_filepath(
        self, media_type: MediaType, prov_item_id: str
    ) -> str | None:
        """Get full filepath on disk for item_id."""
        if prov_item_id is None:
            return None  # guard
        # funky sql queries go here ;-)
        table = f"{media_type.value}s"
        query = (
            f"SELECT json_extract(json_each.value, '$.url') as url FROM {table}"
            " ,json_each(provider_ids) WHERE"
            f" json_extract(json_each.value, '$.prov_id') = '{self.id}'"
            f" AND json_extract(json_each.value, '$.item_id') = '{prov_item_id}'"
        )
        for db_row in await self.mass.database.get_rows_from_query(query):
            file_path = db_row["url"]
            # ensure we have a full path and not relative
            if self.config.path not in file_path:
                file_path = os.path.join(self.config.path, file_path)
            return file_path
        return None

    def _get_item_id(self, file_path: str) -> str:
        """Create item id from filename."""
        return create_safe_string(file_path.replace(self.config.path, ""))
