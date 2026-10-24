"""
Renderer parsing for the native YouTube Music provider.

InnerTube responses are deeply nested "renderer" trees whose container paths
reshuffle often, while renderer names stay stable. So we deep-find renderers by
name, classify items by their navigation target's page type (locale independent)
and return small normalized dicts the provider turns into MA media items.

Normalized items carry a "kind": track, album, artist, playlist, podcast or
episode. Requests pin hl=en, so the few labels we read (album type, shelf
titles, episode dates) are English.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

_DURATION_RE = re.compile(r"^\d{1,2}(?::\d{2}){1,2}$")
_SPOKEN_DURATION_RE = re.compile(r"(\d+)\s*(hr|hour|min|sec)", re.IGNORECASE)
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
_IMAGE_SIZE_RE = re.compile(r"=(?:w\d+-h\d+|s\d+)(?:-[a-z0-9]+)*$", re.IGNORECASE)

PAGE_ALBUM = "MUSIC_PAGE_TYPE_ALBUM"
PAGE_ARTIST = "MUSIC_PAGE_TYPE_ARTIST"
PAGE_USER_CHANNEL = "MUSIC_PAGE_TYPE_USER_CHANNEL"
PAGE_LIBRARY_ARTIST = "MUSIC_PAGE_TYPE_LIBRARY_ARTIST"
PAGE_PLAYLIST = "MUSIC_PAGE_TYPE_PLAYLIST"
PAGE_PODCAST = "MUSIC_PAGE_TYPE_PODCAST_SHOW_DETAIL_PAGE"
PAGE_EPISODE = "MUSIC_PAGE_TYPE_NON_MUSIC_AUDIO_TRACK_PAGE"
PAGE_LYRICS = "MUSIC_PAGE_TYPE_TRACK_LYRICS"
PAGE_RELATED = "MUSIC_PAGE_TYPE_TRACK_RELATED"
VIDEO_TYPE_EPISODE = "MUSIC_VIDEO_TYPE_PODCAST_EPISODE"
VIDEO_TYPE_SONG = "MUSIC_VIDEO_TYPE_ATV"
_ARTIST_PAGES = (PAGE_ARTIST, PAGE_USER_CHANNEL, PAGE_LIBRARY_ARTIST)
_ALBUM_TYPES = {"album": "album", "single": "single", "ep": "ep", "audiobook": "audiobook"}


# ---------------------------------------------------------------- tree helpers


def find_all(obj: Any, key: str, out: list[Any] | None = None) -> list[Any]:
    """Collect every value stored under `key`, anywhere in the tree (not inside matches)."""
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
        if key in obj:
            return obj[key]
        for v in obj.values():
            if (found := find_one(v, key)) is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            if (found := find_one(item, key)) is not None:
                return found
    return None


def get_text(node: Any) -> str:
    """Join a text node's `runs` (or read its `simpleText`)."""
    if not isinstance(node, dict):
        return ""
    if "runs" in node:
        return "".join(run.get("text", "") for run in node["runs"])
    return str(node.get("simpleText", ""))


def get_runs(node: Any) -> list[dict[str, Any]]:
    """Return a text node's runs."""
    if isinstance(node, dict) and isinstance(node.get("runs"), list):
        return [run for run in node["runs"] if isinstance(run, dict)]
    return []


def thumbnails(node: Any) -> list[dict[str, Any]]:
    """Return the largest `thumbnails` list found under the given node."""
    best: list[dict[str, Any]] = []
    for thumbs in find_all(node, "thumbnails"):
        if isinstance(thumbs, list) and len(thumbs) >= len(best):
            best = thumbs
    return [t for t in best if isinstance(t, dict) and t.get("url")]


def image_url(thumbs: list[dict[str, Any]]) -> str | None:
    """
    Return the URL of the stored original image behind a thumbnails list.

    Google image URLs carry a resize spec (`=w544-h544-l90-rj`, `=s120-...`). The
    resized variants are generated on demand and rate limited far harder than
    the stored original, which the bare URL serves straight from cache.
    """
    if not thumbs:
        return None
    best = max(thumbs, key=lambda t: t.get("width", 0) or 0)
    url = str(best["url"])
    if url.startswith("//"):
        url = f"https:{url}"
    if "googleusercontent.com" in url or "ggpht.com" in url:
        url = _IMAGE_SIZE_RE.sub("", url)
    return url


