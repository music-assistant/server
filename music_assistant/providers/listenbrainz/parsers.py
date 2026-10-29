"""Parsers for ListenBrainz API responses."""

from __future__ import annotations

from typing import Any

from music_assistant_models.enums import ExternalID
from music_assistant_models.media_items import Artist, ProviderMapping, UniqueList

JSPF_TRACK_EXTENSION = "https://musicbrainz.org/doc/jspf#track"


def parse_artists(
    track_data: dict[str, Any], provider_instance: str, provider_domain: str
) -> UniqueList[Artist]:
    """Parse the artist credits from a JSPF track."""
    artist_data = track_data["extension"][JSPF_TRACK_EXTENSION]["additional_metadata"]["artists"]
    return UniqueList(
        parse_artist(item, provider_instance, provider_domain) for item in artist_data
    )


def parse_artist(
    artist_data: dict[str, Any], provider_instance: str, provider_domain: str
) -> Artist:
    """Parse a ListenBrainz artist credit."""
    mbid = artist_data["artist_mbid"]
    return Artist(
        item_id=mbid,
        provider=provider_instance,
        name=artist_data["artist_credit_name"],
        provider_mappings={
            ProviderMapping(
                item_id=mbid,
                provider_domain=provider_domain,
                provider_instance=provider_instance,
                available=False,
            )
        },
        external_ids={(ExternalID.MB_ARTIST, mbid)},
    )
