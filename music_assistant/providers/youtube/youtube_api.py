"""YouTube Data API v3 client for search and metadata retrieval.

Uses aiohttp to call the official YouTube Data API when an API key is configured.
Falls back gracefully when the API is unavailable or quota is exceeded.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from .constants import YT_DATA_API_BASE

if TYPE_CHECKING:
    from aiohttp import ClientSession

logger = logging.getLogger(__name__)

# Simple rate limiter: maximum number of concurrent API requests and minimum
# delay between requests to avoid bursting through quota.
_MAX_CONCURRENT = 3
_MIN_REQUEST_INTERVAL = 0.1  # seconds
_MAX_RETRIES = 2

_semaphore: asyncio.Semaphore | None = None
_last_request_time: float = 0.0


def _get_semaphore() -> asyncio.Semaphore:
    """Return (and lazily create) the global concurrency semaphore."""
    global _semaphore  # noqa: PLW0603
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
    return _semaphore


class YouTubeDataAPIError(Exception):
    """Raised when a YouTube Data API call fails."""


async def api_search_videos(
    http_session: ClientSession,
    api_key: str,
    query: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Search for videos using the YouTube Data API v3.

    :param http_session: aiohttp client session.
    :param api_key: YouTube Data API key.
    :param query: Search query string.
    :param limit: Maximum number of results to return.
    """
    params = {
        "part": "snippet",
        "type": "video",
        "q": query,
        "maxResults": str(limit),
        "key": api_key,
    }
    data = await _api_get(http_session, "/search", params)
    items = data.get("items", [])
    if not items:
        return []

    # Fetch video details (duration, stats) in a single batch call
    video_ids = [item["id"]["videoId"] for item in items if item.get("id", {}).get("videoId")]
    details_map = await _batch_video_details(http_session, api_key, video_ids)

    results: list[dict[str, Any]] = []
    for item in items:
        video_id = item.get("id", {}).get("videoId")
        if not video_id:
            continue
        snippet = item.get("snippet", {})
        entry = _snippet_to_entry(video_id, snippet)
        if video_id in details_map:
            entry.update(details_map[video_id])
        results.append(entry)
    return results


