"""Filesystem musicprovider support for MusicAssistant."""
from __future__ import annotations

import asyncio
import os
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator, List, Optional, Set, Tuple

import aiofiles
import xmltodict
from aiofiles.os import wrap
from aiofiles.threadpool.binary import AsyncFileIO
from tinytag.tinytag import TinyTag

from music_assistant.helpers.compare import compare_strings
from music_assistant.helpers.database import SCHEMA_VERSION
from music_assistant.helpers.util import (
    create_clean_string,
    parse_title_and_version,
    try_parse_int,
)
from music_assistant.models.enums import ProviderType
from music_assistant.models.errors import MediaNotFoundError, MusicAssistantError
from music_assistant.models.media_items import (
    Album,
    AlbumType,
    Artist,
    ContentType,
    ImageType,
    ItemMapping,
    MediaItemImage,
    MediaItemProviderId,
    MediaItemType,
    MediaQuality,
    MediaType,
    Playlist,
    StreamDetails,
    StreamType,
    Track,
)
from music_assistant.models.provider import MusicProvider

FALLBACK_ARTIST = "Various Artists"
SPLITTERS = (";", ",", "Featuring", " Feat. ", " Feat ", "feat.", " & ", " / ")
CONTENT_TYPE_EXT = {
    # map of supported file extensions (mapped to ContentType)
    "mp3": ContentType.MP3,
    "m4a": ContentType.M4A,
    "flac": ContentType.FLAC,
    "wav": ContentType.WAV,
    "ogg": ContentType.OGG,
    "wma": ContentType.WMA,
}


async def scantree(path: str) -> AsyncGenerator[os.DirEntry, None]:
    """Recursively yield DirEntry objects for given directory."""

    def is_dir(entry: os.DirEntry) -> bool:
        return entry.is_dir(follow_symlinks=False)

    loop = asyncio.get_running_loop()
    for entry in await loop.run_in_executor(None, os.scandir, path):
        if await loop.run_in_executor(None, is_dir, entry):
            async for subitem in scantree(entry.path):
                yield subitem
        else:
            yield entry


