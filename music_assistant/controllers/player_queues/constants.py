"""Constants for the player queues controller."""

from __future__ import annotations

from music_assistant_models.enums import MediaType

CONF_DEFAULT_ENQUEUE_SELECT_ARTIST = "default_enqueue_select_artist"
CONF_DEFAULT_ENQUEUE_SELECT_ALBUM = "default_enqueue_select_album"

ENQUEUE_SELECT_ARTIST_DEFAULT_VALUE = "all_tracks"
ENQUEUE_SELECT_ALBUM_DEFAULT_VALUE = "all_tracks"

CONF_DEFAULT_ENQUEUE_OPTION_ARTIST = "default_enqueue_option_artist"
CONF_DEFAULT_ENQUEUE_OPTION_ALBUM = "default_enqueue_option_album"
CONF_DEFAULT_ENQUEUE_OPTION_TRACK = "default_enqueue_option_track"
CONF_DEFAULT_ENQUEUE_OPTION_GENRE = "default_enqueue_option_genre"
CONF_DEFAULT_ENQUEUE_OPTION_LIVE_SOURCES = "default_enqueue_option_live_sources"
CONF_DEFAULT_ENQUEUE_OPTION_PLAYLIST = "default_enqueue_option_playlist"
CONF_DEFAULT_ENQUEUE_OPTION_AUDIOBOOK = "default_enqueue_option_audiobook"
CONF_DEFAULT_ENQUEUE_OPTION_PODCAST = "default_enqueue_option_podcast"
CONF_DEFAULT_ENQUEUE_OPTION_SOUND_EFFECT = "default_enqueue_option_sound_effect"
CONF_DEFAULT_ENQUEUE_OPTION_PODCAST_EPISODE = "default_enqueue_option_podcast_episode"
CONF_DEFAULT_ENQUEUE_OPTION_FOLDER = "default_enqueue_option_folder"
CONF_DEFAULT_ENQUEUE_OPTION_COLLECTION = "default_enqueue_option_collection"
CONF_DEFAULT_ENQUEUE_OPTION_UNKNOWN = "default_enqueue_option_unknown"

CONF_AUTOPLAY_LABEL = "autoplay_label"
CONF_AUTOPLAY_MODE = "autoplay_mode"
CONF_AUTOPLAY_PLAYLIST = "autoplay_playlist"
CONF_CROSSFADE_LABEL = "crossfade_label"

CONF_SMART_SHUFFLE_LABEL = "smart_shuffle_label"
CONF_SMART_SHUFFLE_ENABLED = "smart_shuffle_enabled"
CONF_SMART_SHUFFLE_SONG_RECENCY = "smart_shuffle_song_recency"
CONF_SMART_SHUFFLE_ARTIST_RECENCY = "smart_shuffle_artist_recency"
CONF_SMART_SHUFFLE_DUPLICATE_GAP = "smart_shuffle_duplicate_gap"

# window preset values are stored in seconds (0 = off)
SMART_SHUFFLE_SONG_RECENCY_DEFAULT = 7 * 24 * 3600
SMART_SHUFFLE_ARTIST_RECENCY_DEFAULT = 30 * 60
SMART_SHUFFLE_DUPLICATE_GAP_DEFAULT = 3 * 3600

# preset window values in seconds for the smart-shuffle recency selects (0 = off);
# the per-option titles live in strings.json (config_entries.<key>.options.<seconds>)
SMART_SHUFFLE_SONG_RECENCY_OPTIONS = (
    0,
    3600,
    14400,
    43200,
    86400,
    259200,
    604800,
    1209600,
    2419200,
)
SMART_SHUFFLE_ARTIST_RECENCY_OPTIONS = (0, 300, 900, 1800, 3600, 7200)
SMART_SHUFFLE_DUPLICATE_GAP_OPTIONS = (0, 3600, 7200, 10800, 14400, 21600, 28800, 43200, 86400)

# Managed-pool sizing: a queue with dynamic sources is kept as a small bounded pool, topped up
# near the end instead of enqueuing thousands of tracks.
MANAGED_POOL_TARGET = 25
MANAGED_POOL_MAX = 50
# A finite (TRACKS) source is materialized into its own deque and dequeued progressively. This caps
# how many of its tracks are held in memory at once; a larger source pages the remainder in as the
# deque drains, so a huge playlist or a bulk manual enqueue can't balloon internal state.
MANAGED_POOL_SOURCE_CAP = 250

CACHE_CATEGORY_PLAYER_QUEUE_STATE = 0
CACHE_CATEGORY_PLAYER_QUEUE_ITEMS = 1

# A play command is only complete once the player actually reports playback: several player
# implementations return from play_media as soon as the start command is out while the device
# needs another moment to connect and start (AirPlay anchors its audible start in the future).
# The play action - and with it the UI's in-progress indication - is held until the player
# reports PLAYING, capped by this timeout so a player that never reports state can't block.
PLAYBACK_START_TIMEOUT = 5.0

# The queue's cached state/items are written through a debounced timer, so a burst of updates (or
# the per-track updates during playback) collapses into a single write instead of hitting the cache
# db on every signal_update.
QUEUE_CACHE_SAVE_DELAY = 5
# Bumped when the on-disk queue cache layout changes in a way older code can't read; a version
# mismatch makes the restore fall back to a clean queue instead of trusting incompatible data.
CACHE_FORMAT_VERSION = 1

# Media types that may reach us without a duration, so one determined while streaming is applied.
# Radio and audio sources would wrongly become seekable. Tracks always carry a duration and a
# rounded value would affect crossfade and gapless timing.
PROBED_DURATION_MEDIA_TYPES = (
    MediaType.PODCAST_EPISODE,
    MediaType.AUDIOBOOK,
)
