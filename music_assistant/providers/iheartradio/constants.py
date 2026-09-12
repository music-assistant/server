"""Constants for the iHeartRadio provider."""

from __future__ import annotations

from typing import Final

import aiohttp

# -- Config keys --

CONF_COUNTRY: Final[str] = "country"
# Raw config keys holding the persisted session; never shown in the UI.
CONF_PROFILE_ID: Final[str] = "profile_id"
CONF_SESSION_ID: Final[str] = "session_id"
CONF_SESSION_USERNAME: Final[str] = "session_username"

# -- Regions --

# The hostname shard decides which catalogue is served: a station only resolves on the
# shard of the country it broadcasts in.
DEFAULT_COUNTRY: Final[str] = "us"
API_BASE_URLS: Final[dict[str, str]] = {
    "us": "https://api.iheart.com",
    "ca": "https://ca.api.iheart.com",
    "au": "https://au.api.iheart.com",
    "nz": "https://nz.api.iheart.com",
    "mx": "https://mx.api.iheart.com",
}

# The API validates this header against its own client list and answers 400 for a value
# it does not know, so the country must match the shard being addressed.
HEADER_HOST_NAME: Final[str] = "X-hostName"
HEADER_LOCALE: Final[str] = "X-Locale"
API_LOCALE: Final[str] = "en-US"
# Per-request timeout for API calls.
API_TIMEOUT: Final[aiohttp.ClientTimeout] = aiohttp.ClientTimeout(total=20)

# -- Session --

# Each API generation reads the session from its own header pair, so all four go on every
# request once a session exists.
HEADER_PROFILE_ID: Final[str] = "X-IHR-Profile-ID"
HEADER_SESSION_ID: Final[str] = "X-IHR-Session-ID"
HEADER_USER_ID: Final[str] = "X-User-Id"
HEADER_SESSION_ID_V2: Final[str] = "X-Session-Id"
DEVICE_NAME: Final[str] = "web-desktop"
# The v1/v2 endpoints answer a dead session with a 400 carrying one of these codes.
SESSION_EXPIRED_CODES: Final[frozenset[int]] = frozenset({2, 101})

# -- Endpoints --

PATH_LOGIN: Final[str] = "/api/v1/account/login"
PATH_GUEST_LOGIN: Final[str] = "/api/v1/account/loginOrCreateOauthUser"
PATH_SESSION: Final[str] = "/api/v3/session/sessions"
PATH_FOLLOWS_LIVE: Final[str] = "/api/v3/profiles/follows/live"
PATH_FOLLOWS_LIVE_ITEM: Final[str] = "/api/v3/profiles/follows/live/{station_id}"
PATH_FOLLOWS_ARTIST: Final[str] = "/api/v3/profiles/follows/artist"
PATH_FOLLOWS_ARTIST_ITEM: Final[str] = "/api/v3/profiles/follows/artist/{artist_id}"
PATH_PODCAST_FOLLOWS: Final[str] = "/api/v3/podcast/follows"
PATH_PODCAST_FOLLOW_ITEM: Final[str] = "/api/v3/podcast/follows/{podcast_id}"
PATH_ARTIST_STATION: Final[str] = "/api/v2/playlists/{profile_id}/ARTIST/{artist_id}"
PATH_PLAYBACK_STREAMS: Final[str] = "/api/v2/playback/streams"
PATH_PLAYBACK_REPORTING: Final[str] = "/api/v3/playback/reporting"
PATH_ARTIST_PROFILE: Final[str] = "/api/v3/artists/profiles/{artist_id}"
PATH_CATALOG_TRACK: Final[str] = "/api/v3/catalog/tracks/{track_id}"
PATH_CATALOG_ALBUM: Final[str] = "/api/v3/catalog/album/{album_id}"

PATH_LIVE_STATIONS: Final[str] = "/api/v2/content/liveStations"
PATH_LIVE_STATION: Final[str] = "/api/v2/content/liveStations/{station_id}"
PATH_MARKETS: Final[str] = "/api/v2/content/markets"
PATH_GENRES: Final[str] = "/api/v3/catalog/genres"
PATH_NOW_PLAYING: Final[str] = "/api/v3/live-meta/stream/{station_id}/currentTrackMeta"
PATH_PODCAST_CATEGORIES: Final[str] = "/api/v3/podcast/categories"
PATH_PODCAST_CATEGORY: Final[str] = "/api/v3/podcast/categories/{category_id}"
PATH_PODCAST: Final[str] = "/api/v3/podcast/podcasts/{podcast_id}"
PATH_PODCAST_EPISODES: Final[str] = "/api/v3/podcast/podcasts/{podcast_id}/episodes"
PATH_PODCAST_EPISODE: Final[str] = "/api/v3/podcast/episodes/{episode_id}"
PATH_SEARCH: Final[str] = "/api/v3/search/all"

