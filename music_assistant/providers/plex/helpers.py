"""Several helpers/utils for the Plex Music Provider."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

import requests
from music_assistant_models.enums import ImageType
from music_assistant_models.media_items import MediaItemImage, UniqueList
from plexapi.gdm import GDM
from plexapi.library import LibrarySection as PlexLibrarySection
from plexapi.library import MusicSection as PlexMusicSection
from plexapi.server import PlexServer

from music_assistant.providers.plex.constants import AUTH_TOKEN_UNAUTH

if TYPE_CHECKING:
    from plexapi.base import PlexObject

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
    cache_provider = instance_id or local_server_ip

    def _get_libraries() -> list[str]:
        # create a listing of available music libraries on all servers
        all_libraries: list[str] = []
        session = requests.Session()
        session.verify = local_server_verify_cert
        local_server_protocol = "https" if local_server_ssl else "http"
        plex_url = f"{local_server_protocol}://{local_server_ip}:{local_server_port}"
        if not auth_token or auth_token == AUTH_TOKEN_UNAUTH:
            # local (unauthenticated) connection, not via plex.tv
            plex_server = PlexServer(plex_url, session=session)
        else:
            plex_server = PlexServer(plex_url, auth_token, session=session)
        for media_section in cast("list[PlexLibrarySection]", plex_server.library.sections()):
            if media_section.type != PlexMusicSection.TYPE:
                continue
            # TODO: figure out what plex uses as stable id and use that instead of names
            all_libraries.append(f"{plex_server.friendlyName} / {media_section.title}")
        return all_libraries

    if cache := await mass.cache.get(cache_key, checksum=auth_token, provider=cache_provider):
        return cast("list[str]", cache)

    result = await asyncio.to_thread(_get_libraries)
    # use short expiration for in-memory cache
    await mass.cache.set(
        cache_key,
        result,
        checksum=auth_token,
        expiration=3600,
        provider=cache_provider,
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


def get_thumbnail_images(
    plex_media: PlexObject,
    provider_instance_id: str,
    attrs: tuple[str, ...] = ("thumb", "parentThumb", "grandparentThumb"),
) -> UniqueList[MediaItemImage] | None:
    """
    Get the thumbnail of a Plex object as MA image list, if available.

    :param plex_media: The Plex object to extract the thumbnail from.
    :param provider_instance_id: The provider instance id to set on the image.
    :param attrs: Plex attributes to check (in order) for a thumbnail.
    """
    if thumb := plex_media.firstAttr(*attrs):
        return UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=thumb,
                    provider=provider_instance_id,
                    remotely_accessible=False,
                )
            ]
        )
    return None


def get_favorite_from_rating(plex_media: PlexObject, threshold: float) -> bool | None:
    """
    Derive favorite status from the user rating of a Plex object.

    Returns None if the object has no user rating.

    :param plex_media: The Plex object to read the user rating from.
    :param threshold: Minimum rating (0.0-10.0) to consider the item a favorite.
    """
    rating = getattr(plex_media, "userRating", None)
    if rating is None:
        return None
    return float(rating) >= threshold
