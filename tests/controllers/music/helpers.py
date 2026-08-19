"""Shared helpers for the music controller tests."""

from __future__ import annotations

from music_assistant_models.enums import ExternalID
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ItemMapping,
    ProviderMapping,
    Track,
    UniqueList,
)

ISRC = "USRC17607839"


def create_track(
    provider_instance: str,
    item_id: str,
    name: str = "Test Track",
    isrc: str = ISRC,
    duration: int = 200,
) -> Track:
    """
    Create a Track as it would be received from a music provider.

    :param provider_instance: The provider instance id the track originates from.
    :param item_id: The item id of the track on the provider.
    :param name: The track name.
    :param isrc: The ISRC external id to attach to the track.
    :param duration: The track duration in seconds.
    """
    provider_domain = provider_instance.split("_", maxsplit=1)[0]
    return Track(
        item_id=item_id,
        provider=provider_instance,
        name=name,
        duration=duration,
        external_ids={(ExternalID.ISRC, isrc)},
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=provider_domain,
                provider_instance=provider_instance,
                audio_format=AudioFormat(),
            )
        },
        artists=UniqueList(
            [
                Artist(
                    item_id=f"{item_id}_artist",
                    provider=provider_instance,
                    name="Test Artist",
                    provider_mappings={
                        ProviderMapping(
                            item_id=f"{item_id}_artist",
                            provider_domain=provider_domain,
                            provider_instance=provider_instance,
                            audio_format=AudioFormat(),
                        )
                    },
                )
            ]
        ),
    )


def create_album(
    provider_instance: str,
    item_id: str,
    name: str = "Test Album",
    artist_name: str | None = "Test Artist",
) -> Album:
    """
    Create an Album as it would be received from a music provider.

    :param provider_instance: The provider instance id the album originates from.
    :param item_id: The item id of the album on the provider.
    :param name: The album name.
    :param artist_name: The album artist name, or None for an album without artists.
    """
    provider_domain = provider_instance.split("_", maxsplit=1)[0]
    artists: UniqueList[Artist | ItemMapping] = UniqueList()
    if artist_name is not None:
        artists.append(
            Artist(
                item_id=f"{item_id}_artist",
                provider=provider_instance,
                name=artist_name,
                provider_mappings={
                    ProviderMapping(
                        item_id=f"{item_id}_artist",
                        provider_domain=provider_domain,
                        provider_instance=provider_instance,
                        audio_format=AudioFormat(),
                    )
                },
            )
        )
    return Album(
        item_id=item_id,
        provider=provider_instance,
        name=name,
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=provider_domain,
                provider_instance=provider_instance,
                audio_format=AudioFormat(),
            )
        },
        artists=artists,
    )
