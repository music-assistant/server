"""Response parsing helpers for QQ Music provider."""

from __future__ import annotations

from typing import Any

from .helpers import normalize_image_url


def extract_song_id(track_obj: dict[str, Any]) -> int | None:
    """Extract numeric QQ song id from a track-like payload."""
    for key in ("id", "songid", "song_id", "songID"):
        raw_val = track_obj.get(key)
        if raw_val is None:
            continue
        try:
            song_id = int(raw_val)
        except (TypeError, ValueError):
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


def iter_dict_nodes(data: Any) -> list[dict[str, Any]]:
    """Collect dict nodes from nested response payloads."""
    result: list[dict[str, Any]] = []
    if isinstance(data, dict):
        result.append(data)
        for value in data.values():
            result.extend(iter_dict_nodes(value))
    elif isinstance(data, list):
        for value in data:
            result.extend(iter_dict_nodes(value))
    return result


def extract_track_candidates(data: Any) -> list[dict[str, Any]]:
    """Extract potential track dicts from mixed/nested payload."""
    candidates: list[dict[str, Any]] = []
    for node in iter_dict_nodes(data):
        if isinstance(node.get("track"), dict):
            candidates.append(node["track"])
            continue
        if isinstance(node.get("Track"), dict):
            candidates.append(node["Track"])
            continue
        if isinstance(node.get("songInfo"), dict):
            candidates.append(node["songInfo"])
            continue
        if isinstance(node.get("song"), dict):
            candidates.append(node["song"])
            continue
        if node.get("mid") or node.get("songMid") or node.get("songmid"):
            candidates.append(node)
    return candidates


def extract_playlist_candidates(data: Any) -> list[dict[str, Any]]:
    """Extract potential playlist dicts from mixed/nested payload."""
    candidates: list[dict[str, Any]] = []
    for node in iter_dict_nodes(data):
        if isinstance(node.get("Playlist"), dict):
            playlist_obj = node["Playlist"]
            if isinstance(playlist_obj.get("basic"), dict):
                basic = dict(playlist_obj["basic"])
                # Recommend feed often keeps cover/creator outside `basic`.
                cover_obj = playlist_obj.get("cover")
                if isinstance(cover_obj, dict):
                    cover_url = normalize_image_url(
                        cover_obj.get("default_url")
                        or cover_obj.get("medium_url")
                        or cover_obj.get("big_url")
                        or cover_obj.get("small_url")
                    )
                    if cover_url and not basic.get("picurl"):
                        basic["picurl"] = cover_url
                creator_obj = playlist_obj.get("creator")
                if isinstance(creator_obj, dict) and not basic.get("creator"):
                    basic["creator"] = creator_obj
                if playlist_obj.get("desc") and not basic.get("desc"):
                    basic["desc"] = playlist_obj.get("desc")
                candidates.append(basic)
            else:
                candidates.append(playlist_obj)
            continue
        if isinstance(node.get("playlist"), dict):
            playlist_obj = node["playlist"]
            if isinstance(playlist_obj.get("basic"), dict):
                candidates.append(playlist_obj["basic"])
            else:
                candidates.append(playlist_obj)
            continue
        if isinstance(node.get("basic"), dict):
            basic = node["basic"]
            if basic.get("dissid") or basic.get("tid") or basic.get("id"):
                candidates.append(basic)
                continue
        if (node.get("dissid") or node.get("tid") or node.get("id") or node.get("dirid")) and (
            node.get("title") or node.get("name") or node.get("dissname") or node.get("dirName")
        ):
            candidates.append(node)
    return candidates


def describe_payload(data: Any) -> str:
    """Return a compact human-readable summary of payload shape."""
    if isinstance(data, dict):
        keys = ", ".join(sorted(map(str, data.keys()))[:10])
        return f"dict(keys=[{keys}])"
    if isinstance(data, list):
        first_type = type(data[0]).__name__ if data else "empty"
        return f"list(len={len(data)}, first={first_type})"
    return type(data).__name__


def extract_guess_recommend_tracks(data: Any) -> list[dict[str, Any]]:
    """Extract tracks from GuessRecommendResponse raw payload: $.Tracks[*]."""
    if not isinstance(data, dict):
        return []
    tracks = data.get("Tracks")
    if not isinstance(tracks, list):
        return []
    return [item for item in tracks if isinstance(item, dict)]


def extract_radar_recommend_tracks(data: Any) -> list[dict[str, Any]]:
    """Extract tracks from RadarRecommendResponse raw payload: $.VecSongs[*].Track."""
    if not isinstance(data, dict):
        return []
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
    """Extract tracks from RecommendNewSongResponse raw payload: $.songlist[*]."""
    if not isinstance(data, dict):
        return []
    songs = data.get("songlist")
    if not isinstance(songs, list):
        return []
    return [item for item in songs if isinstance(item, dict)]


def extract_recommend_songlists(data: Any) -> list[dict[str, Any]]:
    """Extract playlist basics from RecommendSonglistResponse: $.List[*].Playlist.basic."""
    if not isinstance(data, dict):
        return []
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
