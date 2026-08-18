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

    :param data: JSON response from the ABC Radio now-playing API.
    """
    play: dict[str, Any] = data.get("now") or {}
    recording: dict[str, Any] = play.get("recording") or {}
    title = recording.get("title")
    if not title:
        return None

    # Prefer the display artist ABC's own players use; the recording artist list
    # contains separate performer/composer entries (e.g. for classical works).
    artist = (play.get("summary") or {}).get("artist") or ", ".join(
        name
        for entry in recording.get("artists") or []
        if entry.get("type") == "primary" and (name := entry.get("name"))
    )

    # The play-level release is not always populated; fall back to the
    # first release attached to the recording.
    releases: list[dict[str, Any]] = recording.get("releases") or []
    release: dict[str, Any] = play.get("release") or (releases[0] if releases else {})
    album = release.get("title")
    if album and (year := release.get("release_year")):
        album = f"{album} ({year})"

    artwork: list[dict[str, Any]] = release.get("artwork") or []
    image_url = artwork[0].get("url") if artwork else None

    return StreamMetadata(
        title=title,
        artist=artist or None,
        album=album,
        image_url=image_url,
        duration=recording.get("duration"),
    )
