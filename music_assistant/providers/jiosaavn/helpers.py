"""Helper functions for JioSaavn Music Provider."""

from typing import Any

from music_assistant_models.enums import ContentType, ImageType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    MediaItemImage,
    Playlist,
    ProviderMapping,
    Track,
)
from music_assistant_models.unique_list import UniqueList


def parse_artist(data: dict[str, Any], provider_instance: str, provider_domain: str) -> Artist:
    """Parse JioSaavn artist data to Artist object."""
    artist_id = data.get("id") or data.get("artistId") or ""
    name = data.get("name") or data.get("title") or ""

    # JioSaavn sometimes returns artists with empty names or None IDs
    if not name or not artist_id or artist_id == "None":
        raise InvalidDataError("Artist has no name or invalid ID")

    artist = Artist(
        item_id=str(artist_id),
        provider=provider_instance,
        name=name,
        provider_mappings={
            ProviderMapping(
                item_id=str(artist_id),
                provider_domain=provider_domain,
                provider_instance=provider_instance,
                available=True,
            )
        },
    )

    # Add image if available
    if image_url := data.get("image"):
        # Skip default placeholder images
        if "artist-default" not in image_url and "share-image" not in image_url:
            artist.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_url,
                        provider=provider_instance,
                        remotely_accessible=True,
                    )
                ]
            )

    return artist


def parse_album(data: dict[str, Any], provider_instance: str, provider_domain: str) -> Album:
    """Parse JioSaavn album data to Album object."""
    album_id = data.get("id") or data.get("albumid") or ""
    name = data.get("title") or data.get("name") or ""

    # JioSaavn sometimes returns albums with empty names or invalid IDs
    if not name or not album_id:
        raise InvalidDataError("Album has no name or ID")

    album = Album(
        item_id=str(album_id),
        provider=provider_instance,
        name=name,
        provider_mappings={
            ProviderMapping(
                item_id=str(album_id),
                provider_domain=provider_domain,
                provider_instance=provider_instance,
                available=True,
                audio_format=AudioFormat(
                    content_type=ContentType.AAC,
                    bit_rate=320,
                ),
            )
        },
    )

    # Add artist info
    artist_name = data.get("music") or data.get("primary_artists") or ""
    if artist_name:
        artist_id = data.get("artistId") or artist_name
        album.artists.append(
            Artist(
                item_id=str(artist_id),
                provider=provider_instance,
                name=artist_name,
                provider_mappings={
                    ProviderMapping(
                        item_id=str(artist_id),
                        provider_domain=provider_domain,
                        provider_instance=provider_instance,
                    )
                },
            )
        )

    # Add release year if available
    if year := data.get("year"):
        album.year = int(year) if isinstance(year, str) and year.isdigit() else year

    # Add image if available
    if image_url := data.get("image"):
        album.metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=image_url,
                    provider=provider_instance,
                    remotely_accessible=True,
                )
            ]
        )

    return album


def parse_track(data: dict[str, Any], provider_instance: str, provider_domain: str) -> Track:
    """Parse JioSaavn track data to Track object."""
    track_id = data.get("id") or ""
    name = data.get("title") or data.get("song") or ""

    # Determine duration
    duration = data.get("duration")
    duration_int = int(duration) if duration and str(duration).isdigit() else 0

    track = Track(
        item_id=track_id,
        provider=provider_instance,
        name=name,
        duration=duration_int,
        provider_mappings={
            ProviderMapping(
                item_id=track_id,
                provider_domain=provider_domain,
                provider_instance=provider_instance,
                available=True,
                audio_format=AudioFormat(
                    content_type=ContentType.AAC,
                    bit_rate=320,
                ),
            )
        },
    )

    # Add artists
    artist_name = data.get("primary_artists") or data.get("singers") or data.get("music") or ""
    if artist_name:
        artist_id = data.get("artistId") or artist_name
        track.artists.append(
            Artist(
                item_id=artist_id,
                provider=provider_instance,
                name=artist_name,
                provider_mappings={
                    ProviderMapping(
                        item_id=artist_id,
                        provider_domain=provider_domain,
                        provider_instance=provider_instance,
                    )
                },
            )
        )

    # Add album info
    album_name = data.get("album") or ""
    album_id = data.get("albumid") or data.get("album_id") or ""
    if album_name and album_id:
        track.album = Album(
            item_id=album_id,
            provider=provider_instance,
            name=album_name,
            provider_mappings={
                ProviderMapping(
                    item_id=album_id,
                    provider_domain=provider_domain,
                    provider_instance=provider_instance,
                )
            },
        )

    # Add image if available
    if image_url := data.get("image"):
        track.metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=image_url,
                    provider=provider_instance,
                    remotely_accessible=True,
                )
            ]
        )

    return track


def parse_playlist(data: dict[str, Any], provider_instance: str, provider_domain: str) -> Playlist:
    """Parse JioSaavn playlist data to Playlist object."""
    playlist_id = data.get("id") or data.get("listid") or ""
    name = data.get("title") or data.get("listname") or ""

    playlist = Playlist(
        item_id=playlist_id,
        provider=provider_instance,
        name=name,
        provider_mappings={
            ProviderMapping(
                item_id=playlist_id,
                provider_domain=provider_domain,
                provider_instance=provider_instance,
                available=True,
            )
        },
        is_editable=False,
    )

    # Add owner info if available
    if owner := data.get("firstname") or data.get("username"):
        playlist.owner = owner

    # Add image if available
    if image_url := data.get("image"):
        playlist.metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=image_url,
                    provider=provider_instance,
                    remotely_accessible=True,
                )
            ]
        )

    return playlist