def split_items(org_str: str) -> Tuple[str]:
    """Split up a tags string by common splitter."""
    if isinstance(org_str, list):
        return org_str
    if org_str is None:
        return tuple()
    for splitter in SPLITTERS:
        if splitter in org_str:
            return tuple((x.strip() for x in org_str.split(splitter)))
    return (org_str,)


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
    _attr_supported_mediatypes = [
        MediaType.TRACK,
        MediaType.PLAYLIST,
        MediaType.ARTIST,
        MediaType.ALBUM,
    ]

    async def setup(self) -> bool:
        """Handle async initialization of the provider."""

        isdir = wrap(os.path.exists)

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
            tracks = await self.mass.music.tracks.get_db_items(query, params)
            result += tracks
        if media_types is None or MediaType.ALBUM in media_types:
            query = "SELECT * FROM albums WHERE name LIKE :name AND provider_ids LIKE :prov_type"
            albums = await self.mass.music.albums.get_db_items(query, params)
            result += albums
        if media_types is None or MediaType.ARTIST in media_types:
            query = "SELECT * FROM artists WHERE name LIKE :name AND provider_ids LIKE :prov_type"
            artists = await self.mass.music.artists.get_db_items(query, params)
            result += artists
        if media_types is None or MediaType.PLAYLIST in media_types:
            query = "SELECT * FROM playlists WHERE name LIKE :name AND provider_ids LIKE :prov_type"
            playlists = await self.mass.music.playlists.get_db_items(query, params)
            result += playlists
        return result

    async def sync_library(
        self, media_types: Optional[Tuple[MediaType]] = None
    ) -> None:
        """Run library sync for this provider."""
        cache_key = f"{self.id}.checksums"
        prev_checksums = await self.mass.cache.get(cache_key, SCHEMA_VERSION)
        if prev_checksums is None:
            prev_checksums = {}
        # find all music files in the music directory and all subfolders
        # we work bottom up, as-in we derive all info from the tracks
        cur_checksums = {}
        async with self.mass.database.get_db() as db:
            async for entry in scantree(self.config.path):

                # mtime is used as file checksum
                stat = await asyncio.get_running_loop().run_in_executor(
                    None, entry.stat
                )
                checksum = int(stat.st_mtime)
                cur_checksums[entry.path] = checksum
                if checksum == prev_checksums.get(entry.path):
                    continue
                try:
                    if track := await self._parse_track(entry.path):
                        # process album
                        if track.album:
                            db_album = await self.mass.music.albums.add_db_item(
                                track.album, db=db
                            )
                            if not db_album.in_library:
                                await self.mass.music.albums.set_db_library(
                                    db_album.item_id, True, db=db
                                )
                            # process (album)artist
                            if track.album.artist:
                                db_artist = await self.mass.music.artists.add_db_item(
                                    track.album.artist, db=db
                                )
                                if not db_artist.in_library:
                                    await self.mass.music.artists.set_db_library(
                                        db_artist.item_id, True, db=db
                                    )
                        # add/update track to db
                        db_track = await self.mass.music.tracks.add_db_item(
                            track, db=db
                        )
                        if not db_track.in_library:
                            await self.mass.music.tracks.set_db_library(
                                db_track.item_id, True, db=db
                            )
                    elif playlist := await self._parse_playlist(entry.path):
                        # add/update] playlist to db
                        playlist.metadata.checksum = checksum
                        await self.mass.music.playlists.add_db_item(playlist, db=db)
                except Exception:  # pylint: disable=broad-except
                    # we don't want the whole sync to crash on one file so we catch all exceptions here
                    self.logger.exception("Error processing %s", entry.path)

        # save checksums for next sync
        await self.mass.cache.set(cache_key, cur_checksums, SCHEMA_VERSION)

        # work out deletions
        deleted_files = set(prev_checksums.keys()) - set(cur_checksums.keys())
        artists: Set[ItemMapping] = set()
        albums: Set[ItemMapping] = set()
        # process deleted tracks
        for file_path in deleted_files:
            item_id = self._get_item_id(file_path)
            if db_item := await self.mass.music.tracks.get_db_item_by_prov_id(
                item_id, self.type
            ):
                await self.mass.music.tracks.remove_prov_mapping(
                    db_item.item_id, self.id
                )
                # gather artists(s) attached to this track
                for artist in db_item.artists:
                    artists.add(artist.item_id)
                # gather album and albumartist(s) attached to this track
                if db_item.album:
                    albums.add(db_item.album.item_id)
                    for artist in db_item.album.artists:
                        artists.add(artist.item_id)
        # check if albums are deleted
        for album_id in albums:
            album = await self.mass.music.albums.get_db_item(album_id)
            if not album:
                continue
            prov_album_id = next(
                x.item_id for x in album.provider_ids if x.prov_id == self.id
            )
            album_tracks = await self.get_album_tracks(prov_album_id)
            if album_tracks:
                continue
            # album has no more tracks attached, delete prov mapping
            await self.mass.music.albums.remove_prov_mapping(album_id)
        # check if artists are deleted
        for artist_id in artists:
            artist = await self.mass.music.artists.get_db_item(artist_id)
            prov_artist_id = next(
                x.item_id for x in artist.provider_ids if x.prov_id == self.id
            )
            artist_tracks = await self.get_artist_toptracks(prov_artist_id)
            if artist_tracks:
                continue
            artist_albums = await self.get_artist_albums(prov_artist_id)
            if artist_albums:
                continue
            # artist has no more tracks attached, delete prov mapping
            await self.mass.music.artists.remove_prov_mapping(artist_id)

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        itempath = await self.get_filepath(MediaType.ARTIST, prov_artist_id)
        if await self.exists(itempath):
            # if path exists on disk allow parsing full details to allow refresh of metadata
            return await self._parse_artist(artist_path=itempath)
        return await self.mass.music.artists.get_db_item_by_prov_id(
            provider_item_id=prov_artist_id, provider_id=self.id
        )

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        db_album = await self.mass.music.albums.get_db_item_by_prov_id(
            provider_item_id=prov_album_id, provider_id=self.id
        )
        if db_album is None:
            raise MediaNotFoundError(f"Album not found: {prov_album_id}")
        itempath = await self.get_filepath(MediaType.ALBUM, prov_album_id)
        if await self.exists(itempath):
            # if path exists on disk allow parsing full details to allow refresh of metadata
            return await self._parse_album(None, itempath, db_album.artists)
        return db_album

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        itempath = await self.get_filepath(MediaType.TRACK, prov_track_id)
        return await self._parse_track(itempath)

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        itempath = await self.get_filepath(MediaType.PLAYLIST, prov_playlist_id)
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
        for track in await self.mass.music.tracks.get_db_items(query):
            track.album = db_album
            album_mapping = next(
                (x for x in track.albums if x.item_id == db_album.item_id), None
            )
            track.disc_number = album_mapping.disc_number
            track.track_number = album_mapping.track_number
            result.append(track)
        return result

    async def get_playlist_tracks(self, prov_playlist_id: str) -> List[Track]:
        """Get playlist tracks for given playlist id."""
        result = []
        playlist_path = await self.get_filepath(MediaType.PLAYLIST, prov_playlist_id)
        if not await self.exists(playlist_path):
            raise MediaNotFoundError(f"Playlist path does not exist: {playlist_path}")
        getmtime = wrap(os.path.getmtime)
        mtime = await getmtime(playlist_path)
        checksum = f"{SCHEMA_VERSION}.{int(mtime)}"
        cache_key = f"playlist_{self.id}_tracks_{prov_playlist_id}"
        if cache := await self.mass.cache.get(cache_key, checksum):
            return [Track.from_dict(x) for x in cache]
        playlist_base_path = Path(playlist_path).parent
        index = 0
        try:
            async with self.open_file(playlist_path, "r") as _file:
                for line in await _file.readlines():
                    line = urllib.parse.unquote(line.strip())
                    if line and not line.startswith("#"):
                        # TODO: add support for .pls playlist files
                        if track := await self._parse_playlist_line(
                            line, playlist_base_path
                        ):
                            track.position = index
                            result.append(track)
                            index += 1
        except Exception as err:  # pylint: disable=broad-except
            self.logger.warning(
                "Error while parsing playlist %s", playlist_path, exc_info=err
            )
        await self.mass.cache.set(cache_key, [x.to_dict() for x in result], checksum)
        return result

    async def _parse_playlist_line(self, line: str, playlist_path: str) -> Track | None:
        """Try to parse a track from a playlist line."""
        if "://" in line:
            # track is uri from external provider?
            try:
                return await self.mass.music.get_item_by_uri(line)
            except MusicAssistantError as err:
                self.logger.warning(
                    "Could not parse uri %s to track: %s", line, str(err)
                )
                return None
        # try to treat uri as filename
        if await self.exists(line):
            return await self._parse_track(line)
        rel_path = os.path.join(playlist_path, line)
        if await self.exists(rel_path):
            return await self._parse_track(rel_path)
        return None

    async def get_artist_albums(self, prov_artist_id: str) -> List[Album]:
        """Get a list of albums for the given artist."""
        # filesystem items are always stored in db so we can query the database
        db_artist = await self.mass.music.artists.get_db_item_by_prov_id(
            prov_artist_id, provider_id=self.id
        )
        if db_artist is None:
            raise MediaNotFoundError(f"Artist not found: {prov_artist_id}")
        # TODO: adjust to json query instead of text search
        query = f"SELECT * FROM albums WHERE artists LIKE '%\"{db_artist.item_id}\"%'"
        query += f" AND provider_ids LIKE '%\"{self.type.value}\"%'"
        return await self.mass.music.albums.get_db_items(query)

    async def get_artist_toptracks(self, prov_artist_id: str) -> List[Track]:
        """Get a list of all tracks as we have no clue about preference."""
        # filesystem items are always stored in db so we can query the database
        db_artist = await self.mass.music.artists.get_db_item_by_prov_id(
            prov_artist_id, provider_id=self.id
        )
        if db_artist is None:
            raise MediaNotFoundError(f"Artist not found: {prov_artist_id}")
        # TODO: adjust to json query instead of text search
        query = f"SELECT * FROM tracks WHERE artists LIKE '%\"{db_artist.item_id}\"%'"
        query += f" AND provider_ids LIKE '%\"{self.type.value}\"%'"
        return await self.mass.music.tracks.get_db_items(query)

    async def library_add(self, *args, **kwargs) -> bool:
        """Add item to provider's library. Return true on succes."""
        # already handled by database

    async def library_remove(self, *args, **kwargs) -> bool:
        """Remove item from provider's library. Return true on succes."""
        # already handled by database
        # TODO: do we want to process/offer deletions here ?

    async def add_playlist_tracks(
        self, prov_playlist_id: str, prov_track_ids: List[str]
    ) -> None:
        """Add track(s) to playlist."""
        itempath = await self.get_filepath(MediaType.PLAYLIST, prov_playlist_id)
        if not await self.exists(itempath):
            raise MediaNotFoundError(f"Playlist path does not exist: {itempath}")
        async with self.open_file(itempath, "r") as _file:
            cur_data = await _file.read()
        async with self.open_file(itempath, "w") as _file:
            await _file.write(cur_data)
            for uri in prov_track_ids:
                await _file.write(f"\n{uri}")

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, prov_track_ids: List[str]
    ) -> None:
        """Remove track(s) from playlist."""
        itempath = await self.get_filepath(MediaType.PLAYLIST, prov_playlist_id)
        if not await self.exists(itempath):
            raise MediaNotFoundError(f"Playlist path does not exist: {itempath}")
        cur_lines = []
        async with self.open_file(itempath, "r") as _file:
            for line in await _file.readlines():
                line = urllib.parse.unquote(line.strip())
                if line not in prov_track_ids:
                    cur_lines.append(line)
        async with self.open_file(itempath, "w") as _file:
            for uri in cur_lines:
                await _file.write(f"{uri}\n")

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        itempath = await self.get_filepath(MediaType.TRACK, item_id)
        if not await self.exists(itempath):
            raise MediaNotFoundError(f"Track path does not exist: {itempath}")

        def parse_tag():
            return TinyTag.get(itempath)

        tags = await self.mass.loop.run_in_executor(None, parse_tag)
        _, ext = Path(itempath).name.rsplit(".", 1)
        content_type = CONTENT_TYPE_EXT.get(ext.lower())

        return StreamDetails(
            type=StreamType.FILE,
            provider=self.type,
            item_id=item_id,
            content_type=content_type,
            path=itempath,
            sample_rate=tags.samplerate or 44100,
            bit_depth=16,  # TODO: parse bitdepth
        )

    async def _parse_track(self, track_path: str) -> Track | None:
        """Try to parse a track from a filename by reading its tags."""

        if not await self.exists(track_path):
            raise MediaNotFoundError(f"Track path does not exist: {track_path}")

        if "." not in track_path or track_path.startswith("."):
            # skip system files and files without extension
            return None

        filename_base, ext = Path(track_path).name.rsplit(".", 1)
        content_type = CONTENT_TYPE_EXT.get(ext.lower())
        if content_type is None:
            # unsupported file extension
            return None

        track_item_id = self._get_item_id(track_path)

        # parse ID3 tags with TinyTag
        def parse_tags():
            return TinyTag.get(track_path, image=True, ignore_errors=True)

        tags = await self.mass.loop.run_in_executor(None, parse_tags)

        # prefer title from tags, fallback to filename
        if not tags.title or not tags.artist:
            self.logger.warning(
                "%s is missing ID3 tags, using filename as fallback", track_path
            )
            filename_parts = filename_base.split(" - ", 1)
            if len(filename_parts) == 2:
                tags.artist = tags.artist or filename_parts[0]
                tags.title = tags.title or filename_parts[1]
            else:
                tags.artist = tags.artist or FALLBACK_ARTIST
                tags.title = tags.title or filename_base

        name, version = parse_title_and_version(tags.title)
        track = Track(
            item_id=track_item_id,
            provider=self.type,
            name=name,
            version=version,
            # a track on disk is always in library
            in_library=True,
        )

        # album
        # work out if we have an artist/album/track.ext structure
        if tags.album:
            track_parts = track_path.rsplit(os.sep)
            album_folder = None
            artist_folder = None
            parentdir = os.path.dirname(track_path)
            for _ in range(len(track_parts)):
                dirname = parentdir.rsplit(os.sep)[-1]
                if compare_strings(dirname, tags.albumartist):
                    artist_folder = parentdir
                if compare_strings(dirname, tags.album):
                    album_folder = parentdir
                parentdir = os.path.dirname(parentdir)

            # album artist
            if artist_folder:
                album_artists = [
                    await self._parse_artist(
                        name=tags.albumartist,
                        artist_path=artist_folder,
                        in_library=True,
                    )
                ]
            elif tags.albumartist:
                album_artists = [
                    await self._parse_artist(name=item, in_library=True)
                    for item in split_items(tags.albumartist)
                ]

            else:
                # always fallback to various artists as album artist if user did not tag album artist
                # ID3 tag properly because we must have an album artist
                album_artists = [await self._parse_artist(name=FALLBACK_ARTIST)]
                self.logger.warning(
                    "%s is missing ID3 tag [albumartist], using %s as fallback",
                    track_path,
                    FALLBACK_ARTIST,
                )

            track.album = await self._parse_album(
                tags.album,
                album_folder,
                artists=album_artists,
                in_library=True,
            )
        else:
            self.logger.warning("%s is missing ID3 tag [album]", track_path)

        # track artist(s)
        if tags.artist == tags.albumartist and track.album:
            track.artists = track.album.artists
        else:
            # Parse track artist(s) from artist string using common splitters used in ID3 tags
            # NOTE: do not use a '/' or '&' to prevent artists like AC/DC become messed up
            track_artists_str = tags.artist or FALLBACK_ARTIST
            track.artists = [
                await self._parse_artist(item, in_library=False)
                for item in split_items(track_artists_str)
            ]

        # Check if track has embedded metadata
        img = await self.mass.loop.run_in_executor(None, tags.get_image)
        if not track.metadata.images and img:
            # we do not actually embed the image in the metadata because that would consume too
            # much space and bandwidth. Instead we set the filename as value so the image can
            # be retrieved later in realtime.
            track.metadata.images = [MediaItemImage(ImageType.THUMB, track_path, True)]
            if track.album and not track.album.metadata.images:
                track.album.metadata.images = track.metadata.images

        # parse other info
        track.duration = tags.duration
        track.metadata.genres = set(split_items(tags.genre))
        track.disc_number = try_parse_int(tags.disc)
        track.track_number = try_parse_int(tags.track)
        track.isrc = tags.extra.get("isrc", "")
        if "copyright" in tags.extra:
            track.metadata.copyright = tags.extra["copyright"]
        if "lyrics" in tags.extra:
            track.metadata.lyrics = tags.extra["lyrics"]

        quality_details = ""
        if content_type == ContentType.FLAC:
            # TODO: get bit depth
            quality = MediaQuality.FLAC_LOSSLESS
            if tags.samplerate > 192000:
                quality = MediaQuality.FLAC_LOSSLESS_HI_RES_4
            elif tags.samplerate > 96000:
                quality = MediaQuality.FLAC_LOSSLESS_HI_RES_3
            elif tags.samplerate > 48000:
                quality = MediaQuality.FLAC_LOSSLESS_HI_RES_2
            quality_details = f"{tags.samplerate / 1000} Khz"
        elif track_path.endswith(".ogg"):
            quality = MediaQuality.LOSSY_OGG
            quality_details = f"{tags.bitrate} kbps"
        elif track_path.endswith(".m4a"):
            quality = MediaQuality.LOSSY_AAC
            quality_details = f"{tags.bitrate} kbps"
        else:
            quality = MediaQuality.LOSSY_MP3
            quality_details = f"{tags.bitrate} kbps"
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
        in_library: bool = True,
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
            in_library=in_library,
        )

        if not await self.exists(artist_path):
            # return basic object if there is no dedicated artist folder
            return artist

        # always mark artist as in-library when it exists as folder on disk
        artist.in_library = True

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
        async for _path in scantree(artist_path):
            _filename = _path.path
            ext = _filename.split(".")[-1]
            if ext not in ("jpg", "png"):
                continue
            _filepath = os.path.join(artist_path, _filename)
            for img_type in ImageType:
                if img_type.value in _filepath:
                    images.append(MediaItemImage(img_type, _filepath, True))
                elif _filename == "folder.jpg":
                    images.append(MediaItemImage(ImageType.THUMB, _filepath, True))
        if images:
            artist.metadata.images = images

        return artist

    async def _parse_album(
        self,
        name: Optional[str],
        album_path: Optional[str],
        artists: List[Artist],
        in_library: bool = True,
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
            in_library=in_library,
        )

        if not await self.exists(album_path):
            # return basic object if there is no dedicated album folder
            return album

        # always mark as in-library when it exists as folder on disk
        album.in_library = True

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
            if year := info.get("label"):
                album.year = int(year)
            if genre := info.get("genre"):
                album.metadata.genres = set(split_items(genre))
        # parse name/version
        album.name, album.version = parse_title_and_version(album.name)

        # try to guess the album type
        album_tracks = [
            x async for x in scantree(album_path) if TinyTag.is_supported(x.path)
        ]
        if album.artist.sort_name == "variousartists":
            album.album_type = AlbumType.COMPILATION
        elif len(album_tracks) <= 5:
            album.album_type = AlbumType.SINGLE
        else:
            album.album_type = AlbumType.ALBUM

        # find local images
        images = []
        async for _path in scantree(album_path):
            _filename = _path.path
            ext = _filename.split(".")[-1]
            if ext not in ("jpg", "png"):
                continue
            _filepath = os.path.join(album_path, _filename)
            for img_type in ImageType:
                if img_type.value in _filepath:
                    images.append(MediaItemImage(img_type, _filepath, True))
                elif _filename == "folder.jpg":
                    images.append(MediaItemImage(ImageType.THUMB, _filepath, True))
        if images:
            album.metadata.images = images

        return album

    async def _parse_playlist(self, playlist_path: str) -> Playlist | None:
        """Parse playlist from file."""
        playlist_item_id = self._get_item_id(playlist_path)

        if not playlist_path.endswith(".m3u"):
            return None

        if not await self.exists(playlist_path):
            raise MediaNotFoundError(f"Playlist path does not exist: {playlist_path}")

        name = playlist_path.split(os.sep)[-1].replace(".m3u", "")

        playlist = Playlist(playlist_item_id, provider=self.type, name=name)
        playlist.is_editable = True
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
        return playlist

    async def exists(self, file_path: str) -> bool:
        """Return bool is this FileSystem musicprovider has given file/dir."""
        if not file_path:
            return False  # guard
        # ensure we have a full path and not relative
        if self.config.path not in file_path:
            file_path = os.path.join(self.config.path, file_path)
        _exists = wrap(os.path.exists)
        return await _exists(file_path)

    @asynccontextmanager
    async def open_file(self, file_path: str, mode="rb") -> AsyncFileIO:
        """Return (async) handle to given file."""
        # ensure we have a full path and not relative
        if self.config.path not in file_path:
            file_path = os.path.join(self.config.path, file_path)
        # remote file locations should return a tempfile here ?
        async with aiofiles.open(file_path, mode) as _file:
            yield _file

    async def get_embedded_image(self, file_path) -> bytes | None:
        """Return embedded image data."""
        if not TinyTag.is_supported(file_path):
            return None

        # embedded image in music file
        def _get_data():
            tags = TinyTag.get(file_path, image=True)
            return tags.get_image()

        return await self.mass.loop.run_in_executor(None, _get_data)

    async def get_filepath(
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
        return create_clean_string(file_path.replace(self.config.path, ""))
