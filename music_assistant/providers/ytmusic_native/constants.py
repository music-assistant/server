"""Constants for the native YouTube Music provider."""

from __future__ import annotations

from typing import Final

DOMAIN: Final[str] = "https://music.youtube.com"
API_URL: Final[str] = f"{DOMAIN}/youtubei/v1/"

# Requests replay the web client, so they identify as the browser that holds the session.
USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)
WEB_REMIX_CLIENT_NAME: Final[str] = "WEB_REMIX"
WEB_REMIX_CLIENT_ID: Final[str] = "67"

CONF_COOKIE: Final[str] = "cookie"
CONF_HIGH_QUALITY: Final[str] = "high_quality"
CONF_INCLUDE_VIDEOS: Final[str] = "include_videos_in_search"
CONF_REPORT_PLAYS: Final[str] = "report_plays"

# Re-read the home page config this often, so a new client version or player is used.
BOOTSTRAP_TTL: Final[int] = 30 * 60
# Listened seconds after which a play is reported to the YouTube Music history.
REPORT_PLAY_AFTER: Final[int] = 30
DEFAULT_STREAM_URL_EXPIRATION: Final[int] = 3600

# AAC 256k and Opus 256k, only served to Premium accounts.
PREMIUM_ITAGS: Final[tuple[int, ...]] = (141, 774)
# Premium is required. The existing YouTube Music provider checks it the same way: only
# a Premium account is served itag 141 for this video.
PREMIUM_CHECK_VIDEO_ID: Final[str] = "dQw4w9WgXcQ"
# While playing, check again when no Premium format was seen for this long.
PREMIUM_RECHECK_INTERVAL: Final[int] = 24 * 3600

VARIOUS_ARTISTS_YTM_ID: Final[str] = "UCUTXlgdcKU5vfzFqHOWIvkA"
# Joins a podcast id and an episode video id into one episode item id.
PODCAST_EPISODE_SPLITTER: Final[str] = "|"
# Personal playlists (Liked Music, the mixes) share their id across accounts, so
# their item id gets this delimiter plus the instance id appended.
PLAYLIST_ID_DELIMITER: Final[str] = "🎵"
PERSONAL_PLAYLIST_IDS: Final[tuple[str, ...]] = ("LM", "SE", "RDPN")
PERSONAL_PLAYLIST_PREFIXES: Final[tuple[str, ...]] = ("RDTMAK5uy_",)
# Mix playlists can be endless; cap how many tracks we read from one.
DYNAMIC_PLAYLIST_TRACK_LIMIT: Final[int] = 300
# Only guards against a continuation loop: pages hold 25 to 100 items, so real
# libraries end long before this. Reaching it is logged as a warning.
MAX_CONTINUATIONS: Final[int] = 2000

BROWSE_HOME: Final[str] = "FEmusic_home"
BROWSE_LIBRARY_TRACKS: Final[str] = "FEmusic_liked_videos"
BROWSE_LIBRARY_ALBUMS: Final[str] = "FEmusic_liked_albums"
BROWSE_LIBRARY_PLAYLISTS: Final[str] = "FEmusic_liked_playlists"
BROWSE_LIBRARY_PODCASTS: Final[str] = "FEmusic_library_non_music_audio_list"
# Artists of the songs in the library, and the subscribed artists.
BROWSE_LIBRARY_TRACK_ARTISTS: Final[str] = "FEmusic_library_corpus_track_artists"
BROWSE_LIBRARY_SUBSCRIPTIONS: Final[str] = "FEmusic_library_corpus_artists"

# Search filter params, as the web client's filter chips send them.
SEARCH_PARAMS: Final[dict[str, str]] = {
    "songs": "EgWKAQIIAWoSEAUQCRADEAQQDhAKEBAQFRAR",
    "videos": "EgWKAQIQAWoSEAUQCRADEAQQDhAKEBAQFRAR",
    "albums": "EgWKAQIYAWoSEAUQCRADEAQQDhAKEBAQFRAR",
    "artists": "EgWKAQIgAWoSEAUQCRADEAQQDhAKEBAQFRAR",
    "featured_playlists": "EgeKAQQoADgBahIQBRAJEAMQBBAOEAoQEBAVEBE=",
    "community_playlists": "EgeKAQQoAEABahIQBRAJEAMQBBAOEAoQEBAVEBE=",
    "podcasts": "EgWKAQJQAWoSEAUQCRADEAQQDhAKEBAQFRAR",
}
