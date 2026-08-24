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
    extract_album_mid,
    extract_artist_mid,
    extract_first_text,
    extract_playlist_ids,
    extract_track_mid,
    normalize_image_url,
)


def extract_song_id(track_obj: dict[str, Any]) -> int | None:
    """Extract numeric QQ song id from a track-like payload."""
    for key in ("id", "songid", "song_id", "songID"):
        raw_val = track_obj.get(key)
        if raw_val is None:
            continue
        try:
            song_id = int(raw_val)
        except TypeError, ValueError:
            continue
        if song_id > 0:
            return song_id
    return None


def extract_items(data: dict[str, Any], candidate_keys: tuple[str, ...]) -> list[dict[str, Any]]:
    """Extract list-like payload from possible response keys."""
    for key in candidate_keys:
        val = data.get(key)
        if isinstance(val, list):
            return [item for item in val if isinstance(item, dict)]
        if isinstance(val, dict):
            for nested_key in ("list", "v_playlist", "album_list", "songlist", "items"):
                nested_val = val.get(nested_key)
                if isinstance(nested_val, list):
                    return [item for item in nested_val if isinstance(item, dict)]
    return []


def extract_guess_recommend_tracks(data: Any) -> list[dict[str, Any]]:
    """Extract tracks from GuessRecommendResponse raw payload."""
    if not isinstance(data, dict):
        return []
    tracks = data.get("songs") or data.get("Tracks")
    if not isinstance(tracks, list):
        return []
    return [item for item in tracks if isinstance(item, dict)]


def extract_radar_recommend_tracks(data: Any) -> list[dict[str, Any]]:
    """Extract tracks from RadarRecommendResponse raw payload."""
    if not isinstance(data, dict):
        return []
    songs = data.get("songs")
    if isinstance(songs, list):
        return [item for item in songs if isinstance(item, dict)]
    vec_songs = data.get("VecSongs")
    if not isinstance(vec_songs, list):
        return []
    result: list[dict[str, Any]] = []
    for item in vec_songs:
        if not isinstance(item, dict):
            continue
        track = item.get("Track")
        if isinstance(track, dict):
            result.append(track)
    return result


def extract_newsong_tracks(data: Any) -> list[dict[str, Any]]:
    """Extract tracks from RecommendNewSongResponse raw payload."""
    if not isinstance(data, dict):
        return []
    songs = data.get("songs") or data.get("songlist")
    if not isinstance(songs, list):
        return []
    return [item for item in songs if isinstance(item, dict)]


def extract_recommend_songlists(data: Any) -> list[dict[str, Any]]:
    """Extract playlist basics from RecommendSonglistResponse raw payload."""
    if not isinstance(data, dict):
        return []
    songlists = data.get("songlists")
    if isinstance(songlists, list):
        return [item for item in songlists if isinstance(item, dict)]
    items = data.get("List")
    if not isinstance(items, list):
        return []
    result: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        playlist_obj = item.get("Playlist")
        if not isinstance(playlist_obj, dict):
            continue
        basic = playlist_obj.get("basic")
        if not isinstance(basic, dict):
            continue
        merged = dict(basic)
        cover_obj = playlist_obj.get("cover")
        if isinstance(cover_obj, dict):
            cover_url = normalize_image_url(
                cover_obj.get("default_url")
                or cover_obj.get("medium_url")
                or cover_obj.get("big_url")
                or cover_obj.get("small_url")
            )
            if cover_url and not merged.get("picurl"):
                merged["picurl"] = cover_url
        creator_obj = playlist_obj.get("creator")
        if isinstance(creator_obj, dict) and not merged.get("creator"):
            merged["creator"] = creator_obj
        if playlist_obj.get("desc") and not merged.get("desc"):
            merged["desc"] = playlist_obj.get("desc")
        result.append(merged)
    return result


def get_artist_mapping(
    artist_obj: dict[str, Any] | str, provider_instance_id: str
) -> ItemMapping | None:
    """Build artist item mapping from QQ artist payload."""
    if isinstance(artist_obj, str):
        return None
    artist_id = extract_artist_mid(artist_obj)
    if not artist_id:
        return None
    artist_name = extract_first_text(
        artist_obj,
        (
            "name",
            "title",
            "singerName",
            "singername",
            "singer_name",
            "title_main",
            "search_title",
            "singerName_hilight",
            "name_hilight",
            "title_hilight",
        ),
        "Unknown Artist",
    )
    return ItemMapping(
        media_type=MediaType.ARTIST,
        item_id=artist_id,
        provider=provider_instance_id,
        name=artist_name,
    )


