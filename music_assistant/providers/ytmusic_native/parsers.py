"""
Renderer parsing for the native YouTube Music provider.

YouTube responses are deeply nested "renderer" objects whose container paths
reshuffle constantly, while the renderer *names* stay stable. So we parse by
deep-find on renderer names rather than fixed paths (reverseengeneer.md §4-§5)
and return small normalized dicts that the provider turns into MA media items.
"""

from __future__ import annotations

import re
from typing import Any

_DURATION_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")


def find_all(obj: Any, key: str, out: list[Any] | None = None) -> list[Any]:
    """Collect every value stored under `key`, anywhere in the tree."""
    if out is None:
        out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                out.append(v)
            else:
                find_all(v, key, out)
    elif isinstance(obj, list):
        for item in obj:
            find_all(item, key, out)
    return out


def find_one(obj: Any, key: str) -> Any:
    """Return the first value stored under `key`, depth-first, or None."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                return v
            found = find_one(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_one(item, key)
            if found is not None:
                return found
    return None


def get_text(node: Any) -> str:
    """Join a text node's `runs` (or read its `simpleText`)."""
    if not isinstance(node, dict):
        return ""
    if "runs" in node:
        return "".join(run.get("text", "") for run in node["runs"])
    return node.get("simpleText", "")


def get_thumbnails(node: Any) -> list[dict[str, Any]]:
    """Return the largest `thumbnails` list found under the given node."""
    best: list[dict[str, Any]] = []
    for thumbs in find_all(node, "thumbnails"):
        if isinstance(thumbs, list) and len(thumbs) >= len(best):
            best = thumbs
    return [t for t in best if isinstance(t, dict) and t.get("url")]


def parse_duration(text: str) -> int | None:
    """Convert an `m:ss` / `h:mm:ss` duration string to seconds."""
    if not text or not _DURATION_RE.match(text.strip()):
        return None
    parts = [int(p) for p in text.strip().split(":")]
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds


def _page_type(endpoint: dict[str, Any] | None) -> str | None:
    if not isinstance(endpoint, dict):
        return None
    config = find_one(endpoint, "browseEndpointContextMusicConfig")
    if isinstance(config, dict):
        return config.get("pageType")
    return None


def _is_explicit(node: Any) -> bool:
    for badge in find_all(node, "musicInlineBadgeRenderer"):
        label = find_one(badge, "accessibilityData")
        if isinstance(label, dict) and "explicit" in str(label.get("label", "")).lower():
            return True
    return False


def parse_list_item(renderer: dict[str, Any]) -> dict[str, Any] | None:
    """
    Parse a `musicResponsiveListItemRenderer` (a song/album/artist/playlist row).

    Classification follows the row's own navigationEndpoint: a browseEndpoint
    means album/artist/playlist; a bare playable means a track.
    """
    flex_columns = [
        col.get("musicResponsiveListItemFlexColumnRenderer", {})
        for col in renderer.get("flexColumns", [])
    ]
    if not flex_columns:
        return None
    title = get_text(flex_columns[0].get("text"))
    if not title:
        return None
    thumbnails = get_thumbnails(renderer)
    explicit = _is_explicit(renderer)

    nav = renderer.get("navigationEndpoint")
    page_type = _page_type(nav)
    browse_id = None
    if isinstance(nav, dict):
        browse_id = find_one(nav, "browseId")

    if page_type == "MUSIC_PAGE_TYPE_ARTIST" and browse_id:
        return {"kind": "artist", "channel_id": browse_id, "name": title, "thumbnails": thumbnails}
    if page_type == "MUSIC_PAGE_TYPE_ALBUM" and browse_id:
        return {
            "kind": "album",
            "browse_id": browse_id,
            "name": title,
            "artists": _runs_artists(flex_columns),
            "thumbnails": thumbnails,
            "explicit": explicit,
        }
    if page_type == "MUSIC_PAGE_TYPE_PLAYLIST" and browse_id:
        return {
            "kind": "playlist",
            "playlist_id": browse_id.removeprefix("VL"),
            "name": title,
            "thumbnails": thumbnails,
        }

    # otherwise treat as a track/video
    video_id = _row_video_id(renderer)
    if not video_id:
        return None
    album = _runs_album(flex_columns)
    duration = _row_duration(renderer, flex_columns)
    set_video_id = None
    if isinstance(renderer.get("playlistItemData"), dict):
        set_video_id = renderer["playlistItemData"].get("playlistSetVideoId")
    return {
        "kind": "track",
        "video_id": video_id,
        "name": title,
        "artists": _runs_artists(flex_columns),
        "album": album,
        "duration": duration,
        "thumbnails": thumbnails,
        "explicit": explicit,
        "set_video_id": set_video_id,
    }


