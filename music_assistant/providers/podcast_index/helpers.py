"""Helper functions for Podcast Index provider."""

from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import aiohttp
from music_assistant_models.enums import ContentType, ImageType, MediaType
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    ProviderUnavailableError,
)
from music_assistant_models.media_items import (
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    UniqueList,
)

from .constants import API_BASE_URL

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


async def make_api_request(
    mass: MusicAssistant,
    api_key: str,
    api_secret: str,
    endpoint: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Make authenticated request to Podcast Index API.

    Handles authentication using SHA1 hash of API key, secret, and timestamp.
    Maps HTTP errors appropriately: 401 -> LoginFailed, others -> ProviderUnavailableError.
    """
    # Prepare authentication headers
    auth_date = str(int(time.time()))
    auth_string = api_key + api_secret + auth_date
    auth_hash = hashlib.sha1(auth_string.encode()).hexdigest()

    headers = {
        "X-Auth-Key": api_key,
        "X-Auth-Date": auth_date,
        "Authorization": auth_hash,
    }

    url = f"{API_BASE_URL}/{endpoint}"

    try:
        async with mass.http_session.get(url, headers=headers, params=params or {}) as response:
            response.raise_for_status()

            try:
                data: dict[str, Any] = await response.json()
            except aiohttp.ContentTypeError as err:
                raise InvalidDataError("Invalid JSON response from API") from err

            if str(data.get("status")).lower() != "true":
                raise InvalidDataError(data.get("description") or "API error")

            return data

    except aiohttp.ClientConnectorError as err:
        raise ProviderUnavailableError(f"Failed to connect to Podcast Index API: {err}") from err
    except aiohttp.ServerTimeoutError as err:
        raise ProviderUnavailableError(f"Podcast Index API timeout: {err}") from err
    except aiohttp.ClientResponseError as err:
        if err.status == 401:
            raise LoginFailed(f"Authentication failed: {err.status}") from err
        raise ProviderUnavailableError(f"API request failed: {err.status}") from err


def parse_podcast_from_feed(
    feed_data: dict[str, Any], instance_id: str, domain: str
) -> Podcast | None:
    """Parse podcast from API feed data."""
    feed_url = feed_data.get("url")
    podcast_id = feed_data.get("id")

    if not feed_url or not podcast_id:
        return None

    podcast = Podcast(
        item_id=str(podcast_id),
        name=feed_data.get("title", "Unknown Podcast"),
        publisher=feed_data.get("author") or feed_data.get("ownerName", "Unknown"),
        provider=instance_id,
        provider_mappings={
            ProviderMapping(
                item_id=str(podcast_id),
                provider_domain=domain,
                provider_instance=instance_id,
                url=feed_url,
            )
        },
    )

    # Add metadata
    podcast.metadata.description = feed_data.get("description", "")
    podcast.metadata.explicit = bool(feed_data.get("explicit", False))

    # Set episode count only if provided
    episode_count = feed_data.get("episodeCount")
    if episode_count is not None:
        podcast.total_episodes = int(episode_count) or 0

    # Add image - prefer 'image' field, fallback to 'artwork'
    image_url = feed_data.get("image") or feed_data.get("artwork")
    if image_url:
        podcast.metadata.add_image(
            MediaItemImage(
                type=ImageType.THUMB,
                path=image_url,
                provider=instance_id,
                remotely_accessible=True,
            )
        )

    # Add categories as genres - categories is a dict {id: name}
    categories = feed_data.get("categories", {})
    if categories and isinstance(categories, dict):
        podcast.metadata.genres = set(categories.values())

    # Add language
    language = feed_data.get("language", "")
    if language:
        podcast.metadata.languages = UniqueList([language])

    return podcast


def parse_episode_from_data(
    episode_data: dict[str, Any],
    podcast_id: str,
    episode_idx: int,
    instance_id: str,
    domain: str,
    podcast_name: str | None = None,
) -> PodcastEpisode | None:
    """Parse episode from API episode data."""
    episode_api_id = episode_data.get("id")
    if not episode_api_id:
        return None

    episode_id = f"{podcast_id}|{episode_api_id}"

    position = episode_data.get("episode")
    if position is None:
        position = episode_idx + 1

    if podcast_name is None:
        podcast_name = episode_data.get("feedTitle") or "Unknown Podcast"

    raw_duration = episode_data.get("duration")
    try:
        duration = int(raw_duration) if raw_duration is not None else 0
    except (ValueError, TypeError):
        duration = 0

    episode = PodcastEpisode(
        item_id=episode_id,
        provider=instance_id,
        name=episode_data.get("title", "Unknown Episode"),
        duration=duration,
        position=position,
        podcast=ItemMapping(
            item_id=podcast_id,
            provider=instance_id,
            name=podcast_name,
            media_type=MediaType.PODCAST,
        ),
        provider_mappings={
            ProviderMapping(
                item_id=episode_id,
                provider_domain=domain,
                provider_instance=instance_id,
                available=True,
                audio_format=AudioFormat(
                    content_type=ContentType.try_parse(
                        episode_data.get("enclosureType") or "audio/mpeg"
                    ),
                ),
                url=episode_data.get("enclosureUrl"),
            )
        },
    )

    # Add metadata
    episode.metadata.description = episode_data.get("description", "")
    episode.metadata.explicit = bool(episode_data.get("explicit", 0))

    date_published = episode_data.get("datePublished")
    if date_published:
        episode.metadata.release_date = datetime.fromtimestamp(date_published, tz=UTC)

    image_url = episode_data.get("image") or episode_data.get("feedImage")
    if image_url:
        episode.metadata.add_image(
            MediaItemImage(
                type=ImageType.THUMB,
                path=image_url,
                provider=instance_id,
                remotely_accessible=True,
            )
        )

    return episode