def parse_duration(text: str | None) -> int | None:
    """Convert an `m:ss` / `h:mm:ss` duration to seconds."""
    text = (text or "").strip()
    if not _DURATION_RE.match(text):
        return None
    seconds = 0
    for part in text.split(":"):
        seconds = seconds * 60 + int(part)
    return seconds


def parse_spoken_duration(text: str | None) -> int | None:
    """Convert a duration like `1 hr 55 min` or `45 min` to seconds."""
    factors = {"hr": 3600, "hour": 3600, "min": 60, "sec": 1}
    seconds = sum(
        int(amount) * factors[unit.lower()]
        for amount, unit in _SPOKEN_DURATION_RE.findall(text or "")
    )
    return seconds or None


def page_type(endpoint: Any) -> str | None:
    """Return the music page type of a browse endpoint."""
    config = find_one(endpoint, "browseEndpointContextMusicConfig")
    return config.get("pageType") if isinstance(config, dict) else None


def browse_id(endpoint: Any) -> str | None:
    """Return the browseId of a (nested) browse endpoint."""
    browse = find_one(endpoint, "browseEndpoint")
    return browse.get("browseId") if isinstance(browse, dict) else None


def video_type(node: Any) -> str | None:
    """Return the musicVideoType (ATV song, OMV/UGC video, episode, ...)."""
    config = find_one(node, "watchEndpointMusicConfig")
    return config.get("musicVideoType") if isinstance(config, dict) else None


def continuation_token(response: Any) -> str | None:
    """Return the token for the next page of a browse response, in either style."""
    if isinstance(legacy := find_one(response, "nextContinuationData"), dict):
        if token := legacy.get("continuation"):
            return str(token)
    for item in find_all(response, "continuationItemRenderer"):
        if token := find_one(item, "token"):
            return str(token)
    return None


# ----------------------------------------------------------------- run helpers


def strip_artist_id(raw_id: str) -> str:
    """Library artist pages are `MPLA` + the artist's channel id."""
    return raw_id.removeprefix("MPLA")


def _run_artists(runs: list[dict[str, Any]]) -> list[dict[str, str]]:
    artists: list[dict[str, str]] = []
    for run in runs:
        endpoint = run.get("navigationEndpoint")
        target = browse_id(endpoint)
        if not target:
            continue
        kind = page_type(endpoint)
        if kind in _ARTIST_PAGES or (kind is None and target.startswith("UC")):
            artists.append({"id": strip_artist_id(target), "name": run.get("text", "").strip()})
    return artists


def _run_album(runs: list[dict[str, Any]]) -> dict[str, str] | None:
    for run in runs:
        endpoint = run.get("navigationEndpoint")
        if page_type(endpoint) == PAGE_ALBUM and (target := browse_id(endpoint)):
            return {"id": target, "name": run.get("text", "").strip()}
    return None


def _run_duration(runs: list[dict[str, Any]]) -> int | None:
    for run in runs:
        if not run.get("navigationEndpoint") and (seconds := parse_duration(run.get("text"))):
            return seconds
    return None


def _run_year(runs: list[dict[str, Any]]) -> int | None:
    for run in runs:
        text = run.get("text", "").strip()
        if _YEAR_RE.match(text):
            return int(text)
    return None


def _album_type(runs: list[dict[str, Any]]) -> str | None:
    if runs:
        return _ALBUM_TYPES.get(runs[0].get("text", "").strip().lower())
    return None


def _explicit(node: Any) -> bool:
    for badge in find_all(node, "musicInlineBadgeRenderer"):
        label = find_one(badge, "label")
        if isinstance(label, str) and "explicit" in label.lower():
            return True
    return False


def _menu_targets(node: Any) -> dict[str, str]:
    """Map page type -> browseId for the "Go to artist/album" entries of an item menu."""
    targets: dict[str, str] = {}
    for item in find_all(
        node.get("menu") if isinstance(node, dict) else None, "menuNavigationItemRenderer"
    ):
        endpoint = item.get("navigationEndpoint")
        kind, target = page_type(endpoint), browse_id(endpoint)
        if kind and target:
            targets.setdefault(kind, target)
    return targets


def _flex_runs(row: dict[str, Any]) -> list[list[dict[str, Any]]]:
    return [
        get_runs(col.get("musicResponsiveListItemFlexColumnRenderer", {}).get("text"))
        for col in row.get("flexColumns", [])
    ]