def parse_two_row_item(renderer: dict[str, Any]) -> dict[str, Any] | None:
    """Parse a `musicTwoRowItemRenderer` (a home/grid card) into a normalized item."""
    title = get_text(renderer.get("title"))
    if not title:
        return None
    nav = renderer.get("navigationEndpoint")
    thumbnails = get_thumbnails(renderer)
    subtitle_runs = renderer.get("subtitle", {}).get("runs", [])
    page_type = _page_type(nav)
    if isinstance(nav, dict) and find_one(nav, "watchEndpoint") and not page_type:
        watch = find_one(nav, "watchEndpoint")
        video_id = watch.get("videoId") if isinstance(watch, dict) else None
        if not video_id:
            return None
        return {
            "kind": "track",
            "video_id": video_id,
            "name": title,
            "artists": _subtitle_artists(subtitle_runs),
            "album": None,
            "duration": None,
            "thumbnails": thumbnails,
            "explicit": _is_explicit(renderer),
            "set_video_id": None,
        }
    browse_id = find_one(nav, "browseId") if isinstance(nav, dict) else None
    if not browse_id:
        return None
    if page_type == "MUSIC_PAGE_TYPE_ARTIST":
        return {"kind": "artist", "channel_id": browse_id, "name": title, "thumbnails": thumbnails}
    if page_type == "MUSIC_PAGE_TYPE_PLAYLIST":
        return {
            "kind": "playlist",
            "playlist_id": browse_id.removeprefix("VL"),
            "name": title,
            "thumbnails": thumbnails,
            "author": _subtitle_text(subtitle_runs),
        }
    # default: album / single / EP
    return {
        "kind": "album",
        "browse_id": browse_id,
        "name": title,
        "artists": _subtitle_artists(subtitle_runs),
        "thumbnails": thumbnails,
        "explicit": _is_explicit(renderer),
        "year": _subtitle_year(subtitle_runs),
    }


