"""Parsers for KION Music API responses."""

from __future__ import annotations

from contextlib import suppress
from datetime import datetime
from typing import TYPE_CHECKING

from music_assistant_models.enums import (
    AlbumType,
    ContentType,
    ImageType,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    MediaItemImage,
    Playlist,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.helpers.util import parse_title_and_version

from .constants import IMAGE_SIZE_LARGE

if TYPE_CHECKING:
    from yandex_music import Album as YandexAlbum
    from yandex_music import Artist as YandexArtist
    from yandex_music import Playlist as YandexPlaylist
    from yandex_music import Track as YandexTrack

    from .provider import KionMusicProvider


def _get_image_url(cover_uri: str | None, size: str = IMAGE_SIZE_LARGE) -> str | None:
    """Convert cover URI to full URL.

    :param cover_uri: Cover URI template.
    :param size: Image size (e.g., '1000x1000').
    :return: Full image URL or None.
    """
    if not cover_uri:
        return None
    # Cover URIs come in format "avatars.yandex.net/get-music-content/xxx/yyy/%%"
    # Replace %% with the desired size
    return f"https://{cover_uri.replace('%%', size)}"


def parse_artist(provider: KionMusicProvider, artist_obj: YandexArtist) -> Artist:
    """Parse a KION Music artist object to MA Artist model.

    :param provider: The KION Music provider instance.
    :param artist_obj: API artist object.
    :return: Music Assistant Artist model.
    """
    artist_id = str(artist_obj.id)
    artist = Artist(
        item_id=artist_id,
        provider=provider.instance_id,
        name=artist_obj.name or "Unknown Artist",
        provider_mappings={
            ProviderMapping(
                item_id=artist_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                url=f"https://music.mts.ru/artist/{artist_id}",
            )
        },
    )

    # Add image if available
    if artist_obj.cover:
        image_url = _get_image_url(artist_obj.cover.uri)
        if image_url:
            artist.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_url,
                        provider=provider.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )
    elif artist_obj.og_image:
        image_url = _get_image_url(artist_obj.og_image)
        if image_url:
            artist.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_url,
                        provider=provider.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )

    return artist


def parse_album(provider: KionMusicProvider, album_obj: YandexAlbum) -> Album:
    """Parse a KION Music album object to MA Album model.

    :param provider: The KION Music provider instance.
    :param album_obj: API album object.
    :return: Music Assistant Album model.
    """
    name, version = parse_title_and_version(
        album_obj.title or "Unknown Album",
        album_obj.version or None,
    )
    album_id = str(album_obj.id)

    # Determine availability
    available = album_obj.available or False

    album = Album(
        item_id=album_id,
        provider=provider.instance_id,
        name=name,
        version=version,
        provider_mappings={
            ProviderMapping(
                item_id=album_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                audio_format=AudioFormat(
                    content_type=ContentType.UNKNOWN,
                ),
                url=f"https://music.mts.ru/album/{album_id}",
                available=available,
            )
        },
    )

    # Parse artists
    various_artist_album = False
    if album_obj.artists:
        for artist in album_obj.artists:
            if artist.name and artist.name.lower() in ("various artists", "сборник"):
                various_artist_album = True
            album.artists.append(parse_artist(provider, artist))

    # Determine album type
    album_type_str = album_obj.type or "album"
    if album_type_str == "compilation" or various_artist_album:
        album.album_type = AlbumType.COMPILATION
    elif album_type_str == "single":
        album.album_type = AlbumType.SINGLE
    else:
        album.album_type = AlbumType.ALBUM

    # Parse year
    if album_obj.year:
        album.year = album_obj.year
    if album_obj.release_date:
        with suppress(ValueError):
            album.metadata.release_date = datetime.fromisoformat(album_obj.release_date)

    # Parse metadata
    if album_obj.genre:
        album.metadata.genres = {album_obj.genre}

    # Add cover image
    if album_obj.cover_uri:
        image_url = _get_image_url(album_obj.cover_uri)
        if image_url:
            album.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_url,
                        provider=provider.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )
    elif album_obj.og_image:
        image_url = _get_image_url(album_obj.og_image)
        if image_url:
            album.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_url,
                        provider=provider.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )

    return album


