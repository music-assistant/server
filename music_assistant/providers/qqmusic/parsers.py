"""Response parsing helpers for QQ Music provider."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from typing import Any

from music_assistant_models.enums import ImageType, MediaType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import (
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

from music_assistant.helpers.util import parse_title_and_version

from .helpers import (
    clean_text,
    normalize_image_url,
)


def get_artist_mapping(artist_obj: dict[str, Any], provider_instance_id: str) -> ItemMapping | None:
    """Build an artist item mapping from an SDK model dump."""
    artist_id = str(artist_obj.get("mid") or "")
    if not artist_id:
        return None
    return ItemMapping(
        media_type=MediaType.ARTIST,
        item_id=artist_id,
        provider=provider_instance_id,
        name=clean_text(artist_obj.get("name") or artist_obj.get("title"), "Unknown Artist"),
    )


def parse_artist(
    artist_obj: dict[str, Any], provider_domain: str, provider_instance_id: str
) -> Artist:
    """Parse an SDK artist model dump."""
    artist_id = str(artist_obj.get("mid") or "")
    if not artist_id:
        raise InvalidDataError("Artist model does not contain mid")
    artist_name = clean_text(artist_obj.get("name") or artist_obj.get("title"), "Unknown Artist")
    artist = Artist(
        item_id=artist_id,
        provider=provider_instance_id,
        name=artist_name,
        provider_mappings={
            ProviderMapping(
                item_id=artist_id,
                provider_domain=provider_domain,
                provider_instance=provider_instance_id,
                url=f"https://y.qq.com/n/ryqq/singer/{artist_id}",
            )
        },
    )
    subtitle = clean_text(
        artist_obj.get("subtitle") or artist_obj.get("desc") or artist_obj.get("abt") or "",
        "",
    )
    if subtitle:
        artist.metadata.description = subtitle
        artist.metadata.description_language = "zh"
    artist_image = (
        normalize_image_url(
            artist_obj.get("pic") or artist_obj.get("avatar_url") or artist_obj.get("singer_pic")
        )
        or f"https://y.qq.com/music/photo_new/T001R500x500M000{artist_id}.jpg"
    )
    artist.metadata.images = UniqueList(
        [
            MediaItemImage(
                type=ImageType.THUMB,
                path=artist_image,
                provider=provider_instance_id,
                remotely_accessible=True,
            )
        ]
    )
    return artist


def parse_album(
    album_obj: dict[str, Any],
    provider_domain: str,
    provider_instance_id: str,
) -> Album:
    """Parse an SDK album model dump."""
    album_id = str(album_obj.get("mid") or "")
    if not album_id:
        raise InvalidDataError("Album model does not contain mid")
    raw_album_name = str(album_obj.get("title") or album_obj.get("name") or "Unknown Album")
    album_subtitle = clean_text(album_obj.get("subtitle"), "")
    album_name, album_version = parse_title_and_version(raw_album_name, album_subtitle)
    album_name = clean_text(album_name, "Unknown Album")
    album = Album(
        item_id=album_id,
        provider=provider_instance_id,
        name=album_name,
        version=album_version,
        provider_mappings={
            ProviderMapping(
                item_id=album_id,
                provider_domain=provider_domain,
                provider_instance=provider_instance_id,
                url=f"https://y.qq.com/n/ryqq/albumDetail/{album_id}",
            )
        },
    )
    if release_str := album_obj.get("time_public"):
        with suppress(ValueError):
            album.year = datetime.strptime(release_str, "%Y-%m-%d").year  # noqa: DTZ007
    album_desc = clean_text(album_obj.get("desc") or album_obj.get("description"), "")
    if album_desc:
        album.metadata.description = album_desc
        album.metadata.description_language = "zh"
    singer_list = album_obj.get("singers") or album_obj.get("singer_list") or []
    artist_iterable = singer_list if isinstance(singer_list, list) else []
    for artist_obj in artist_iterable:
        if isinstance(artist_obj, dict) and (
            artist_mapping := get_artist_mapping(artist_obj, provider_instance_id)
        ):
            album.artists.append(artist_mapping)
    cover_path = normalize_image_url(album_obj.get("pic"))
    if cover_path:
        album.metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=cover_path,
                    provider=provider_instance_id,
                    remotely_accessible=True,
                )
            ]
        )
    else:
        album.metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=f"https://y.qq.com/music/photo_new/T002R500x500M000{album_id}.jpg",
                    provider=provider_instance_id,
                    remotely_accessible=True,
                )
            ]
        )
    return album


def parse_track(
    track_obj: dict[str, Any],
    provider_domain: str,
    provider_instance_id: str,
    get_max_supported_audio_format: Callable[[dict[str, Any]], tuple[AudioFormat, str | None]],
) -> Track:
    """Parse an SDK song model dump."""
    track_id = str(track_obj.get("mid") or "")
    if not track_id:
        raise InvalidDataError("Song model does not contain mid")
    raw_track_name = clean_text(track_obj.get("title") or track_obj.get("name"), "Unknown Track")
    track_subtitle = clean_text(track_obj.get("subtitle") or track_obj.get("title_extra"), "")
    track_name, track_version = parse_title_and_version(raw_track_name, track_subtitle)
    duration = track_obj.get("interval")
    max_audio_format, max_quality_label = get_max_supported_audio_format(track_obj)
    track = Track(
        item_id=track_id,
        provider=provider_instance_id,
        name=track_name,
        version=track_version,
        duration=int(duration) if duration else 0,
        provider_mappings={
            ProviderMapping(
                item_id=track_id,
                provider_domain=provider_domain,
                provider_instance=provider_instance_id,
                audio_format=max_audio_format,
                url=f"https://y.qq.com/n/ryqq/songDetail/{track_id}",
                details=max_quality_label,
            )
        },
    )
    track_desc = clean_text(track_obj.get("desc") or track_obj.get("content"), "")
    if track_desc:
        track.metadata.description = track_desc
        track.metadata.description_language = "zh"
    album_obj = track_obj.get("album")
    if isinstance(album_obj, dict):
        album_id = str(album_obj.get("mid") or "")
        album_name = clean_text(album_obj.get("name") or album_obj.get("title"))
    else:
        album_id = ""
        album_name = ""
    if album_id and album_name:
        track.album = Album(
            item_id=album_id,
            provider=provider_instance_id,
            name=album_name,
            provider_mappings={
                ProviderMapping(
                    item_id=album_id,
                    provider_domain=provider_domain,
                    provider_instance=provider_instance_id,
                    url=f"https://y.qq.com/n/ryqq/albumDetail/{album_id}",
                )
            },
        )

    raw_singer = track_obj.get("singer")
    singer_list = raw_singer if isinstance(raw_singer, list) else []
    for artist_obj in singer_list:
        if isinstance(artist_obj, dict) and (
            artist_mapping := get_artist_mapping(artist_obj, provider_instance_id)
        ):
            track.artists.append(artist_mapping)
    if album_id:
        if isinstance(track.album, Album):
            track.album.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=f"https://y.qq.com/music/photo_new/T002R500x500M000{album_id}.jpg",
                        provider=provider_instance_id,
                        remotely_accessible=True,
                    )
                ]
            )
        track.metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=f"https://y.qq.com/music/photo_new/T002R500x500M000{album_id}.jpg",
                    provider=provider_instance_id,
                    remotely_accessible=True,
                )
            ]
        )
    return track


def build_playlist_id(dissid: int | str, dirid: int | str) -> str:
    """Build provider playlist id from dissid and dirid."""
    return f"{dissid}:{dirid}"


def parse_playlist_id(prov_playlist_id: str) -> tuple[int, int]:
    """Parse provider playlist id into (dissid, dirid)."""
    if ":" in prov_playlist_id:
        dissid_raw, dirid_raw = prov_playlist_id.split(":", 1)
        return (int(dissid_raw), int(dirid_raw))
    return (int(prov_playlist_id), 0)


def parse_playlist(
    playlist_obj: dict[str, Any],
    provider_domain: str,
    provider_instance_id: str,
) -> Playlist:
    """Parse an SDK song-list model dump."""
    dissid = playlist_obj.get("id") or 0
    dirid = playlist_obj.get("dirid") or 0
    if not dissid:
        raise InvalidDataError("Song-list model missing id")
    playlist_id = build_playlist_id(dissid, dirid)
    playlist_name = clean_text(
        playlist_obj.get("title") or "QQ Music Playlist",
        "QQ Music Playlist",
    )
    playlist = Playlist(
        item_id=playlist_id,
        provider=provider_instance_id,
        name=playlist_name,
        provider_mappings={
            ProviderMapping(
                item_id=playlist_id,
                provider_domain=provider_domain,
                provider_instance=provider_instance_id,
                url=f"https://y.qq.com/n/ryqq/playlist/{dissid}",
            )
        },
    )
    owner_name = str(
        playlist_obj.get("creator", {}).get("nick")
        or playlist_obj.get("creator_nick")
        or playlist_obj.get("nickname")
        or playlist_obj.get("nick")
        or ""
    )
    if owner_name:
        playlist.owner = owner_name
    description = clean_text(playlist_obj.get("desc"), "")
    if description:
        playlist.metadata.description = description
        playlist.metadata.description_language = "zh"

    cover = normalize_image_url(playlist_obj.get("picurl"))
    if cover:
        playlist.metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=cover,
                    provider=provider_instance_id,
                    remotely_accessible=True,
                )
            ]
        )
    return playlist
