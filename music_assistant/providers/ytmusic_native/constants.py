"""Constants for the native YouTube Music provider."""

from __future__ import annotations

from typing import Final

DOMAIN: Final[str] = "https://music.youtube.com"
BASE_URL: Final[str] = f"{DOMAIN}/youtubei/v1/"

# A real desktop Chrome UA. The metadata path replays the web client (WEB_REMIX),
# so the UA must look like the browser that captured the session.
USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)

# WEB_REMIX (the music.youtube.com web client) and ANDROID_VR (the audio
# fallback that bypasses the bot-wall with a blessed visitorData and no cookies).
WEB_REMIX_CLIENT_NAME: Final[str] = "WEB_REMIX"
WEB_REMIX_CLIENT_ID: Final[str] = "67"
ANDROID_VR_CLIENT_NAME: Final[str] = "ANDROID_VR"
ANDROID_VR_CLIENT_VERSION: Final[str] = "1.62.27"
ANDROID_VR_CLIENT_ID: Final[str] = "28"
ANDROID_VR_OS_VERSION: Final[str] = "12L"
ANDROID_VR_USER_AGENT: Final[str] = (
    "com.google.android.apps.youtube.vr.oculus/1.62.27 "
    "(Linux; U; Android 12L; eureka-user Build/SQ3A.220605.009.A1) gzip"
)

CONF_COOKIE: Final[str] = "cookie"
CONF_VISITOR_DATA: Final[str] = "visitor_data"

VARIOUS_ARTISTS_YTM_ID: Final[str] = "UCUTXlgdcKU5vfzFqHOWIvkA"

# A short, always-available track used to probe whether the account can stream
# premium (itag 141) formats.
PREMIUM_CHECK_VIDEO_ID: Final[str] = "dQw4w9WgXcQ"
# AAC 256k and Opus 256k. itag 141 is what we verify for "has premium".
PREMIUM_ITAGS: Final[tuple[int, ...]] = (141, 774)

DEFAULT_STREAM_URL_EXPIRATION: Final[int] = 3600  # 1 hour
BASE_JS_CACHE_TTL: Final[int] = 6 * 3600  # 6 hours

# Library browse ids.
BROWSE_LIBRARY_TRACKS: Final[str] = "FEmusic_liked_videos"
BROWSE_LIBRARY_ALBUMS: Final[str] = "FEmusic_liked_albums"
BROWSE_LIBRARY_ARTISTS: Final[str] = "FEmusic_library_corpus_track_artists"
BROWSE_LIBRARY_PLAYLISTS: Final[str] = "FEmusic_liked_playlists"
BROWSE_HOME: Final[str] = "FEmusic_home"
LIKED_SONGS_PLAYLIST_ID: Final[str] = "LM"

# Search filter params (the `params` field), probed live (reverseengeneer.md §6).
SEARCH_FILTER_PARAMS: Final[dict[str, str]] = {
    "songs": "EgWKAQIIAWoKEAkQBRAKEAMQBA==",
    "videos": "EgWKAQIQAWoKEAkQChAFEAMQBA==",
    "albums": "EgWKAQIYAWoKEAkQChAFEAMQBA==",
    "artists": "EgWKAQIgAWoKEAkQChAFEAMQBA==",
    "playlists": "EgWKAQIoAWoKEAkQChAFEAMQBA==",
}
