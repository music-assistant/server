"""Helper functions for Phish.in provider."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
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
    Playlist,
    ProviderMapping,
    Track,
)
from music_assistant_models.unique_list import UniqueList

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
                raise MediaNotFoundError(f"Resource not found: {url}")
            response.raise_for_status()
            return await response.json()
    except MediaNotFoundError:
        raise
    except aiohttp.ClientError as err:
        provider.logger.error("API request failed for %s: %s", url, err)
        raise ProviderUnavailableError(f"Phish.in API unavailable: {err}") from err


def show_to_album(provider: MusicProvider, show_data: dict[str, Any]) -> Album:
    """Convert a Phish.in show to a Music Assistant Album."""
    show_date = show_data.get("date", "")
    venue_data = show_data.get("venue", {})
    venue_name = venue_data.get("name", "Unknown Venue")
    location = venue_data.get("location", "")

    album_name = f"{show_date} - {venue_name}"
    if location:
        album_name += f", {location}"

    # Create metadata with image
    album_cover_url = show_data.get("album_cover_url") or FALLBACK_ALBUM_IMAGE
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
    year = None
    if show_date and "-" in show_date:
        with contextlib.suppress(ValueError, IndexError):
            year = int(show_date.split("-")[0])

    # Create details string for provider mapping
    details_parts = [f"venue:{venue_name}"]
    if location:
        details_parts.append(f"location:{location}")
    if show_data.get("duration"):
        details_parts.append(f"duration:{show_data.get('duration')}")

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

    # Add external IDs for metadata enrichment
    artist.add_external_id(ExternalID.MB_ARTIST, PHISH_MUSICBRAINZ_ID)
    artist.add_external_id(ExternalID.DISCOGS, PHISH_DISCOGS_ID)
    artist.add_external_id(ExternalID.TADB, PHISH_TADB_ID)

    return artist


def _extract_version_from_title(full_title: str) -> tuple[str, str]:
    """Extract song title and version from full title with performance indicators.

    Returns:
        Tuple of (clean_song_title, version_string)
    """
    song_title = full_title
    version = None
    performance_indicators = ["set", "soundcheck", "check", "encore"]

    # Check for prefix: "(Check) Song Name"
    if full_title.startswith("(") and ") " in full_title:
        end_paren = full_title.index(") ")
        prefix = full_title[1:end_paren]
        if any(indicator in prefix.lower() for indicator in performance_indicators):
            version = prefix
            song_title = full_title[end_paren + 2 :]

    # Check for suffix: "Song Name (Soundcheck)"
    if " (" in song_title and song_title.endswith(")"):
        base_title, suffix = song_title.rsplit(" (", 1)
        suffix = suffix.rstrip(")")
        if any(indicator in suffix.lower() for indicator in performance_indicators):
            version = f"{version}, {suffix}" if version else suffix
            song_title = base_title

    return song_title, version or ""


def _create_album_mapping(
    provider: MusicProvider,
    show_date: str,
    show_data: dict[str, Any] | None,
) -> ItemMapping | None:
    """Create album ItemMapping with image for a track."""
    if not show_date:
        return None

    venue_name = show_data.get("venue", {}).get("name", "") if show_data else ""

    # Create the image for the album mapping
    album_image = None
    if show_data:
        image_url = show_data.get("album_cover_url") or FALLBACK_ALBUM_IMAGE
        album_image = MediaItemImage(
            type=ImageType.THUMB,
            path=image_url,
            provider=provider.instance_id,
            remotely_accessible=True,
        )

    return ItemMapping(
        item_id=show_date,
        provider=provider.instance_id,
        name=f"{show_date} - {venue_name}" if venue_name else show_date,
        media_type=MediaType.ALBUM,
        available=True,
        image=album_image,
    )


def _build_track_details(
    track_data: dict[str, Any],
    song_data: dict[str, Any],
    show_date: str,
    set_name: str,
    venue_name: str,
) -> str:
    """Build details string for provider mapping."""
    details_parts = [f"song_slug:{song_data.get('slug', '')}"]

    if set_name:
        details_parts.append(f"set_name:{set_name}")
    if show_date:
        details_parts.append(f"show_date:{show_date}")
    if venue_name:
        details_parts.append(f"venue:{venue_name}")
    if track_data.get("tags"):
        tag_names = [tag.get("name", "") for tag in track_data.get("tags", [])]
        details_parts.append(f"tags:{','.join(tag_names)}")
    if track_data.get("likes_count"):
        details_parts.append(f"likes_count:{track_data.get('likes_count', 0)}")

    return "|".join(details_parts)


def track_to_ma_track(
    provider: MusicProvider,
    track_data: dict[str, Any],
    show_data: dict[str, Any] | None = None,
) -> Track:
    """Convert a Phish.in track to a Music Assistant Track."""
    track_id = str(track_data.get("id", ""))

    # Extract song info and version
    songs = track_data.get("songs", [])
    song_data = songs[0] if songs else {}
    full_title = track_data.get("title", "Unknown Song")
    song_title, version = _extract_version_from_title(full_title)

    # Extract basic track info
    duration_ms = track_data.get("duration")
    duration = int(duration_ms / 1000) if duration_ms else 0
    position = track_data.get("position")
    track_number = int(position) if position is not None else 0
    set_name = track_data.get("set_name", "")

    # Get show information
    if show_data is None:
        show_data = track_data.get("show", {})
    show_date = show_data.get("date", "")
    venue_name = show_data.get("venue", {}).get("name", "")

    # Create artist mapping
    phish_artist = ItemMapping(
        item_id=PHISH_ARTIST_ID,
        provider=provider.instance_id,
        name=PHISH_ARTIST_NAME,
        media_type=MediaType.ARTIST,
        available=True,
    )

    # Create album mapping with image
    album_mapping = _create_album_mapping(provider, show_date, show_data)

    # Build details string
    details = _build_track_details(track_data, song_data, show_date, set_name, venue_name)

    # Create metadata with image
    metadata = MediaItemMetadata()
    if show_data:
        image_url = show_data.get("album_cover_url")
        if image_url:
            metadata = MediaItemMetadata(
                images=UniqueList(
                    [
                        MediaItemImage(
                            type=ImageType.THUMB,
                            path=image_url,
                            provider=provider.instance_id,
                            remotely_accessible=True,
                        )
                    ]
                )
            )

    return Track(
        item_id=track_id,
        provider=provider.instance_id,
        name=song_title,
        version=version,
        artists=UniqueList([phish_artist]),
        album=album_mapping,
        duration=duration,
        track_number=track_number,
        metadata=metadata,
        provider_mappings={
            ProviderMapping(
                item_id=track_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                available=bool(track_data.get("mp3_url")),
                audio_format=AudioFormat(content_type=ContentType.MP3),
                url=track_data.get("mp3_url"),
                details=details,
            )
        },
    )


def playlist_to_ma_playlist(provider: MusicProvider, playlist_data: dict[str, Any]) -> Playlist:
    """Convert phish.in playlist data to Music Assistant Playlist."""
    playlist_id = str(playlist_data["id"])

    metadata = MediaItemMetadata(
        description=playlist_data.get("description"),
        images=UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=FALLBACK_ALBUM_IMAGE,
                    provider=provider.instance_id,
                    remotely_accessible=True,
                )
            ]
        ),
    )

    return Playlist(
        item_id=playlist_id,
        provider=provider.instance_id,
        name=playlist_data.get("name", ""),
        owner=playlist_data.get("username", ""),
        is_editable=False,
        metadata=metadata,
        provider_mappings={
            ProviderMapping(
                item_id=playlist_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                available=True,
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
    search_query: str,
) -> tuple[list[Artist], list[Album], list[Track], list[Playlist]]:
    """Parse search results into MA media items."""
    search_term = search_query.lower()

    def contains_search_term(text: str | None) -> bool:
        return search_term in text.lower() if text else False

    def strip_performance_indicators(title: str) -> str:
        """Strip performance indicators like (Set1), (Soundcheck), etc. from title."""
        song_title = title
        performance_indicators = ["set", "soundcheck", "check", "encore"]

        # Check for prefix: "(Check) Song"
        if song_title.startswith("(") and ") " in song_title:
            end_paren = song_title.index(") ")
            prefix = song_title[1:end_paren]
            if any(indicator in prefix.lower() for indicator in performance_indicators):
                song_title = song_title[end_paren + 2 :]

        # Check for suffix: "Song (Set1)"
        if " (" in song_title and song_title.endswith(")"):
            base_title, suffix = song_title.rsplit(" (", 1)
            suffix = suffix.rstrip(")")
            if any(indicator in suffix.lower() for indicator in performance_indicators):
                song_title = base_title

        return song_title

    artists: list[Artist] = _parse_artists(provider, media_types)
    albums: list[Album] = _parse_albums(provider, search_data, media_types, contains_search_term)
    tracks: list[Track] = _parse_tracks(
        provider, search_data, media_types, contains_search_term, strip_performance_indicators
    )
    playlists: list[Playlist] = _parse_playlists(
        provider, search_data, media_types, contains_search_term
    )

    return artists, albums, tracks, playlists


def _parse_artists(provider: MusicProvider, media_types: list[MediaType]) -> list[Artist]:
    """Parse artists from search results."""
    artists: list[Artist] = []
    if MediaType.ARTIST in media_types:
        metadata = MediaItemMetadata(
            images=UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=FALLBACK_ALBUM_IMAGE,
                        provider=provider.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )
        )

        phish_artist_full = Artist(
            item_id=PHISH_ARTIST_ID,
            provider=provider.instance_id,
            name=PHISH_ARTIST_NAME,
            metadata=metadata,
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

    return artists


def _parse_albums(
    provider: MusicProvider,
    search_data: dict[str, Any],
    media_types: list[MediaType],
    contains_search_term: Callable[[str | None], bool],
) -> list[Album]:
    """Parse albums from search results."""
    albums: list[Album] = []
    if MediaType.ALBUM not in media_types:
        return albums

    # Add exact show if present
    if search_data.get("exact_show"):
        show = search_data["exact_show"]
        venue_name = show.get("venue_name", "")
        if contains_search_term(venue_name):
            albums.append(show_to_album(provider, show))

    # Add other shows
    for show in search_data.get("other_shows", []):
        venue_name = show.get("venue_name", "")
        if contains_search_term(venue_name):
            albums.append(show_to_album(provider, show))

    # Add venue shows (from additional API calls)
    for show in search_data.get("venue_shows", []):
        venue_name = show.get("venue_name", "")
        if contains_search_term(venue_name):
            albums.append(show_to_album(provider, show))

    return albums


def _parse_tracks(
    provider: MusicProvider,
    search_data: dict[str, Any],
    media_types: list[MediaType],
    contains_search_term: Callable[[str | None], bool],
    strip_performance_indicators: Callable[[str], str],
) -> list[Track]:
    """Parse tracks from search results."""
    tracks: list[Track] = []
    if MediaType.TRACK not in media_types:
        return tracks

    for track_data in search_data.get("tracks", []):
        full_title = track_data.get("title", "")
        # Strip performance indicators to get base song name for matching
        clean_title = strip_performance_indicators(full_title)

        if contains_search_term(clean_title):
            # Extract show data from track data for image
            show_data = {
                "date": track_data.get("show_date"),
                "album_cover_url": track_data.get("show_album_cover_url"),
                "venue": {"name": track_data.get("venue_name")},
            }
            tracks.append(track_to_ma_track(provider, track_data, show_data))

    # Deduplicate by album - only return one track per show
    seen_albums = set()
    unique_tracks = []
    for track in tracks:
        album_id = track.album.item_id if track.album else None
        if album_id and album_id not in seen_albums:
            seen_albums.add(album_id)
            unique_tracks.append(track)
        elif not album_id:
            unique_tracks.append(track)

    return unique_tracks


def _parse_playlists(
    provider: MusicProvider,
    search_data: dict[str, Any],
    media_types: list[MediaType],
    contains_search_term: Callable[[str | None], bool],
) -> list[Playlist]:
    """Parse playlists from search results."""
    playlists: list[Playlist] = []
    if MediaType.PLAYLIST in media_types:
        for playlist_data in search_data.get("playlists", []):
            playlist_name = playlist_data.get("name", "")
            if contains_search_term(playlist_name):
                playlists.append(playlist_to_ma_playlist(provider, playlist_data))

    return playlists