# ----------------------------------------------------------------- item parsers


def _track(
    video_id: str,
    title: str,
    runs: list[dict[str, Any]],
    node: Any,
    duration: int | None = None,
) -> dict[str, Any]:
    return {
        "kind": "track",
        "video_id": video_id,
        "title": title,
        "artists": _run_artists(runs),
        "album": _run_album(runs),
        "duration": duration or _run_duration(runs),
        "thumbnails": thumbnails(node),
        "explicit": _explicit(node),
        "video_type": video_type(node),
        "set_video_id": None,
        "track_number": None,
    }


def _episode(video_id: str, title: str, runs: list[dict[str, Any]], node: Any) -> dict[str, Any]:
    podcast: dict[str, str] | None = None
    for run in runs:
        endpoint = run.get("navigationEndpoint")
        if page_type(endpoint) == PAGE_PODCAST and (target := browse_id(endpoint)):
            podcast = {"id": target.removeprefix("MPSP"), "name": run.get("text", "")}
    return {
        "kind": "episode",
        "video_id": video_id,
        "title": title,
        "podcast": podcast,
        "date": _episode_date(run.get("text", "") for run in runs),
        "duration": _run_duration(runs),
        "description": None,
        "thumbnails": thumbnails(node),
        "progress": None,
    }


def _browse_item(
    target: str, kind: str | None, title: str, runs: list[dict[str, Any]], node: Any
) -> dict[str, Any] | None:
    thumbs = thumbnails(node)
    if kind == PAGE_ALBUM or (kind is None and target.startswith("MPRE")):
        return {
            "kind": "album",
            "browse_id": target,
            "title": title,
            "artists": _run_artists(runs),
            "year": _run_year(runs),
            "album_type": _album_type(runs),
            "thumbnails": thumbs,
            "explicit": _explicit(node),
        }
    if kind in _ARTIST_PAGES or (kind is None and target.startswith("UC")):
        return {
            "kind": "artist",
            "channel_id": strip_artist_id(target),
            "name": title,
            "thumbnails": thumbs,
        }
    if kind == PAGE_PLAYLIST or (kind is None and target.startswith("VL")):
        authors = _run_artists(runs)
        return {
            "kind": "playlist",
            "playlist_id": target.removeprefix("VL"),
            "title": title,
            "author": authors[0]["name"] if authors else _author_text(runs),
            "thumbnails": thumbs,
        }
    if kind == PAGE_PODCAST:
        return {
            "kind": "podcast",
            "podcast_id": target.removeprefix("MPSP"),
            "title": title,
            "author": _author_text(runs),
            "thumbnails": thumbs,
        }
    return None


def _author_text(runs: list[dict[str, Any]]) -> str | None:
    """Return the owner from a subtitle like `Playlist • Owner • 35 songs`."""
    parts = [str(run.get("text", "")).strip() for run in runs]
    parts = [p for p in parts if p and p != "•"]
    labels = ("playlist", "podcast", "album", "single", "ep", "mix")
    if parts and parts[0].lower() in labels:
        parts = parts[1:]
    if not parts or parts[0].lower() == "auto playlist":
        return None
    return parts[0]


def parse_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """Parse a `musicResponsiveListItemRenderer` (a list row of any kind)."""
    columns = _flex_runs(row)
    if not columns:
        return None
    title = "".join(run.get("text", "") for run in columns[0]).strip()
    if not title:
        return None
    runs = [run for column in columns[1:] for run in column]
    nav = row.get("navigationEndpoint")
    if target := browse_id(nav):
        if item := _browse_item(target, page_type(nav), title, runs, row):
            return item
    item_data = row.get("playlistItemData") or {}
    video_id = item_data.get("videoId")
    if not video_id:
        play = find_one(row.get("overlay"), "watchEndpoint") or find_one(
            columns[0], "watchEndpoint"
        )
        video_id = play.get("videoId") if isinstance(play, dict) else None
    if not video_id:
        return None
    if video_type(row) == VIDEO_TYPE_EPISODE:
        return _episode(video_id, title, runs, row)
    fixed = [get_text(find_one(col, "text")) for col in row.get("fixedColumns", [])]
    track = _track(
        video_id,
        title,
        runs,
        row,
        next((d for text in fixed if (d := parse_duration(text))), None),
    )
    track["set_video_id"] = item_data.get("playlistSetVideoId")
    index = get_text(row.get("index"))
    track["track_number"] = int(index) if index.isdigit() else None
    if not track["artists"]:
        # rows on artist/album pages often credit the artist only in the menu
        if artist_id := _menu_targets(row).get(PAGE_ARTIST):
            name = next((r.get("text", "") for r in runs if r.get("text", "").strip(" •")), "")
            track["artists"] = [{"id": artist_id, "name": name}]
    return track


