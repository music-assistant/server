"""Helper module for parsing the Tidal API.

This helpers file is an async wrapper around the excellent tidalapi package.
While the tidalapi package does an excellent job at parsing the Tidal results,
it is unfortunately not async, which is required for Music Assistant to run smoothly.
This also nicely separates the parsing logic from the Tidal provider logic.
"""

import asyncio
import json
from time import time

import tidalapi


async def get_library_artists(session: tidalapi.Session, user_id: str) -> dict[str, str]:
    """Async wrapper around the tidalapi User Favorites Artists function."""

    def _get_library_artists():
        return tidalapi.Favorites(session, user_id).artists(limit=9999)

    return await asyncio.to_thread(_get_library_artists)


async def get_library_albums(session: tidalapi.Session, user_id: str) -> dict[str, str]:
    """Async wrapper around the tidalapi User Favorites Albums function."""

    def _get_library_albums():
        return tidalapi.Favorites(session, user_id).albums(limit=9999)

    return await asyncio.to_thread(_get_library_albums)


async def get_album(session: tidalapi.Session, prov_album_id: str) -> dict[str, str]:
    """Async wrapper around the tidalapi get_album function."""

    def _get_album():
        return tidalapi.Album(session, prov_album_id)

    return await asyncio.to_thread(_get_album)

async def get_library_tracks(session: tidalapi.Session, user_id: str) -> dict[str, str]:
    """Async wrapper around the tidalapi get_library_tracks function."""

    def _get_library_tracks():
        return tidalapi.Favorites(session, user_id).tracks(limit=9999)

    return await asyncio.to_thread(_get_library_tracks)

async def get_library_playlists(session: tidalapi.Session, user_id: str) -> dict[str, str]:
    """Async wrapper around the tidalapi get_library_playlists function."""

    def _get_library_playlists():
        return tidalapi.Favorites(session, user_id).playlists(limit=9999)

    return await asyncio.to_thread(_get_library_playlists)