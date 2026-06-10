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
    MB_ISRC_CONCURRENCY_LIMIT,
    PROVIDER_SEARCH_LIMIT,
    SEARCH_CONCURRENCY_LIMIT,
)

if TYPE_CHECKING:
    from music_assistant import MusicAssistant
    from music_assistant.controllers.media.albums import AlbumsController
    from music_assistant.controllers.media.artists import ArtistsController
    from music_assistant.controllers.media.tracks import TracksController
    from music_assistant.providers.musicbrainz import MusicbrainzProvider

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.lastfm_recommendations")

# Limit concurrent provider searches to avoid overwhelming their APIs.
_SEARCH_SEMAPHORE = asyncio.Semaphore(SEARCH_CONCURRENCY_LIMIT)

# Cap concurrent MusicBrainz ISRC lookups so a single artist's top-track fan-out
# doesn't dump dozens of requests onto the shared MusicBrainz rate-limit budget at once.
_MB_ISRC_SEMAPHORE = asyncio.Semaphore(MB_ISRC_CONCURRENCY_LIMIT)


def _has_matching_external_ids(
    item_mapping: ItemMapping, media_item: Artist | Album | Track
) -> bool:
    """
    Return True if the ItemMapping shares any external IDs with the media item.

    :param item_mapping: ItemMapping with external IDs from Last.fm.
    :param media_item: Artist or Track to compare against.
    """
    if not item_mapping.external_ids:
        return False

    return bool(item_mapping.external_ids & media_item.external_ids)


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
    require_external_id_match: bool,
) -> Artist | Album | Track | None:
    """
    Search multiple providers concurrently and return the best match.

    :param ctrl: Controller for the media type.
    :param item_mapping: ItemMapping to search for.
    :param providers: List of providers to search.
    :param require_external_id_match: If True, prefer matches on external IDs and reject
        results whose external IDs contradict the ItemMapping.
    """
    tasks = [
        asyncio.create_task(_search_provider(ctrl, item_mapping, provider))
        for provider in providers
    ]

    fallback_result = None

    for task in asyncio.as_completed(tasks):
        result = await task
        if result is None:
            continue

        if not require_external_id_match:
            names_match = compare_strings(item_mapping.name, result.name, strict=False)

            # Album/track search names may be "Artist - Title" while results expose only "Title".
            if not names_match and " - " in item_mapping.name:
                title_part = item_mapping.name.split(" - ", 1)[1]
                names_match = compare_strings(title_part, result.name, strict=False)

            if names_match:
                LOGGER.debug(
                    "Name match on %s: %s (searched: %s)",
                    result.provider,
                    result.name,
                    item_mapping.name,
                )
                for t in tasks:
                    if not t.done():
                        t.cancel()
                return result

            LOGGER.debug(
                "Rejecting %s from %s: name mismatch (searched: %s)",
                result.name,
                result.provider,
                item_mapping.name,
            )
            if not fallback_result:
                fallback_result = result
            continue

        if _has_matching_external_ids(item_mapping, result):
            LOGGER.debug(
                "External ID match on %s: %s",
                result.provider,
                result.name,
            )
            for t in tasks:
                if not t.done():
                    t.cancel()
            return result

        # A result that exposes external IDs of a matching type but none that match is a
        # different item; only consider results without such IDs as a name-based fallback.
        result_has_external_ids = any(
            ext_id[0] in {ext_id_check[0] for ext_id_check in item_mapping.external_ids}
            for ext_id in result.external_ids
        )

        if result_has_external_ids:
            LOGGER.debug(
                "Rejecting %s from %s: has external IDs but they don't match",
                result.name,
                result.provider,
            )
        elif not fallback_result:
            names_match = compare_strings(item_mapping.name, result.name, strict=False)

            if not names_match and " - " in item_mapping.name:
                title_part = item_mapping.name.split(" - ", 1)[1]
                names_match = compare_strings(title_part, result.name, strict=False)

            if names_match:
                LOGGER.debug(
                    "Saving %s from %s as fallback (no external IDs to verify)",
                    result.name,
                    result.provider,
                )
                fallback_result = result
            else:
                LOGGER.debug(
                    "Not saving %s from %s as fallback: name mismatch",
                    result.name,
                    result.provider,
                )

    if fallback_result:
        LOGGER.debug("No external ID matches found, using fallback result")
    return fallback_result


async def _resolve_item(
    item_mapping: ItemMapping, mass: MusicAssistant, provider_instance_to_skip: str
) -> Artist | Album | Track | None:
    """
    Resolve an ItemMapping to a library or provider item.

    :param item_mapping: ItemMapping with metadata and external IDs from Last.fm.
    :param mass: MusicAssistant instance.
    :param provider_instance_to_skip: Provider instance to skip (ourselves).
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

    # Streaming providers only expose ISRCs for tracks; for artists/albums rely on name matching.
    require_external_id_match = False
    if item_mapping.media_type == MediaType.TRACK:
        has_isrc = any(ext_id[0] == ExternalID.ISRC for ext_id in item_mapping.external_ids)
        if has_isrc:
            require_external_id_match = True

    result = await _search_providers_concurrent(
        ctrl, item_mapping, streaming_providers, require_external_id_match
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

        # Streaming providers match tracks on ISRC, so enrich MBIDs with ISRCs where possible.
        mb_provider = mass.get_provider("musicbrainz")
        if mb_provider:
            LOGGER.debug("Resolving MBID %s to ISRCs via MusicBrainz", mbid)
            async with _MB_ISRC_SEMAPHORE:
                isrcs = await cast("MusicbrainzProvider", mb_provider).get_isrcs_for_recording(mbid)
            if isrcs:
                LOGGER.debug("Found %d ISRCs for MBID %s: %s", len(isrcs), mbid, isrcs)
                for isrc in isrcs:
                    external_ids.add((ExternalID.ISRC, isrc))
            else:
                LOGGER.debug("No ISRCs found for MBID %s", mbid)
        else:
            LOGGER.debug("MusicBrainz provider not available for ISRC lookup")
    else:
        LOGGER.debug("Track has no MBID, cannot resolve ISRCs")

    item_mapping = ItemMapping(
        media_type=MediaType.TRACK,
        item_id="temp",
        provider="lastfm_recommendations",
        name=f"{artist_name} - {name}",
        external_ids=external_ids,
    )

    return cast("Track | None", await _resolve_item(item_mapping, mass, provider_instance))


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

    return cast("Album | None", await _resolve_item(item_mapping, mass, provider_instance))
