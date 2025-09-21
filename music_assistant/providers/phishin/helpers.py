"""Helper functions for Phish.in provider."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import aiohttp
from music_assistant_models.enums import ContentType, MediaType
from music_assistant_models.errors import ProviderUnavailableError
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ItemMapping,
    ProviderMapping,
    Track,
)
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.cache import use_cache

from .constants import (
    API_BASE_URL,
    PHISH_ARTIST_ID,
    PHISH_ARTIST_NAME,
    REQUEST_TIMEOUT,
)

if TYPE_CHECKING:
    from music_assistant.models.music_provider import MusicProvider


@use_cache(expiration=3600)  # 1 hour
async def api_request(
    provider: MusicProvider,
    endpoint: str,
    params: dict[str, Any] | None = None,
) -> Any:  # Change from dict[str, Any] to Any
    """Make an API request to Phish.in."""
    url = f"{API_BASE_URL}{endpoint}"

    try:
        async with provider.mass.http_session.get(
            url,
            params=params,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        ) as response:
            response.raise_for_status()
            return await response.json()
    except aiohttp.ClientError as err:
        provider.logger.error("API request failed for %s: %s", url, err)
        raise ProviderUnavailableError(f"Phish.in API unavailable: {err}") from err
    except Exception as err:
        provider.logger.error("Unexpected error for %s: %s", url, err)
        raise ProviderUnavailableError(f"Phish.in API error: {err}") from err


async def get_phish_artist(provider: MusicProvider) -> Artist:
    """Get the main Phish artist object."""
    return Artist(
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


def show_to_album(provider: MusicProvider, show_data: dict[str, Any]) -> Album:
    """Convert a Phish.in show to a Music Assistant Album."""
    show_date = show_data.get("date", "")
    venue_name = show_data.get("venue", {}).get("name", "Unknown Venue")
    location = show_data.get("venue", {}).get("location", "")

    # Create album name from date and venue
    album_name = f"{show_date} - {venue_name}"
    if location:
        album_name += f", {location}"

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
    if show_data.get("sbd"):
        details_parts.append("sbd:true")
    if show_data.get("remastered"):
        details_parts.append("remastered:true")

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
        provider_mappings={
            ProviderMapping(
                item_id=show_date,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                available=show_data.get("incomplete", False) is False,
                audio_format=AudioFormat(content_type=ContentType.MP3),
                details="|".join(details_parts),
            )
        },
    )


def track_to_ma_track(
    provider: MusicProvider,
    track_data: dict[str, Any],
    show_data: dict[str, Any] | None = None,
) -> Track:
    """Convert a Phish.in track to a Music Assistant Track."""
    track_id = str(track_data.get("id", ""))
    song_data = track_data.get("song", {})
    song_title = song_data.get("title", "Unknown Song")

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
    if track_data.get("tags"):
        details_parts.append(f"tags:{','.join(track_data.get('tags', []))}")
    if track_data.get("likes_count"):
        details_parts.append(f"likes_count:{track_data.get('likes_count', 0)}")

    return Track(
        item_id=track_id,
        provider=provider.instance_id,
        name=track_title,
        artists=UniqueList([phish_artist]),  # Use UniqueList with ItemMapping
        album=album_mapping,  # Use ItemMapping instead of ProviderMapping
        duration=duration,  # int, not int | None
        track_number=track_number,  # int, not int | None
        provider_mappings={
            ProviderMapping(
                item_id=track_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                available=bool(track_data.get("mp3")),
                audio_format=AudioFormat(content_type=ContentType.MP3),
                url=track_data.get("mp3"),
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

    # Shows become albums
    if MediaType.ALBUM in media_types:
        shows = search_data.get("data", {}).get("shows", [])
        for show in shows:
            try:
                album = show_to_album(provider, show)
                albums.append(album)
            except Exception as err:
                provider.logger.warning("Failed to parse show %s: %s", show.get("date"), err)

    # Songs/tracks - search might return songs which we can convert to example tracks
    if MediaType.TRACK in media_types:
        songs = search_data.get("data", {}).get("songs", [])
        for song in songs:
            try:
                # Create ItemMapping for Phish artist
                phish_artist = ItemMapping(
                    item_id=PHISH_ARTIST_ID,
                    provider=provider.instance_id,
                    name=PHISH_ARTIST_NAME,
                    media_type=MediaType.ARTIST,
                    available=True,
                )

                # Create a basic track from song info (without specific show context)
                track = Track(
                    item_id=f"song_{song.get('slug', '')}",
                    provider=provider.instance_id,
                    name=song.get("title", "Unknown Song"),
                    artists=UniqueList([phish_artist]),  # Use UniqueList with ItemMapping
                    duration=0,  # No duration info available from song search
                    track_number=0,  # No track number from song search
                    provider_mappings={
                        ProviderMapping(
                            item_id=f"song_{song.get('slug', '')}",
                            provider_domain=provider.domain,
                            provider_instance=provider.instance_id,
                            available=True,
                            details=f"slug:{song.get('slug', '')}|artist:"
                            f"{song.get('artist', '')}|times_played:"
                            f"{song.get('times_played', 0)}|debut:{song.get('debut', '')}"
                            f"|last_played:{song.get('last_played', '')}",
                        )
                    },
                    # Remove metadata parameter
                )
                tracks.append(track)
            except Exception as err:
                provider.logger.warning("Failed to parse song %s: %s", song.get("title"), err)

    # Artists - always return Phish as the main artist if requested
    if MediaType.ARTIST in media_types:
        try:
            phish_artist_full = Artist(  # Use different variable name
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
