"""Parsers for ListenBrainz API responses."""

from __future__ import annotations

import html
import re
from typing import Any

from music_assistant_models.enums import ExternalID, MediaType
from music_assistant_models.media_items import (
    Artist,
    ItemMapping,
    MediaItemMetadata,
    Playlist,
    ProviderMapping,
    Track,
    UniqueList,
)

JSPF_PLAYLIST_EXTENSION = "https://musicbrainz.org/doc/jspf#playlist"
JSPF_TRACK_EXTENSION = "https://musicbrainz.org/doc/jspf#track"


def parse_playlist(
    playlist_data: dict[str, Any], provider_instance: str, provider_domain: str
) -> Playlist:
    """Parse a ListenBrainz JSPF playlist."""
    playlist_id = playlist_data["identifier"].rsplit("/", 1)[-1]
    annotation = playlist_data.get("annotation")
    if isinstance(annotation, str):
        annotation = html.unescape(annotation)
        annotation = re.sub(r"<[^>]+>", " ", annotation)
        annotation = " ".join(annotation.split())
        if not annotation:
            annotation = None
    return Playlist(
        item_id=playlist_id,
        provider=provider_instance,
        name=playlist_data["title"],
        owner="listenbrainz",
        is_editable=False,
        metadata=MediaItemMetadata(description=annotation) if annotation else MediaItemMetadata(),
        provider_mappings={
            ProviderMapping(
                item_id=playlist_id,
                provider_domain=provider_domain,
                provider_instance=provider_instance,
                is_unique=True,
            )
        },
    )


def parse_playlist_source_patch(playlist_data: dict[str, Any]) -> str | None:
    """Return the ListenBrainz algorithm source patch for a JSPF playlist."""
    try:
        source_patch = playlist_data["extension"][JSPF_PLAYLIST_EXTENSION]["additional_metadata"][
            "algorithm_metadata"
        ]["source_patch"]
    except KeyError, TypeError:
        return None
    return source_patch if isinstance(source_patch, str) else None


def parse_playlist_last_modified(playlist_data: dict[str, Any]) -> str | None:
    """Return the ``last_modified_at`` timestamp for a JSPF playlist, if present."""
    try:
        last_modified = playlist_data["extension"][JSPF_PLAYLIST_EXTENSION]["last_modified_at"]
    except KeyError, TypeError:
        return None
    return last_modified if isinstance(last_modified, str) else None


def parse_track(track_data: dict[str, Any], provider_instance: str, provider_domain: str) -> Track:
    """Parse a JSPF playlist entry into an unavailable ListenBrainz track."""
    recording_mbid = track_data["identifier"][0].rsplit("/", 1)[-1]
    album_name = track_data.get("album")
    return Track(
        item_id=recording_mbid,
        provider=provider_instance,
        name=track_data.get("title") or recording_mbid,
        duration=(track_data.get("duration") or 0) // 1000,
        artists=UniqueList(parse_artists(track_data, provider_instance, provider_domain)),
        album=(
            ItemMapping(
                item_id=album_name,
                provider=provider_instance,
                name=album_name,
                media_type=MediaType.ALBUM,
            )
            if album_name
            else None
        ),
        provider_mappings={
            ProviderMapping(
                item_id=recording_mbid,
                provider_domain=provider_domain,
                provider_instance=provider_instance,
                available=False,
            )
        },
        external_ids={(ExternalID.MB_RECORDING, recording_mbid)},
    )


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
