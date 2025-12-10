"""Library-related helpers for the Jellyfin provider.

This module contains logic extracted from the large provider file to make the
implementation easier to test and read. Methods preserve the original semantics
and reuse the same parser helpers used across the provider.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Any

from aiojellyfin import Connection, NotFound
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    Artist,
    Playlist,
    ProviderMapping,
    Track,
)

from music_assistant.constants import UNKNOWN_ARTIST_ID_MBID
from music_assistant.providers.jellyfin.parsers import (
    parse_album,
    parse_artist,
    parse_playlist,
    parse_track,
)

from .const import (
    ALBUM_FIELDS,
    ARTIST_FIELDS,
    ITEM_KEY_COLLECTION_TYPE,
    ITEM_KEY_ID,
    ITEM_KEY_MEDIA_STREAMS,
    ITEM_KEY_NAME,
    TRACK_FIELDS,
    UNKNOWN_ARTIST_MAPPING,
)

FAKE_ARTIST_PREFIX = "_fake://"


class JellyfinLibrary:
    """Helper class for Jellyfin music library operations."""

    def __init__(
        self, client: Connection, logger: logging.Logger, instance_id: str, domain: str
    ) -> None:
        """Initialize JellyfinLibrary helper."""
        self._client = client
        self._logger = logger
        self._instance_id = instance_id
        self._domain = domain

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Yield all artists from the Jellyfin music library."""
        response = await self._client.get_media_folders()
        for library in response["Items"]:
            if ITEM_KEY_COLLECTION_TYPE in library and library[ITEM_KEY_COLLECTION_TYPE] in "music":
                stream = (
                    self._client.artists.parent(library[ITEM_KEY_ID])
                    .enable_userdata()
                    .fields(*ARTIST_FIELDS)
                    .stream(100)
                )
                async for artist in stream:
                    yield parse_artist(self._logger, self._instance_id, self._client, artist)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Yield all albums from the Jellyfin music library."""
        response = await self._client.get_media_folders()
        for library in response["Items"]:
            if ITEM_KEY_COLLECTION_TYPE in library and library[ITEM_KEY_COLLECTION_TYPE] in "music":
                stream = (
                    self._client.albums.parent(library[ITEM_KEY_ID])
                    .enable_userdata()
                    .fields(*ALBUM_FIELDS)
                    .stream(100)
                )
                async for album in stream:
                    yield parse_album(self._logger, self._instance_id, self._client, album)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Yield all tracks from the Jellyfin music library."""
        response = await self._client.get_media_folders()
        for library in response["Items"]:
            if ITEM_KEY_COLLECTION_TYPE in library and library[ITEM_KEY_COLLECTION_TYPE] in "music":
                stream = (
                    self._client.tracks.parent(library[ITEM_KEY_ID])
                    .enable_userdata()
                    .fields(*TRACK_FIELDS)
                    .stream(100)
                )
                async for track in stream:
                    if not track[ITEM_KEY_MEDIA_STREAMS]:
                        self._logger.warning(
                            "Invalid track %s: Does not have any media streams",
                            track[ITEM_KEY_NAME],
                        )
                        continue
                    yield parse_track(self._logger, self._instance_id, self._client, track)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Yield all playlists from the Jellyfin music library."""
        response = await self._client.get_media_folders()
        for library in response["Items"]:
            if (
                ITEM_KEY_COLLECTION_TYPE in library
                and library[ITEM_KEY_COLLECTION_TYPE] in "playlists"
            ):
                stream = (
                    self._client.playlists.parent(library[ITEM_KEY_ID])
                    .enable_userdata()
                    .stream(100)
                )
                async for playlist in stream:
                    if self._is_audio_playlist(playlist):  # type: ignore[arg-type]
                        yield parse_playlist(self._instance_id, self._client, playlist)

    def _is_audio_playlist(self, playlist: dict[str, Any]) -> bool:
        """Check if a playlist is an audio playlist or has no media type."""
        if "MediaType" not in playlist:
            return True
        return str(playlist.get("MediaType")) == "Audio"

    async def get_album(self, prov_album_id: str) -> Album:
        """Get album details by provider album ID."""
        try:
            album = await self._client.get_album(prov_album_id)
        except NotFound as exc:
            raise MediaNotFoundError(f"Item {prov_album_id} not found") from exc
        return parse_album(self._logger, self._instance_id, self._client, album)

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get all tracks for a given album ID."""
        jellyfin_album_tracks = (
            await self._client.tracks.parent(prov_album_id)
            .enable_userdata()
            .fields(*TRACK_FIELDS)
            .request()
        )
        return [
            parse_track(self._logger, self._instance_id, self._client, jellyfin_album_track)
            for jellyfin_album_track in jellyfin_album_tracks["Items"]
        ]

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get artist details by provider artist ID."""
        if prov_artist_id == UNKNOWN_ARTIST_MAPPING.item_id:
            artist = Artist(
                item_id=UNKNOWN_ARTIST_MAPPING.item_id,
                name=UNKNOWN_ARTIST_MAPPING.name,
                provider=self._instance_id,
                provider_mappings={
                    ProviderMapping(
                        item_id=UNKNOWN_ARTIST_MAPPING.item_id,
                        provider_domain=self._domain,
                        provider_instance=self._instance_id,
                    )
                },
            )
            artist.mbid = UNKNOWN_ARTIST_ID_MBID
            return artist

        try:
            jellyfin_artist = await self._client.get_artist(prov_artist_id)
        except NotFound as exc:
            raise MediaNotFoundError(f"Item {prov_artist_id} not found") from exc
        return parse_artist(self._logger, self._instance_id, self._client, jellyfin_artist)

    async def get_track(self, prov_track_id: str) -> Track:
        """Get track details by provider track ID."""
        try:
            track = await self._client.get_track(prov_track_id)
        except NotFound as exc:
            raise MediaNotFoundError(f"Item {prov_track_id} not found") from exc
        return parse_track(self._logger, self._instance_id, self._client, track)

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get playlist details by provider playlist ID."""
        try:
            playlist = await self._client.get_playlist(prov_playlist_id)
        except NotFound as exc:
            raise MediaNotFoundError(f"Item {prov_playlist_id} not found") from exc
        return parse_playlist(self._instance_id, self._client, playlist)

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get all tracks for a given playlist ID and page."""
        result: list[Track] = []
        playlist_items = (
            await self._client.tracks.in_playlist(prov_playlist_id)
            .enable_userdata()
            .fields(*TRACK_FIELDS)
            .limit(100)
            .start_index(page * 100)
            .request()
        )
        for index, jellyfin_track in enumerate(playlist_items["Items"], 1):
            pos = (page * 100) + index
            try:
                if track := parse_track(
                    self._logger, self._instance_id, self._client, jellyfin_track
                ):
                    track.position = pos
                    result.append(track)
            except (KeyError, ValueError) as err:
                self._logger.error(
                    "Skipping track %s: %s",
                    jellyfin_track.get(ITEM_KEY_NAME, index),
                    str(err),
                )
        return result

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get all albums for a given artist ID."""
        if not prov_artist_id.startswith(FAKE_ARTIST_PREFIX):
            return []
        albums = (
            await self._client.albums.parent(prov_artist_id)
            .fields(*ALBUM_FIELDS)
            .enable_userdata()
            .request()
        )
        return [
            parse_album(self._logger, self._instance_id, self._client, album)
            for album in albums["Items"]
        ]
