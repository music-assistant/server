"""Filesystem musicprovider support for MusicAssistant."""
from __future__ import annotations

import asyncio
import base64
import os
import urllib.parse
from typing import TYPE_CHECKING, AsyncGenerator, List, Optional, Tuple

import aiofiles
import xmltodict
from tinytag.tinytag import TinyTag

from music_assistant.helpers.compare import compare_strings
from music_assistant.helpers.util import parse_title_and_version, try_parse_int
from music_assistant.models.config import MusicProviderConfig
from music_assistant.models.errors import MediaNotFoundError, MusicAssistantError
from music_assistant.models.media_items import (
    Album,
    AlbumType,
    Artist,
    ContentType,
    ImageType,
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

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


def split_items(org_str: str, splitters: Tuple[str] = None) -> Tuple[str]:
    """Split up a tags string by common splitter."""
    if isinstance(org_str, list):
        return org_str
    if splitters is None:
        splitters = ("/", ";", ",")
    if org_str is None:
        return tuple()
    for splitter in splitters:
        if splitter in org_str:
            return tuple((x.strip() for x in org_str.split(splitter)))
    return (org_str,)


FALLBACK_ARTIST = "Various Artists"
ARTIST_SPLITTERS = (";", ",", "Featuring", " Feat. ", " Feat ", "feat.", " & ")


class FileSystemProvider(MusicProvider):
    """
    Very basic implementation of a musicprovider for local files.

    Assumes files are stored on disk in format <artist>/<album>/<track.ext>
    Reads ID3 tags from file and falls back to parsing filename
    Supports m3u files only for playlists
    Supports having URI's from streaming providers within m3u playlist
    Should be compatible with LMS
    """

    _attr_supported_mediatypes = [
        MediaType.TRACK,
        MediaType.PLAYLIST,
        MediaType.ARTIST,
        MediaType.ALBUM,
    ]

    def __init__(self, mass: MusicAssistant, config: MusicProviderConfig) -> None:
        """Initialize MusicProvider."""
        super().__init__(mass, config)
        self._attr_id = "filesystem"
        self._attr_name = "Filesystem"
        self._music_dir = config.path
        self._cache_built = asyncio.Event()

    async def setup(self) -> bool:
        """Handle async initialization of the provider."""

        if not os.path.isdir(self._music_dir):
            raise MediaNotFoundError(
                f"Music Directory {self._music_dir} does not exist"
            )

        return True

    async def search(
        self, search_query: str, media_types=Optional[List[MediaType]], limit: int = 5
    ) -> List[MediaItemType]:
        """
        Perform search on musicprovider.

            :param search_query: Search query.
            :param media_types: A list of media_types to include. All types if None.
            :param limit: Number of items to return in the search (per type).
        """
        result = []
        async for track in self.get_library_tracks(True):
            for search_part in search_query.split(" - "):
                if media_types is None or MediaType.TRACK in media_types:
                    if compare_strings(track.name, search_part):
                        result.append(track)
                if media_types is None or MediaType.ALBUM in media_types:
                    if track.album:
                        if compare_strings(track.album.name, search_part):
                            result.append(track.album)
                if media_types is None or MediaType.ARTIST in media_types:
                    if track.album and track.album.artist:
                        if compare_strings(track.album.artist, search_part):
                            result.append(track.album.artist)
        return result

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve all library artists."""
        cur_ids = set()
        # for the sake of simplicity we only iterate over the files in one location only,
        # which is the library tracks where we recursively enumerate the directory structure
        # library artists = unique album artists across all tracks
        # the track listing is cached so this should be (pretty) fast
        async for track in self.get_library_tracks(True):
            if track.album is None or track.album.artist is None:
                continue
            if track.album.artist.item_id in cur_ids:
                continue
            yield track.album.artist
            cur_ids.add(track.album.artist.item_id)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Get album folders recursively."""
        cur_ids = set()
        # for the sake of simplicity we only iterate over the files in one location only,
        # which is the library tracks where we recurisvely enumerate the directory structure
        # library albums = unique albums across all tracks
        # the track listing is cached so this should be (pretty) fast
        async for track in self.get_library_tracks(True):
            if track.album is None:
                continue
            if track.album.item_id in cur_ids:
                continue
            yield track.album
            cur_ids.add(track.album.item_id)

    async def get_library_tracks(self, use_cache=False) -> AsyncGenerator[Track, None]:
        """Get all tracks recursively."""
        # pylint: disable=arguments-differ
        # we cache the entire tracks listing for performance and convenience reasons
        # so we can easily retrieve the library artists and albums from the tracks listing
        if use_cache:
            await self._cache_built.wait()

        # find all music files in the music directory and all subfolders
        for _root, _dirs, _files in os.walk(self._music_dir):
            for file in _files:
                filename = os.path.join(_root, file)
                if track := await self._parse_track(filename):
                    yield track
        # wakeup event that we now have built the cache
        self._cache_built.set()

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve playlists from disk."""
        if not self._music_dir:
            return
        cur_ids = set()
        for filename in os.listdir(self._music_dir):
            filepath = os.path.join(self._music_dir, filename)
            if (
                os.path.isfile(filepath)
                and not filename.startswith(".")
                and filename.lower().endswith(".m3u")
            ):
                playlist = await self._parse_playlist(filepath)
                if playlist:
                    yield playlist
                    cur_ids.add(playlist.item_id)

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        return await self._parse_artist(prov_artist_id)

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        return await self._parse_album(prov_album_id)

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        return await self._parse_track(prov_track_id)

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        itempath = self._get_full_filename(prov_playlist_id)
        if not os.path.isfile(itempath):
            raise MediaNotFoundError(f"playlist path does not exist: {itempath}")
        return await self._parse_playlist(itempath)

    async def get_album_tracks(self, prov_album_id) -> List[Track]:
        """Get album tracks for given album id."""
        return [
            track
            async for track in self.get_library_tracks(True)
            if track.album is not None and track.album.item_id == prov_album_id
        ]

    async def get_playlist_tracks(self, prov_playlist_id: str) -> List[Track]:
        """Get playlist tracks for given playlist id."""
        result = []
        itempath = self._get_full_filename(prov_playlist_id)
        if not os.path.isfile(itempath):
            raise MediaNotFoundError(f"playlist path does not exist: {itempath}")
        index = 0
        async with aiofiles.open(itempath, "r") as _file:
            for line in await _file.readlines():
                line = urllib.parse.unquote(line.strip())
                if line and not line.startswith("#"):
                    if track := await self._parse_track_from_uri(line):
                        track.position = index
                        result.append(track)
                        index += 1
        return result

    async def get_artist_albums(self, prov_artist_id: str) -> List[Album]:
        """Get a list of albums for the given artist."""
        result = []
        cur_ids = set()
        async for track in self.get_library_tracks(True):
            if track.album is None:
                continue
            if track.album.item_id in cur_ids:
                continue
            if track.album.artist is None:
                continue
            if track.album.artist.item_id != prov_artist_id:
                continue
            result.append(track.album)
            cur_ids.add(track.album.item_id)
        return result

    async def get_artist_toptracks(self, prov_artist_id: str) -> List[Track]:
        """Get a list of all tracks as we have no clue about preference."""
        return [
            track
            async for track in self.get_library_tracks(True)
            if track.artists is not None
            and (
                (prov_artist_id in (x.item_id for x in track.artists))
                or (
                    track.album is not None
                    and track.album.artist is not None
                    and track.album.artist.item_id == prov_artist_id
                )
            )
        ]

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        itempath = self._get_full_filename(item_id)
        if not os.path.isfile(itempath):
            raise MediaNotFoundError(f"Track path does not exist: {itempath}")

        def parse_tag():
            return TinyTag.get(itempath)

        tags = await self.mass.loop.run_in_executor(None, parse_tag)

        return StreamDetails(
            type=StreamType.FILE,
            provider=self.id,
            item_id=item_id,
            content_type=ContentType(itempath.split(".")[-1]),
            path=itempath,
            sample_rate=tags.samplerate or 44100,
            bit_depth=16,  # TODO: parse bitdepth
        )

    async def get_embedded_image(self, filename: str) -> str | None:
        """Return the embedded image of an audio file as base64 string."""
        if not TinyTag.is_supported(filename):
            return None

        def parse_tags():
            return TinyTag.get(filename, tags=True, image=True, ignore_errors=True)

        tags = await self.mass.loop.run_in_executor(None, parse_tags)
        if image_data := tags.get_image():
            enc_image = base64.b64encode(image_data).decode()
            enc_image = f"data:image/png;base64,{enc_image}"
            return enc_image

    async def _parse_track(self, track_path: str) -> Track | None:
        """Try to parse a track from a filename by reading its tags."""
        track_item_id = self._get_relative_filename(track_path)
        track_path = self._get_full_filename(track_path)

        # reading file/tags is slow so we keep a cache and checksum
        cache_checksum = self._get_checksum(track_path)
        cache_key = f"{self.id}_tracks_{track_item_id}"
        if cache := await self.mass.cache.get(cache_key, cache_checksum):
            return Track.from_dict(cache)

        if not os.path.isfile(track_path):
            raise MediaNotFoundError(f"Track path does not exist: {track_path}")

        if not TinyTag.is_supported(track_path):
            return None

        def parse_tags():
            return TinyTag.get(track_path, image=True, ignore_errors=True)

        # parse ID3 tags with TinyTag
        try:
            tags = await self.mass.loop.run_in_executor(None, parse_tags)
        except Exception as err:  # pylint: disable=broad-except
            self.logger.error("Error processing %s: %s", track_item_id, str(err))
            return None

        # prefer title from tag, fallback to filename
        if tags.title:
            track_title = tags.title
        else:
            ext = track_item_id.split(".")[-1]
            track_title = track_item_id.replace(f".{ext}", "").replace("_", " ")
            self.logger.warning(
                "%s is missing ID3 tags, use filename as fallback", track_item_id
            )

        name, version = parse_title_and_version(track_title)
        track = Track(
            item_id=track_item_id, provider=self.id, name=name, version=version
        )

        # work out if we have an artist/album/track.ext structure
        track_parts = track_item_id.rsplit(os.sep)
        if len(track_parts) == 3:
            album_path = os.path.dirname(track_path)
            artist_path = os.path.dirname(album_path)
            album_artist = await self._parse_artist(artist_path, True)
            track.album = await self._parse_album(album_path, album_artist, True)

        if track.album is None and tags.album:
            album_id = tags.album
            album_name, album_version = parse_title_and_version(tags.album)
            track.album = Album(
                item_id=album_id,
                provider=self._attr_id,
                name=album_name,
                version=album_version,
                year=try_parse_int(tags.year) if tags.year else None,
            )
            if tags.albumartist:
                track.album.artist = Artist(
                    item_id=tags.albumartist,
                    provider=self.id,
                    name=tags.albumartist,
                )

            # try to guess the album type
            if name.lower() == album_name.lower():
                track.album.album_type = AlbumType.SINGLE
            elif track.album.artist and track.album.artist not in (
                x.name for x in track.artists
            ):
                track.album.album_type = AlbumType.COMPILATION
            else:
                track.album.album_type = AlbumType.ALBUM

        # Parse track artist(s) from artist string using common splitters used in ID3 tags
        # NOTE: do not use a '/' or '&' to prevent artists like AC/DC become messed up
        track_artists_str = tags.artist or FALLBACK_ARTIST
        track.artists = [
            Artist(
                item_id=item,
                provider=self._attr_id,
                name=item,
            )
            for item in split_items(track_artists_str, ARTIST_SPLITTERS)
        ]

        # Check if track has embedded metadata
        img = await self.mass.loop.run_in_executor(None, tags.get_image)
        if not track.metadata.images and img:
            # we do not actually embed the image in the metadata because that would consume too
            # much space and bandwidth. Instead we set the filename as value so the image can
            # be retrieved later in realtime.
            track.metadata.images = {MediaItemImage(ImageType.THUMB, track_path, True)}
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
        # store last modified time as checksum
        track.metadata.checksum = cache_checksum

        quality_details = ""
        if track_path.endswith(".flac"):
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
        await self.mass.cache.set(
            cache_key, track.to_dict(), cache_checksum, 86400 * 365 * 5
        )
        return track

    async def _parse_artist(self, artist_path: str, skip_cache=False) -> Artist | None:
        """Lookup metadata in Artist folder."""
        artist_item_id = self._get_relative_filename(artist_path)
        artist_path = self._get_full_filename(artist_path)
        name = artist_path.split(os.sep)[-1]

        cache_key = f"{self.id}.artist.{artist_item_id}"
        if not skip_cache:
            if cache := await self.mass.cache.get(cache_key):
                return Artist.from_dict(cache)

        if not os.path.isdir(artist_path):
            # Return basic/empty object when path not found on disk
            # This probably means that music is not organized in the recommended structure
            # (artist/album/track.ext)
            return Artist(
                artist_item_id,
                self.id,
                name,
            )

        artist = Artist(
            artist_item_id,
            self.id,
            name,
        )
        nfo_file = os.path.join(artist_path, "artist.nfo")
        if os.path.isfile(nfo_file):
            # found NFO file with metadata
            # https://kodi.wiki/view/NFO_files/Artists
            async with aiofiles.open(nfo_file) as _file:
                data = await _file.read()
            info = await self.mass.loop.run_in_executor(None, xmltodict.parse, data)
            info = info["artist"]
            artist.name = info.get("title", info.get("name", name))
            artist.sort_name = info.get("sortname")
            artist.musicbrainz_id = info.get("musicbrainzartistid")
            artist.metadata.description = info.get("biography")
            if genre := info.get("genre"):
                artist.metadata.genres = set(split_items(genre))
            if not artist.musicbrainz_id:
                for uid in info.get("uniqueid", []):
                    if uid["@type"] == "MusicBrainzArtist":
                        artist.musicbrainz_id = uid["#text"]
        # find local images
        images = set()
        for _filename in os.listdir(artist_path):
            ext = _filename.split(".")[-1]
            if ext not in ("jpg", "png"):
                continue
            _filepath = os.path.join(artist_path, _filename)
            for img_type in ImageType:
                if img_type.value in _filepath:
                    images.add(MediaItemImage(img_type, _filepath, True))
                elif _filename == "folder.jpg":
                    images.add(MediaItemImage(ImageType.THUMB, _filepath, True))
        if images:
            artist.metadata.images = images

        await self.mass.cache.set(cache_key, artist.to_dict())
        return artist

    async def _parse_album(
        self, album_path: str, artist: Optional[Artist] = None, skip_cache=False
    ) -> Album | None:
        """Lookup metadata in Album folder."""
        album_item_id = self._get_relative_filename(album_path)
        album_path = self._get_full_filename(album_path)
        name = album_path.split(os.sep)[-1]

        cache_key = f"{self.id}.album.{album_item_id}"
        if not skip_cache:
            if cache := await self.mass.cache.get(cache_key):
                return Album.from_dict(cache)

        if not os.path.isdir(album_path):
            # Return basic/empty object when path not found on disk
            # This probably means that music is not organized in the recommended structure
            # (artist/album/track.ext)
            name, version = parse_title_and_version(name)
            return Album(album_item_id, self.id, name, version=version)

        album = Album(album_item_id, self.id, name, artist=artist)
        nfo_file = os.path.join(album_path, "album.nfo")
        if os.path.isfile(nfo_file):
            # found NFO file with metadata
            # https://kodi.wiki/view/NFO_files/Artists
            async with aiofiles.open(nfo_file) as _file:
                data = await _file.read()
            info = await self.mass.loop.run_in_executor(None, xmltodict.parse, data)
            info = info["album"]
            album.name = info.get("title", info.get("name", name))
            album.sort_name = info.get("sortname")
            album.musicbrainz_id = info.get("musicbrainzreleasegroupid")
            album.metadata.description = info.get("review")
            if year := info.get("label"):
                album.year = int(year)
            if genre := info.get("genre"):
                album.metadata.genres = set(split_items(genre))

            for uid in info.get("uniqueid", []):
                if uid["@type"] == "MusicBrainzReleaseGroup":
                    if not album.musicbrainz_id:
                        album.musicbrainz_id = uid["#text"]
                if uid["@type"] == "MusicBrainzAlbumArtist":
                    if album.artist and not album.artist.musicbrainz_id:
                        album.artist.musicbrainz_id = uid["#text"]
        # parse name/version
        album.name, album.version = parse_title_and_version(album.name)
        # find local images
        images = set()
        for _filename in os.listdir(album_path):
            ext = _filename.split(".")[-1]
            if ext not in ("jpg", "png"):
                continue
            _filepath = os.path.join(album_path, _filename)
            for img_type in ImageType:
                if img_type.value in _filepath:
                    images.add(MediaItemImage(img_type, _filepath, True))
                elif _filename == "folder.jpg":
                    images.add(MediaItemImage(ImageType.THUMB, _filepath, True))
        if images:
            album.metadata.images = images

        await self.mass.cache.set(cache_key, album.to_dict())
        return album

    async def _parse_playlist(self, filename: str) -> Playlist | None:
        """Parse playlist from file."""
        # use the relative filename as item_id
        filename_base = filename.replace(self._music_dir, "")
        if filename_base.startswith(os.sep):
            filename_base = filename_base[1:]
        prov_item_id = filename_base

        name = filename.split(os.sep)[-1].replace(".m3u", "")

        playlist = Playlist(prov_item_id, provider=self.id, name=name)
        playlist.is_editable = True
        playlist.add_provider_id(
            MediaItemProviderId(
                item_id=prov_item_id, prov_type=self.type, prov_id=self.id, url=filename
            )
        )
        playlist.owner = self._attr_name
        playlist.metadata.checksum = self._get_checksum(filename)
        return playlist

    async def _parse_track_from_uri(self, uri):
        """Try to parse a track from an uri found in playlist."""
        if "://" in uri:
            # track is uri from external provider?
            try:
                return await self.mass.music.get_item_by_uri(uri)
            except MusicAssistantError as err:
                self.logger.warning(
                    "Could not parse uri %s to track: %s", uri, str(err)
                )
                return None
        # try to treat uri as filename
        try:
            return await self.get_track(uri)
        except MediaNotFoundError:
            return None

    def _get_full_filename(self, item_id: str) -> str:
        """Get filename for item_id."""
        if self._music_dir in item_id:
            return item_id
        return os.path.join(self._music_dir, item_id)

    def _get_relative_filename(self, filename: str, playlist: bool = False) -> str:
        """Return item_id for given filename."""
        # we simply use the base filename as item_id
        filename_base = filename.replace(self._music_dir, "")
        if filename_base.startswith(os.sep):
            filename_base = filename_base[1:]
        return filename_base

    @staticmethod
    def _get_checksum(filename: str) -> str:
        """Get checksum for file."""
        # use last modified time as checksum
        return str(os.path.getmtime(filename))