def parse_track(provider: KionMusicProvider, track_obj: YandexTrack) -> Track:
    """Parse a KION Music track object to MA Track model.

    :param provider: The KION Music provider instance.
    :param track_obj: API track object.
    :return: Music Assistant Track model.
    """
    name, version = parse_title_and_version(
        track_obj.title or "Unknown Track",
        track_obj.version or None,
    )
    track_id = str(track_obj.id)

    # Determine availability
    available = track_obj.available or False

    # Duration is in milliseconds in KION API
    duration = (track_obj.duration_ms or 0) // 1000

    track = Track(
        item_id=track_id,
        provider=provider.instance_id,
        name=name,
        version=version,
        duration=duration,
        provider_mappings={
            ProviderMapping(
                item_id=track_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                audio_format=AudioFormat(
                    content_type=ContentType.UNKNOWN,
                ),
                url=f"https://music.mts.ru/track/{track_id}",
                available=available,
            )
        },
    )

    # Parse artists
    if track_obj.artists:
        track.artists = UniqueList()
        for artist in track_obj.artists:
            track.artists.append(parse_artist(provider, artist))

    # Parse album (full data so album gets cover art in the library)
    if track_obj.albums and len(track_obj.albums) > 0:
        album_obj = track_obj.albums[0]
        track.album = parse_album(provider, album_obj)
        # Also set track image from album cover if available
        if album_obj.cover_uri:
            image_url = _get_image_url(album_obj.cover_uri)
            if image_url:
                track.metadata.images = UniqueList(
                    [
                        MediaItemImage(
                            type=ImageType.THUMB,
                            path=image_url,
                            provider=provider.instance_id,
                            remotely_accessible=True,
                        )
                    ]
                )

    # Parse external IDs
    if track_obj.real_id:
        # real_id can be used as an identifier
        pass

    # Metadata
    if track_obj.content_warning:
        track.metadata.explicit = track_obj.content_warning == "explicit"

    return track


def parse_playlist(
    provider: KionMusicProvider, playlist_obj: YandexPlaylist, owner_name: str | None = None
) -> Playlist:
    """Parse a KION Music playlist object to MA Playlist model.

    :param provider: The KION Music provider instance.
    :param playlist_obj: API playlist object.
    :param owner_name: Optional owner name override.
    :return: Music Assistant Playlist model.
    """
    # Playlist ID is a combination of owner uid and playlist kind
    owner_id = str(playlist_obj.owner.uid) if playlist_obj.owner else str(provider.client.user_id)
    playlist_kind = str(playlist_obj.kind)
    playlist_id = f"{owner_id}:{playlist_kind}"

    # Determine if editable (user owns the playlist)
    is_editable = owner_id == str(provider.client.user_id)

    # Get owner name
    if owner_name is None:
        if playlist_obj.owner and playlist_obj.owner.name:
            owner_name = playlist_obj.owner.name
        elif is_editable:
            owner_name = "Me"
        else:
            owner_name = "KION Music"

    playlist = Playlist(
        item_id=playlist_id,
        provider=provider.instance_id,
        name=playlist_obj.title or "Unknown Playlist",
        owner=owner_name,
        provider_mappings={
            ProviderMapping(
                item_id=playlist_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                url=f"https://music.mts.ru/users/{owner_id}/playlists/{playlist_kind}",
                is_unique=is_editable,
            )
        },
        is_editable=is_editable,
    )

    # Metadata
    if playlist_obj.description:
        playlist.metadata.description = playlist_obj.description

    # Add cover image
    if playlist_obj.cover:
        # Cover can be CoverImage or a string
        cover = playlist_obj.cover
        if hasattr(cover, "uri") and cover.uri:
            image_url = _get_image_url(cover.uri)
            if image_url:
                playlist.metadata.images = UniqueList(
                    [
                        MediaItemImage(
                            type=ImageType.THUMB,
                            path=image_url,
                            provider=provider.instance_id,
                            remotely_accessible=True,
                        )
                    ]
                )
    elif playlist_obj.og_image:
        image_url = _get_image_url(playlist_obj.og_image)
        if image_url:
            playlist.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_url,
                        provider=provider.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )

    return playlist
