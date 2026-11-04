"""Constants for the Deezer provider."""

from deezer_python_gql.generated.enums import RepeatModeInput
from music_assistant_models.enums import MediaType, RepeatMode

# -- Virtual playlist IDs --

FLOW_PLAYLIST_ID = "flow"
FLOW_CONFIG_PREFIX = "flow_config_"
DEFAULT_FLOW_CONFIG_ID = "default"
SMART_TRACKLIST_PREFIX = "smart_tracklist_"
RECOMMENDED_TRACKS_PLAYLIST_ID = "recommended_tracks"
TOP_CHARTS_PLAYLIST_ID = "top_charts"
USER_TOP_TRACKS_PLAYLIST_ID = "user_top_tracks"
SHAKER_PREFIX = "shaker_"
SHAKER_CURATED_PREFIX = "shaker_curated_"
PERSONAL_SONGS_PLAYLIST_ID = "personal_songs"
SHAKER_MIX_COVER = "https://cdn-assets.dzcdn.net/shaker/_next/static/media/group_mix.d986951b.svg"

# -- Personal item ID prefixes --

PERSONAL_ARTIST_PREFIX = "personal_artist_"
PERSONAL_ALBUM_PREFIX = "personal_album_"

# Bump when personal-item parsing changes to invalidate cached parsed items.
PERSONAL_METADATA_VERSION = "2"

# -- Pagination page sizes --

FAVORITES_PAGE_SIZE = 50
AUDIOBOOK_CHAPTERS_PAGE_SIZE = 200
FLOW_BATCH_COUNT = 2

# -- Browse folder names (used as path segments for routing) --

BROWSE_MADE_FOR_YOU = "Made For You"
BROWSE_EXPLORE = "Explore"
BROWSE_RECENTLY_PLAYED = "Recently Played"
BROWSE_SHAKER = "Shaker"
BROWSE_AUDIOBOOKS = "Discover Audiobooks"
BROWSE_MOODS = "Moods"
BROWSE_GENRES = "Genres"
BROWSE_YOUR_TOP_ARTISTS = "Your Top Artists"
BROWSE_YOUR_TOP_ALBUMS = "Your Top Albums"
BROWSE_RECOMMENDED_PLAYLISTS = "Recommended Playlists"
BROWSE_RECOMMENDED_ARTIST_PLAYLISTS = "Recommended Artist Playlists"
BROWSE_PERSONALIZED_PLAYLISTS = "Personalized Playlists"
BROWSE_TOP_ALBUMS = "Top Albums"
BROWSE_TOP_ARTISTS = "Top Artists"
BROWSE_TOP_PLAYLISTS = "Top Playlists"
BROWSE_ALL_FLOWS = "All Flows"

# -- Track decryption --

DECRYPT_KEY_LENGTH = 16
DECRYPT_KEY_ERROR = (
    "The Deezer decryption key is missing or invalid. "
    "Use an official Music Assistant build to play encrypted audio."
)

# -- Listen logging --

# Deezer lists an album, artist or playlist under recently played from the context of a listen
LISTEN_CONTEXT_TYPES = {
    MediaType.ALBUM: "album_page",
    MediaType.ARTIST: "artist_top",
    MediaType.PLAYLIST: "playlist_page",
}

# -- Queue sync --

CONF_SYNC_QUEUE = "sync_queue"
# tracks of the Music Assistant queue saved as the Deezer queue, from the current one on
QUEUE_SYNC_SIZE = 50
# the configuration the Deezer apps save with a plain queue
QUEUE_SYNC_CONFIGURATION = (
    '{"allowEdit":true,"allowShuffle":true,"allowedRepeatModes":["ONE","ALL"],'
    '"version":2,"queueType":"basic"}'
)
QUEUE_SYNC_REPEAT_MODES = {
    RepeatMode.OFF: RepeatModeInput.NONE,
    RepeatMode.ONE: RepeatModeInput.ONE,
    RepeatMode.ALL: RepeatModeInput.ALL,
}
