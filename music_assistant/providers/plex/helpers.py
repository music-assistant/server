"""Several helpers/utils for the Plex Music Provider."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

import requests
import plexapi.exceptions
from plexapi.gdm import GDM
from plexapi.library import LibrarySection as PlexLibrarySection
from plexapi.library import MusicSection as PlexMusicSection
from plexapi.server import PlexServer
from plexapi.myplex import MyPlexAccount

from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    SetupFailedError,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


async def get_libraries(
    mass: MusicAssistant,
    auth_token: str | None,
    local_server_ssl: bool,
    local_server_ip: str,
    local_server_port: str,
    local_server_verify_cert: bool,
    instance_id: str | None = None,
) -> list[str]:
    """
    Get all music libraries for all plex servers.

    Returns a list of Library names in format ['servername / library name', ...]

    :param mass: MusicAssistant instance.
    :param auth_token: Authentication token for Plex server.
    :param local_server_ssl: Whether to use SSL/HTTPS.
    :param local_server_ip: IP address of the Plex server.
    :param local_server_port: Port of the Plex server.
    :param local_server_verify_cert: Whether to verify SSL certificate.
    :param instance_id: Provider instance ID to use for cache isolation.
    """
    cache_key = "plex_libraries"

    def _get_libraries() -> list[str]:
        # create a listing of available music libraries on all servers
        all_libraries: list[str] = []
        session = requests.Session()
        session.verify = local_server_verify_cert
        local_server_protocol = "https" if local_server_ssl else "http"
        base_url = f"{local_server_protocol}://{local_server_ip}:{local_server_port}"

        plex_server: PlexServer | None = None

        # Plex.tv for resource discovery
        if auth_token and auth_token != "local_auth":
            try:
                account = MyPlexAccount(token=auth_token)
            except plexapi.exceptions.Unauthorized as err:
                raise LoginFailed("Plex.tv authentication failed") from err

            # Try to find the resource that matches local IP/port
            for resource in account.resources():
                if "server" not in resource.provides:
                    continue
                try:
                    for conn in resource.connections:
                        if conn.address == local_server_ip and str(conn.port) == str(local_server_port):
                            plex_server = PlexServer(
                                f"{conn.protocol}://{conn.address}:{conn.port}",
                                token=resource.accessToken,
                                session=session,
                            )
                            break
                    if plex_server:
                        break
                except Exception:
                    continue
            if plex_server is None:
                raise LoginFailed(
                    f"Configured Plex server {local_server_ip}:{local_server_port} not found in Plex.tv resources"
                )

        else:
            plex_server = PlexServer(base_url, session=session)

        for media_section in cast("list[PlexLibrarySection]", plex_server.library.sections()):
            if media_section.type != PlexMusicSection.TYPE:
                continue
            # TODO: figure out what plex uses as stable id and use that instead of names
            all_libraries.append(f"{plex_server.friendlyName} / {media_section.title}")
        return all_libraries

    if cache := await mass.cache.get(
        cache_key, checksum=auth_token, provider=instance_id or local_server_ip
    ):
        return cast("list[str]", cache)

    result = await asyncio.to_thread(_get_libraries)
    # use short expiration for in-memory cache
    await mass.cache.set(
        cache_key,
        result,
        checksum=auth_token,
        expiration=3600,
        provider=instance_id or "default",
    )
    return result


async def discover_local_servers() -> tuple[str, int] | tuple[None, None]:
    """Discover all local plex servers on the network."""

    def _discover_local_servers() -> tuple[str, int] | tuple[None, None]:
        gdm = GDM()
        gdm.scan()
        if len(gdm.entries) > 0:
            entry = gdm.entries[0]
            data = entry.get("data")
            local_server_ip = entry.get("from")[0]
            local_server_port = data.get("Port")
            return local_server_ip, local_server_port
        return None, None

    return await asyncio.to_thread(_discover_local_servers)
