"""Helper functions for the ABC Radio provider."""

from typing import Any

from music_assistant_models.enums import ImageType
from music_assistant_models.media_items import MediaItemImage, ProviderMapping, Radio
from music_assistant_models.streamdetails import StreamMetadata

from .constants import ABC_RADIO_STATIONS


def parse_radio(station_id: str, instance_id: str, provider_domain: str) -> Radio:
    """
    Create a Radio object from static station information.

    :param station_id: A known ABC Radio station id; callers must validate.
    :param instance_id: The provider instance id.
    :param provider_domain: The provider domain string.
    """
    station = ABC_RADIO_STATIONS[station_id]
    radio = Radio(
        provider=instance_id,
        item_id=station_id,
        name=station["name"],
        provider_mappings={
            ProviderMapping(
                item_id=station_id,
                provider_domain=provider_domain,
                provider_instance=instance_id,
                available=True,
            )
        },
    )
    radio.metadata.description = station["description"]
    radio.metadata.add_image(
        MediaItemImage(
            provider=instance_id,
            type=ImageType.THUMB,
            path=station["logo_url"],
            remotely_accessible=True,
        )
    )
    return radio


def parse_now_playing(data: dict[str, Any]) -> StreamMetadata | None:
    """
    Parse a now-playing API response into StreamMetadata.

    Returns None when no track is currently playing (e.g. talk programming).

    :param data: JSON response from the ABC core-next musicNowPlaying API.
    """
    # mapiNow is null when nothing is playing.
    play: dict[str, Any] = data.get("mapiNow") or {}
    title = play.get("title")
    if not title:
        return None

    release: dict[str, Any] = play.get("release") or {}
    album = play.get("releaseTitle") or release.get("title")
    # Only the Classic stations fill in properties (year, performers, label).
    if album and (year := (play.get("properties") or {}).get("year")):
        album = f"{album} ({year})"

    # Prefer the full-size release artwork; primaryImage is a 340px resize.
    artwork: list[dict[str, Any]] = release.get("artwork") or []
    image_url = (artwork[0].get("url") if artwork else None) or play.get("primaryImage")

    return StreamMetadata(
        title=title,
        artist=play.get("artist") or None,
        album=album,
        image_url=image_url,
        duration=play.get("duration"),
    )
