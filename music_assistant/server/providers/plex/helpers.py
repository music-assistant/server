"""Several helpers/utils for the Plex Music Provider."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import plexapi.exceptions
from plexapi.library import LibrarySection as PlexLibrarySection
from plexapi.library import MusicSection as PlexMusicSection
from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer

if TYPE_CHECKING:
    from music_assistant.server import MusicAssistant


async def get_libraries(mass: MusicAssistant, auth_token: str) -> set[str]:
    """
    Get all music libraries for all plex servers.

    Returns a set of Library names in format 'servername / library name'
    """
    cache_key = "plex_libraries"

    def _get_libraries():
        # create a listing of available music libraries on all servers
        all_libraries: list[str] = []
        plex_account = MyPlexAccount(token=auth_token)
        for resource in plex_account.resources():
            if "server" not in resource.provides:
                continue
            try:
                plex_server: PlexServer = resource.connect(None, 10)
            except plexapi.exceptions.NotFound:
                continue
            for media_section in plex_server.library.sections():
                media_section: PlexLibrarySection  # noqa: PLW2901
                if media_section.type != PlexMusicSection.TYPE:
                    continue
                # TODO: figure out what plex uses as stable id and use that instead of names
                all_libraries.append(f"{resource.name} / {media_section.title}")
        return all_libraries

    if cache := await mass.cache.get(cache_key, checksum=auth_token):
        return cache

    result = await asyncio.to_thread(_get_libraries)
    # use short expiration for in-memory cache
    await mass.cache.set(cache_key, result, checksum=auth_token, expiration=3600)
    return result