# -- Streaming --

# Preference order for the station's stream variants: HLS is what iHeart's own web player
# uses, Shoutcast is the plain fallback every station has, and a .pls playlist (US only)
# is unwrapped by the streams controller.
STREAM_PREFERENCE: Final[tuple[str, ...]] = (
    "secure_hls_stream",
    "secure_shoutcast_stream",
    "secure_pls_stream",
    "hls_stream",
    "shoutcast_stream",
    "pls_stream",
)

# How often MA asks us for fresh now-playing data while a station is playing.
STREAM_METADATA_UPDATE_INTERVAL: Final[int] = 15

# -- Item ids --

# A podcast episode is looked up on its own endpoint, but a PodcastEpisode must name its
# parent podcast, so the episode's MA id carries both.
ID_SEPARATOR: Final[str] = ":"
# An artist radio shares the Radio media type with live stations, whose ids are plain
# numbers, so its id names the seed artist behind a prefix.
ARTIST_RADIO_PREFIX: Final[str] = "artist:"
ARTIST_IMAGE_URL: Final[str] = "https://i.iheart.com/v3/catalog/artist/{artist_id}"

# -- Artist radio --

# Analytics context the API expects on station and playback calls; the website sends
# this value from an artist page and the API accepts any of its known values.
PLAYED_FROM: Final[int] = 66
STATION_TYPE_RADIO: Final[str] = "RADIO"
# postStreams answers with this error code when a station has run out of songs.
STATION_OUT_OF_SONGS_CODE: Final[int] = 617
REPORT_STATUS_START: Final[str] = "START"
REPORT_STATUS_DONE: Final[str] = "DONE"
REPORT_STATUS_SKIP: Final[str] = "SKIP"
# How long a batch's track urls are served for. The urls carry no expiry, so this is a
# conservative window; a track from an older batch is refused rather than handed to ffmpeg.
BATCH_URL_TTL: Final[int] = 1800
# Batches retained per station so queued and recently played tracks stay resolvable; the
# queue keeps about 25 tracks ahead and a batch holds 3.
MAX_RETAINED_BATCHES: Final[int] = 12
# Stations holding batches at once; the least recently played one is dropped past this.
MAX_ACTIVE_STATIONS: Final[int] = 10

# -- Paging --

# The largest page the API serves; a country's full market list fits in one.
STATION_PAGE_LIMIT: Final[int] = 500
MARKET_PAGE_LIMIT: Final[int] = 500
EPISODE_PAGE_LIMIT: Final[int] = 100
# The follow lists cap their page size at 25.
FOLLOWS_PAGE_LIMIT: Final[int] = 25
PODCAST_FOLLOWS_PAGE_LIMIT: Final[int] = 20
# Podcasts with a decade of episodes exist; cap the walk so listing one cannot fire an
# unbounded number of requests. The newest episodes are listed first, so a capped listing
# drops the oldest.
MAX_EPISODE_PAGES: Final[int] = 5

# -- Caching --

CACHE_CATEGORY_STATIONS: Final[int] = 0
CACHE_CATEGORY_PODCASTS: Final[int] = 1
CACHE_CATEGORY_CATALOG: Final[int] = 2
CACHE_CATEGORY_SEARCH: Final[int] = 3

CACHE_TTL_STATION: Final[int] = 3600 * 6
# Markets, genres and podcast categories change a few times a year at most.
CACHE_TTL_CATALOG: Final[int] = 3600 * 24 * 7
CACHE_TTL_PODCAST: Final[int] = 3600 * 24
CACHE_TTL_EPISODES: Final[int] = 3600
CACHE_TTL_SEARCH: Final[int] = 3600

# -- Browse paths --

BROWSE_LIVE: Final[str] = "live"
BROWSE_MARKETS: Final[str] = "markets"
BROWSE_GENRES: Final[str] = "genres"
BROWSE_PODCASTS: Final[str] = "podcasts"
