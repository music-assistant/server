"""Several helpers/utils for the Plex Music Provider."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import requests
from plexapi.gdm import GDM
from plexapi.library import LibrarySection as PlexLibrarySection
from plexapi.library import MusicSection as PlexMusicSection
from plexapi.server import PlexServer

if TYPE_CHECKING:
    from music_assistant.server import MusicAssistant


async def get_libraries(
    mass: MusicAssistant,
    auth_token: str,
    local_server_ssl: bool,
    local_server_ip: str,
    local_server_port: str,
    local_server_verify_cert: bool,
) -> list[str]:
    """
    Get all music libraries for all plex servers.

    Returns a dict of Library names in format {'servername / library name':'baseurl'}
    """
    cache_key = "plex_libraries"

    def _get_libraries():
        # create a listing of available music libraries on all servers
        all_libraries: list[str] = []
        session = requests.Session()
        session.verify = local_server_verify_cert
        local_server_protocol = "https" if local_server_ssl else "http"
        if auth_token is None:
            plex_server: PlexServer = PlexServer(
                f"{local_server_protocol}://{local_server_ip}:{local_server_port}"
            )
        else:
            plex_server: PlexServer = PlexServer(
                f"{local_server_protocol}://{local_server_ip}:{local_server_port}",
                auth_token,
                session=session,
            )
        for media_section in plex_server.library.sections():
            media_section: PlexLibrarySection
            if media_section.type != PlexMusicSection.TYPE:
                continue
            # TODO: figure out what plex uses as stable id and use that instead of names
            all_libraries.append(f"{plex_server.friendlyName} / {media_section.title}")
        return all_libraries

    if cache := await mass.cache.get(cache_key, checksum=auth_token):
        return cache

    result = await asyncio.to_thread(_get_libraries)
    # use short expiration for in-memory cache
    await mass.cache.set(cache_key, result, checksum=auth_token, expiration=3600)
    return result


async def discover_local_servers():
    """Discover all local plex servers on the network."""

    def _discover_local_servers():
        gdm = GDM()
        gdm.scan()
        if len(gdm.entries) > 0:
            entry = gdm.entries[0]
            data = entry.get("data")
            local_server_ip = entry.get("from")[0]
            local_server_port = data.get("Port")
            return local_server_ip, local_server_port
        else:
            return None, None

    return await asyncio.to_thread(_discover_local_servers)
