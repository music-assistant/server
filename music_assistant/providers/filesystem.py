"""Filesystem musicprovider support for MusicAssistant."""
from __future__ import annotations

import base64
import os
from typing import List, Optional, Tuple

import aiofiles
from music_assistant.helpers.compare import compare_strings, get_compare_string
from music_assistant.helpers.util import parse_title_and_version, try_parse_int
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
from tinytag import TinyTag


def split_items(org_str: str) -> Tuple[str]:
    """Split up a tag string by common splitter."""
    if org_str is None:
        return tuple()
    for splitter in ["/", ";", ","]:
        if splitter in org_str:
            return tuple((x.strip() for x in org_str.split(splitter)))
    return (org_str,)


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
        for track in await self.get_library_tracks():
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
        prev_ids = set()
        for track in await self.get_library_tracks():
            if track.album is not None and track.album.artist is not None:
                if track.album.artist.item_id not in prev_ids:
                    result.append(track.album.artist)
                    prev_ids.add(track.album.artist.item_id)
        return result

    async def get_library_albums(self) -> List[Album]:
        """Get album folders recursively."""
        result = []
        prev_ids = set()
        for track in await self.get_library_tracks():
            if track.album is not None:
                if track.album.item_id not in prev_ids:
                    result.append(track.album)
                    prev_ids.add(track.album.item_id)
        return result

    async def get_library_tracks(self) -> List[Track]:
        """Get all tracks recursively."""
        # TODO: apply caching for very large libraries ?
        result = []
        for _root, _dirs, _files in os.walk(self._music_dir):
            for file in _files:
                filename = os.path.join(_root, file)
                if TinyTag.is_supported(filename):
                    if track := await self._parse_track(filename):
                        result.append(track)
        return result

    async def get_library_playlists(self) -> List[Playlist]:
        """Retrieve playlists from disk."""
        if not self._playlists_dir:
            return []
        result = []
        for filename in os.listdir(self._playlists_dir):
            filepath = os.path.join(self._playlists_dir, filename)
            if (
                os.path.isfile(filepath)
                and not filename.startswith(".")
                and filename.lower().endswith(".m3u")
            ):
                playlist = await self.get_playlist(filepath)
                if playlist:
                    result.append(playlist)
        return result

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        return next(
            (
                track.album.artist
                for track in await self.get_library_tracks()
                if track.album is not None
                and track.album.artist is not None
                and track.album.artist.item_id == prov_artist_id
            ),
            None,
        )

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        return next(
            (
                track.album
                for track in await self.get_library_tracks()
                if track.album is not None and track.album.item_id == prov_album_id
            ),
            None,
        )

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        if os.sep not in prov_track_id:
            itempath = base64.b64decode(prov_track_id).decode("utf-8")
        else:
            itempath = prov_track_id
        if not os.path.isfile(itempath):
            self.logger.error("track path does not exist: %s", itempath)
            return None
        return await self._parse_track(itempath)

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        if os.sep not in prov_playlist_id:
            itempath = base64.b64decode(prov_playlist_id).decode("utf-8")
        else:
            itempath = prov_playlist_id
            prov_playlist_id = base64.b64encode(itempath.encode("utf-8")).decode(
                "utf-8"
            )
        if not os.path.isfile(itempath):
            self.logger.error("playlist path does not exist: %s", itempath)
            return None
        name = itempath.split(os.sep)[-1].replace(".m3u", "")
        playlist = Playlist(prov_playlist_id, provider=self.id, name=name)
        playlist.is_editable = True
        playlist.provider_ids.append(
            MediaItemProviderId(provider=self.id, item_id=prov_playlist_id)
        )
        playlist.owner = self._attr_name
        playlist.checksum = os.path.getmtime(itempath)
        return playlist

    async def get_album_tracks(self, prov_album_id) -> List[Track]:
        """Get album tracks for given album id."""
        return [
            track
            for track in await self.get_library_tracks()
            if track.album is not None and track.album.item_id == prov_album_id
        ]

    async def get_playlist_tracks(self, prov_playlist_id: str) -> List[Track]:
        """Get playlist tracks for given playlist id."""
        result = []
        if os.sep not in prov_playlist_id:
            itempath = base64.b64decode(prov_playlist_id).decode("utf-8")
        else:
            itempath = prov_playlist_id
        if not os.path.isfile(itempath):
            self.logger.error("playlist path does not exist: %s", itempath)
            return result
        index = 0
        async with aiofiles.open(itempath, "r") as _file:
            for line in await _file.readlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    track = await self._parse_track_from_uri(line)
                    if track:
                        result.append(track)
                        index += 1
        return result

    async def get_artist_albums(self, prov_artist_id: str) -> List[Album]:
        """Get a list of albums for the given artist."""
        return [
            track.album
            for track in await self.get_library_tracks()
            if track.album is not None
            and track.album.artist is not None
            and track.album.artist.item_id == prov_artist_id
        ]

    async def get_artist_toptracks(self, prov_artist_id: str) -> List[Track]:
        """Get a list of all tracks as we have no clue about preference."""
        return [
            track
            for track in await self.get_library_tracks()
            if track.artists is not None
            and prov_artist_id in [x.item_id for x in track.provider_ids]
        ]

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        filename = item_id
        if os.sep not in item_id:
            filename = base64.b64decode(item_id).decode("utf-8")
        if not os.path.isfile(filename):
            return None

        def parse_tag():
            return TinyTag.get(filename)

        tag = await self.mass.loop.run_in_executor(None, parse_tag)

        return StreamDetails(
            type=StreamType.FILE,
            provider=self.id,
            item_id=item_id,
            content_type=ContentType(filename.split(".")[-1]),
            path=filename,
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
        prov_item_id = base64.b64encode(filename.encode("utf-8")).decode("utf-8")
        name, version = parse_title_and_version(tag.title)
        track = Track(
            item_id=prov_item_id, provider=self.id, name=name, version=version
        )
        track.duration = tag.duration
        # parse track artists
        track.artists = [
            Artist(
                item_id=get_compare_string(item),
                provider=self._attr_id,
                name=item,
            )
            for item in split_items(tag.artist)
        ]

        # parse album
        if tag.album is not None:
            track.album = Album(
                item_id=get_compare_string(tag.album),
                provider=self._attr_id,
                name=tag.album,
                year=try_parse_int(tag.year),
            )
            if tag.albumartist is not None:
                track.album.artist = Artist(
                    item_id=get_compare_string(tag.albumartist),
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
        track.metadata["genres"] = split_items(tag.genre)
        track.disc_number = try_parse_int(tag.disc)
        track.track_number = try_parse_int(tag.track)
        track.isrc = tag.extra.get("isrc", "")
        if "copyright" in tag.extra:
            track.metadata["copyright"] = tag.extra["copyright"]
        if "lyrics" in tag.extra:
            track.metadata["lyrics"] = tag.extra["lyrics"]

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
        track.provider_ids.append(
            MediaItemProviderId(
                provider=self.id,
                item_id=prov_item_id,
                quality=quality,
                details=quality_details,
            )
        )
        return track

    async def _parse_track_from_uri(self, uri):
        """Try to parse a track from an uri found in playlist."""
        # pylint: disable=broad-except
        if "://" in uri:
            # track is uri from external provider?
            try:
                return await self.mass.music.get_item_by_uri(uri)
            except Exception as exc:
                self.logger.warning(
                    "Could not parse uri %s to track: %s", uri, str(exc)
                )
                return None
        # try to treat uri as filename
        # TODO: filename could be related to musicdir or full path
        track = await self.get_track(uri)
        if track:
            return track
        track = await self.get_track(os.path.join(self._music_dir, uri))
        if track:
            return track
        return None
