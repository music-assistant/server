"""Helper functions and parsers for the Global Player music provider."""

from __future__ import annotations

from typing import Any

from music_assistant_models.enums import ImageType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    MediaItemImage,
    MediaItemMetadata,
    ProviderMapping,
    Radio,
)


def parse_radio(station_data: dict[str, Any], instance_id: str, provider_domain: str) -> Radio:
    """
    Parse a station dictionary from Global Player API into a Radio item.

    :param station_data: Station dictionary from the Global Player brands API.
    :param instance_id: The provider instance ID.
    :param provider_domain: The provider domain string.
    """
    station_id = str(station_data.get("id", ""))
    if not station_id:
        raise MediaNotFoundError("Station data missing id")

    name = str(station_data.get("name") or station_data.get("title") or "Unknown Station")
    tagline = station_data.get("tagline") or station_data.get("description")

    radio = Radio(
        provider=instance_id,
        item_id=station_id,
        name=name,
        metadata=MediaItemMetadata(
            description=tagline,
        ),
        provider_mappings={
            ProviderMapping(
                item_id=station_id,
                provider_domain=provider_domain,
                provider_instance=instance_id,
                available=True,
            )
        },
    )

    logo_url = station_data.get("brandLogo") or station_data.get("imageUrl")
    if logo_url:
        radio.metadata.add_image(
            MediaItemImage(
                provider=instance_id,
                type=ImageType.THUMB,
                path=logo_url,
                remotely_accessible=True,
            )
        )

    return radio


def parse_stream_url(playable_data: dict[str, Any]) -> str:
    """
    Extract the playable stream URL from a Global Player playables API response.

    :param playable_data: JSON response dictionary from the playables endpoint.
    """
    playback_entries = playable_data.get("playback", [])
    if isinstance(playback_entries, list):
        # Look for the public stream marked usable without authentication
        for entry in playback_entries:
            if not isinstance(entry, dict):
                continue
            if entry.get("canUse") == "true" and (url := entry.get("url")):
                return str(url)

    station_id = playable_data.get("id", "unknown")
    raise MediaNotFoundError(f"No playable stream found for station {station_id}")
