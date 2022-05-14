"""All logic for metadata retrieval."""
from __future__ import annotations

from base64 import b64encode
from time import time
from typing import TYPE_CHECKING, Optional

from music_assistant.helpers.database import TABLE_THUMBS
from music_assistant.helpers.images import create_thumbnail
from music_assistant.models.media_items import Album, Artist, Playlist, Radio, Track

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
        if metadata := await self.fanarttv.get_artist_metadata(artist):
            artist.metadata.update(metadata)
        if metadata := await self.audiodb.get_artist_metadata(artist):
            artist.metadata.update(metadata)

        artist.metadata.last_refresh = int(time())

    async def get_album_metadata(self, album: Album) -> None:
        """Get/update rich metadata for an album."""
        if metadata := await self.audiodb.get_album_metadata(album):
            album.metadata.update(metadata)
        if metadata := await self.fanarttv.get_album_metadata(album):
            album.metadata.update(metadata)

        album.metadata.last_refresh = int(time())

    async def get_track_metadata(self, track: Track) -> None:
        """Get/update rich metadata for a track."""
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
            if track.metadata.genres:
                playlist.metadata.genres.update(track.metadata.genres)
            elif track.album.metadata.genres:
                playlist.metadata.genres.update(track.album.metadata.genres)
        # TODO: create mosaic thumb/fanart from playlist tracks
        playlist.metadata.last_refresh = int(time())

    async def get_radio_metadata(self, radio: Radio) -> None:
        # pylint: disable=no-self-use
        """Get/update rich metadata for a radio station."""
        # NOTE: we do not have any metadata for radiso so consider this future proofing ;-)
        radio.metadata.last_refresh = int(time())

    async def get_artist_musicbrainz_id(self, artist: Artist) -> str:
        """Fetch musicbrainz id by performing search using the artist name, albums and tracks."""
        # try with album first
        for lookup_album in await self.mass.music.artists.get_provider_artist_albums(
            artist.item_id, artist.provider
        ):
            if artist.name != lookup_album.artist.name:
                continue
            musicbrainz_id = await self.musicbrainz.get_mb_artist_id(
                artist.name,
                albumname=lookup_album.name,
                album_upc=lookup_album.upc,
            )
            if musicbrainz_id:
                return musicbrainz_id
        # fallback to track
        for lookup_track in await self.mass.music.artists.get_provider_artist_toptracks(
            artist.item_id, artist.provider
        ):
            musicbrainz_id = await self.musicbrainz.get_mb_artist_id(
                artist.name,
                trackname=lookup_track.name,
                track_isrc=lookup_track.isrc,
            )
            if musicbrainz_id:
                return musicbrainz_id
        # lookup failed, use the shitty workaround to use the name as id.
        self.logger.warning("Unable to get musicbrainz ID for artist %s !", artist.name)
        return artist.name

    async def get_thumbnail(
        self, path: str, size: Optional[int], base64: bool = False
    ) -> bytes | str:
        """Get/create thumbnail image for path."""
        match = {"path": path, "size": size}
        if result := await self.mass.database.get_row(TABLE_THUMBS, match):
            thumbnail = result["data"]
        else:
            # create thumbnail if it doesn't exist
            thumbnail = await create_thumbnail(self.mass, path, size)
            await self.mass.database.insert_or_replace(
                TABLE_THUMBS, {**match, "data": thumbnail}
            )
        if base64:
            enc_image = b64encode(thumbnail).decode()
            thumbnail = f"data:image/png;base64,{enc_image}"
        return thumbnail
