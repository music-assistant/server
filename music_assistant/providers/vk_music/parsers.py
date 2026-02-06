"""Parsers for VK Music API responses."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ContentType, ImageType, MediaType
from music_assistant_models.media_items import (
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    Playlist,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.helpers.util import parse_title_and_version

from .constants import VK_MUSIC_BASE_URL

if TYPE_CHECKING:
    from vkpymusic.models import Playlist as VKPlaylist
    from vkpymusic.models import Song as VKSong

    from .provider import VKMusicProvider


def _create_synthetic_artist_id(artist_name: str) -> str:
    """Create a synthetic artist ID from the artist name.

    VK Music doesn't have artist IDs, only names, so we use
    the artist name directly as the ID.

    :param artist_name: Artist name string.
    :return: Artist name used as synthetic artist ID.
    """
    return artist_name


def _create_synthetic_artist(provider: VKMusicProvider, artist_name: str) -> ItemMapping:
    """Create a synthetic artist ItemMapping from a name string.

    :param provider: The VK Music provider instance.
    :param artist_name: Artist name string.
    :return: ItemMapping for the artist.
    """
    artist_id = _create_synthetic_artist_id(artist_name)
    return ItemMapping(
        media_type=MediaType.ARTIST,
        item_id=artist_id,
        provider=provider.instance_id,
        name=artist_name,
    )


def parse_artist(provider: VKMusicProvider, artist_name: str) -> Artist:
    """Create an Artist object from a name string.

    VK Music doesn't have artist entities, only name strings in tracks.
    We create synthetic artists using the name as the ID.

    :param provider: The VK Music provider instance.
    :param artist_name: Artist name string.
    :return: Music Assistant Artist model.
    """
    artist_id = _create_synthetic_artist_id(artist_name)
    return Artist(
        item_id=artist_id,
        provider=provider.instance_id,
        name=artist_name,
        provider_mappings={
            ProviderMapping(
                item_id=artist_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
            )
        },
    )


def parse_track(provider: VKMusicProvider, song: VKSong) -> Track:
    """Parse VK Song object to MA Track model.

    :param provider: The VK Music provider instance.
    :param song: VK Song object.
    :return: Music Assistant Track model.
    """
    # Create composite track ID: owner_id_track_id
    track_id = f"{song.owner_id}_{song.track_id}"

    name, version = parse_title_and_version(
        song.title or "Unknown Track",
        None,
    )

    track = Track(
        item_id=track_id,
        provider=provider.instance_id,
        name=name,
        version=version,
        duration=song.duration or 0,
        provider_mappings={
            ProviderMapping(
                item_id=track_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                audio_format=AudioFormat(
                    content_type=ContentType.MP3,
                ),
                url=f"{VK_MUSIC_BASE_URL}/audio{track_id}",
                available=True,
            )
        },
    )

    # Parse artist (VK only has single artist string)
    if song.artist:
        track.artists = UniqueList([_create_synthetic_artist(provider, song.artist)])

    # Try to add cover image if available on the song object.
    # vkpymusic Song (v3.5.1) does not expose album cover data,
    # but we check for common attributes for forward compatibility.
    for img_attr in ("photo", "album_cover", "cover_url", "thumb"):
        img_url = getattr(song, img_attr, None)
        if img_url and isinstance(img_url, str) and img_url.startswith("http"):
            track.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=img_url,
                        provider=provider.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )
            break

    return track


def parse_playlist(provider: VKMusicProvider, playlist: VKPlaylist) -> Playlist:
    """Parse VK Playlist object to MA Playlist model.

    :param provider: The VK Music provider instance.
    :param playlist: VK Playlist object.
    :return: Music Assistant Playlist model.
    """
    # Create composite playlist ID: owner_id_playlist_id_access_key
    if playlist.access_key:
        playlist_id = f"{playlist.owner_id}_{playlist.playlist_id}_{playlist.access_key}"
    else:
        playlist_id = f"{playlist.owner_id}_{playlist.playlist_id}"

    # Determine if user owns this playlist
    is_own_playlist = playlist.owner_id == provider.client.user_id

    result = Playlist(
        item_id=playlist_id,
        provider=provider.instance_id,
        name=playlist.title or "Unknown Playlist",
        owner="Me" if is_own_playlist else "VK Music",
        provider_mappings={
            ProviderMapping(
                item_id=playlist_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                url=f"{VK_MUSIC_BASE_URL}/playlist/{playlist.owner_id}_{playlist.playlist_id}",
                is_unique=is_own_playlist,
            )
        },
        is_editable=False,  # VK API doesn't support playlist editing
    )

    # Add description if available
    if playlist.description:
        result.metadata.description = playlist.description

    # Add cover image if available
    if playlist.photo:
        result.metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=playlist.photo,
                    provider=provider.instance_id,
                    remotely_accessible=True,
                )
            ]
        )

    return result
