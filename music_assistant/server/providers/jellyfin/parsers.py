"""Parse Jellyfin metadata into Music Assistant models."""

from __future__ import annotations

import logging
from logging import Logger
from typing import TYPE_CHECKING

from aiojellyfin import ImageType as JellyImageType

from music_assistant.common.models.enums import ContentType, ExternalID, ImageType, MediaType
from music_assistant.common.models.errors import InvalidDataError
from music_assistant.common.models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    Playlist,
    ProviderMapping,
    Track,
    UniqueList,
)

from .const import (
    DOMAIN,
    ITEM_KEY_ALBUM,
    ITEM_KEY_ALBUM_ARTIST,
    ITEM_KEY_ALBUM_ARTISTS,
    ITEM_KEY_ALBUM_ID,
    ITEM_KEY_ARTIST_ITEMS,
    ITEM_KEY_CAN_DOWNLOAD,
    ITEM_KEY_ID,
    ITEM_KEY_IMAGE_TAGS,
    ITEM_KEY_MEDIA_CODEC,
    ITEM_KEY_MEDIA_STREAMS,
    ITEM_KEY_MUSICBRAINZ_ALBUM,
    ITEM_KEY_MUSICBRAINZ_ARTIST,
    ITEM_KEY_MUSICBRAINZ_RELEASE_GROUP,
    ITEM_KEY_MUSICBRAINZ_TRACK,
    ITEM_KEY_NAME,
    ITEM_KEY_OVERVIEW,
    ITEM_KEY_PARENT_INDEX_NUM,
    ITEM_KEY_PRODUCTION_YEAR,
    ITEM_KEY_PROVIDER_IDS,
    ITEM_KEY_RUNTIME_TICKS,
    ITEM_KEY_SORT_NAME,
    ITEM_KEY_USER_DATA,
    MEDIA_IMAGE_TYPES,
    UNKNOWN_ARTIST_MAPPING,
    USER_DATA_KEY_IS_FAVORITE,
)

if TYPE_CHECKING:
    from aiojellyfin import Album as JellyAlbum
    from aiojellyfin import Artist as JellyArtist
    from aiojellyfin import Connection
    from aiojellyfin import MediaItem as JellyMediaItem
    from aiojellyfin import Playlist as JellyPlaylist
    from aiojellyfin import Track as JellyTrack


def parse_album(
    logger: Logger, instance_id: str, connection: Connection, jellyfin_album: JellyAlbum
) -> Album:
    """Parse a Jellyfin Album response to an Album model object."""
    album_id = jellyfin_album[ITEM_KEY_ID]
    album = Album(
        item_id=album_id,
        provider=DOMAIN,
        name=jellyfin_album[ITEM_KEY_NAME],
        provider_mappings={
            ProviderMapping(
                item_id=str(album_id),
                provider_domain=DOMAIN,
                provider_instance=instance_id,
            )
        },
    )
    if ITEM_KEY_PRODUCTION_YEAR in jellyfin_album:
        album.year = jellyfin_album[ITEM_KEY_PRODUCTION_YEAR]
    album.metadata.images = _get_artwork(instance_id, connection, jellyfin_album)
    if ITEM_KEY_OVERVIEW in jellyfin_album:
        album.metadata.description = jellyfin_album[ITEM_KEY_OVERVIEW]
    if ITEM_KEY_MUSICBRAINZ_ALBUM in jellyfin_album[ITEM_KEY_PROVIDER_IDS]:
        try:
            album.add_external_id(
                ExternalID.MB_ALBUM,
                jellyfin_album[ITEM_KEY_PROVIDER_IDS][ITEM_KEY_MUSICBRAINZ_ALBUM],
            )
        except InvalidDataError as error:
            logger.warning(
                "Jellyfin has an invalid musicbrainz album id for album %s",
                album.name,
                exc_info=error if logger.isEnabledFor(logging.DEBUG) else None,
            )
    if ITEM_KEY_MUSICBRAINZ_RELEASE_GROUP in jellyfin_album[ITEM_KEY_PROVIDER_IDS]:
        try:
            album.add_external_id(
                ExternalID.MB_RELEASEGROUP,
                jellyfin_album[ITEM_KEY_PROVIDER_IDS][ITEM_KEY_MUSICBRAINZ_RELEASE_GROUP],
            )
        except InvalidDataError as error:
            logger.warning(
                "Jellyfin has an invalid musicbrainz id for album %s",
                album.name,
                exc_info=error if logger.isEnabledFor(logging.DEBUG) else None,
            )
    if ITEM_KEY_SORT_NAME in jellyfin_album:
        album.sort_name = jellyfin_album[ITEM_KEY_SORT_NAME]
    if ITEM_KEY_ALBUM_ARTIST in jellyfin_album:
        for album_artist in jellyfin_album[ITEM_KEY_ALBUM_ARTISTS]:
            album.artists.append(
                ItemMapping(
                    media_type=MediaType.ARTIST,
                    item_id=album_artist[ITEM_KEY_ID],
                    provider=instance_id,
                    name=album_artist[ITEM_KEY_NAME],
                )
            )
    elif len(jellyfin_album.get(ITEM_KEY_ARTIST_ITEMS, [])) >= 1:
        for artist_item in jellyfin_album[ITEM_KEY_ARTIST_ITEMS]:
            album.artists.append(
                ItemMapping(
                    media_type=MediaType.ARTIST,
                    item_id=artist_item[ITEM_KEY_ID],
                    provider=instance_id,
                    name=artist_item[ITEM_KEY_NAME],
                )
            )
    else:
        album.artists.append(UNKNOWN_ARTIST_MAPPING)

    user_data = jellyfin_album.get(ITEM_KEY_USER_DATA, {})
    album.favorite = user_data.get(USER_DATA_KEY_IS_FAVORITE, False)
    return album