async def api_search_channels(
    http_session: ClientSession,
    api_key: str,
    query: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Search for channels using the YouTube Data API v3.

    :param http_session: aiohttp client session.
    :param api_key: YouTube Data API key.
    :param query: Search query string.
    :param limit: Maximum number of results to return.
    """
    params = {
        "part": "snippet",
        "type": "channel",
        "q": query,
        "maxResults": str(limit),
        "key": api_key,
    }
    data = await _api_get(http_session, "/search", params)
    results: list[dict[str, Any]] = []
    for item in data.get("items", []):
        channel_id = item.get("id", {}).get("channelId")
        if not channel_id:
            continue
        snippet = item.get("snippet", {})
        results.append(
            {
                "id": channel_id,
                "channel_id": channel_id,
                "channel": snippet.get("channelTitle", ""),
                "title": snippet.get("title", ""),
                "description": snippet.get("description", ""),
                "thumbnails": _api_thumbnails(snippet.get("thumbnails", {})),
            }
        )
    return results


async def api_search_playlists(
    http_session: ClientSession,
    api_key: str,
    query: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Search for playlists using the YouTube Data API v3.

    :param http_session: aiohttp client session.
    :param api_key: YouTube Data API key.
    :param query: Search query string.
    :param limit: Maximum number of results to return.
    """
    params = {
        "part": "snippet",
        "type": "playlist",
        "q": query,
        "maxResults": str(limit),
        "key": api_key,
    }
    data = await _api_get(http_session, "/search", params)
    results: list[dict[str, Any]] = []
    for item in data.get("items", []):
        playlist_id = item.get("id", {}).get("playlistId")
        if not playlist_id:
            continue
        snippet = item.get("snippet", {})
        results.append(
            {
                "id": playlist_id,
                "title": snippet.get("title", ""),
                "channel": snippet.get("channelTitle", ""),
                "channel_id": snippet.get("channelId", ""),
                "description": snippet.get("description", ""),
                "thumbnails": _api_thumbnails(snippet.get("thumbnails", {})),
            }
        )
    return results


async def api_get_video(
    http_session: ClientSession,
    api_key: str,
    video_id: str,
) -> dict[str, Any] | None:
    """Get video details by ID using the YouTube Data API v3.

    :param http_session: aiohttp client session.
    :param api_key: YouTube Data API key.
    :param video_id: YouTube video ID.
    """
    details_map = await _batch_video_details(http_session, api_key, [video_id])
    if video_id not in details_map:
        return None
    return details_map[video_id]


async def api_get_channel(
    http_session: ClientSession,
    api_key: str,
    channel_id: str,
) -> dict[str, Any] | None:
    """Get channel details by ID using the YouTube Data API v3.

    :param http_session: aiohttp client session.
    :param api_key: YouTube Data API key.
    :param channel_id: YouTube channel ID.
    """
    params = {
        "part": "snippet",
        "id": channel_id,
        "key": api_key,
    }
    data = await _api_get(http_session, "/channels", params)
    items = data.get("items", [])
    if not items:
        return None
    snippet = items[0].get("snippet", {})
    return {
        "id": channel_id,
        "channel_id": channel_id,
        "channel": snippet.get("title", ""),
        "title": snippet.get("title", ""),
        "description": snippet.get("description", ""),
        "thumbnails": _api_thumbnails(snippet.get("thumbnails", {})),
    }


async def api_get_channel_videos(
    http_session: ClientSession,
    api_key: str,
    channel_id: str,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Get recent videos from a channel using the YouTube Data API v3.

    :param http_session: aiohttp client session.
    :param api_key: YouTube Data API key.
    :param channel_id: YouTube channel ID.
    :param limit: Maximum number of videos to return.
    """
    params = {
        "part": "snippet",
        "type": "video",
        "channelId": channel_id,
        "order": "date",
        "maxResults": str(limit),
        "key": api_key,
    }
    data = await _api_get(http_session, "/search", params)
    items = data.get("items", [])
    if not items:
        return []

    video_ids = [item["id"]["videoId"] for item in items if item.get("id", {}).get("videoId")]
    details_map = await _batch_video_details(http_session, api_key, video_ids)

    results: list[dict[str, Any]] = []
    for item in items:
        video_id = item.get("id", {}).get("videoId")
        if not video_id:
            continue
        snippet = item.get("snippet", {})
        entry = _snippet_to_entry(video_id, snippet)
        if video_id in details_map:
            entry.update(details_map[video_id])
        results.append(entry)
    return results


async def api_get_channel_playlists(
    http_session: ClientSession,
    api_key: str,
    channel_id: str,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Get playlists from a channel using the YouTube Data API v3.

    :param http_session: aiohttp client session.
    :param api_key: YouTube Data API key.
    :param channel_id: YouTube channel ID.
    :param limit: Maximum number of playlists to return.
    """
    params = {
        "part": "snippet",
        "channelId": channel_id,
        "maxResults": str(min(limit, 50)),
        "key": api_key,
    }
    data = await _api_get(http_session, "/playlists", params)
    results: list[dict[str, Any]] = []
    for item in data.get("items", []):
        playlist_id = item.get("id")
        if not playlist_id:
            continue
        snippet = item.get("snippet", {})
        results.append(
            {
                "id": playlist_id,
                "title": snippet.get("title", ""),
                "channel": snippet.get("channelTitle", ""),
                "channel_id": snippet.get("channelId", ""),
                "description": snippet.get("description", ""),
                "thumbnails": _api_thumbnails(snippet.get("thumbnails", {})),
            }
        )
    return results


async def api_get_playlist(
    http_session: ClientSession,
    api_key: str,
    playlist_id: str,
) -> dict[str, Any] | None:
    """Get playlist details by ID using the YouTube Data API v3.

    :param http_session: aiohttp client session.
    :param api_key: YouTube Data API key.
    :param playlist_id: YouTube playlist ID.
    """
    params = {
        "part": "snippet",
        "id": playlist_id,
        "key": api_key,
    }
    data = await _api_get(http_session, "/playlists", params)
    items = data.get("items", [])
    if not items:
        return None
    snippet = items[0].get("snippet", {})
    return {
        "id": playlist_id,
        "title": snippet.get("title", ""),
        "channel": snippet.get("channelTitle", ""),
        "channel_id": snippet.get("channelId", ""),
        "description": snippet.get("description", ""),
        "thumbnails": _api_thumbnails(snippet.get("thumbnails", {})),
    }


async def api_get_playlist_videos(
    http_session: ClientSession,
    api_key: str,
    playlist_id: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Get videos from a playlist using the YouTube Data API v3.

    Follows nextPageToken pagination to retrieve up to ``limit`` videos.

    :param http_session: aiohttp client session.
    :param api_key: YouTube Data API key.
    :param playlist_id: YouTube playlist ID.
    :param limit: Maximum number of videos to return.
    """
    all_items: list[dict[str, Any]] = []
    page_token: str | None = None
    while len(all_items) < limit:
        page_size = min(limit - len(all_items), 50)
        params: dict[str, str] = {
            "part": "snippet",
            "playlistId": playlist_id,
            "maxResults": str(page_size),
            "key": api_key,
        }
        if page_token:
            params["pageToken"] = page_token
        data = await _api_get(http_session, "/playlistItems", params)
        items = data.get("items", [])
        if not items:
            break
        all_items.extend(items)
        page_token = data.get("nextPageToken")
        if not page_token:
            break

    video_ids = [
        item["snippet"]["resourceId"]["videoId"]
        for item in all_items
        if item.get("snippet", {}).get("resourceId", {}).get("videoId")
    ]
    details_map = await _batch_video_details(http_session, api_key, video_ids)

    results: list[dict[str, Any]] = []
    for item in all_items:
        video_id = item.get("snippet", {}).get("resourceId", {}).get("videoId")
        if not video_id:
            continue
        snippet = item.get("snippet", {})
        entry = _snippet_to_entry(video_id, snippet)
        if video_id in details_map:
            entry.update(details_map[video_id])
        results.append(entry)
    return results


async def _batch_video_details(
    http_session: ClientSession,
    api_key: str,
    video_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Fetch video details (duration, snippet) for multiple video IDs.

    Handles batches of up to 50 IDs per API call.

    :param http_session: aiohttp client session.
    :param api_key: YouTube Data API key.
    :param video_ids: List of YouTube video IDs.
    """
    if not video_ids:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        params = {
            "part": "snippet,contentDetails",
            "id": ",".join(batch),
            "key": api_key,
        }
        data = await _api_get(http_session, "/videos", params)
        for item in data.get("items", []):
            vid = item.get("id")
            if not vid:
                continue
            snippet = item.get("snippet", {})
            content_details = item.get("contentDetails", {})
            entry = _snippet_to_entry(vid, snippet)
            entry["duration"] = _parse_iso8601_duration(content_details.get("duration", ""))
            result[vid] = entry
    return result


def _snippet_to_entry(video_id: str, snippet: dict[str, Any]) -> dict[str, Any]:
    """Convert a YouTube API snippet into a yt-dlp-compatible entry dict.

    :param video_id: YouTube video ID.
    :param snippet: Snippet dict from the YouTube Data API response.
    """
    return {
        "id": video_id,
        "title": snippet.get("title", ""),
        "uploader": snippet.get("channelTitle", ""),
        "channel": snippet.get("channelTitle", ""),
        "channel_id": snippet.get("channelId", ""),
        "description": snippet.get("description", ""),
        "thumbnails": _api_thumbnails(snippet.get("thumbnails", {})),
    }


def _api_thumbnails(thumbs: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert YouTube API thumbnail dict to yt-dlp-style thumbnail list.

    :param thumbs: Thumbnails dict from the YouTube Data API response.
    """
    result: list[dict[str, Any]] = []
    for thumb in thumbs.values():
        if url := thumb.get("url"):
            result.append(
                {
                    "url": url,
                    "width": thumb.get("width", 0),
                    "height": thumb.get("height", 0),
                }
            )
    return result


def _parse_iso8601_duration(duration: str) -> int:
    """Parse an ISO 8601 duration string (e.g. PT1H2M30S) to seconds.

    :param duration: ISO 8601 duration string from the YouTube Data API.
    """
    if not duration or not duration.startswith("PT"):
        return 0
    duration = duration[2:]
    total = 0
    current = ""
    for char in duration:
        if char.isdigit():
            current += char
        elif char == "H":
            total += int(current or 0) * 3600
            current = ""
        elif char == "M":
            total += int(current or 0) * 60
            current = ""
        elif char == "S":
            total += int(current or 0)
            current = ""
    return total


async def _api_get(
    http_session: ClientSession,
    endpoint: str,
    params: dict[str, str],
) -> dict[str, Any]:
    """Make a GET request to the YouTube Data API v3.

    Applies concurrency limiting, minimum inter-request delay, and retries on
    429 (rate-limited) responses.  All HTTP and network errors are wrapped as
    YouTubeDataAPIError so callers can fall back to yt-dlp with a single except
    clause.

    :param http_session: aiohttp client session.
    :param endpoint: API endpoint path (e.g. "/search").
    :param params: Query parameters.
    """
    global _last_request_time  # noqa: PLW0603
    url = f"{YT_DATA_API_BASE}{endpoint}"

    for attempt in range(_MAX_RETRIES + 1):
        try:
            async with _get_semaphore():
                # Enforce minimum delay between requests
                now = asyncio.get_event_loop().time()
                wait = _MIN_REQUEST_INTERVAL - (now - _last_request_time)
                if wait > 0:
                    await asyncio.sleep(wait)
                _last_request_time = asyncio.get_event_loop().time()

                async with http_session.get(url, params=params) as response:
                    if response.status == 429:
                        retry_after = float(response.headers.get("Retry-After", 1))
                        if attempt < _MAX_RETRIES:
                            logger.warning(
                                "YouTube Data API rate-limited, retrying in %.1fs",
                                retry_after,
                            )
                            await asyncio.sleep(retry_after)
                            continue
                        raise YouTubeDataAPIError("API rate-limited after retries")
                    if response.status == 403:
                        data = await response.json()
                        error_reason = ""
                        for err in data.get("error", {}).get("errors", []):
                            error_reason = err.get("reason", "")
                        logger.warning("YouTube Data API quota/auth error: %s", error_reason)
                        raise YouTubeDataAPIError(f"API forbidden: {error_reason}")
                    if not response.ok:
                        raise YouTubeDataAPIError(
                            f"API request failed: {endpoint} returned {response.status}"
                        )
                    result: dict[str, Any] = await response.json()
                    return result
        except YouTubeDataAPIError:
            raise
        except Exception as err:
            raise YouTubeDataAPIError(f"API request error: {err}") from err

    raise YouTubeDataAPIError("API request failed after retries")
