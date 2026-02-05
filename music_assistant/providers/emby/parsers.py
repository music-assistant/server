"""Parsers for Emby API responses."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ImageType
from music_assistant_models.media_items import (
    Album,
    Artist,
    ItemMapping,
    MediaItemImage,
    Playlist,
    ProviderMapping,
    Track,
)
from music_assistant_models.unique_list import UniqueList

if TYPE_CHECKING:
    from music_assistant.providers.emby import EmbyProvider


def parse_track(
    logger: logging.Logger,
    instance_id: str,
    provider: EmbyProvider,
    item: dict[str, Any],
) -> Track:
    """Parse an Emby Audio item into a Track."""
    # ruff: noqa: ARG001
    track_id = str(item.get("Id"))
    name = str(item.get("Name"))

    # Extract artist info
    artists = UniqueList[Artist | ItemMapping]()
    if artist_items := item.get("ArtistItems"):
        for artist_item in artist_items:
            artist_name = str(artist_item.get("Name"))
            artist_id = str(artist_item.get("Id"))

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

    album_id = str(item.get("AlbumId"))
    album_name = str(item.get("Album"))

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

    duration = int(item.get("RunTimeTicks", 0) / 10000000)  # Convert ticks to seconds

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
            )
        },
    )

    # Extract images
    if "Primary" in item.get("ImageTags", {}):
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
    logger: logging.Logger,
    instance_id: str,
    provider: EmbyProvider,
    item: dict[str, Any],
) -> Artist:
    """Parse an Emby MusicArtist item into an Artist."""
    artist_id = str(item.get("Id"))
    name = str(item.get("Name"))

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
    if "Primary" in item.get("ImageTags", {}):
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
    logger: logging.Logger,
    instance_id: str,
    provider: EmbyProvider,
    item: dict[str, Any],
) -> Album:
    """Parse an Emby MusicAlbum item into an Album."""
    album_id = str(item.get("Id"))
    name = str(item.get("Name"))

    # Extract artist info
    artists = UniqueList[Artist | ItemMapping]()
    if artist_items := item.get("ArtistItems"):
        for artist_item in artist_items:
            artist_id = str(artist_item.get("Id"))
            artist_name = str(artist_item.get("Name"))

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
    playlist_id = str(item.get("Id"))
    name = str(item.get("Name"))

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
    if "Primary" in item.get("ImageTags", {}):
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
