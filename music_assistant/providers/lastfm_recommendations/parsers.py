"""Parsers to convert Last.fm API responses to Music Assistant media items."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.enums import ExternalID, MediaType, ProviderFeature
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.media_items import Album, Artist, ItemMapping, Track

from music_assistant.constants import MASS_LOGGER_NAME
from music_assistant.helpers.compare import compare_strings
from music_assistant.providers.lastfm_recommendations.constants import (
    PROVIDER_SEARCH_LIMIT,
    SEARCH_CONCURRENCY_LIMIT,
)

if TYPE_CHECKING:
    from music_assistant import MusicAssistant
    from music_assistant.controllers.music.media.albums import AlbumsController
    from music_assistant.controllers.music.media.artists import ArtistsController
    from music_assistant.controllers.music.media.tracks import TracksController

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.lastfm_recommendations")

# Limit concurrent provider searches to avoid overwhelming their APIs.
_SEARCH_SEMAPHORE = asyncio.Semaphore(SEARCH_CONCURRENCY_LIMIT)


def _is_matching_result(
    item_mapping: ItemMapping, result: Artist | Album | Track, artist_name: str | None
) -> bool:
    """
    Return True if a search result matches the searched item by name (and artist, if known).

    :param item_mapping: ItemMapping that was searched for.
    :param result: Search result to verify.
    :param artist_name: Artist name to verify against the result's artists, if known.
    """
    # Album/track search names are "Artist - Title" while results usually expose only the title.
    searched_name = item_mapping.name
    title_part = (
        searched_name[len(artist_name) + 3 :]
        if artist_name and searched_name.startswith(f"{artist_name} - ")
        else searched_name
    )
    if not compare_strings(title_part, result.name, strict=False) and not compare_strings(
        searched_name, result.name, strict=False
    ):
        return False

    # Verify the artist too: a matching title from a different artist (cover or karaoke
    # version) must not pass. Results without artist info can't contradict, accept those.
    result_artists = getattr(result, "artists", None)
    if artist_name and result_artists:
        return any(
            compare_strings(artist_name, result_artist.name, strict=False)
            for result_artist in result_artists
        )
    return True


def _get_streaming_providers(
    mass: MusicAssistant, item_mapping: ItemMapping, provider_instance_to_skip: str
) -> list[Any]:
    """
    Return streaming providers that support the ItemMapping's media type.

    :param mass: MusicAssistant instance.
    :param item_mapping: ItemMapping with the media type to search for.
    :param provider_instance_to_skip: Provider instance to skip (ourselves).
    """
    streaming_providers = []
    for p in mass.music.providers:
        if p.instance_id == provider_instance_to_skip:
            continue
        if not p.is_streaming_provider:
            continue

        if item_mapping.media_type == MediaType.ARTIST:
            if ProviderFeature.LIBRARY_ARTISTS not in p.supported_features:
                continue
        elif item_mapping.media_type == MediaType.ALBUM:
            if ProviderFeature.LIBRARY_ALBUMS not in p.supported_features:
                continue
        elif item_mapping.media_type == MediaType.TRACK:
            if ProviderFeature.LIBRARY_TRACKS not in p.supported_features:
                continue

        streaming_providers.append(p)
    return streaming_providers


async def _search_provider(
    ctrl: ArtistsController | AlbumsController | TracksController,
    item_mapping: ItemMapping,
    provider: Any,
) -> Artist | Album | Track | None:
    """
    Search a single provider for a matching item.

    :param ctrl: Controller for the media type.
    :param item_mapping: ItemMapping to search for.
    :param provider: Provider instance to search.
    """
    async with _SEARCH_SEMAPHORE:
        try:
            LOGGER.debug(
                "Searching %s on %s for: %s",
                item_mapping.media_type.value,
                provider.name,
                item_mapping.name,
            )
            # Use a higher limit to work around provider bugs (e.g. Spotify misbehaves at limit=1).
            search_results = await ctrl.search(
                item_mapping.name, provider.instance_id, limit=PROVIDER_SEARCH_LIMIT
            )
            if not search_results:
                return None

            return search_results[0]
        except MusicAssistantError as err:
            LOGGER.debug("Provider %s search failed: %s", provider.name, type(err).__name__)
            return None


async def _search_providers_concurrent(
    ctrl: ArtistsController | AlbumsController | TracksController,
    item_mapping: ItemMapping,
    providers: list[Any],
    artist_name: str | None,
) -> Artist | Album | Track | None:
    """
    Search multiple providers concurrently and return the first verified match.

    :param ctrl: Controller for the media type.
    :param item_mapping: ItemMapping to search for.
    :param providers: List of providers to search.
    :param artist_name: Artist name to verify candidate matches against, if known.
    """
    tasks = [
        asyncio.create_task(_search_provider(ctrl, item_mapping, provider))
        for provider in providers
    ]

    for task in asyncio.as_completed(tasks):
        result = await task
        if result is None:
            continue

        if _is_matching_result(item_mapping, result, artist_name):
            LOGGER.debug(
                "Match on %s: %s (searched: %s)",
                result.provider,
                result.name,
                item_mapping.name,
            )
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            return result

        LOGGER.debug(
            "Rejecting %s from %s: name mismatch (searched: %s)",
            result.name,
            result.provider,
            item_mapping.name,
        )

    return None


async def _resolve_item(
    item_mapping: ItemMapping,
    mass: MusicAssistant,
    provider_instance_to_skip: str,
    artist_name: str | None = None,
) -> Artist | Album | Track | None:
    """
    Resolve an ItemMapping to a library or provider item.

    :param item_mapping: ItemMapping with metadata and external IDs from Last.fm.
    :param mass: MusicAssistant instance.
    :param provider_instance_to_skip: Provider instance to skip (ourselves).
    :param artist_name: Artist name to verify candidate matches against, if known.
    """
    ctrl: ArtistsController | AlbumsController | TracksController
    if item_mapping.media_type == MediaType.ARTIST:
        ctrl = mass.music.artists
    elif item_mapping.media_type == MediaType.ALBUM:
        ctrl = mass.music.albums
    elif item_mapping.media_type == MediaType.TRACK:
        ctrl = mass.music.tracks
    else:
        return None

    LOGGER.debug(
        "Resolving %s: %s (external IDs: %s)",
        item_mapping.media_type.value,
        item_mapping.name,
        item_mapping.external_ids or "none",
    )

    if library_item := await ctrl.get_library_item_by_external_ids(item_mapping.external_ids):
        LOGGER.debug("Found %s in library: %s", item_mapping.media_type.value, library_item.name)
        return library_item

    streaming_providers = _get_streaming_providers(mass, item_mapping, provider_instance_to_skip)
    if not streaming_providers:
        LOGGER.debug("No streaming providers available for resolution")
        return None

    result = await _search_providers_concurrent(
        ctrl, item_mapping, streaming_providers, artist_name
    )
    if result is None:
        LOGGER.debug("Could not resolve %s: %s", item_mapping.media_type.value, item_mapping.name)
        return None

    # Streaming providers expose ISRCs (and sometimes MBIDs) that Last.fm doesn't always
    # provide; re-check the library against the resolved item's external IDs so we prefer
    # the user's own copy when it exists.
    if result.external_ids:
        if library_item := await ctrl.get_library_item_by_external_ids(result.external_ids):
            LOGGER.debug(
                "Found %s in library via resolved external IDs: %s",
                item_mapping.media_type.value,
                library_item.name,
            )
            return library_item

    return result


async def parse_artist(
    lastfm_artist: dict[str, Any], mass: MusicAssistant, provider_instance: str
) -> Artist | None:
    """
    Parse a Last.fm artist and resolve it to a library or provider Artist.

    :param lastfm_artist: Raw Last.fm artist dict with 'name' and 'mbid' fields.
    :param mass: MusicAssistant instance for accessing library and providers.
    :param provider_instance: Provider instance ID to skip when searching.
    """
    name = lastfm_artist.get("name", "Unknown Artist")
    mbid = lastfm_artist.get("mbid")

    external_ids = set()
    if mbid:
        external_ids.add((ExternalID.MB_ARTIST, mbid))

    item_mapping = ItemMapping(
        media_type=MediaType.ARTIST,
        item_id="temp",
        provider="lastfm_recommendations",
        name=name,
        external_ids=external_ids,
    )

    return cast("Artist | None", await _resolve_item(item_mapping, mass, provider_instance))


async def parse_track(
    lastfm_track: dict[str, Any],
    mass: MusicAssistant,
    provider_instance: str,
) -> Track | None:
    """
    Parse a Last.fm track and resolve it to a library or provider Track.

    :param lastfm_track: Raw Last.fm track dict with 'name', 'artist', 'mbid', 'duration'.
    :param mass: MusicAssistant instance for accessing library and providers.
    :param provider_instance: Provider instance ID to skip when searching.
    """
    name = lastfm_track.get("name", "Unknown Track")
    mbid = lastfm_track.get("mbid")

    artist_data = lastfm_track.get("artist", {})
    if isinstance(artist_data, str):
        artist_name = artist_data
    else:
        artist_name = artist_data.get("name", "Unknown Artist")

    external_ids = set()
    if mbid:
        external_ids.add((ExternalID.MB_RECORDING, mbid))

    item_mapping = ItemMapping(
        media_type=MediaType.TRACK,
        item_id="temp",
        provider="lastfm_recommendations",
        name=f"{artist_name} - {name}",
        external_ids=external_ids,
    )

    return cast(
        "Track | None", await _resolve_item(item_mapping, mass, provider_instance, artist_name)
    )


async def parse_album(
    lastfm_album: dict[str, Any], mass: MusicAssistant, provider_instance: str
) -> Album | None:
    """
    Parse a Last.fm album and resolve it to a library or provider Album.

    :param lastfm_album: Raw Last.fm album dict with 'name', 'artist', 'mbid'.
    :param mass: MusicAssistant instance for accessing library and providers.
    :param provider_instance: Provider instance ID to skip when searching.
    """
    name = lastfm_album.get("name", "Unknown Album")
    mbid = lastfm_album.get("mbid")

    artist_data = lastfm_album.get("artist", {})
    if isinstance(artist_data, str):
        artist_name = artist_data
    else:
        artist_name = artist_data.get("name", "Unknown Artist")

    external_ids = set()
    if mbid:
        external_ids.add((ExternalID.MB_ALBUM, mbid))

    item_mapping = ItemMapping(
        media_type=MediaType.ALBUM,
        item_id="temp",
        provider="lastfm_recommendations",
        name=f"{artist_name} - {name}",
        external_ids=external_ids,
    )

    return cast(
        "Album | None", await _resolve_item(item_mapping, mass, provider_instance, artist_name)
    )
