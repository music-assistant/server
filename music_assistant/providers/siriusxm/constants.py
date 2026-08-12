"""Constants for the SiriusXM provider."""

# -- Config keys --
# Kept as-is from the previous implementation so existing setups keep working.

CONF_SXM_USERNAME = "sxm_email_address"
CONF_SXM_PASSWORD = "sxm_password"
CONF_SXM_REGION = "sxm_region"
CONF_BITRATE = "bitrate"

# -- Streaming --

# Bitrates SiriusXM offers. Not every channel carries every one, so the
# requested bitrate is matched against what the channel actually reports.
BITRATE_256 = "256k"
BITRATE_96 = "96k"
BITRATE_64 = "64k"
BITRATE_32 = "32k"
BITRATES = (BITRATE_256, BITRATE_96, BITRATE_64, BITRATE_32)

# The local proxy binds to loopback: the SiriusXM CDN needs an Authorization
# header that ffmpeg won't send, so playback goes through the proxy instead.
PROXY_BIND_IP = "127.0.0.1"

# Signed CDN tokens are short-lived, so streamdetails must not outlive them.
STREAM_EXPIRATION = 300

# How often MA polls us for fresh now-playing data during playback.
STREAM_METADATA_UPDATE_INTERVAL = 15

# -- Caching --

# The channel catalog costs ~24 sequential requests to walk, and changes
# rarely; aiosxm caches it internally, this bounds our own lookups too.
CACHE_TTL_CHANNELS = 3600 * 6

# -- Item ids --

# Entity types that return a queue of discrete tracks rather than a broadcast.
# These surface as dynamic playlists; only linear channels are true radio.
TRACK_QUEUE_TYPES = frozenset({"artist-station", "channel-xtra"})

# A SiriusXM track only exists inside its parent's tune response — it cannot be
# tuned on its own — so a track's MA id has to carry the parent that owns it.
ID_SEPARATOR = "|"

# -- Caching --

# A track's CDN url is signed for an hour; drop cached tracks before then so a
# stale url is never handed to a player.
CACHE_TTL_TRACKS = 3000

# How many tracks to hand MA per request, and the point at which the buffer is
# topped up from SiriusXM. Its queue cursor only moves when we pull, so holding
# a little ahead lets a listener see what is actually coming next.
QUEUE_BATCH_SIZE = 3
QUEUE_LOW_WATER_MARK = 3

# -- Browse paths --

BROWSE_LIBRARY = "library"
BROWSE_CHANNELS = "channels"
BROWSE_XTRA = "xtra"
BROWSE_GENRES = "genres"
BROWSE_STATIONS = "stations"
