"""Filesystem musicprovider support for MusicAssistant."""
from __future__ import annotations

import os
from typing import List, Optional, Tuple

import aiofiles
from tinytag import TinyTag

from music_assistant.helpers.compare import compare_strings
from music_assistant.helpers.util import parse_title_and_version, try_parse_int
from music_assistant.models.errors import MediaNotFoundError, MusicAssistantError
from music_assistant.models.media_items import (
    Album,
    AlbumType,
    Artist,
    ContentType,
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


def split_items(org_str: str) -> Tuple[str]:
    """Split up a tag string by common splitter."""
    if org_str is None:
        return tuple()
    for splitter in ["/", ";", ","]:
        if splitter in org_str:
            return tuple((x.strip() for x in org_str.split(splitter)))
    return (org_str,)


DB_TABLE = "filesystem_mappings"


class FileSystemProvider(MusicProvider):
    """
    Very basic implementation of a musicprovider for local files.

    Assumes files are stored on disk in format <artist>/<album>/<track.ext>
    Reads ID3 tags from file and falls back to parsing filename
    Supports m3u files only for playlists
    Supports having URI's from streaming providers within m3u playlist
    Should be compatible with LMS
    """

    def __init__(self, music_dir: str, playlist_dir: Optional[str] = None) -> None:
        """
        Initialize the Filesystem provider.

        music_dir: Directory on disk containing music files
        playlist_dir: Directory on disk containing playlist files (optional)

        """
        self._attr_id = "filesystem"
        self._attr_name = "Filesystem"
        self._playlists_dir = playlist_dir
        self._music_dir = music_dir
        self._attr_supported_mediatypes = [
            MediaType.ARTIST,
            MediaType.ALBUM,
            MediaType.TRACK,
        ]
        if playlist_dir is not None:
            self._attr_supported_mediatypes.append(MediaType.PLAYLIST)
        self._cached_tracks: List[Track] = []

    async def setup(self) -> None:
        """Handle async initialization of the provider."""
        if not os.path.isdir(self._music_dir):
            raise FileNotFoundError(f"Music Directory {self._music_dir} does not exist")
        if self._playlists_dir is not None and not os.path.isdir(self._playlists_dir):
            raise FileNotFoundError(
                f"Playlist Directory {self._playlists_dir} does not exist"
            )
        # simple db table to keep a mapping of filename to id
        async with self.mass.database.get_db() as _db:
            await _db.execute(
                f"""CREATE TABLE IF NOT EXISTS {DB_TABLE}(
                        item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        filename TEXT NOT NULL,
                        media_type TEXT NOT NULL,
                        UNIQUE(filename, media_type)
                    );"""
            )

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
        for track in await self.get_library_tracks(True):
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

    async def get_library_artists(self) -> List[Artist]:
        """Retrieve all library artists."""
        result = []
        cur_ids = set()
        for track in await self.get_library_tracks(False):
            if track.album is not None and track.album.artist is not None:
                if track.album.artist.item_id not in cur_ids:
                    result.append(track.album.artist)
                    cur_ids.add(track.album.artist.item_id)
        return result

    async def get_library_albums(self) -> List[Album]:
        """Get album folders recursively."""
        result = []
        cur_ids = set()
        for track in await self.get_library_tracks(False):
            if track.album is not None:
                if track.album.item_id not in cur_ids:
                    result.append(track.album)
                    cur_ids.add(track.album.item_id)
        return result

    async def get_library_tracks(self, allow_cache=False) -> List[Track]:
        """Get all tracks recursively."""
        # pylint: disable = arguments-differ
        if allow_cache and self._cached_tracks:
            return self._cached_tracks
        result = []
        cur_ids = set()
        for _root, _dirs, _files in os.walk(self._music_dir):
            for file in _files:
                filename = os.path.join(_root, file)
                if track := await self._parse_track(filename):
                    result.append(track)
                    cur_ids.add(track.item_id)
        self._cached_tracks = result
        return result

    async def get_library_playlists(self) -> List[Playlist]:
        """Retrieve playlists from disk."""
        if not self._playlists_dir:
            return []
        result = []
        cur_ids = set()
        for filename in os.listdir(self._playlists_dir):
            filepath = os.path.join(self._playlists_dir, filename)
            if (
                os.path.isfile(filepath)
                and not filename.startswith(".")
                and filename.lower().endswith(".m3u")
            ):
                playlist = await self._parse_playlist(filepath)
                if playlist:
                    result.append(playlist)
                    cur_ids.add(playlist.item_id)
        return result

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        if album_artist := next(
            (
                track.album.artist
                for track in await self.get_library_tracks(True)
                if track.album is not None
                and track.album.artist is not None
                and track.album.artist.item_id == prov_artist_id
            ),
            None,
        ):
            return album_artist
        # fallback to track_artist
        for track in await self.get_library_tracks(True):
            for artist in track.artists:
                if artist.item_id == prov_artist_id:
                    return artist
        return None

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        return next(
            (
                track.album
                for track in await self.get_library_tracks(True)
                if track.album is not None and track.album.item_id == prov_album_id
            ),
            None,
        )

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        if os.sep in prov_track_id:
            # this is already a filename
            itempath = prov_track_id
        else:
            itempath = await self._get_filename(prov_track_id, MediaType.TRACK)
        if not os.path.isfile(itempath):
            raise MediaNotFoundError(f"Track path does not exist: {itempath}")
        return await self._parse_track(itempath)

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        if os.sep in prov_playlist_id:
            # this is already a filename
            itempath = prov_playlist_id
        else:
            itempath = await self._get_filename(prov_playlist_id, MediaType.PLAYLIST)
        if not os.path.isfile(itempath):
            raise MediaNotFoundError(f"playlist path does not exist: {itempath}")
        return await self._parse_playlist(itempath)

    async def get_album_tracks(self, prov_album_id) -> List[Track]:
        """Get album tracks for given album id."""
        return [
            track
            for track in await self.get_library_tracks(True)
            if track.album is not None and track.album.item_id == prov_album_id
        ]

    async def get_playlist_tracks(self, prov_playlist_id: str) -> List[Track]:
        """Get playlist tracks for given playlist id."""
        result = []
        if os.sep in prov_playlist_id:
            # this is already a filename
            itempath = prov_playlist_id
        else:
            itempath = await self._get_filename(prov_playlist_id, MediaType.PLAYLIST)
        if not os.path.isfile(itempath):
            raise MediaNotFoundError(f"playlist path does not exist: {itempath}")
        index = 0
        async with aiofiles.open(itempath, "r") as _file:
            for line in await _file.readlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    if track := await self._parse_track_from_uri(line):
                        result.append(track)
                        index += 1
        return result

    async def get_artist_albums(self, prov_artist_id: str) -> List[Album]:
        """Get a list of albums for the given artist."""
        return [
            track.album
            for track in await self.get_library_tracks(True)
            if track.album is not None
            and track.album.artist is not None
            and track.album.artist.item_id == prov_artist_id
        ]

    async def get_artist_toptracks(self, prov_artist_id: str) -> List[Track]:
        """Get a list of all tracks as we have no clue about preference."""
        return [
            track
            for track in await self.get_library_tracks(True)
            if track.artists is not None
            and prov_artist_id in (x.item_id for x in track.provider_ids)
        ]

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        if os.sep in item_id:
            # this is already a filename
            itempath = item_id
        else:
            itempath = await self._get_filename(item_id, MediaType.TRACK)
        if not os.path.isfile(itempath):
            raise MediaNotFoundError(f"Track path does not exist: {itempath}")

        def parse_tag():
            return TinyTag.get(itempath)

        tag = await self.mass.loop.run_in_executor(None, parse_tag)

        return StreamDetails(
            type=StreamType.FILE,
            provider=self.id,
            item_id=item_id,
            content_type=ContentType(itempath.split(".")[-1]),
            path=itempath,
            sample_rate=tag.samplerate or 44100,
            bit_depth=16,  # TODO: parse bitdepth
        )

    async def _parse_track(self, filename: str) -> Track | None:
        """Try to parse a track from a filename by reading its tags."""
        if not TinyTag.is_supported(filename):
            return None

        def parse_tag():
            return TinyTag.get(filename)

        # TODO: Fall back to parsing base details from filename if no tags found/supported
        tag = await self.mass.loop.run_in_executor(None, parse_tag)

        # we need at least a title and artist
        if tag.title is None or tag.artist is None:
            self.logger.warning("Skipping track due to invalid ID3 tags: %s", filename)
            return None

        prov_item_id = await self._get_item_id(filename, MediaType.TRACK)
        name, version = parse_title_and_version(tag.title)
        track = Track(
            item_id=prov_item_id, provider=self.id, name=name, version=version
        )
        track.duration = tag.duration
        # parse track artists
        track.artists = [
            Artist(
                item_id=item,
                provider=self._attr_id,
                name=item,
            )
            for item in split_items(tag.artist)
        ]

        # parse album
        if tag.album is not None:
            track.album = Album(
                item_id=tag.album,
                provider=self._attr_id,
                name=tag.album,
                year=try_parse_int(tag.year),
            )
            if tag.albumartist is not None:
                track.album.artist = Artist(
                    item_id=tag.albumartist,
                    provider=self._attr_id,
                    name=tag.albumartist,
                )
            if tag.title.lower().startswith(tag.album.lower()):
                track.album.album_type = AlbumType.SINGLE
            elif tag.albumartist not in split_items(tag.artist):
                track.album.album_type = AlbumType.COMPILATION
            else:
                track.album.album_type = AlbumType.ALBUM
        # parse other info
        track.metadata.genres = set(split_items(tag.genre))
        track.disc_number = try_parse_int(tag.disc)
        track.track_number = try_parse_int(tag.track)
        track.isrc = tag.extra.get("isrc", "")
        if "copyright" in tag.extra:
            track.metadata.copyright = tag.extra["copyright"]
        if "lyrics" in tag.extra:
            track.metadata.lyrics = tag.extra["lyrics"]

        quality_details = ""
        if filename.endswith(".flac"):
            # TODO: get bit depth
            quality = MediaQuality.FLAC_LOSSLESS
            if tag.samplerate > 192000:
                quality = MediaQuality.FLAC_LOSSLESS_HI_RES_4
            elif tag.samplerate > 96000:
                quality = MediaQuality.FLAC_LOSSLESS_HI_RES_3
            elif tag.samplerate > 48000:
                quality = MediaQuality.FLAC_LOSSLESS_HI_RES_2
            quality_details = f"{tag.samplerate / 1000} Khz"
        elif filename.endswith(".ogg"):
            quality = MediaQuality.LOSSY_OGG
            quality_details = f"{tag.bitrate} kbps"
        elif filename.endswith(".m4a"):
            quality = MediaQuality.LOSSY_AAC
            quality_details = f"{tag.bitrate} kbps"
        else:
            quality = MediaQuality.LOSSY_MP3
            quality_details = f"{tag.bitrate} kbps"
        track.add_provider_id(
            MediaItemProviderId(
                provider=self.id,
                item_id=prov_item_id,
                quality=quality,
                details=quality_details,
                url=filename,
            )
        )
        return track

    async def _parse_playlist(self, filename: str) -> Playlist | None:
        """Parse playlist from file."""
        name = filename.split(os.sep)[-1].replace(".m3u", "")
        prov_item_id = await self._get_item_id(filename, MediaType.PLAYLIST)
        playlist = Playlist(prov_item_id, provider=self.id, name=name)
        playlist.is_editable = True
        playlist.add_provider_id(
            MediaItemProviderId(provider=self.id, item_id=prov_item_id, url=filename)
        )
        playlist.owner = self._attr_name
        playlist.checksum = str(os.path.getmtime(filename))
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

    async def _get_item_id(self, filename: str, media_type: MediaType) -> str:
        """Get/create item ID for given filename."""
        # we store the relative path in db
        filename_base = filename.replace(self._music_dir, "")
        if filename_base.startswith(os.sep):
            filename_base = filename_base[1:]
        match = {"filename": filename_base, "media_type": media_type.value}
        if db_row := await self.mass.database.get_row(DB_TABLE, match):
            return str(db_row["item_id"])
        # filename not yet known in db, create new record
        db_row = await self.mass.database.insert_or_replace(DB_TABLE, match)
        return str(db_row["item_id"])

    async def _get_filename(self, item_id: str, media_type: MediaType) -> str:
        """Get/create ID for given filename."""
        match = {"item_id": int(item_id), "media_type": media_type.value}
        db_row = await self.mass.database.get_row(DB_TABLE, match)
        if not db_row:
            raise MediaNotFoundError(f"Item not found: {item_id}")
        if media_type == MediaType.PLAYLIST:
            return os.path.join(self._playlists_dir, db_row["filename"])
        return os.path.join(self._music_dir, db_row["filename"])
