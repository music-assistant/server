"""Parsers for ListenBrainz API responses."""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING

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

from music_assistant.constants import EXTERNAL_ID_PROVIDER
from music_assistant.helpers.external_ids import external_item_id

if TYPE_CHECKING:
    from music_assistant.providers.listenbrainz.models import (
        ListenBrainzArtist,
        ListenBrainzPlaylist,
        ListenBrainzTrack,
    )


def parse_playlist(
    playlist_data: ListenBrainzPlaylist, provider_instance: str, provider_domain: str
) -> Playlist:
    """Parse a ListenBrainz JSPF playlist."""
    playlist_id = playlist_data.playlist_id
    annotation = playlist_data.annotation
    if isinstance(annotation, str):
        annotation = html.unescape(annotation)
        annotation = re.sub(r"<[^>]+>", " ", annotation)
        annotation = " ".join(annotation.split())
        if not annotation:
            annotation = None
    return Playlist(
        item_id=playlist_id,
        provider=provider_instance,
        name=playlist_data.title,
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


def parse_track(
    track_data: ListenBrainzTrack, provider_instance: str, provider_domain: str
) -> Track:
    """Parse a JSPF playlist entry into a track resolved by the core when opened or played."""
    recording_mbid = track_data.recording_mbid
    album_name = track_data.album_name
    return Track(
        item_id=external_item_id(ExternalID.MB_RECORDING, recording_mbid),
        provider=EXTERNAL_ID_PROVIDER,
        name=track_data.title or recording_mbid,
        duration=(track_data.duration_ms or 0) // 1000,
        artists=UniqueList(parse_artists(track_data.artists, provider_instance, provider_domain)),
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
        provider_mappings=set(),
        external_ids={(ExternalID.MB_RECORDING, recording_mbid)},
    )


def parse_artists(
    artist_data: tuple[ListenBrainzArtist, ...], provider_instance: str, provider_domain: str
) -> UniqueList[Artist]:
    """Parse the artist credits from a JSPF track."""
    return UniqueList(
        parse_artist(item, provider_instance, provider_domain) for item in artist_data
    )


def parse_artist(
    artist_data: ListenBrainzArtist, provider_instance: str, provider_domain: str
) -> Artist:
    """Parse a ListenBrainz artist credit."""
    mbid = artist_data.artist_mbid
    return Artist(
        item_id=mbid,
        provider=provider_instance,
        name=artist_data.name,
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
