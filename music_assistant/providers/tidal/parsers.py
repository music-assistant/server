"""Parsers for Tidal API responses."""

from __future__ import annotations

from contextlib import suppress
from datetime import datetime
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import (
    AlbumType,
    ContentType,
    ExternalID,
    ImageType,
    MediaType,
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

from music_assistant.helpers.util import infer_album_type, parse_title_and_version

from .constants import BROWSE_URL, RESOURCES_URL

if TYPE_CHECKING:
    from .provider import TidalProvider


def parse_artist(provider: TidalProvider, artist_obj: dict[str, Any]) -> Artist:
    """Parse tidal artist object to generic layout."""
    # Handle both full artist objects and nested ones coming from albums/tracks
    artist_obj_data = artist_obj.get("item", artist_obj)
    artist_id = str(artist_obj_data["id"])
    artist = Artist(
        item_id=artist_id,
        provider=provider.instance_id,
        name=artist_obj_data["name"],
        provider_mappings={
            ProviderMapping(
                item_id=artist_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                # NOTE: don't use the /browse endpoint as it's
                # not working for musicbrainz lookups
                url=f"https://tidal.com/artist/{artist_id}",
            )
        },
    )
    # metadata
    if "created" in artist_obj:
        with suppress(ValueError):
            artist.date_added = datetime.fromisoformat(artist_obj["created"])
    if artist_obj_data["picture"]:
        picture_id = artist_obj_data["picture"].replace("-", "/")
        image_url = f"{RESOURCES_URL}/{picture_id}/750x750.jpg"
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


def parse_album(provider: TidalProvider, album_obj: dict[str, Any]) -> Album:
    """Parse tidal album object to generic layout."""
    album_obj_data = album_obj.get("item", album_obj)
    name, version = parse_title_and_version(
        album_obj_data.get("title", "Unknown Album"),
        album_obj_data.get("version") or None,
    )
    album_id = str(album_obj_data.get("id", ""))

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
                    content_type=ContentType.FLAC,
                ),
                url=f"https://tidal.com/album/{album_id}",
                available=album_obj.get("streamReady", True),  # Default to available
            )
        },
    )

    # Safely handle artists array
    various_artist_album: bool = False
    for artist_obj in album_obj_data.get("artists", []):
        try:
            if artist_obj.get("name") == "Various Artists":
                various_artist_album = True
            album.artists.append(parse_artist(provider, artist_obj))
        except (KeyError, TypeError) as err:
            provider.logger.warning("Error parsing artist in album %s: %s", name, err)

    # Safely determine album type
    album_type = album_obj_data.get("type", "ALBUM")
    if album_type == "COMPILATION" or various_artist_album:
        album.album_type = AlbumType.COMPILATION
    elif album_type == "ALBUM":
        album.album_type = AlbumType.ALBUM
    elif album_type == "EP":
        album.album_type = AlbumType.EP
    elif album_type == "SINGLE":
        album.album_type = AlbumType.SINGLE

    # Try inference - override if it finds something more specific
    inferred_type = infer_album_type(name, version)
    if inferred_type in (AlbumType.SOUNDTRACK, AlbumType.LIVE):
        album.album_type = inferred_type

    # Safely parse year
    if release_date := album_obj_data.get("releaseDate", ""):
        try:
            album.year = int(release_date.split("-")[0])
        except (ValueError, IndexError):
            provider.logger.debug("Invalid release date format: %s", release_date)
        with suppress(ValueError):
            album.metadata.release_date = datetime.fromisoformat(release_date)

    # Safely set metadata
    if "created" in album_obj:
        with suppress(ValueError):
            album.date_added = datetime.fromisoformat(album_obj["created"])
    upc = album_obj_data.get("upc")
    if upc:
        album.external_ids.add((ExternalID.BARCODE, upc))

    album.metadata.copyright = album_obj_data.get("copyright", "")
    album.metadata.explicit = album_obj_data.get("explicit", False)
    album.metadata.popularity = album_obj_data.get("popularity", 0)

    # Safely handle cover image
    cover = album_obj_data.get("cover")
    if cover:
        picture_id = cover.replace("-", "/")
        image_url = f"{RESOURCES_URL}/{picture_id}/750x750.jpg"
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


