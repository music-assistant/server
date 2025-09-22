"""Helper functions for Phish.in provider."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import aiohttp
from music_assistant_models.enums import AlbumType, ContentType, ExternalID, ImageType, MediaType
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    MediaItemMetadata,
    ProviderMapping,
    Track,
)
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.cache import use_cache

from .constants import (
    API_BASE_URL,
    FALLBACK_ALBUM_IMAGE,
    PHISH_ARTIST_ID,
    PHISH_ARTIST_NAME,
    PHISH_DISCOGS_ID,
    PHISH_MUSICBRAINZ_ID,
    PHISH_TADB_ID,
    REQUEST_TIMEOUT,
)

if TYPE_CHECKING:
    from music_assistant.models.music_provider import MusicProvider


@use_cache(expiration=3600)  # 1 hour
async def api_request(
    provider: MusicProvider,
    endpoint: str,
    params: dict[str, Any] | None = None,
) -> Any:
    """Make an API request to Phish.in."""
    url = f"{API_BASE_URL}{endpoint}"

    try:
        async with provider.mass.http_session.get(
            url,
            params=params,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        ) as response:
            if response.status == 404:
                # 404 is expected for non-existent shows/items
                raise MediaNotFoundError(f"Resource not found: {url}")
            response.raise_for_status()
            return await response.json()
    except MediaNotFoundError:
        # Re-raise MediaNotFoundError as-is
        raise
    except aiohttp.ClientError as err:
        provider.logger.error("API request failed for %s: %s", url, err)
        raise ProviderUnavailableError(f"Phish.in API unavailable: {err}") from err
    except Exception as err:
        provider.logger.error("Unexpected error for %s: %s", url, err)
        raise ProviderUnavailableError(f"Phish.in API error: {err}") from err


def show_to_album(provider: MusicProvider, show_data: dict[str, Any]) -> Album:
    """Convert a Phish.in show to a Music Assistant Album."""
    show_date = show_data.get("date", "")
    # API change: venue is now a nested object
    venue_data = show_data.get("venue", {})
    venue_name = venue_data.get("name", "Unknown Venue")
    location = venue_data.get("location", "")

    # Create album name from date and venue
    album_name = f"{show_date} - {venue_name}"
    if location:
        album_name += f", {location}"

    album_cover_url = show_data.get("album_cover_url") or FALLBACK_ALBUM_IMAGE

    # Create metadata with image
    metadata = MediaItemMetadata(
        images=UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=album_cover_url,
                    provider=provider.instance_id,
                    remotely_accessible=True,
                )
            ]
        )
    )

    # Parse year from date string (YYYY-MM-DD format)
    try:
        year = int(show_date.split("-")[0]) if show_date and "-" in show_date else None
    except (ValueError, IndexError):
        year = None

    # Create details string for provider mapping
    details_parts = [f"venue:{venue_name}"]
    if location:
        details_parts.append(f"location:{location}")
    if show_data.get("duration"):
        details_parts.append(f"duration:{show_data.get('duration')}")
    # API change: use audio_status instead of sbd/remastered
    audio_status = show_data.get("audio_status", "missing")
    details_parts.append(f"audio_status:{audio_status}")
    if show_data.get("tour_name"):
        details_parts.append(f"tour:{show_data.get('tour_name')}")

    # Create ItemMapping for Phish artist
    phish_artist = ItemMapping(
        item_id=PHISH_ARTIST_ID,
        provider=provider.instance_id,
        name=PHISH_ARTIST_NAME,
        media_type=MediaType.ARTIST,
        available=True,
    )

    return Album(
        item_id=show_date,
        provider=provider.instance_id,
        name=album_name,
        artists=UniqueList([phish_artist]),
        year=year,
        album_type=AlbumType.LIVE,
        metadata=metadata,
        provider_mappings={
            ProviderMapping(
                item_id=show_date,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                # API change: use audio_status instead of incomplete
                available=audio_status in ["complete", "partial"],
                audio_format=AudioFormat(content_type=ContentType.MP3),
                details="|".join(details_parts),
            )
        },
    )


async def get_phish_artist(provider: MusicProvider) -> Artist:
    """Get the main Phish artist object."""
    artist = Artist(
        item_id=PHISH_ARTIST_ID,
        provider=provider.instance_id,
        name=PHISH_ARTIST_NAME,
        provider_mappings={
            ProviderMapping(
                item_id=PHISH_ARTIST_ID,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                available=True,
            )
        },
    )

    # Add MusicBrainz ID for metadata enrichment
    artist.add_external_id(ExternalID.MB_ARTIST, PHISH_MUSICBRAINZ_ID)
    artist.add_external_id(ExternalID.DISCOGS, PHISH_DISCOGS_ID)
    artist.add_external_id(ExternalID.TADB, PHISH_TADB_ID)

    return artist


def track_to_ma_track(
    provider: MusicProvider,
    track_data: dict[str, Any],
    show_data: dict[str, Any] | None = None,
) -> Track:
    """Convert a Phish.in track to a Music Assistant Track."""
    track_id = str(track_data.get("id", ""))

    # Fix: Get song data from songs array instead of song object
    songs = track_data.get("songs", [])
    song_data = songs[0] if songs else {}
    song_title = track_data.get("title", "Unknown Song")

    # Duration in milliseconds, convert to seconds
    duration_ms = track_data.get("duration")
    duration = int(duration_ms / 1000) if duration_ms else 0  # Default to 0, not None

    # Track position in set
    position = track_data.get("position")
    track_number = int(position) if position is not None else 0  # Default to 0, not None
    set_name = track_data.get("set_name", "")

    # Show information
    if show_data is None:
        show_data = track_data.get("show", {})

    show_date = show_data.get("date", "")
    venue_name = show_data.get("venue", {}).get("name", "")

    # Create track title with set info if available
    track_title = song_title
    if set_name and position:
        track_title = f"{song_title} ({set_name})"

    # Create ItemMapping for Phish artist
    phish_artist = ItemMapping(
        item_id=PHISH_ARTIST_ID,
        provider=provider.instance_id,
        name=PHISH_ARTIST_NAME,
        media_type=MediaType.ARTIST,
        available=True,
    )

    # Create album ItemMapping if show_date is available
    album_mapping = None
    if show_date:
        album_mapping = ItemMapping(
            item_id=show_date,
            provider=provider.instance_id,
            name=f"{show_date} - {venue_name}" if venue_name else show_date,
            media_type=MediaType.ALBUM,
            available=True,
        )

    # Create details string for provider mapping
    details_parts = [f"song_slug:{song_data.get('slug', '')}"]
    if set_name:
        details_parts.append(f"set_name:{set_name}")
    if show_date:
        details_parts.append(f"show_date:{show_date}")
    if venue_name:
        details_parts.append(f"venue:{venue_name}")

    # Fix: Extract tag names from tag objects
    if track_data.get("tags"):
        tag_names = [tag.get("name", "") for tag in track_data.get("tags", [])]
        details_parts.append(f"tags:{','.join(tag_names)}")

    if track_data.get("likes_count"):
        details_parts.append(f"likes_count:{track_data.get('likes_count', 0)}")

    metadata = MediaItemMetadata()

    if show_data and show_data.get("album_cover_url"):
        # Determine image path
        image_path = (
            show_data.get("album_cover_url")
            if show_data and show_data.get("album_cover_url")
            else FALLBACK_ALBUM_IMAGE
        ) or FALLBACK_ALBUM_IMAGE

        # Create metadata with the determined image
        metadata = MediaItemMetadata(
            images=UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_path,
                        provider=provider.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )
        )

    return Track(
        item_id=track_id,
        provider=provider.instance_id,
        name=track_title,
        artists=UniqueList([phish_artist]),  # Use UniqueList with ItemMapping
        album=album_mapping,  # Use ItemMapping instead of ProviderMapping
        duration=duration,  # int, not int | None
        track_number=track_number,  # int, not int | None
        metadata=metadata,
        provider_mappings={
            ProviderMapping(
                item_id=track_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                available=bool(track_data.get("mp3_url")),
                audio_format=AudioFormat(content_type=ContentType.MP3),
                url=track_data.get("mp3_url"),
                details="|".join(details_parts),  # Store metadata in details
            )
        },
    )


def get_main_artist_mapping(provider: MusicProvider) -> ProviderMapping:
    """Get artist mapping for Phish."""
    return ProviderMapping(
        item_id=PHISH_ARTIST_ID,
        provider_domain=provider.domain,
        provider_instance=provider.instance_id,
        available=True,
    )


def get_album_mapping(provider: MusicProvider, show_date: str) -> ProviderMapping:
    """Get album mapping for a show date."""
    return ProviderMapping(
        item_id=show_date,
        provider_domain=provider.domain,
        provider_instance=provider.instance_id,
        available=True,
    )


def parse_search_results(
    provider: MusicProvider,
    search_data: dict[str, Any],
    media_types: list[MediaType],
) -> tuple[list[Artist], list[Album], list[Track]]:
    """Parse search results into MA media items."""
    artists = []
    albums = []
    tracks = []

    # Shows become albums - check exact_show and other_shows
    if MediaType.ALBUM in media_types:
        # Add exact show if present
        if search_data.get("exact_show"):
            try:
                album = show_to_album(provider, search_data["exact_show"])
                albums.append(album)
            except Exception as err:
                provider.logger.warning(
                    "Failed to parse exact show %s: %s", search_data["exact_show"].get("date"), err
                )

        # Add other shows
        for show in search_data.get("other_shows", []):
            try:
                album = show_to_album(provider, show)
                albums.append(album)
            except Exception as err:
                provider.logger.warning("Failed to parse show %s: %s", show.get("date"), err)

    # Search tracks - API returns tracks array directly
    if MediaType.TRACK in media_types:
        for track_data in search_data.get("tracks", []):
            try:
                track = track_to_ma_track(provider, track_data)
                tracks.append(track)
            except Exception as err:
                provider.logger.warning("Failed to parse track %s: %s", track_data.get("id"), err)

    # Artists - always return Phish as the main artist if requested
    if MediaType.ARTIST in media_types:
        try:
            phish_artist_full = Artist(
                item_id=PHISH_ARTIST_ID,
                provider=provider.instance_id,
                name=PHISH_ARTIST_NAME,
                provider_mappings={
                    ProviderMapping(
                        item_id=PHISH_ARTIST_ID,
                        provider_domain=provider.domain,
                        provider_instance=provider.instance_id,
                        available=True,
                    )
                },
            )
            artists.append(phish_artist_full)
        except Exception as err:
            provider.logger.warning("Failed to create Phish artist: %s", err)

    return artists, albums, tracks
