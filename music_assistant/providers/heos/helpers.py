"""Helpers for HEOS Player Provider."""

from urllib.parse import urlencode

from pyheos import HeosNowPlayingMedia
from pyheos.util.mediauri import BASE_URI


def media_uri_from_now_playing_media(now_playing_media: HeosNowPlayingMedia) -> str:
    """Generate a media URI based on available data in now playing media."""
    base_uri = f"{BASE_URI}/{now_playing_media.source_id}/{now_playing_media.type}"

    params: dict[str, str] = {}

    if now_playing_media.song:
        params["song"] = now_playing_media.song
    if now_playing_media.station:
        params["station"] = now_playing_media.station
    if now_playing_media.album:
        params["album"] = now_playing_media.album
    if now_playing_media.artist:
        params["artist"] = now_playing_media.artist
    if now_playing_media.image_url:
        params["image_url"] = now_playing_media.image_url
    if now_playing_media.album_id:
        params["album_id"] = now_playing_media.album_id
    if now_playing_media.media_id:
        params["media_id"] = now_playing_media.media_id
    if now_playing_media.queue_id:
        params["queue_id"] = str(now_playing_media.queue_id)

    return f"{base_uri}?{urlencode(params)}"