def parse_track(
    provider: TidalProvider,
    track_obj: dict[str, Any],
    lyrics: dict[str, str] | None = None,
) -> Track:
    """Parse tidal track object to generic layout."""
    track_obj_data = track_obj.get("item", track_obj)
    name, version = parse_title_and_version(
        track_obj_data.get("title", "Unknown"),
        track_obj_data.get("version") or None,
    )
    track_id = str(track_obj_data.get("id", 0))
    media_metadata = track_obj_data.get("mediaMetadata") or {}
    tags = media_metadata.get("tags", [])
    hi_res_lossless = any(tag in tags for tag in ["HIRES_LOSSLESS", "HI_RES_LOSSLESS"])
    track = Track(
        item_id=track_id,
        provider=provider.instance_id,
        name=name,
        version=version,
        duration=track_obj_data.get("duration", 0),
        provider_mappings={
            ProviderMapping(
                item_id=str(track_id),
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                audio_format=AudioFormat(
                    content_type=ContentType.FLAC,
                    bit_depth=24 if hi_res_lossless else 16,
                ),
                url=f"https://tidal.com/track/{track_id}",
                available=track_obj_data["streamReady"],
            )
        },
        disc_number=track_obj_data.get("volumeNumber", 0) or 0,
        track_number=track_obj_data.get("trackNumber", 0) or 0,
    )
    if "isrc" in track_obj_data:
        track.external_ids.add((ExternalID.ISRC, track_obj_data["isrc"]))
    track.artists = UniqueList()
    for track_artist in track_obj_data["artists"]:
        artist = parse_artist(provider, track_artist)
        track.artists.append(artist)
    # metadata
    if "created" in track_obj:
        with suppress(ValueError):
            track.date_added = datetime.fromisoformat(track_obj["created"])
    track.metadata.explicit = track_obj_data["explicit"]
    track.metadata.popularity = track_obj_data["popularity"]
    if "copyright" in track_obj_data:
        track.metadata.copyright = track_obj_data["copyright"]
    if lyrics and "lyrics" in lyrics:
        track.metadata.lyrics = lyrics["lyrics"]
    if lyrics and "subtitles" in lyrics:
        track.metadata.lrc_lyrics = lyrics["subtitles"]
    if track_obj_data["album"]:
        # Here we use an ItemMapping as Tidal returns
        # minimal data when getting an Album from a Track
        track.album = provider.get_item_mapping(
            media_type=MediaType.ALBUM,
            key=str(track_obj_data["album"]["id"]),
            name=track_obj_data["album"]["title"],
        )
        if track_obj_data["album"]["cover"]:
            picture_id = track_obj_data["album"]["cover"].replace("-", "/")
            image_url = f"{RESOURCES_URL}/{picture_id}/750x750.jpg"
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
    return track


def parse_playlist(
    provider: TidalProvider, playlist_obj: dict[str, Any], is_mix: bool = False
) -> Playlist:
    """Parse tidal playlist object to generic layout."""
    playlist_obj_data = playlist_obj.get("playlist", playlist_obj)
    # Get ID based on playlist type
    raw_id = str(playlist_obj_data.get("id" if is_mix else "uuid", ""))

    # Add prefix for mixes to distinguish them
    playlist_id = f"mix_{raw_id}" if is_mix else raw_id

    # Owner logic differs between types
    if is_mix:
        owner_name = "Created by Tidal"
        is_editable = False
    else:
        creator_id = None
        creator = playlist_obj_data.get("creator", {})
        if creator:
            creator_id = creator.get("id")
        is_editable = bool(creator_id and str(creator_id) == str(provider.auth.user_id))

        owner_name = "Tidal"
        if is_editable:
            if provider.auth.user.profile_name:
                owner_name = provider.auth.user.profile_name
            elif provider.auth.user.user_name:
                owner_name = provider.auth.user.user_name
            elif provider.auth.user_id:
                owner_name = str(provider.auth.user_id)

    # URL path differs by type - use raw_id for URLs
    url_path = "mix" if is_mix else "playlist"

    playlist = Playlist(
        item_id=playlist_id,
        provider=provider.instance_id,
        name=playlist_obj_data.get("title", "Unknown"),
        owner=owner_name,
        provider_mappings={
            ProviderMapping(
                item_id=playlist_id,  # Use raw ID for provider mapping
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                url=f"{BROWSE_URL}/{url_path}/{raw_id}",
                is_unique=is_editable,  # user-owned playlists are unique
            )
        },
        is_editable=is_editable,
    )

    # Metadata - different fields based on type
    if "created" in playlist_obj:
        with suppress(ValueError):
            playlist.date_added = datetime.fromisoformat(playlist_obj["created"])
    # Add the description from the subtitle for mixes
    if is_mix:
        subtitle = playlist_obj_data.get("subTitle")
        if subtitle:
            playlist.metadata.description = subtitle

    # Handle images differently based on type
    if is_mix:
        if pictures := playlist_obj_data.get("images", {}).get("MEDIUM"):
            image_url = pictures.get("url", "")
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
    elif picture := (playlist_obj_data.get("squareImage") or playlist_obj_data.get("image")):
        picture_id = picture.replace("-", "/")
        image_url = f"{RESOURCES_URL}/{picture_id}/750x750.jpg"
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