def parse_artist(
    logger: Logger, instance_id: str, connection: Connection, jellyfin_artist: JellyArtist
) -> Artist:
    """Parse a Jellyfin Artist response to Artist model object."""
    artist_id = jellyfin_artist[ITEM_KEY_ID]
    artist = Artist(
        item_id=artist_id,
        name=jellyfin_artist[ITEM_KEY_NAME],
        provider=DOMAIN,
        provider_mappings={
            ProviderMapping(
                item_id=str(artist_id),
                provider_domain=DOMAIN,
                provider_instance=instance_id,
            )
        },
    )
    if ITEM_KEY_OVERVIEW in jellyfin_artist:
        artist.metadata.description = jellyfin_artist[ITEM_KEY_OVERVIEW]
    if ITEM_KEY_MUSICBRAINZ_ARTIST in jellyfin_artist[ITEM_KEY_PROVIDER_IDS]:
        try:
            artist.mbid = jellyfin_artist[ITEM_KEY_PROVIDER_IDS][ITEM_KEY_MUSICBRAINZ_ARTIST]
        except InvalidDataError as error:
            logger.warning(
                "Jellyfin has an invalid musicbrainz id for artist %s",
                artist.name,
                exc_info=error if logger.isEnabledFor(logging.DEBUG) else None,
            )
    if ITEM_KEY_SORT_NAME in jellyfin_artist:
        artist.sort_name = jellyfin_artist[ITEM_KEY_SORT_NAME]
    artist.metadata.images = _get_artwork(instance_id, connection, jellyfin_artist)
    user_data = jellyfin_artist.get(ITEM_KEY_USER_DATA, {})
    artist.favorite = user_data.get(USER_DATA_KEY_IS_FAVORITE, False)
    return artist