def parse_items(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse every list-item and card renderer found in a response."""
    items: list[dict[str, Any]] = []
    for renderer in find_all(response, "musicResponsiveListItemRenderer"):
        if parsed := parse_list_item(renderer):
            items.append(parsed)
    for renderer in find_all(response, "musicTwoRowItemRenderer"):
        if parsed := parse_two_row_item(renderer):
            items.append(parsed)
    return items


def parse_search(response: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Split a search response into normalized items per media kind."""
    result: dict[str, list[dict[str, Any]]] = {
        "track": [],
        "album": [],
        "artist": [],
        "playlist": [],
    }
    for item in parse_items(response):
        result.setdefault(item["kind"], []).append(item)
    return result


def parse_album(response: dict[str, Any], album_id: str) -> dict[str, Any]:
    """Parse an album browse response into header + track list."""
    header = find_one(response, "musicResponsiveHeaderRenderer") or find_one(
        response, "musicDetailHeaderRenderer"
    )
    header = header or {}
    name = get_text(header.get("title"))
    artists = _strapline_artists(header)
    tracks: list[dict[str, Any]] = []
    for renderer in find_all(response, "musicResponsiveListItemRenderer"):
        parsed = parse_list_item(renderer)
        if parsed and parsed["kind"] == "track":
            if not parsed["artists"]:
                parsed["artists"] = artists
            tracks.append(parsed)
    return {
        "browse_id": album_id,
        "name": name,
        "artists": artists,
        "thumbnails": get_thumbnails(header) or get_thumbnails(response),
        "year": _subtitle_year(find_one(header, "subtitle") or {}),
        "playlist_id": find_one(response, "audioPlaylistId"),
        "tracks": tracks,
    }


def parse_artist(response: dict[str, Any]) -> dict[str, Any]:
    """Parse an artist browse response into header + top tracks + album cards."""
    header = (
        find_one(response, "musicImmersiveHeaderRenderer")
        or find_one(response, "musicVisualHeaderRenderer")
        or {}
    )
    subscribe = find_one(response, "subscribeButtonRenderer") or {}
    top_tracks: list[dict[str, Any]] = []
    for renderer in find_all(response, "musicResponsiveListItemRenderer"):
        parsed = parse_list_item(renderer)
        if parsed and parsed["kind"] == "track":
            top_tracks.append(parsed)
    albums = [item for item in parse_items(response) if item["kind"] == "album"]
    return {
        "channel_id": subscribe.get("channelId") or find_one(header, "browseId"),
        "name": get_text(header.get("title")),
        "description": get_text(find_one(header, "description")),
        "thumbnails": get_thumbnails(header),
        "subscribed": subscribe.get("subscribed", False),
        "top_tracks": top_tracks,
        "albums": albums,
    }


def parse_playlist(response: dict[str, Any], playlist_id: str) -> dict[str, Any]:
    """Parse a playlist browse response into header + track list."""
    header = find_one(response, "musicResponsiveHeaderRenderer") or find_one(
        response, "musicDetailHeaderRenderer"
    )
    header = header or {}
    tracks: list[dict[str, Any]] = []
    for renderer in find_all(response, "musicResponsiveListItemRenderer"):
        parsed = parse_list_item(renderer)
        if parsed and parsed["kind"] == "track":
            tracks.append(parsed)
    return {
        "playlist_id": playlist_id,
        "name": get_text(header.get("title")),
        "description": get_text(find_one(header, "description")),
        "thumbnails": get_thumbnails(header),
        "author": get_text(find_one(header, "straplineTextOne")),
        "tracks": tracks,
    }


def parse_watch_tracks(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse `playlistPanelVideoRenderer` rows (watch queue / radio / similar)."""
    tracks: list[dict[str, Any]] = []
    for renderer in find_all(response, "playlistPanelVideoRenderer"):
        video_id = renderer.get("videoId")
        if not video_id:
            continue
        long_byline = renderer.get("longBylineText", {})
        tracks.append(
            {
                "kind": "track",
                "video_id": video_id,
                "name": get_text(renderer.get("title")),
                "artists": _runs_to_artists(long_byline.get("runs", [])),
                "album": None,
                "duration": parse_duration(get_text(renderer.get("lengthText"))),
                "thumbnails": get_thumbnails(renderer),
                "explicit": _is_explicit(renderer),
                "set_video_id": None,
            }
        )
    return tracks


def find_continuation(response: dict[str, Any]) -> str | None:
    """Return a continuation token from a response, if present."""
    if token := find_one(response, "continuation"):
        return token if isinstance(token, str) else None
    command = find_one(response, "continuationCommand")
    if isinstance(command, dict):
        return command.get("token")
    return None


# ----------------- private helpers -----------------


def _row_video_id(renderer: dict[str, Any]) -> str | None:
    play_button = find_one(renderer, "musicPlayButtonRenderer")
    if isinstance(play_button, dict):
        watch = find_one(play_button.get("playNavigationEndpoint", {}), "watchEndpoint")
        if isinstance(watch, dict) and watch.get("videoId"):
            return watch["videoId"]
    item_data = renderer.get("playlistItemData")
    if isinstance(item_data, dict) and item_data.get("videoId"):
        return item_data["videoId"]
    watch = find_one(renderer, "watchEndpoint")
    if isinstance(watch, dict):
        return watch.get("videoId")
    return None


def _row_duration(renderer: dict[str, Any], flex_columns: list[dict[str, Any]]) -> int | None:
    for fixed in renderer.get("fixedColumns", []):
        text = get_text(find_one(fixed, "text"))
        if duration := parse_duration(text):
            return duration
    for col in flex_columns:
        for run in col.get("text", {}).get("runs", []):
            if not run.get("navigationEndpoint") and (
                duration := parse_duration(run.get("text", ""))
            ):
                return duration
    return None


def _runs_artists(flex_columns: list[dict[str, Any]]) -> list[dict[str, str]]:
    # the artist runs live in the second flex column for song rows
    for col in flex_columns[1:]:
        artists = _runs_to_artists(col.get("text", {}).get("runs", []))
        if artists:
            return artists
    return []


def _runs_album(flex_columns: list[dict[str, Any]]) -> dict[str, str] | None:
    for col in flex_columns[1:]:
        for run in col.get("text", {}).get("runs", []):
            if _page_type(run.get("navigationEndpoint")) == "MUSIC_PAGE_TYPE_ALBUM":
                browse_id = find_one(run.get("navigationEndpoint", {}), "browseId")
                if browse_id:
                    return {"id": browse_id, "name": run.get("text", "")}
    return None


def _runs_to_artists(runs: list[dict[str, Any]]) -> list[dict[str, str]]:
    artists: list[dict[str, str]] = []
    for run in runs:
        endpoint = run.get("navigationEndpoint")
        if _page_type(endpoint) in ("MUSIC_PAGE_TYPE_ARTIST", "MUSIC_PAGE_TYPE_USER_CHANNEL"):
            browse_id = find_one(endpoint, "browseId")
            if browse_id:
                artists.append({"id": browse_id, "name": run.get("text", "")})
    return artists


def _subtitle_artists(runs: list[dict[str, Any]]) -> list[dict[str, str]]:
    return _runs_to_artists(runs)


def _subtitle_text(runs: list[dict[str, Any]]) -> str:
    return "".join(run.get("text", "") for run in runs)


def _subtitle_year(node: Any) -> str | None:
    runs = node.get("runs", []) if isinstance(node, dict) else node
    if not isinstance(runs, list):
        return None
    for run in runs:
        text = run.get("text", "") if isinstance(run, dict) else ""
        if text.strip().isdigit() and len(text.strip()) == 4:
            return text.strip()
    return None


def _strapline_artists(header: dict[str, Any]) -> list[dict[str, str]]:
    strapline = header.get("straplineTextOne")
    if isinstance(strapline, dict):
        artists = _runs_to_artists(strapline.get("runs", []))
        if artists:
            return artists
        text = get_text(strapline)
        if text:
            return [{"id": "", "name": text}]
    return []