def parse_card(card: dict[str, Any]) -> dict[str, Any] | None:
    """Parse a `musicTwoRowItemRenderer` (a card in a carousel or grid)."""
    title = get_text(card.get("title")).strip()
    if not title:
        return None
    runs = get_runs(card.get("subtitle"))
    nav = card.get("navigationEndpoint") or {}
    if target := browse_id(nav):
        return _browse_item(target, page_type(nav), title, runs, card)
    if isinstance(watch := nav.get("watchEndpoint"), dict) and watch.get("videoId"):
        if video_type(nav) == VIDEO_TYPE_EPISODE:
            return _episode(watch["videoId"], title, runs, card)
        return _track(watch["videoId"], title, runs, card)
    if isinstance(mix := nav.get("watchPlaylistEndpoint"), dict) and mix.get("playlistId"):
        return {
            "kind": "playlist",
            "playlist_id": mix["playlistId"],
            "title": title,
            "author": _author_text(runs),
            "thumbnails": thumbnails(card),
        }
    return None


def parse_items(node: Any) -> list[dict[str, Any]]:
    """Parse every list row and card under the node."""
    items = [
        item
        for row in find_all(node, "musicResponsiveListItemRenderer")
        if (item := parse_row(row))
    ]
    items += [
        item for card in find_all(node, "musicTwoRowItemRenderer") if (item := parse_card(card))
    ]
    return items


def parse_shelves(response: Any) -> list[dict[str, Any]]:
    """Parse the carousels/shelves of a feed (home, artist, related) into titled item lists."""
    shelves: list[dict[str, Any]] = []
    for key in ("musicCarouselShelfRenderer", "musicShelfRenderer", "gridRenderer"):
        for shelf in find_all(response, key):
            header = shelf.get("header") or {}
            title = get_text(
                find_one(header, "title") if header else shelf.get("title")
            ) or get_text(shelf.get("title"))
            more = find_one(header, "browseEndpoint") or find_one(
                shelf.get("bottomEndpoint"), "browseEndpoint"
            )
            shelves.append(
                {
                    "title": title.strip(),
                    "items": parse_items(shelf.get("contents") or shelf.get("items")),
                    "more_browse_id": more.get("browseId") if isinstance(more, dict) else None,
                    "more_params": more.get("params") if isinstance(more, dict) else None,
                }
            )
    return [shelf for shelf in shelves if shelf["items"]]


# ----------------------------------------------------------------- page parsers


def _header(response: Any) -> dict[str, Any]:
    for key in (
        "musicResponsiveHeaderRenderer",
        "musicDetailHeaderRenderer",
        "musicImmersiveHeaderRenderer",
        "musicVisualHeaderRenderer",
    ):
        if isinstance(header := find_one(response, key), dict):
            return header
    return {}


def _description(response: Any, header: dict[str, Any]) -> str | None:
    # the header holds it either as plain text or wrapped in a description shelf
    for node in (header.get("description"), response):
        shelf = find_one(node, "musicDescriptionShelfRenderer")
        if isinstance(shelf, dict) and (text := get_text(shelf.get("description"))):
            return text
    return get_text(header.get("description")) or None


def _library_toggle(header: dict[str, Any]) -> tuple[str | None, bool]:
    """Return (playlistId the save toggle likes, saved?) from a page header's buttons."""
    for toggle in find_all(header.get("buttons"), "toggleButtonRenderer"):
        like = find_one(toggle.get("defaultServiceEndpoint"), "likeEndpoint")
        if isinstance(like, dict):
            target = like.get("target", {}).get("playlistId")
            return target, bool(toggle.get("isToggled"))
    return None, False


