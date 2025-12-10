"""Search-related helpers for the Jellyfin provider.

Extracts search logic from the provider so the main provider file can remain
a thin façade delegating to these helpers.
"""

from __future__ import annotations

import logging
from asyncio import TaskGroup
from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import (
    Album,
    Artist,
    Playlist,
    SearchResults,
    Track,
)

from music_assistant.providers.jellyfin.parsers import (
    parse_album,
    parse_artist,
    parse_playlist,
    parse_track,
)

from .const import ALBUM_FIELDS, ARTIST_FIELDS, TRACK_FIELDS

if TYPE_CHECKING:
    from aiojellyfin import Connection


class JellyfinSearch:
    """Helper class for Jellyfin search operations."""

    def __init__(self, client: Connection, logger: logging.Logger, instance_id: str) -> None:
        """Initialize JellyfinSearch helper.

        :param client: Jellyfin Connection client.
        :param logger: Logger instance.
        :param instance_id: Provider instance ID.
        """
        self._client = client
        self._logger = logger
        self._instance_id = instance_id

    async def search_track(self, search_query: str, limit: int) -> list[Track]:
        """Search for tracks matching the given query.

        :param search_query: The search query string.
        :param limit: Maximum number of results to return.
        :return: List of matching tracks.
        """
        resultset = (
            await self._client.tracks.search_term(search_query)
            .limit(limit)
            .enable_userdata()
            .fields(*TRACK_FIELDS)
            .request()
        )
        tracks = []
        for item in resultset["Items"]:
            tracks.append(parse_track(self._logger, self._instance_id, self._client, item))
        return tracks

    async def search_album(self, search_query: str, limit: int) -> list[Album]:
        """Search for albums matching the given query.

        :param search_query: The search query string.
        :param limit: Maximum number of results to return.
        :return: List of matching albums.
        """
        if "-" in search_query:
            searchterms = search_query.split(" - ")
            albumname = searchterms[1]
        else:
            albumname = search_query
        resultset = (
            await self._client.albums.search_term(albumname)
            .limit(limit)
            .enable_userdata()
            .fields(*ALBUM_FIELDS)
            .request()
        )
        albums = []
        for item in resultset["Items"]:
            albums.append(parse_album(self._logger, self._instance_id, self._client, item))
        return albums

    async def search_artist(self, search_query: str, limit: int) -> list[Artist]:
        """Search for artists matching the given query.

        :param search_query: The search query string.
        :param limit: Maximum number of results to return.
        :return: List of matching artists.
        """
        resultset = (
            await self._client.artists.search_term(search_query)
            .limit(limit)
            .enable_userdata()
            .fields(*ARTIST_FIELDS)
            .request()
        )
        artists = []
        for item in resultset["Items"]:
            artists.append(parse_artist(self._logger, self._instance_id, self._client, item))
        return artists

    async def search_playlist(self, search_query: str, limit: int) -> list[Playlist]:
        """Search for playlists matching the given query.

        :param search_query: The search query string.
        :param limit: Maximum number of results to return.
        :return: List of matching playlists.
        """
        resultset = (
            await self._client.playlists.search_term(search_query)
            .limit(limit)
            .enable_userdata()
            .request()
        )
        playlists = []
        for item in resultset["Items"]:
            playlists.append(parse_playlist(self._instance_id, self._client, item))
        return playlists

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 20,
    ) -> SearchResults:
        """Perform search on the Jellyfin library.

        :param search_query: Search query string.
        :param media_types: List of media types to search for.
        :param limit: Number of items to return per type.
        :return: SearchResults containing matching items.
        """
        artists = None
        albums = None
        tracks = None
        playlists = None

        async with TaskGroup() as tg:
            if MediaType.ARTIST in media_types:
                artists = tg.create_task(self.search_artist(search_query, limit))
            if MediaType.ALBUM in media_types:
                albums = tg.create_task(self.search_album(search_query, limit))
            if MediaType.TRACK in media_types:
                tracks = tg.create_task(self.search_track(search_query, limit))
            if MediaType.PLAYLIST in media_types:
                playlists = tg.create_task(self.search_playlist(search_query, limit))

        search_results = SearchResults()

        if artists:
            search_results.artists = artists.result()
        if albums:
            search_results.albums = albums.result()
        if tracks:
            search_results.tracks = tracks.result()
        if playlists:
            search_results.playlists = playlists.result()

        return search_results
