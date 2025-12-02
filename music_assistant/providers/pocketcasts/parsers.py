"""Parsing utilities to convert Pocket Casts API responses into Music Assistant model objects."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ContentType, ImageType, MediaType
from music_assistant_models.media_items import (
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    MediaItemMetadata,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    UniqueList,
)

from .constants import (
    POCKETCASTS_ARTWORK_URL,
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
    STATUS_NOT_PLAYED,
)

if TYPE_CHECKING:
    from .provider import PocketCastsProvider


def parse_podcast(provider: PocketCastsProvider, podcast_data: dict[str, Any]) -> Podcast:
    """Parse a podcast from Pocket Casts API response into a Podcast object.

    :param provider: The PocketCastsProvider instance.
    :param podcast_data: Raw podcast data from API.
    :return: Parsed Podcast object.
    """
    podcast_uuid = podcast_data.get("uuid", "")
    title = podcast_data.get("title", "Unknown Podcast")
    author = podcast_data.get("author", "")
    description = podcast_data.get("description", "")

    # Build artwork URL
    artwork_url = POCKETCASTS_ARTWORK_URL.format(uuid=podcast_uuid)

    # Create metadata with artwork
    metadata = MediaItemMetadata(
        description=description,
        images=UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=artwork_url,
                    provider=provider.lookup_key,
                    remotely_accessible=True,
                )
            ]
        ),
    )

    # Create provider mapping
    provider_mapping = ProviderMapping(
        item_id=podcast_uuid,
        provider_domain=provider.domain,
        provider_instance=provider.instance_id,
        available=True,
    )

    return Podcast(
        item_id=podcast_uuid,
        provider=provider.domain,
        name=title,
        publisher=author,
        provider_mappings={provider_mapping},
        metadata=metadata,
    )


def parse_podcast_episode(
    provider: PocketCastsProvider,
    episode_data: dict[str, Any],
    podcast_uuid: str,
    podcast_title: str,
    podcast_artwork_url: str,
    position: int,
    progress_info: dict[str, Any] | None = None,
) -> PodcastEpisode | None:
    """Parse an episode from Pocket Casts API response into a PodcastEpisode object.

    :param provider: The PocketCastsProvider instance.
    :param episode_data: Raw episode data from API.
    :param podcast_uuid: UUID of the parent podcast.
    :param podcast_title: Title of the parent podcast.
    :param podcast_artwork_url: Artwork URL for the podcast (fallback).
    :param position: Position/index of the episode.
    :param progress_info: Optional progress info dict with playingStatus and playedUpTo.
    :return: PodcastEpisode or None if essential data is missing.
    """
    episode_uuid = episode_data.get("uuid")
    if not episode_uuid:
        return None

    title = episode_data.get("title", "Unknown Episode")
    stream_url = episode_data.get("url")
    if not stream_url:
        provider.logger.debug("Episode %s has no stream URL, skipping", episode_uuid)
        return None

    duration = episode_data.get("duration", 0)
    file_type = episode_data.get("fileType", "")
    published = episode_data.get("published")  # ISO 8601 string

    # Episode ID format: "{podcast_uuid} {episode_uuid}"
    episode_id = f"{podcast_uuid} {episode_uuid}"

    # Determine content type from file_type
    content_type = ContentType.try_parse(file_type) if file_type else ContentType.UNKNOWN
    if content_type == ContentType.UNKNOWN and stream_url:
        content_type = ContentType.try_parse(stream_url)

    # Create the episode
    episode = PodcastEpisode(
        item_id=episode_id,
        provider=provider.lookup_key,
        name=title,
        duration=duration,
        position=position,
        podcast=ItemMapping(
            item_id=podcast_uuid,
            provider=provider.lookup_key,
            name=podcast_title,
            media_type=MediaType.PODCAST,
        ),
        provider_mappings={
            ProviderMapping(
                item_id=episode_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                audio_format=AudioFormat(content_type=content_type),
                url=stream_url,
            )
        },
    )

    # Parse and set release date if available
    if published:
        try:
            # Handle ISO 8601 format (e.g., "2025-01-15T06:00:00Z")
            # Replace "Z" suffix with "+00:00" for broader compatibility
            if published.endswith("Z"):
                published = published.replace("Z", "+00:00")
            release_date = datetime.fromisoformat(published)
            episode.metadata.release_date = release_date
        except ValueError:
            pass  # Ignore invalid date format

    # Set episode artwork (fallback to podcast artwork)
    episode.metadata.images = UniqueList(
        [
            MediaItemImage(
                type=ImageType.THUMB,
                path=podcast_artwork_url,
                provider=provider.lookup_key,
                remotely_accessible=True,
            )
        ]
    )

    # Apply progress info if available
    if progress_info:
        playing_status = progress_info.get("playingStatus")
        played_up_to = progress_info.get("playedUpTo")

        if playing_status == STATUS_COMPLETED:
            episode.fully_played = True
            episode.resume_position_ms = 0
        elif playing_status == STATUS_IN_PROGRESS and played_up_to is not None:
            episode.fully_played = False
            episode.resume_position_ms = played_up_to * 1000  # Convert to ms
        elif playing_status == STATUS_NOT_PLAYED:
            episode.fully_played = False
            episode.resume_position_ms = 0

    return episode


def parse_browse_episode(
    provider: PocketCastsProvider, ep_data: dict[str, Any]
) -> PodcastEpisode | None:
    """Parse an episode from browse API response.

    :param provider: The PocketCastsProvider instance.
    :param ep_data: Episode data from browse APIs (in_progress, starred, etc.).
    :return: Parsed PodcastEpisode or None if essential data is missing.
    """
    episode_uuid = ep_data.get("uuid")
    if not episode_uuid:
        return None

    # Different APIs use different keys for podcast UUID
    podcast_uuid = ep_data.get("podcastUuid") or ep_data.get("podcast", "")
    title = ep_data.get("title", "Unknown Episode")
    podcast_title = ep_data.get("podcastTitle", "Unknown Podcast")

    # Episode ID format: "{podcast_uuid} {episode_uuid}" (matching main parser)
    episode_id = f"{podcast_uuid} {episode_uuid}"

    # Duration in seconds
    duration = ep_data.get("duration", 0)

    # Determine played status from playingStatus field
    playing_status = ep_data.get("playingStatus", STATUS_NOT_PLAYED)
    played_up_to = ep_data.get("playedUpTo", 0)

    if playing_status == STATUS_COMPLETED:
        fully_played = True
        resume_position_ms = 0
    elif playing_status == STATUS_IN_PROGRESS and played_up_to is not None:
        fully_played = False
        resume_position_ms = int(played_up_to * 1000)
    else:
        fully_played = False
        resume_position_ms = 0

    # Build images from podcast UUID
    images: UniqueList[MediaItemImage] = UniqueList()
    if podcast_uuid:
        artwork_url = POCKETCASTS_ARTWORK_URL.format(uuid=podcast_uuid)
        images.append(
            MediaItemImage(
                type=ImageType.THUMB,
                path=artwork_url,
                provider=provider.lookup_key,
                remotely_accessible=True,
            )
        )

    # Create provider mapping
    provider_mapping = ProviderMapping(
        item_id=episode_id,
        provider_domain=provider.domain,
        provider_instance=provider.instance_id,
        available=True,
        audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
        url=ep_data.get("url", ""),
    )

    # Create metadata
    metadata = MediaItemMetadata(
        description=ep_data.get("showNotes"),
        images=images if images else None,
    )

    # Create podcast item mapping for the parent podcast
    podcast_mapping = ItemMapping(
        media_type=MediaType.PODCAST,
        item_id=podcast_uuid,
        provider=provider.lookup_key,
        name=podcast_title,
    )

    return PodcastEpisode(
        item_id=episode_id,
        provider=provider.lookup_key,
        name=title,
        duration=duration,
        position=0,
        podcast=podcast_mapping,
        provider_mappings={provider_mapping},
        metadata=metadata,
        fully_played=fully_played,
        resume_position_ms=resume_position_ms,
    )


def parse_bookmark(provider: PocketCastsProvider, bookmark: dict[str, Any]) -> PodcastEpisode:
    """Parse a bookmark into a PodcastEpisode.

    The bookmark will be displayed with its title and will start playback
    at the bookmarked timestamp.

    :param provider: The PocketCastsProvider instance.
    :param bookmark: Bookmark data from /user/bookmark/list API.
    :return: Parsed PodcastEpisode that starts at the bookmark timestamp.
    """
    podcast_uuid = bookmark.get("podcastUuid", "")
    episode_uuid = bookmark.get("episodeUuid", "")
    bookmark_title = bookmark.get("title", "Bookmark")
    bookmark_time = bookmark.get("time", 0)  # Seconds into episode

    # Episode ID format: "{podcast_uuid} {episode_uuid}@bookmark:{timestamp}"
    # The @bookmark:{timestamp} suffix tells get_resume_position to use this timestamp
    # instead of fetching from the API
    episode_id = f"{podcast_uuid} {episode_uuid}@bookmark:{bookmark_time}"

    # Build images from podcast UUID
    images: UniqueList[MediaItemImage] = UniqueList()
    if podcast_uuid:
        artwork_url = POCKETCASTS_ARTWORK_URL.format(uuid=podcast_uuid)
        images.append(
            MediaItemImage(
                type=ImageType.THUMB,
                path=artwork_url,
                provider=provider.lookup_key,
                remotely_accessible=True,
            )
        )

    # Create provider mapping - URL will be fetched when playing
    provider_mapping = ProviderMapping(
        item_id=episode_id,
        provider_domain=provider.domain,
        provider_instance=provider.instance_id,
        available=True,
        audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
    )

    # Create metadata with bookmark info
    metadata = MediaItemMetadata(
        images=images if images else None,
    )

    # Create podcast item mapping
    podcast_mapping = ItemMapping(
        media_type=MediaType.PODCAST,
        item_id=podcast_uuid,
        provider=provider.lookup_key,
        name="",  # We don't have podcast title from bookmark API
    )

    # Use bookmark title as episode name, include timestamp info
    display_name = f"{bookmark_title} @ {format_timestamp(bookmark_time)}"

    return PodcastEpisode(
        item_id=episode_id,
        provider=provider.lookup_key,
        name=display_name,
        duration=0,  # Unknown from bookmark data
        position=0,
        podcast=podcast_mapping,
        provider_mappings={provider_mapping},
        metadata=metadata,
        fully_played=False,
        resume_position_ms=bookmark_time * 1000,  # Start at bookmark timestamp
    )


def format_timestamp(seconds: int) -> str:
    """Format seconds into MM:SS or HH:MM:SS string.

    :param seconds: Number of seconds to format.
    :return: Formatted timestamp string.
    """
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