def parse_track(
    logger: Logger, instance_id: str, client: Connection, jellyfin_track: JellyTrack
) -> Track:
    """Parse a Jellyfin Track response to a Track model object."""
    available = False
    content = None
    available = jellyfin_track[ITEM_KEY_CAN_DOWNLOAD]
    content = jellyfin_track[ITEM_KEY_MEDIA_STREAMS][0][ITEM_KEY_MEDIA_CODEC]
    track = Track(
        item_id=jellyfin_track[ITEM_KEY_ID],
        provider=instance_id,
        name=jellyfin_track[ITEM_KEY_NAME],
        provider_mappings={
            ProviderMapping(
                item_id=jellyfin_track[ITEM_KEY_ID],
                provider_domain=DOMAIN,
                provider_instance=instance_id,
                available=available,
                audio_format=AudioFormat(
                    content_type=(
                        ContentType.try_parse(content) if content else ContentType.UNKNOWN
                    ),
                ),
                url=client.audio_url(jellyfin_track[ITEM_KEY_ID]),
            )
        },
    )

    track.disc_number = jellyfin_track.get(ITEM_KEY_PARENT_INDEX_NUM, 0)
    track.track_number = jellyfin_track.get("IndexNumber", 0)
    if track.track_number is not None and track.track_number >= 0:
        track.position = track.track_number

    track.metadata.images = _get_artwork(instance_id, client, jellyfin_track)

    if jellyfin_track[ITEM_KEY_ARTIST_ITEMS]:
        for artist_item in jellyfin_track[ITEM_KEY_ARTIST_ITEMS]:
            track.artists.append(
                ItemMapping(
                    media_type=MediaType.ARTIST,
                    item_id=artist_item[ITEM_KEY_ID],
                    provider=instance_id,
                    name=artist_item[ITEM_KEY_NAME],
                )
            )
    else:
        track.artists.append(UNKNOWN_ARTIST_MAPPING)

    if ITEM_KEY_ALBUM_ID in jellyfin_track:
        if not (album_name := jellyfin_track.get(ITEM_KEY_ALBUM)):
            logger.debug("Track %s has AlbumID but no AlbumName", track.name)
            album_name = f"Unknown Album ({jellyfin_track[ITEM_KEY_ALBUM_ID]})"
        track.album = ItemMapping(
            media_type=MediaType.ALBUM,
            item_id=jellyfin_track[ITEM_KEY_ALBUM_ID],
            provider=instance_id,
            name=album_name,
        )

    if ITEM_KEY_RUNTIME_TICKS in jellyfin_track:
        track.duration = int(
            jellyfin_track[ITEM_KEY_RUNTIME_TICKS] / 10000000
        )  # 10000000 ticks per millisecond
    if ITEM_KEY_MUSICBRAINZ_TRACK in jellyfin_track[ITEM_KEY_PROVIDER_IDS]:
        track_mbid = jellyfin_track[ITEM_KEY_PROVIDER_IDS][ITEM_KEY_MUSICBRAINZ_TRACK]
        try:
            track.mbid = track_mbid
        except InvalidDataError as error:
            logger.warning(
                "Jellyfin has an invalid musicbrainz id for track %s",
                track.name,
                exc_info=error if logger.isEnabledFor(logging.DEBUG) else None,
            )
    user_data = jellyfin_track.get(ITEM_KEY_USER_DATA, {})
    track.favorite = user_data.get(USER_DATA_KEY_IS_FAVORITE, False)
    return track


def parse_playlist(
    instance_id: str, client: Connection, jellyfin_playlist: JellyPlaylist
) -> Playlist:
    """Parse a Jellyfin Playlist response to a Playlist object."""
    playlistid = jellyfin_playlist[ITEM_KEY_ID]
    playlist = Playlist(
        item_id=playlistid,
        provider=DOMAIN,
        name=jellyfin_playlist[ITEM_KEY_NAME],
        provider_mappings={
            ProviderMapping(
                item_id=playlistid,
                provider_domain=DOMAIN,
                provider_instance=instance_id,
            )
        },
    )
    if ITEM_KEY_OVERVIEW in jellyfin_playlist:
        playlist.metadata.description = jellyfin_playlist[ITEM_KEY_OVERVIEW]
    playlist.metadata.images = _get_artwork(instance_id, client, jellyfin_playlist)
    user_data = jellyfin_playlist.get(ITEM_KEY_USER_DATA, {})
    playlist.favorite = user_data.get(USER_DATA_KEY_IS_FAVORITE, False)
    playlist.is_editable = False
    return playlist


def _get_artwork(
    instance_id: str, client: Connection, media_item: JellyMediaItem
) -> UniqueList[MediaItemImage]:
    images: UniqueList[MediaItemImage] = UniqueList()

    for i, _ in enumerate(media_item.get("BackdropImageTags", [])):
        images.append(
            MediaItemImage(
                type=ImageType.FANART,
                path=client.artwork(media_item[ITEM_KEY_ID], JellyImageType.Backdrop, index=i),
                provider=instance_id,
                remotely_accessible=False,
            )
        )

    image_tags = media_item[ITEM_KEY_IMAGE_TAGS]
    for jelly_image_type, image_type in MEDIA_IMAGE_TYPES.items():
        if jelly_image_type in image_tags:
            images.append(
                MediaItemImage(
                    type=image_type,
                    path=client.artwork(media_item[ITEM_KEY_ID], jelly_image_type),
                    provider=instance_id,
                    remotely_accessible=False,
                )
            )

    return images