def parse_artist(
    artist_obj: dict[str, Any], provider_domain: str, provider_instance_id: str
) -> Artist:
    """Parse raw qqmusic artist object."""
    artist_id = extract_artist_mid(artist_obj)
    if not artist_id:
        raise InvalidDataError("Artist object does not contain id/mid")
    artist_name = extract_first_text(
        artist_obj,
        (
            "name",
            "Name",
            "title",
            "singerName",
            "singername",
            "singer_name",
            "SingerName",
            "title_main",
            "search_title",
            "singerName_hilight",
            "name_hilight",
            "title_hilight",
            "singer_hilight",
            "matchName",
            "match_name",
            "nickname",
            "nick",
        ),
        "Unknown Artist",
    )
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
        artist_obj.get("subtitle") or artist_obj.get("desc") or artist_obj.get("description") or "",
        "",
    )
    if subtitle:
        artist.metadata.description = subtitle
        artist.metadata.description_language = "zh"
    artist_image = (
        normalize_image_url(
            artist_obj.get("singerPic")
            or artist_obj.get("pic")
            or artist_obj.get("avatar")
            or artist_obj.get("Avatar")
            or artist_obj.get("AvatarUrl")
            or artist_obj.get("singer_pic")
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
    """Parse raw qqmusic album object."""
    album_id = extract_album_mid(album_obj)
    if not album_id:
        raise InvalidDataError("Album object does not contain id/mid")
    raw_album_name = str(
        album_obj.get("title")
        or album_obj.get("name")
        or album_obj.get("albumName")
        or "Unknown Album"
    )
    album_subtitle = clean_text(
        album_obj.get("subtitle")
        or album_obj.get("albumTranName")
        or album_obj.get("title_extra")
        or "",
        "",
    )
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
    if release_str := album_obj.get("time_public") or album_obj.get("publishDate"):
        with suppress(ValueError):
            album.year = datetime.strptime(release_str, "%Y-%m-%d").year  # noqa: DTZ007
    album_desc = clean_text(
        album_obj.get("description")
        or album_obj.get("description2")
        or album_obj.get("desc")
        or "",
        "",
    )
    if album_desc:
        album.metadata.description = album_desc
        album.metadata.description_language = "zh"
    artist_candidates = album_obj.get("singer")
    if isinstance(artist_candidates, dict):
        artist_iterable: list[dict[str, Any] | str] = [artist_candidates]
    elif isinstance(artist_candidates, list):
        artist_iterable = artist_candidates
    elif isinstance(artist_candidates, str):
        artist_iterable = [artist_candidates]
    else:
        singer_list = album_obj.get("singer_list")
        artist_iterable = singer_list if isinstance(singer_list, list) else []
    for artist_obj in artist_iterable:
        if artist_mapping := get_artist_mapping(artist_obj, provider_instance_id):
            album.artists.append(artist_mapping)
    cover_path = normalize_image_url(
        album_obj.get("pic")
        or album_obj.get("picUrl")
        or album_obj.get("picurl")
        or album_obj.get("logo")
        or album_obj.get("cover")
    )
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


def parse_track(  # noqa: PLR0915
    track_obj: dict[str, Any],
    provider_domain: str,
    provider_instance_id: str,
    get_max_supported_audio_format: Callable[[dict[str, Any]], tuple[AudioFormat, str | None]],
) -> Track:
    """Parse raw qqmusic track object."""
    track_id = extract_track_mid(track_obj)
    if not track_id:
        raise InvalidDataError("Track object does not contain id/mid")
    raw_track_name = clean_text(
        track_obj.get("title") or track_obj.get("name"),
        "Unknown Track",
    )
    track_subtitle = clean_text(
        track_obj.get("subtitle") or track_obj.get("title_extra") or "",
        "",
    )
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
    track_desc = clean_text(
        track_obj.get("desc") or track_obj.get("content") or "",
        "",
    )
    if track_desc:
        track.metadata.description = track_desc
        track.metadata.description_language = "zh"
    album_obj = track_obj.get("album")
    album_id = ""
    album_name = ""
    if isinstance(album_obj, dict):
        album_id = str(
            album_obj.get("mid")
            or album_obj.get("albumMid")
            or album_obj.get("albumMID")
            or album_obj.get("albummid")
            or album_obj.get("albumID")
            or album_obj.get("albumid")
            or album_obj.get("id")
            or ""
        )
        album_name = clean_text(
            album_obj.get("name")
            or album_obj.get("title")
            or album_obj.get("albumName")
            or album_obj.get("albumTitle")
        )
    if not album_id:
        album_id = str(
            track_obj.get("albummid")
            or track_obj.get("albumMid")
            or track_obj.get("albumMID")
            or track_obj.get("albumid")
            or track_obj.get("albumID")
            or ""
        )
    if not album_name:
        album_name = clean_text(
            track_obj.get("albumname")
            or track_obj.get("albumName")
            or track_obj.get("albumTitle")
            or track_obj.get("album_title")
        )
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

    singer_list: list[dict[str, Any] | str]
    raw_singer = track_obj.get("singer") or track_obj.get("singer_list") or track_obj.get("singers")
    if isinstance(raw_singer, dict):
        singer_list = [raw_singer]
    elif isinstance(raw_singer, list):
        singer_list = raw_singer
    elif isinstance(raw_singer, str):
        singer_list = [raw_singer]
    else:
        singer_list = []
    for artist_obj in singer_list:
        if artist_mapping := get_artist_mapping(artist_obj, provider_instance_id):
            track.artists.append(artist_mapping)
    if not track.artists:
        fallback_artist_id = str(
            track_obj.get("singerMid")
            or track_obj.get("singerMID")
            or track_obj.get("singer_mid")
            or ""
        )
        fallback_artist_name = clean_text(
            track_obj.get("singerName")
            or track_obj.get("singername")
            or track_obj.get("singer_name")
            or ""
        )
        if fallback_artist_id and fallback_artist_name:
            track.artists.append(
                ItemMapping(
                    media_type=MediaType.ARTIST,
                    item_id=fallback_artist_id,
                    provider=provider_instance_id,
                    name=fallback_artist_name,
                )
            )

    cover_id = ""
    if isinstance(album_obj, dict):
        cover_id = str(
            album_obj.get("mid")
            or album_obj.get("albumMid")
            or album_obj.get("albumMID")
            or album_obj.get("albummid")
            or ""
        )
    if not cover_id:
        cover_id = album_id
    if cover_id:
        if isinstance(track.album, Album):
            track.album.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=f"https://y.qq.com/music/photo_new/T002R500x500M000{cover_id}.jpg",
                        provider=provider_instance_id,
                        remotely_accessible=True,
                    )
                ]
            )
        track.metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=f"https://y.qq.com/music/photo_new/T002R500x500M000{cover_id}.jpg",
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
    """Parse raw qqmusic playlist object."""
    dissid, dirid = extract_playlist_ids(playlist_obj)
    if not dissid:
        raise InvalidDataError("Playlist object missing dissid/tid")
    playlist_id = build_playlist_id(dissid, dirid)
    playlist_name = clean_text(
        playlist_obj.get("dirName")
        or playlist_obj.get("dirname")
        or playlist_obj.get("name")
        or playlist_obj.get("title")
        or playlist_obj.get("search_title")
        or playlist_obj.get("name_hilight")
        or playlist_obj.get("title_hilight")
        or playlist_obj.get("diss_name")
        or playlist_obj.get("dissname")
        or "QQ Music Playlist",
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
        playlist_obj.get("creator", {}).get("name")
        or playlist_obj.get("creator", {}).get("nick")
        or playlist_obj.get("nickname")
        or playlist_obj.get("nick")
        or ""
    )
    if owner_name:
        playlist.owner = owner_name
    description = clean_text(
        playlist_obj.get("desc") or playlist_obj.get("description"),
        "",
    )
    if description:
        playlist.metadata.description = description
        playlist.metadata.description_language = "zh"

    cover_val = playlist_obj.get("cover")
    cover = ""
    if isinstance(cover_val, dict):
        cover = normalize_image_url(
            cover_val.get("default_url")
            or cover_val.get("medium_url")
            or cover_val.get("big_url")
            or cover_val.get("small_url")
        )
    if not cover:
        cover = normalize_image_url(
            cover_val
            or playlist_obj.get("logo")
            or playlist_obj.get("picurl")
            or playlist_obj.get("cover_url_big")
            or playlist_obj.get("pic")
            or playlist_obj.get("picUrl")
        )
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
