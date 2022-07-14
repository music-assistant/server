"""All logic for metadata retrieval."""
from __future__ import annotations

from base64 import b64encode
from time import time
from typing import TYPE_CHECKING, Optional

from music_assistant.helpers.database import TABLE_THUMBS
from music_assistant.helpers.images import create_thumbnail
from music_assistant.models.enums import ImageType, MediaType
from music_assistant.models.media_items import (
    Album,
    Artist,
    ItemMapping,
    MediaItemType,
    Playlist,
    Radio,
    Track,
)

from .audiodb import TheAudioDb
from .fanarttv import FanartTv
from .musicbrainz import MusicBrainz

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


class MetaDataController:
    """Several helpers to search and store metadata for mediaitems."""

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize class."""
        self.mass = mass
        self.cache = mass.cache
        self.logger = mass.logger.getChild("metadata")
        self.fanarttv = FanartTv(mass)
        self.musicbrainz = MusicBrainz(mass)
        self.audiodb = TheAudioDb(mass)
        self._pref_lang: Optional[str] = None

    @property
    def preferred_language(self) -> str:
        """
        Return preferred language for metadata as 2 letter country code (uppercase).

        Defaults to English (EN).
        """
        return self._pref_lang or "EN"

    @preferred_language.setter
    def preferred_language(self, lang: str) -> None:
        """
        Set preferred language to 2 letter country code.

        Can only be set once.
        """
        if self._pref_lang is None:
            self._pref_lang = lang.upper()

    async def setup(self):
        """Async initialize of module."""

    async def get_artist_metadata(self, artist: Artist) -> None:
        """Get/update rich metadata for an artist."""
        if not artist.musicbrainz_id:
            artist.musicbrainz_id = await self.get_artist_musicbrainz_id(artist)

        if artist.musicbrainz_id:
            if metadata := await self.fanarttv.get_artist_metadata(artist):
                artist.metadata.update(metadata)
            if metadata := await self.audiodb.get_artist_metadata(artist):
                artist.metadata.update(metadata)

        artist.metadata.last_refresh = int(time())

    async def get_album_metadata(self, album: Album) -> None:
        """Get/update rich metadata for an album."""
        if not (album.musicbrainz_id or album.artist):
            return
        if metadata := await self.audiodb.get_album_metadata(album):
            album.metadata.update(metadata)
        if metadata := await self.fanarttv.get_album_metadata(album):
            album.metadata.update(metadata)

        album.metadata.last_refresh = int(time())

    async def get_track_metadata(self, track: Track) -> None:
        """Get/update rich metadata for a track."""
        if not (track.album and track.artists):
            return
        if metadata := await self.audiodb.get_track_metadata(track):
            track.metadata.update(metadata)

        track.metadata.last_refresh = int(time())

    async def get_playlist_metadata(self, playlist: Playlist) -> None:
        """Get/update rich metadata for a playlist."""
        # retrieve genres from tracks
        # TODO: retrieve style/mood ?
        playlist.metadata.genres = set()
        for track in await self.mass.music.playlists.tracks(
            playlist.item_id, playlist.provider
        ):
            if track.media_type != MediaType.TRACK:
                # filter out radio items
                continue
            if track.metadata.genres:
                playlist.metadata.genres.update(track.metadata.genres)
            elif track.album and track.album.metadata.genres:
                playlist.metadata.genres.update(track.album.metadata.genres)
        # TODO: create mosaic thumb/fanart from playlist tracks
        playlist.metadata.last_refresh = int(time())

    async def get_radio_metadata(self, radio: Radio) -> None:
        """Get/update rich metadata for a radio station."""
        # NOTE: we do not have any metadata for radio so consider this future proofing ;-)
        radio.metadata.last_refresh = int(time())

    async def get_artist_musicbrainz_id(self, artist: Artist) -> str | None:
        """Fetch musicbrainz id by performing search using the artist name, albums and tracks."""
        ref_albums = await self.mass.music.artists.albums(artist=artist)
        # first try audiodb
        if musicbrainz_id := await self.audiodb.get_musicbrainz_id(artist, ref_albums):
            return musicbrainz_id
        # try again with musicbrainz with albums with upc
        for ref_album in ref_albums:
            if ref_album.upc:
                if musicbrainz_id := await self.musicbrainz.get_mb_artist_id(
                    artist.name,
                    album_upc=ref_album.upc,
                ):
                    return musicbrainz_id
            if ref_album.musicbrainz_id:
                if musicbrainz_id := await self.musicbrainz.search_artist_by_album_mbid(
                    artist.name, ref_album.musicbrainz_id
                ):
                    return musicbrainz_id

        # try again with matching on track isrc
        ref_tracks = await self.mass.music.artists.toptracks(artist=artist)
        for ref_track in ref_tracks:
            for isrc in ref_track.isrcs:
                if musicbrainz_id := await self.musicbrainz.get_mb_artist_id(
                    artist.name,
                    track_isrc=isrc,
                ):
                    return musicbrainz_id

        # last restort: track matching by name
        for ref_track in ref_tracks:
            if musicbrainz_id := await self.musicbrainz.get_mb_artist_id(
                artist.name,
                trackname=ref_track.name,
            ):
                return musicbrainz_id
        # lookup failed
        ref_albums_str = "/".join(x.name for x in ref_albums) or "none"
        ref_tracks_str = "/".join(x.name for x in ref_tracks) or "none"
        self.logger.info(
            "Unable to get musicbrainz ID for artist %s\n"
            " - using lookup-album(s): %s\n"
            " - using lookup-track(s): %s\n",
            artist.name,
            ref_albums_str,
            ref_tracks_str,
        )
        return None

    async def get_image_data_for_item(
        self,
        media_item: MediaItemType,
        img_type: ImageType = ImageType.THUMB,
        size: int = 0,
    ) -> bytes | None:
        """Get image data for given MedaItem."""
        img_path = await self.get_image_url_for_item(
            media_item=media_item,
            img_type=img_type,
            allow_local=True,
            local_as_base64=False,
        )
        if not img_path:
            return None
        return await self.get_thumbnail(img_path, size)

    async def get_image_url_for_item(
        self,
        media_item: MediaItemType,
        img_type: ImageType = ImageType.THUMB,
        allow_local: bool = True,
        local_as_base64: bool = False,
    ) -> str | None:
        """Get url to image for given media media_item."""
        if not media_item:
            return None
        if isinstance(media_item, ItemMapping):
            media_item = await self.mass.music.get_item_by_uri(media_item.uri)
        if media_item and media_item.metadata.images:
            for img in media_item.metadata.images:
                if img.type != img_type:
                    continue
                if img.is_file and not allow_local:
                    continue
                if img.is_file and local_as_base64:
                    # return base64 string of the image (compatible with browsers)
                    return await self.get_thumbnail(img.url, base64=True)
                return img.url

        # retry with track's album
        if media_item.media_type == MediaType.TRACK and media_item.album:
            return await self.get_image_url_for_item(
                media_item.album, img_type, allow_local, local_as_base64
            )

        # try artist instead for albums
        if media_item.media_type == MediaType.ALBUM and media_item.artist:
            return await self.get_image_url_for_item(
                media_item.artist, img_type, allow_local, local_as_base64
            )

        # last resort: track artist(s)
        if media_item.media_type == MediaType.TRACK and media_item.artists:
            for artist in media_item.artists:
                return await self.get_image_url_for_item(
                    artist, img_type, allow_local, local_as_base64
                )

        return None

    async def get_thumbnail(
        self, path: str, size: int = 0, base64: bool = False
    ) -> bytes | str:
        """Get/create thumbnail image for path (image url or local path)."""
        # check if we already have this cached in the db
        match_path = path.split("?")[0].split("&")[0]
        match = {"path": match_path, "size": size}
        if result := await self.mass.database.get_row(TABLE_THUMBS, match):
            thumbnail = result["data"]
        else:
            # create thumbnail if it doesn't exist
            thumbnail = await create_thumbnail(self.mass, path, size)
            await self.mass.database.insert(
                TABLE_THUMBS, {**match, "data": thumbnail}, allow_replace=True
            )
        if base64:
            enc_image = b64encode(thumbnail).decode()
            thumbnail = f"data:image/png;base64,{enc_image}"
        return thumbnail
