"""Parsers for Emby API responses."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ContentType, ImageType
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    Playlist,
    ProviderMapping,
    Track,
)
from music_assistant_models.unique_list import UniqueList

from music_assistant.providers.emby.const import (
    AUDIO_STREAM_BIT_DEPTH,
    AUDIO_STREAM_CHANNELS,
    AUDIO_STREAM_CODEC,
    AUDIO_STREAM_SAMPLE_RATE,
    ITEM_KEY_ALBUM_ID,
    ITEM_KEY_ALBUM_NAME,
    ITEM_KEY_ARTIST_ITEMS,
    ITEM_KEY_CONTAINER,
    ITEM_KEY_ID,
    ITEM_KEY_IMAGE_TAGS,
    ITEM_KEY_MEDIA_STREAMS,
    ITEM_KEY_NAME,
    ITEM_KEY_RUNTIME_TICKS,
    ITEM_KEY_TYPE,
)

if TYPE_CHECKING:
    from music_assistant.providers.emby import EmbyProvider


def parse_track(
    instance_id: str,
    provider: EmbyProvider,
    item: dict[str, Any],
) -> Track:
    """Parse an Emby Audio item into a Track."""
    track_id = str(item.get(ITEM_KEY_ID))
    name = str(item.get(ITEM_KEY_NAME))

    # Extract artist info
    artists = UniqueList[Artist | ItemMapping]()
    if artist_items := item.get(ITEM_KEY_ARTIST_ITEMS):
        for artist_item in artist_items:
            artist_name = str(artist_item.get(ITEM_KEY_NAME))
            artist_id = str(artist_item.get(ITEM_KEY_ID))

            artists.append(
                Artist(
                    item_id=artist_id,
                    name=artist_name,
                    provider=instance_id,
                    provider_mappings={
                        ProviderMapping(
                            item_id=artist_id,
                            provider_domain=provider.domain,
                            provider_instance=instance_id,
                        )
                    },
                )
            )

    album_id = str(item.get(ITEM_KEY_ALBUM_ID))
    album_name = str(item.get(ITEM_KEY_ALBUM_NAME))

    album = Album(
        item_id=album_id,
        name=album_name,
        provider=instance_id,
        provider_mappings={
            ProviderMapping(
                item_id=album_id,
                provider_domain=provider.domain,
                provider_instance=instance_id,
            )
        },
    )

    duration = int(item.get(ITEM_KEY_RUNTIME_TICKS, 0) / 10000000)  # Convert ticks to seconds
    media_streams = item.get(ITEM_KEY_MEDIA_STREAMS, [{}])
    audio_stream = next((dict(s) for s in media_streams if s.get(ITEM_KEY_TYPE) == "Audio"), {})

    track = Track(
        item_id=track_id,
        name=name,
        album=album,
        artists=artists,
        duration=duration,
        provider=instance_id,
        provider_mappings={
            ProviderMapping(
                item_id=track_id,
                provider_domain=provider.domain,
                provider_instance=instance_id,
                audio_format=AudioFormat(
                    content_type=ContentType.try_parse(str(item.get(ITEM_KEY_CONTAINER))),
                    codec_type=ContentType.try_parse(str(audio_stream.get(AUDIO_STREAM_CODEC))),
                    sample_rate=int(audio_stream.get(AUDIO_STREAM_SAMPLE_RATE, 44100)),
                    bit_depth=int(audio_stream.get(AUDIO_STREAM_BIT_DEPTH, 16)),
                    channels=int(audio_stream.get(AUDIO_STREAM_CHANNELS, 2)),
                ),
            )
        },
    )

    # Extract images
    if "Primary" in item.get(ITEM_KEY_IMAGE_TAGS, {}):
        image_url = f"{provider._base_url}Items/{track_id}/Images/Primary"
        if track.metadata.images is None:
            track.metadata.images = UniqueList[MediaItemImage]()
        track.metadata.images.append(
            MediaItemImage(
                type=ImageType.THUMB,
                path=image_url,
                provider=instance_id,
                remotely_accessible=True,
            )
        )

    return track


def parse_artist(
    instance_id: str,
    provider: EmbyProvider,
    item: dict[str, Any],
) -> Artist:
    """Parse an Emby MusicArtist item into an Artist."""
    artist_id = str(item.get(ITEM_KEY_ID))
    name = str(item.get(ITEM_KEY_NAME))

    artist = Artist(
        item_id=artist_id,
        name=name,
        provider=instance_id,
        provider_mappings={
            ProviderMapping(
                item_id=artist_id,
                provider_domain=provider.domain,
                provider_instance=instance_id,
            )
        },
    )

    # Extract images
    if "Primary" in item.get(ITEM_KEY_IMAGE_TAGS, {}):
        image_url = f"{provider._base_url}Items/{artist_id}/Images/Primary"
        if artist.metadata.images is None:
            artist.metadata.images = UniqueList[MediaItemImage]()
        artist.metadata.images.append(
            MediaItemImage(
                type=ImageType.THUMB,
                path=image_url,
                provider=instance_id,
                remotely_accessible=True,
            )
        )

    return artist


def parse_album(
    instance_id: str,
    provider: EmbyProvider,
    item: dict[str, Any],
) -> Album:
    """Parse an Emby MusicAlbum item into an Album."""
    album_id = str(item.get(ITEM_KEY_ID))
    name = str(item.get(ITEM_KEY_NAME))

    # Extract artist info
    artists = UniqueList[Artist | ItemMapping]()
    if artist_items := item.get(ITEM_KEY_ARTIST_ITEMS):
        for artist_item in artist_items:
            artist_id = str(artist_item.get(ITEM_KEY_ID))
            artist_name = str(artist_item.get(ITEM_KEY_NAME))

            artists.append(
                Artist(
                    item_id=artist_id,
                    name=artist_name,
                    provider=instance_id,
                    provider_mappings={
                        ProviderMapping(
                            item_id=artist_id,
                            provider_domain=provider.domain,
                            provider_instance=instance_id,
                        )
                    },
                )
            )

    album = Album(
        item_id=album_id,
        name=name,
        artists=artists,
        provider=instance_id,
        provider_mappings={
            ProviderMapping(
                item_id=album_id,
                provider_domain=provider.domain,
                provider_instance=instance_id,
            )
        },
    )

    # Extract images
    if image_id := item.get("PrimaryImageItemId"):
        image_url = f"{provider._base_url}Items/{image_id}/Images/Primary"
        if album.metadata.images is None:
            album.metadata.images = UniqueList[MediaItemImage]()
        album.metadata.images.append(
            MediaItemImage(
                type=ImageType.THUMB,
                path=image_url,
                provider=instance_id,
                remotely_accessible=True,
            )
        )

    return album


def parse_playlist(
    instance_id: str,
    provider: EmbyProvider,
    item: dict[str, Any],
) -> Playlist:
    """Parse an Emby Playlist item into a Playlist."""
    playlist_id = str(item.get(ITEM_KEY_ID))
    name = str(item.get(ITEM_KEY_NAME))

    playlist = Playlist(
        item_id=playlist_id,
        name=name,
        provider=instance_id,
        provider_mappings={
            ProviderMapping(
                item_id=playlist_id,
                provider_domain=provider.domain,
                provider_instance=instance_id,
            )
        },
    )
    # Extract images
    if "Primary" in item.get(ITEM_KEY_IMAGE_TAGS, {}):
        image_url = f"{provider._base_url}Items/{playlist_id}/Images/Primary"
        if playlist.metadata.images is None:
            playlist.metadata.images = UniqueList[MediaItemImage]()
        playlist.metadata.images.append(
            MediaItemImage(
                type=ImageType.THUMB,
                path=image_url,
                provider=instance_id,
                remotely_accessible=True,
            )
        )

    return playlist