def parse_album_page(response: Any, album_id: str) -> dict[str, Any]:
    """Parse an album page (browse MPRE...) into the album, its tracks and other versions."""
    header = _header(response)
    title = get_text(header.get("title")).strip()
    strapline = get_runs(header.get("straplineTextOne"))
    artists = _run_artists(strapline)
    if not artists and (name := get_text(header.get("straplineTextOne")).strip()):
        artists = [{"id": "", "name": name}]
    subtitle = get_runs(header.get("subtitle"))
    playlist_id, saved = _library_toggle(header)
    if not playlist_id and isinstance(play := find_one(header, "watchEndpoint"), dict):
        playlist_id = play.get("playlistId")
    album = {
        "kind": "album",
        "browse_id": album_id,
        "title": title,
        "artists": artists,
        "year": _run_year(subtitle),
        "album_type": _album_type(subtitle),
        "thumbnails": thumbnails(header.get("thumbnail") or header),
        "explicit": _explicit(header),
        "description": _description(response, header),
        "playlist_id": playlist_id,
        "saved": saved,
    }
    tracks: list[dict[str, Any]] = []
    for row in find_all(response, "musicResponsiveListItemRenderer"):
        item = parse_row(row)
        if not item or item["kind"] != "track":
            continue
        item["artists"] = item["artists"] or [a for a in artists if a["id"]]
        item["album"] = {"id": album_id, "name": title}
        item["thumbnails"] = item["thumbnails"] or album["thumbnails"]
        item["explicit"] = item["explicit"] or False
        tracks.append(item)
    versions: list[dict[str, Any]] = []
    for shelf in parse_shelves(response):
        if "version" in shelf["title"].lower():
            versions += [item for item in shelf["items"] if item["kind"] == "album"]
    return {"album": album, "tracks": tracks, "versions": versions}


def parse_artist_page(response: Any, artist_id: str) -> dict[str, Any]:
    """Parse an artist page (browse UC...) into the artist, top songs and shelves."""
    header = _header(response)
    subscribe = find_one(header, "subscribeButtonRenderer") or {}
    top_shelf = find_one(response, "musicShelfRenderer") or {}
    songs_target = find_one(top_shelf.get("bottomEndpoint"), "browseEndpoint") or find_one(
        top_shelf.get("title"), "browseEndpoint"
    )
    return {
        "artist": {
            "kind": "artist",
            "channel_id": artist_id,
            "name": get_text(header.get("title")).strip(),
            "thumbnails": thumbnails(header.get("thumbnail") or header),
            "description": _description(response, header),
        },
        # subscribing needs the uploader channel, which can differ from the page id
        "subscribe_channel_id": subscribe.get("channelId"),
        "subscribed": bool(subscribe.get("subscribed")),
        "top_tracks": [
            item
            for row in find_all(top_shelf, "musicResponsiveListItemRenderer")
            if (item := parse_row(row)) and item["kind"] == "track"
        ],
        "songs_playlist_id": (
            songs_target.get("browseId", "").removeprefix("VL")
            if isinstance(songs_target, dict)
            else None
        ),
        "shelves": [
            shelf
            for shelf in parse_shelves(response)
            if shelf["title"] != get_text(top_shelf.get("title"))
        ],
    }


def parse_playlist_page(response: Any, playlist_id: str) -> dict[str, Any]:
    """Parse a playlist page (browse VL...) into the playlist and its first tracks."""
    header = _header(response)
    author = get_text(header.get("straplineTextOne")).strip()
    if not author and isinstance(
        stack := find_one(header.get("facepile"), "avatarStackViewModel"), dict
    ):
        author = str((stack.get("text") or {}).get("content", "")).strip()
    if not author and isinstance(facepile := find_one(header, "musicFacepileRenderer"), dict):
        author = get_text(facepile.get("text")).strip()
    saved_id, saved = _library_toggle(header)
    return {
        "playlist": {
            "kind": "playlist",
            "playlist_id": playlist_id,
            "title": get_text(header.get("title")).strip(),
            "author": author or None,
            "thumbnails": thumbnails(header.get("thumbnail") or header),
            "description": _description(response, header),
            "editable": find_one(response, "musicEditablePlaylistDetailHeaderRenderer") is not None,
            "saved": saved if saved_id else None,
        },
        "tracks": parse_playlist_tracks(response),
        "continuation": playlist_continuation(response),
    }


