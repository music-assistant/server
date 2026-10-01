"""Helper functions and parsers for the Global Player music provider."""

from __future__ import annotations

from typing import Any

from music_assistant_models.enums import ImageType
from music_assistant_models.errors import UnplayableMediaError
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
    station_id = str(station_data["id"])

    radio = Radio(
        provider=instance_id,
        item_id=station_id,
        name=station_data["name"],
        metadata=MediaItemMetadata(
            description=station_data.get("tagline"),
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

    logo_url = station_data.get("brandLogo")
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


def parse_stream_url(playable_data: dict[str, Any], station_id: str) -> str:
    """
    Extract the playable stream URL from a Global Player playables API response.

    :param playable_data: JSON response dictionary from the playables endpoint.
    :param station_id: The station identifier.
    """
    playback_entries: list[dict[str, Any]] = playable_data.get("playback", [])
    for entry in playback_entries:
        if entry.get("canUse") == "true":
            url: str = entry["url"]
            return url
    raise UnplayableMediaError(f"No free stream available for station {station_id}")
