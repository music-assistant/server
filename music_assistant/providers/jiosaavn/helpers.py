"""Helper functions for JioSaavn Music Provider."""

import base64
import binascii
import contextlib
import html
from typing import Any

from Crypto.Cipher import DES
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

from music_assistant.helpers.app_vars import app_var  # type: ignore[attr-defined]

# DES encryption key for stream URL decryption
DES_KEY = app_var(11)


def decrypt_stream_url(encrypted_url: str) -> str:
    """Decrypt JioSaavn stream URL."""
    try:
        encrypted_data = base64.b64decode(encrypted_url.strip())
        cipher = DES.new(DES_KEY, DES.MODE_ECB)
        decrypted_data = cipher.decrypt(encrypted_data)
        decrypted_url = decrypted_data.decode("utf-8").rstrip(
            "\x00\x01\x02\x03\x04\x05\x06\x07\x08"
        )
        return decrypted_url.replace("_96.mp4", "_320.mp4")
    except (binascii.Error, UnicodeDecodeError, ValueError) as err:
        raise InvalidDataError(f"Failed to decrypt stream URL: {err}") from err


def parse_artist(data: dict[str, Any], provider_instance: str, provider_domain: str) -> Artist:
    """Parse JioSaavn artist data to Artist object."""
    # Try multiple possible ID fields
    artist_id = str(data.get("artistId") or data.get("id") or data.get("artistid") or "")
    # Try multiple possible name fields
    name = html.unescape(data.get("name") or data.get("title") or "")

    if not name or not artist_id:
        raise InvalidDataError(f"Artist has no name or ID. Data: {data}")

    artist = Artist(
        item_id=artist_id,
        provider=provider_instance,
        name=name,
        provider_mappings={
            ProviderMapping(
                item_id=artist_id,
                provider_domain=provider_domain,
                provider_instance=provider_instance,
                available=True,
            )
        },
    )

    # Add image if available
    if image_url := data.get("image"):
        if "artist-default" not in image_url and "share-image" not in image_url:
            artist.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_url.replace("150x150", "500x500"),
                        provider=provider_instance,
                        remotely_accessible=True,
                    )
                ]
            )

    return artist


def parse_album(data: dict[str, Any], provider_instance: str, provider_domain: str) -> Album:
    """Parse JioSaavn album data to Album object."""
    album_id = str(data.get("albumid") or data.get("id") or "")
    name = html.unescape(data.get("title") or data.get("name") or "")

    if not name or not album_id:
        raise InvalidDataError("Album has no name or ID")

    album = Album(
        item_id=album_id,
        provider=provider_instance,
        name=name,
        provider_mappings={
            ProviderMapping(
                item_id=album_id,
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
    primary_artists = data.get("primary_artists") or data.get("music") or ""
    if primary_artists:
        artist_id = data.get("primary_artists_id") or primary_artists
        album.artists.append(
            Artist(
                item_id=str(artist_id),
                provider=provider_instance,
                name=html.unescape(primary_artists),
                provider_mappings={
                    ProviderMapping(
                        item_id=str(artist_id),
                        provider_domain=provider_domain,
                        provider_instance=provider_instance,
                    )
                },
            )
        )

    # Add year
    if year := data.get("year"):
        with contextlib.suppress(ValueError, TypeError):
            album.year = int(year)

    # Add image
    if image_url := data.get("image"):
        album.metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=image_url.replace("150x150", "500x500"),
                    provider=provider_instance,
                    remotely_accessible=True,
                )
            ]
        )

    return album


def parse_track(data: dict[str, Any], provider_instance: str, provider_domain: str) -> Track:
    """Parse JioSaavn track data to Track object."""
    track_id = str(data.get("id") or "")
    # Handle both search results (title) and details (song)
    name = html.unescape(data.get("song") or data.get("title") or "")

    if not name or not track_id:
        raise InvalidDataError("Track has no name or ID")

    # Get duration
    duration_str = data.get("duration") or data.get("more_info", {}).get("duration") or "0"
    try:
        duration = int(duration_str)
    except (ValueError, TypeError):
        duration = 0

    track = Track(
        item_id=track_id,
        provider=provider_instance,
        name=name,
        duration=duration,
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
    primary_artists = data.get("primary_artists") or data.get("singers") or ""
    if primary_artists:
        artist_id = data.get("primary_artists_id") or primary_artists
        track.artists.append(
            Artist(
                item_id=str(artist_id),
                provider=provider_instance,
                name=html.unescape(primary_artists),
                provider_mappings={
                    ProviderMapping(
                        item_id=str(artist_id),
                        provider_domain=provider_domain,
                        provider_instance=provider_instance,
                    )
                },
            )
        )

    # Add album
    album_name = data.get("album") or ""
    album_id = data.get("albumid") or data.get("album_id") or ""
    if album_name and album_id:
        track.album = Album(
            item_id=str(album_id),
            provider=provider_instance,
            name=html.unescape(album_name),
            provider_mappings={
                ProviderMapping(
                    item_id=str(album_id),
                    provider_domain=provider_domain,
                    provider_instance=provider_instance,
                )
            },
        )

    # Add image
    if image_url := data.get("image"):
        track.metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=image_url.replace("150x150", "500x500"),
                    provider=provider_instance,
                    remotely_accessible=True,
                )
            ]
        )

    return track


def parse_playlist(data: dict[str, Any], provider_instance: str, provider_domain: str) -> Playlist:
    """Parse JioSaavn playlist data to Playlist object."""
    playlist_id = str(data.get("listid") or data.get("id") or "")
    name = html.unescape(data.get("listname") or data.get("title") or "")

    if not name or not playlist_id:
        raise InvalidDataError("Playlist has no name or ID")

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

    # Add owner
    if owner := data.get("firstname") or data.get("username"):
        playlist.owner = owner

    # Add image
    if image_url := data.get("image"):
        playlist.metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=image_url.replace("150x150", "500x500"),
                    provider=provider_instance,
                    remotely_accessible=True,
                )
            ]
        )

    return playlist