def _playlist_scope(response: Any) -> Any:
    """
    Return the part of a playlist response that holds the playlist's own entries.

    An owned playlist's page also carries a shelf of suggested songs, which must
    not be read as entries (nor its token followed as the playlist's next page).
    """
    for key in (
        "musicPlaylistShelfRenderer",
        "appendContinuationItemsAction",
        "musicPlaylistShelfContinuation",
    ):
        if scope := find_all(response, key):
            return scope
    return response


def parse_playlist_tracks(response: Any) -> list[dict[str, Any]]:
    """Parse the entries of a playlist page or one of its continuations."""
    tracks = []
    for row in find_all(_playlist_scope(response), "musicResponsiveListItemRenderer"):
        if (item := parse_row(row)) and item["kind"] in ("track", "episode"):
            tracks.append(item)
    return tracks


def playlist_continuation(response: Any) -> str | None:
    """Return the token for the next page of a playlist's entries."""
    return continuation_token(_playlist_scope(response))


def _panel_row(panel: dict[str, Any]) -> dict[str, Any] | None:
    """Parse a `playlistPanelVideoRenderer` (a watch queue row)."""
    video_id = panel.get("videoId")
    if not video_id:
        return None
    runs = get_runs(panel.get("longBylineText"))
    track = _track(
        video_id,
        get_text(panel.get("title")).strip(),
        runs,
        panel,
        parse_duration(get_text(panel.get("lengthText"))),
    )
    targets = _menu_targets(panel)
    if not track["album"] and (album_id := targets.get(PAGE_ALBUM)):
        track["album"] = {"id": album_id, "name": ""}
    if not track["artists"] and (artist_id := targets.get(PAGE_ARTIST)):
        name = get_text(panel.get("shortBylineText")).split(",")[0].split(" & ")[0].strip()
        track["artists"] = [{"id": artist_id, "name": name}]
    return track


def parse_watch_queue(response: Any) -> list[dict[str, Any]]:
    """
    Parse a watch queue (`next`, e.g. a radio) into tracks, in queue order.

    Songs arrive as a wrapper pairing two versions of one song: the audio track
    (ATV) and its music video. We keep the audio track, so the queue does not
    play a song twice. Its row credits the artists without links while the video
    row links them, so it borrows the ids from its partner.
    """
    tracks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for container in find_all(response, "playlistPanelRenderer") + find_all(
        response, "playlistPanelContinuation"
    ):
        for content in container.get("contents", []):
            if wrapper := content.get("playlistPanelVideoWrapperRenderer"):
                track = _panel_row(
                    find_one(wrapper.get("primaryRenderer"), "playlistPanelVideoRenderer") or {}
                )
                partner = _panel_row(
                    find_one(wrapper.get("counterpart"), "playlistPanelVideoRenderer") or {}
                )
                song_is_partner = (
                    track
                    and partner
                    and partner["video_type"] == VIDEO_TYPE_SONG
                    and track["video_type"] != VIDEO_TYPE_SONG
                )
                if song_is_partner:
                    track, partner = partner, track
                if track and partner:
                    track["artists"] = track["artists"] or partner["artists"]
                    track["album"] = track["album"] or partner["album"]
            else:
                track = _panel_row(content.get("playlistPanelVideoRenderer") or {})
            if track and track["video_id"] not in seen:
                seen.add(track["video_id"])
                tracks.append(track)
    return tracks


def radio_continuation(response: Any) -> str | None:
    """Return the token for the next page of a radio queue."""
    radio = find_one(response, "nextRadioContinuationData")
    token = radio.get("continuation") if isinstance(radio, dict) else None
    return str(token) if token else None


def parse_watch_track(response: Any, video_id: str) -> dict[str, Any] | None:
    """
    Parse the requested track out of a `next` response for one video.

    Also returns its like status and the browse ids of its lyrics and related tabs.
    """
    track = None
    for wrapper in find_all(response, "playlistPanelVideoWrapperRenderer"):
        rows = [
            _panel_row(find_one(wrapper.get(key), "playlistPanelVideoRenderer") or {})
            for key in ("primaryRenderer", "counterpart")
        ]
        own = next((row for row in rows if row and row["video_id"] == video_id), None)
        if own:
            partner = next((row for row in rows if row and row is not own), None)
            if partner:
                own["artists"] = own["artists"] or partner["artists"]
                own["album"] = own["album"] or partner["album"]
            track = own
            break
    if track is None:
        for panel in find_all(response, "playlistPanelVideoRenderer"):
            if panel.get("videoId") == video_id:
                track = _panel_row(panel)
                break
    if track is None:
        return None
    tabs = {
        page_type(tab.get("endpoint")): browse_id(tab.get("endpoint"))
        for tab in find_all(response, "tabRenderer")
    }
    like = find_one(response.get("playerOverlays"), "likeButtonRenderer") or find_one(
        response, "likeButtonRenderer"
    )
    track["like_status"] = like.get("likeStatus") if isinstance(like, dict) else None
    track["lyrics_browse_id"] = tabs.get(PAGE_LYRICS)
    track["related_browse_id"] = tabs.get(PAGE_RELATED)
    return track


