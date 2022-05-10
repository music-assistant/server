"""Filesystem musicprovider support for MusicAssistant."""
from __future__ import annotations

import base64
import os
from typing import List, Optional, Tuple

import aiofiles
from tinytag.tinytag import TinyTag

from music_assistant.helpers.compare import compare_strings
from music_assistant.helpers.util import parse_title_and_version, try_parse_int
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


def split_items(org_str: str, splitters: Tuple[str] = None) -> Tuple[str]:
    """Split up a tags string by common splitter."""
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
        # for the sake of simplicity we only iterate over the files in one location only,
        # which is the library tracks where we recursively enumerate the directory structure
        # library artists = unique album artists across all tracks
        # the track listing is cached so this should be (pretty) fast
        for track in await self.get_library_tracks(True):
            if track.album is None or track.album is None:
                continue
            if track.album.artist.item_id in cur_ids:
                continue
            result.append(track.album.artist)
            cur_ids.add(track.album.artist.item_id)
        return result

    async def get_library_albums(self) -> List[Album]:
        """Get album folders recursively."""
        result = []
        cur_ids = set()
        # for the sake of simplicity we only iterate over the files in one location only,
        # which is the library tracks where we recurisvely enumerate the directory structure
        # library albums = unique albums across all tracks
        # the track listing is cached so this should be (pretty) fast
        for track in await self.get_library_tracks(True):
            if track.album is None:
                continue
            if track.album.item_id in cur_ids:
                continue
            result.append(track.album)
            cur_ids.add(track.album.item_id)
        return result

    async def get_library_tracks(self, allow_cache=False) -> List[Track]:
        """Get all tracks recursively."""
        # pylint: disable = arguments-differ
        # we cache this listing in memory for performance and convenience reasons
        # so we can easy retrieve the library artists and albums from the tracks listing
        # if this may ever lead to memory issues, we can do the caching in db instead.
        if allow_cache and self._cached_tracks:
            return self._cached_tracks
        result = []
        cur_ids = set()
        # find all music files in the music directory and all subfolders
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
        itempath = self._get_filename(prov_track_id)
        if not os.path.isfile(itempath):
            raise MediaNotFoundError(f"Track path does not exist: {itempath}")
        return await self._parse_track(itempath)

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        itempath = self._get_filename(prov_playlist_id)
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
        itempath = self._get_filename(prov_playlist_id)
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
        result = []
        cur_ids = set()
        for track in await self.get_library_tracks(True):
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
            for track in await self.get_library_tracks(True)
            if track.artists is not None
            and prov_artist_id in (x.item_id for x in track.artists)
        ]

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        itempath = self._get_filename(item_id)
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

    async def _parse_track(self, filename: str) -> Track | None:
        """Try to parse a track from a filename by reading its tags."""
        if not TinyTag.is_supported(filename):
            return None

        def parse_tags():
            return TinyTag.get(filename, image=True, ignore_errors=True)

        # parse ID3 tags with TinyTag
        tags = await self.mass.loop.run_in_executor(None, parse_tags)

        # use the relative filename as item_id
        filename_base = filename.replace(self._music_dir, "")
        if filename_base.startswith(os.sep):
            filename_base = filename_base[1:]
        prov_item_id = filename_base

        # work out if we have an artist/album/track.ext structure
        filename_base = filename.replace(self._music_dir, "")
        if filename_base.startswith(os.sep):
            filename_base = filename_base[1:]
        track_parts = filename_base.rsplit(os.sep)
        if track_parts == 3:
            album_artist_name = track_parts[0]
            album_name = track_parts[1]
        album_artist_name = tags.albumartist
        album_name = tags.album

        # prefer title from tag, fallback to filename
        if tags.title:
            track_title = tags.title
        else:
            ext = filename_base.split(".")[-1]
            track_title = filename_base.replace(f".{ext}", "").replace("_", " ")
            self.logger.warning(
                "%s is missing ID3 tags, use filename as fallback", filename_base
            )

        name, version = parse_title_and_version(track_title)
        track = Track(
            item_id=prov_item_id, provider=self.id, name=name, version=version
        )

        # Parse track artist(s) from artist string using common splitters used in ID3 tags
        # NOTE: do not use a '/' or '&' to prevent artists like AC/DC become messed up
        track_artists_str = tags.artist or album_artist_name or FALLBACK_ARTIST
        track.artists = [
            Artist(
                item_id=item,
                provider=self._attr_id,
                name=item,
            )
            for item in split_items(track_artists_str, ARTIST_SPLITTERS)
        ]

        # Check if track has embedded metadata
        if tags.get_image():
            # we do not actually embed the image in the metadata because that would consume too
            # much space and bandwidth. Instead we set the filename as value so the image can
            # be retrieved later in realtime.
            track.metadata.images = {MediaItemImage(ImageType.EMBEDDED_THUMB, filename)}

        # Parse album (only if we have album + album artist tags)
        if album_name and album_artist_name:
            album_id = album_name
            album_name, album_version = parse_title_and_version(album_name)
            track.album = Album(
                item_id=album_id,
                provider=self._attr_id,
                name=album_name,
                version=album_version,
                year=try_parse_int(tags.year) if tags.year else None,
                artist=Artist(
                    item_id=album_artist_name,
                    provider=self._attr_id,
                    name=album_artist_name,
                ),
            )
            track.album.metadata.images = track.metadata.images

            # try to guess the album type
            if name.lower() == album_name.lower():
                track.album.album_type = AlbumType.SINGLE
            elif album_artist_name not in (x.name for x in track.artists):
                track.album.album_type = AlbumType.COMPILATION
            else:
                track.album.album_type = AlbumType.ALBUM

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
        if filename.endswith(".flac"):
            # TODO: get bit depth
            quality = MediaQuality.FLAC_LOSSLESS
            if tags.samplerate > 192000:
                quality = MediaQuality.FLAC_LOSSLESS_HI_RES_4
            elif tags.samplerate > 96000:
                quality = MediaQuality.FLAC_LOSSLESS_HI_RES_3
            elif tags.samplerate > 48000:
                quality = MediaQuality.FLAC_LOSSLESS_HI_RES_2
            quality_details = f"{tags.samplerate / 1000} Khz"
        elif filename.endswith(".ogg"):
            quality = MediaQuality.LOSSY_OGG
            quality_details = f"{tags.bitrate} kbps"
        elif filename.endswith(".m4a"):
            quality = MediaQuality.LOSSY_AAC
            quality_details = f"{tags.bitrate} kbps"
        else:
            quality = MediaQuality.LOSSY_MP3
            quality_details = f"{tags.bitrate} kbps"
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
        # use the relative filename as item_id
        filename_base = filename.replace(self._music_dir, "")
        if filename_base.startswith(os.sep):
            filename_base = filename_base[1:]
        prov_item_id = filename_base

        name = filename.split(os.sep)[-1].replace(".m3u", "")

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

    def _get_filename(self, item_id: str, playlist: bool = False) -> str:
        """Get filename for item_id."""
        if self._music_dir in item_id:
            return item_id
        if playlist:
            return os.path.join(self._playlists_dir, item_id)
        return os.path.join(self._music_dir, item_id)
