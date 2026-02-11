"""Parsers for Zvuk Music API responses."""

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

from .constants import IMAGE_SIZE_LARGE, ZVUK_BASE_URL

if TYPE_CHECKING:
    from zvuk_music import Artist as ZvukArtist
    from zvuk_music import Playlist as ZvukPlaylist
    from zvuk_music import Release as ZvukRelease
    from zvuk_music import Track as ZvukTrack
    from zvuk_music.models.artist import SimpleArtist as ZvukSimpleArtist
    from zvuk_music.models.common import Image as ZvukImage
    from zvuk_music.models.playlist import SimplePlaylist as ZvukSimplePlaylist
    from zvuk_music.models.release import SimpleRelease as ZvukSimpleRelease
    from zvuk_music.models.track import SimpleTrack as ZvukSimpleTrack

    from .provider import ZvukMusicProvider


def _get_image_url(image: ZvukImage | None, size: int = IMAGE_SIZE_LARGE) -> str | None:
    """Convert Zvuk Image to full URL.

    :param image: Zvuk Image object.
    :param size: Image size in pixels.
    :return: Full image URL or None.
    """
    if not image:
        return None
    url = image.get_url(size, size)
    return url if url else None


def parse_artist(provider: ZvukMusicProvider, artist_obj: ZvukArtist | ZvukSimpleArtist) -> Artist:
    """Parse Zvuk artist object to MA Artist model.

    :param provider: The Zvuk Music provider instance.
    :param artist_obj: Zvuk artist or SimpleArtist object.
    :return: Music Assistant Artist model.
    """
    artist_id = str(artist_obj.id)
    artist = Artist(
        item_id=artist_id,
        provider=provider.instance_id,
        name=artist_obj.title or "Unknown Artist",
        provider_mappings={
            ProviderMapping(
                item_id=artist_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                url=f"{ZVUK_BASE_URL}/artist/{artist_id}",
            )
        },
    )

    if artist_obj.image:
        image_url = _get_image_url(artist_obj.image)
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


def parse_album(provider: ZvukMusicProvider, release_obj: ZvukRelease | ZvukSimpleRelease) -> Album:
    """Parse Zvuk release object to MA Album model.

    :param provider: The Zvuk Music provider instance.
    :param release_obj: Zvuk release or SimpleRelease object.
    :return: Music Assistant Album model.
    """
    name, version = parse_title_and_version(
        release_obj.title or "Unknown Album",
    )
    album_id = str(release_obj.id)

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
                url=f"{ZVUK_BASE_URL}/release/{album_id}",
            )
        },
    )

    # Parse artists
    if release_obj.artists:
        for artist in release_obj.artists:
            album.artists.append(parse_artist(provider, artist))

    # Determine album type from ReleaseType
    if release_obj.type:
        release_type_value = (
            release_obj.type.value if hasattr(release_obj.type, "value") else str(release_obj.type)
        )
        if release_type_value == "compilation":
            album.album_type = AlbumType.COMPILATION
        elif release_type_value == "single":
            album.album_type = AlbumType.SINGLE
        elif release_type_value == "ep":
            album.album_type = AlbumType.EP
        else:
            album.album_type = AlbumType.ALBUM
    else:
        album.album_type = AlbumType.ALBUM

    # Parse date
    if release_obj.date:
        # get_year() is available on both Release and SimpleRelease
        year = release_obj.get_year()
        if year:
            album.year = year
        with suppress(ValueError):
            album.metadata.release_date = datetime.fromisoformat(release_obj.date)

    # Parse genres (only available on full Release, not SimpleRelease)
    if hasattr(release_obj, "genres") and release_obj.genres:
        album.metadata.genres = {genre.name for genre in release_obj.genres if genre.name}

    # Parse explicit flag
    if release_obj.explicit:
        album.metadata.explicit = True

    # Add cover image
    if release_obj.image:
        image_url = _get_image_url(release_obj.image)
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


def parse_track(provider: ZvukMusicProvider, track_obj: ZvukTrack | ZvukSimpleTrack) -> Track:
    """Parse Zvuk track object to MA Track model.

    :param provider: The Zvuk Music provider instance.
    :param track_obj: Zvuk track or SimpleTrack object.
    :return: Music Assistant Track model.
    """
    name, version = parse_title_and_version(
        track_obj.title or "Unknown Track",
    )
    track_id = str(track_obj.id)

    # Duration is already in seconds in Zvuk API
    duration = track_obj.duration or 0

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
                url=f"{ZVUK_BASE_URL}/track/{track_id}",
            )
        },
    )

    # Parse artists
    if track_obj.artists:
        track.artists = UniqueList()
        for artist in track_obj.artists:
            track.artists.append(parse_artist(provider, artist))

    # Parse album from release (available on both Track and SimpleTrack)
    if track_obj.release:
        track.album = provider.get_item_mapping(
            media_type="album",
            key=str(track_obj.release.id),
            name=track_obj.release.title or "Unknown Album",
        )
        # Get image from release
        if track_obj.release.image:
            image_url = _get_image_url(track_obj.release.image)
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

    # Track number (position in release, only on full Track)
    if hasattr(track_obj, "position") and track_obj.position is not None:
        track.track_number = track_obj.position

    # Explicit flag (boolean on both Track and SimpleTrack)
    if track_obj.explicit:
        track.metadata.explicit = True

    return track


def parse_playlist(
    provider: ZvukMusicProvider, playlist_obj: ZvukPlaylist | ZvukSimplePlaylist
) -> Playlist:
    """Parse Zvuk playlist object to MA Playlist model.

    :param provider: The Zvuk Music provider instance.
    :param playlist_obj: Zvuk playlist or SimplePlaylist object.
    :return: Music Assistant Playlist model.
    """
    playlist_id = str(playlist_obj.id)

    # Determine if editable (user owns the playlist)
    # user_id is only available on full Playlist, not SimplePlaylist
    is_editable = False
    owner_name = "Zvuk Music"
    user_id = getattr(playlist_obj, "user_id", None)
    if user_id and provider.client.user_id:
        is_editable = str(user_id) == str(provider.client.user_id)
        if is_editable:
            owner_name = "Me"

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
                url=f"{ZVUK_BASE_URL}/playlist/{playlist_id}",
                is_unique=is_editable,
            )
        },
        is_editable=is_editable,
    )

    # Metadata
    if playlist_obj.description:
        playlist.metadata.description = playlist_obj.description

    # Add cover image
    if playlist_obj.image:
        image_url = _get_image_url(playlist_obj.image)
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