def parse_lyrics(response: Any) -> tuple[str | None, str | None]:
    """Return (lyrics, source line) from a lyrics browse response."""
    shelf = find_one(response, "musicDescriptionShelfRenderer")
    if not isinstance(shelf, dict):
        return None, None
    return get_text(shelf.get("description")) or None, get_text(shelf.get("footer")) or None


def parse_podcast_page(response: Any, podcast_id: str) -> dict[str, Any]:
    """Parse a podcast page (browse MPSP...) into the podcast and its first episodes."""
    header = _header(response)
    saved_id, saved = _library_toggle(header)
    podcast = {
        "kind": "podcast",
        "podcast_id": podcast_id,
        "title": get_text(header.get("title")).strip(),
        "author": get_text(header.get("straplineTextOne")).strip() or None,
        "thumbnails": thumbnails(header.get("thumbnail") or header),
        "description": _description(response, header),
        "saved": saved if saved_id else None,
    }
    return {
        "podcast": podcast,
        "episodes": parse_episodes(response, podcast),
        "continuation": continuation_token(response),
    }


def parse_episodes(response: Any, podcast: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Parse the `musicMultiRowListItemRenderer` episode rows of a podcast (or episode) page."""
    episodes = []
    for row in find_all(response, "musicMultiRowListItemRenderer"):
        watch = find_one(row.get("onTap"), "watchEndpoint")
        if not isinstance(watch, dict) or not watch.get("videoId"):
            continue
        progress = (
            row.get("playbackProgress") or find_one(row, "musicPlaybackProgressRenderer") or {}
        )
        progress = progress.get("musicPlaybackProgressRenderer", progress)
        subtitle = get_text(row.get("subtitle"))
        episodes.append(
            {
                "kind": "episode",
                "video_id": watch["videoId"],
                "title": get_text(row.get("title")).strip(),
                "podcast": {"id": podcast["podcast_id"], "name": podcast["title"]}
                if podcast
                else None,
                "date": _episode_date(subtitle.split("•")),
                "duration": parse_spoken_duration(get_text(progress.get("playbackProgressText"))),
                "description": get_text(row.get("description")) or None,
                "thumbnails": thumbnails(row.get("thumbnail") or row),
                "progress": progress.get("playbackProgressPercentage"),
            }
        )
    return episodes


def parse_episode_page(response: Any, video_id: str) -> dict[str, Any] | None:
    """Parse a single episode page (browse MPED + video id)."""
    header = _header(response)
    if not get_text(header.get("title")):
        return None
    podcast = None
    for run in get_runs(header.get("straplineTextOne")):
        if (target := browse_id(run.get("navigationEndpoint"))) and target.startswith("MPSP"):
            podcast = {"id": target.removeprefix("MPSP"), "name": run.get("text", "")}
    progress = header.get("progress") or {}
    progress = progress.get("musicPlaybackProgressRenderer", progress)
    return {
        "kind": "episode",
        "video_id": video_id,
        "title": get_text(header.get("title")).strip(),
        "podcast": podcast,
        "date": _episode_date(get_text(header.get("subtitle")).split("•")),
        "duration": parse_spoken_duration(get_text(progress.get("playbackProgressText"))),
        "description": _description(response, {}),
        "thumbnails": thumbnails(header.get("thumbnail") or header),
        "progress": progress.get("playbackProgressPercentage"),
    }


def _episode_date(parts: Any) -> datetime | None:
    for part in parts:
        text = str(part).strip()
        for fmt in ("%b %d, %Y", "%B %d, %Y"):
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=UTC)
            except ValueError:
                continue
    return None
