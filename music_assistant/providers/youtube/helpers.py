"""Synchronous yt-dlp wrapper functions for the YouTube provider.

All functions in this module are blocking and intended to be called via
asyncio.to_thread() from the async provider layer.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote_plus

from music_assistant_models.errors import UnplayableMediaError

from .constants import LIVE_STATUSES, YT_DOMAIN


def is_live_entry(entry: dict[str, Any]) -> bool:
    """Return True if the yt-dlp entry represents an active live stream.

    :param entry: Raw yt-dlp info dict.
    """
    if entry.get("is_live"):
        return True
    return entry.get("live_status", "") in LIVE_STATUSES


def search_yt(
    yt_dlp: Any, ydl_opts: dict[str, Any], query: str, limit: int
) -> list[dict[str, Any]]:
    """Run a YouTube search and return flat video entries (no stream extraction).

    :param yt_dlp: The imported yt_dlp module.
    :param ydl_opts: Base yt-dlp options dict.
    :param query: Search query string.
    :param limit: Maximum number of results to return.
    """
    opts = {**ydl_opts, "extract_flat": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    return info.get("entries", []) if info else []


def extract_video_info(
    yt_dlp: Any, ydl_opts: dict[str, Any], video_id: str
) -> dict[str, Any] | None:
    """Extract full video metadata for a given video id.

    :param yt_dlp: The imported yt_dlp module.
    :param ydl_opts: Base yt-dlp options dict.
    :param video_id: YouTube video ID.
    """
    # Use permissive format to avoid "Requested format is not available" errors —
    # we only need metadata here, not a specific stream format.
    opts = {**ydl_opts, "skip_download": True, "format": "bestaudio/best"}
    with yt_dlp.YoutubeDL(opts) as ydl:
        result: dict[str, Any] | None = ydl.extract_info(
            f"{YT_DOMAIN}/watch?v={video_id}", download=False
        )
    return result


def search_channels(
    yt_dlp: Any, ydl_opts: dict[str, Any], query: str, limit: int
) -> list[dict[str, Any]]:
    """Run a YouTube search filtered to channels and return flat entries.

    :param yt_dlp: The imported yt_dlp module.
    :param ydl_opts: Base yt-dlp options dict.
    :param query: Search query string.
    :param limit: Maximum number of results to return.
    """
    opts = {**ydl_opts, "extract_flat": True, "skip_download": True, "playlistend": limit}
    url = f"{YT_DOMAIN}/results?search_query={quote_plus(query)}&sp=EgIQAg%3D%3D"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        return []
    return [e for e in info.get("entries", []) if e][:limit]


def search_playlists(
    yt_dlp: Any, ydl_opts: dict[str, Any], query: str, limit: int
) -> list[dict[str, Any]]:
    """Run a YouTube search filtered to playlists and return flat entries.

    :param yt_dlp: The imported yt_dlp module.
    :param ydl_opts: Base yt-dlp options dict.
    :param query: Search query string.
    :param limit: Maximum number of results to return.
    """
    opts = {**ydl_opts, "extract_flat": True, "skip_download": True, "playlistend": limit}
    url = f"{YT_DOMAIN}/results?search_query={quote_plus(query)}&sp=EgIQAw%3D%3D"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        return []
    return [e for e in info.get("entries", []) if e][:limit]


def extract_channel_videos(
    yt_dlp: Any, ydl_opts: dict[str, Any], channel_id: str, limit: int
) -> list[dict[str, Any]]:
    """Extract flat video entries from a channel's videos tab.

    :param yt_dlp: The imported yt_dlp module.
    :param ydl_opts: Base yt-dlp options dict.
    :param channel_id: YouTube channel ID.
    :param limit: Maximum number of video entries to return.
    """
    opts = {
        **ydl_opts,
        "extract_flat": True,
        "skip_download": True,
        "playlistend": limit,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"{YT_DOMAIN}/channel/{channel_id}/videos", download=False)
    if not info:
        return []
    return [e for e in info.get("entries", []) if e][:limit]


def extract_channel_info(
    yt_dlp: Any, ydl_opts: dict[str, Any], channel_id: str
) -> dict[str, Any] | None:
    """Extract channel metadata for a given channel id.

    :param yt_dlp: The imported yt_dlp module.
    :param ydl_opts: Base yt-dlp options dict.
    :param channel_id: YouTube channel ID.
    """
    opts = {
        **ydl_opts,
        "extract_flat": True,
        # playlistend=0 fetches channel metadata only, skipping all video entries
        "playlistend": 0,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        result: dict[str, Any] | None = ydl.extract_info(
            f"{YT_DOMAIN}/channel/{channel_id}", download=False
        )
    return result


def extract_channel_playlists(
    yt_dlp: Any, ydl_opts: dict[str, Any], channel_id: str, limit: int
) -> list[dict[str, Any]]:
    """Extract flat playlist entries from a channel's playlists tab.

    :param yt_dlp: The imported yt_dlp module.
    :param ydl_opts: Base yt-dlp options dict.
    :param channel_id: YouTube channel ID.
    :param limit: Maximum number of playlist entries to return.
    """
    opts = {
        **ydl_opts,
        "extract_flat": True,
        "skip_download": True,
        "playlistend": limit,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"{YT_DOMAIN}/channel/{channel_id}/playlists", download=False)
    if not info:
        return []
    return [e for e in info.get("entries", []) if e][:limit]


def extract_playlist_info(
    yt_dlp: Any, ydl_opts: dict[str, Any], playlist_id: str
) -> dict[str, Any] | None:
    """Extract playlist metadata (without video entries).

    :param yt_dlp: The imported yt_dlp module.
    :param ydl_opts: Base yt-dlp options dict.
    :param playlist_id: YouTube playlist ID.
    """
    opts = {
        **ydl_opts,
        "extract_flat": True,
        "playlistend": 0,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        result: dict[str, Any] | None = ydl.extract_info(
            f"{YT_DOMAIN}/playlist?list={playlist_id}", download=False
        )
    return result


def extract_playlist_videos(
    yt_dlp: Any, ydl_opts: dict[str, Any], playlist_id: str, limit: int
) -> list[dict[str, Any]]:
    """Extract flat video entries from a YouTube playlist.

    :param yt_dlp: The imported yt_dlp module.
    :param ydl_opts: Base yt-dlp options dict.
    :param playlist_id: YouTube playlist ID.
    :param limit: Maximum number of video entries to return.
    """
    opts = {
        **ydl_opts,
        "extract_flat": True,
        "skip_download": True,
        "playlistend": limit,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"{YT_DOMAIN}/playlist?list={playlist_id}", download=False)
    if not info:
        return []
    return [e for e in info.get("entries", []) if e][:limit]


def extract_stream_or_live(yt_dlp: Any, ydl_opts: dict[str, Any], video_id: str) -> dict[str, Any]:
    """Extract stream info in a single yt-dlp call, handling both VOD and live.

    Returns a dict with ``is_live`` set to True or False.
    For VOD, the dict also contains the selected audio stream format fields.
    For live streams, the dict contains ``manifest_url``, ``title``, and ``uploader``.

    :param yt_dlp: The imported yt_dlp module.
    :param ydl_opts: Base yt-dlp options dict.
    :param video_id: YouTube video ID.
    """
    opts = {**ydl_opts}
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(f"{YT_DOMAIN}/watch?v={video_id}", download=False)
        except yt_dlp.utils.DownloadError as err:
            raise UnplayableMediaError(str(err)) from err
        if not info:
            raise UnplayableMediaError(f"No stream info found for {video_id}")

        if is_live_entry(info):
            return _extract_hls_manifest(info, video_id)

        # Try m4a first, then any audio-only format, then best muxed format.
        # Some restricted videos only serve muxed formats via the web client.
        format_selector = ydl.build_format_selector("m4a/bestaudio/best")
        stream_format = next(format_selector({"formats": info["formats"]}), None)
        if not stream_format:
            raise UnplayableMediaError(f"No audio stream found for {video_id}")
        result: dict[str, Any] = {**stream_format, "is_live": False}
        return result


def _extract_hls_manifest(info: dict[str, Any], video_id: str) -> dict[str, Any]:
    """Extract HLS manifest URL from a live stream info dict.

    :param info: Raw yt-dlp info dict for a live stream.
    :param video_id: YouTube video ID (for error messages).
    """
    manifest_url = info.get("manifest_url")
    if not manifest_url:
        for fmt in info.get("formats") or []:
            if fmt.get("protocol") in ("m3u8", "m3u8_native") and fmt.get("manifest_url"):
                manifest_url = fmt["manifest_url"]
                break
    if not manifest_url:
        for fmt in info.get("formats") or []:
            if fmt.get("protocol") in ("m3u8", "m3u8_native") and fmt.get("url"):
                manifest_url = fmt["url"]
                break
    if not manifest_url:
        raise UnplayableMediaError(f"No HLS manifest found for live stream {video_id}")
    return {
        "is_live": True,
        "manifest_url": manifest_url,
        "title": info.get("title"),
        "uploader": info.get("uploader") or info.get("channel"),
    }
