"""
Seed tracks for dynamic-playlist generation.

Given a media item, return the representative tracks used to seed dynamic generation (the
radio_playlist provider, the smart_playlist seed mode): an artist's top tracks, an album's or
playlist's tracks, a sample of a genre's tracks, or the track itself. Items that can't seed
generation -- a radio station, audiobook, podcast, or a non-singer artist -- yield an empty list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import ArtistType, MediaType
from music_assistant_models.media_items import Track

from music_assistant.constants import DB_TABLE_GENRE_MEDIA_ITEM_MAPPING

if TYPE_CHECKING:
    from music_assistant_models.media_items import Artist, MediaItemType

    from music_assistant.mass import MusicAssistant

GENRE_SEED_SAMPLE_SIZE = 50


async def seed_tracks(mass: MusicAssistant, item: MediaItemType) -> list[Track]:
    """
    Return the representative seed tracks for a media item.

    :param mass: The MusicAssistant instance.
    :param item: The media item to derive seed tracks from.
    """
    if item.media_type == MediaType.TRACK:
        return [cast("Track", item)]
    if item.media_type == MediaType.ALBUM:
        return await mass.music.albums.tracks(item.item_id, item.provider, in_library_only=False)
    if item.media_type == MediaType.ARTIST:
        if cast("Artist", item).artist_type != ArtistType.SINGER:
            return []
        # prefer the top tracks as seeds, falling back to all tracks
        return await mass.music.artists.top_tracks(
            item.item_id, item.provider
        ) or await mass.music.artists.tracks(item.item_id, item.provider)
    if item.media_type == MediaType.PLAYLIST:
        return [
            track
            async for track in mass.music.playlists.tracks(item.item_id, item.provider)
            if isinstance(track, Track) and track.available
        ]
    if item.media_type == MediaType.GENRE:
        gm = DB_TABLE_GENRE_MEDIA_ITEM_MAPPING
        query = (
            f"EXISTS(SELECT 1 FROM {gm} gm WHERE gm.media_id = tracks.item_id "
            "AND gm.media_type = 'track' AND gm.genre_id = :genre_id)"
        )
        return await mass.music.tracks.get_library_items_by_query(
            extra_query_parts=[query],
            extra_query_params={"genre_id": int(item.item_id)},
            limit=GENRE_SEED_SAMPLE_SIZE,
            order_by="random",
        )
    return []
