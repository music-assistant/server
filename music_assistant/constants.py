"""All constants for Music Assistant."""

import pathlib
from typing import Final

API_SCHEMA_VERSION: Final[int] = 23
MIN_SCHEMA_VERSION: Final[int] = 23
DB_SCHEMA_VERSION: Final[int] = 27

ROOT_LOGGER_NAME: Final[str] = "music_assistant"

UNKNOWN_ARTIST: Final[str] = "Unknown Artist"
VARIOUS_ARTISTS_NAME: Final[str] = "Various Artists"
VARIOUS_ARTISTS_ID_MBID: Final[str] = "89ad4ac3-39f7-470e-963a-56509c546377"


RESOURCES_DIR: Final[pathlib.Path] = (
    pathlib.Path(__file__).parent.resolve().joinpath("server/helpers/resources")
)

ANNOUNCE_ALERT_FILE: Final[str] = str(RESOURCES_DIR.joinpath("announce.mp3"))
SILENCE_FILE: Final[str] = str(RESOURCES_DIR.joinpath("silence.mp3"))

# if duration is None (e.g. radio stream):Final[str] = 48 hours
FALLBACK_DURATION: Final[int] = 172800

# config keys
CONF_SERVER_ID: Final[str] = "server_id"
CONF_IP_ADDRESS: Final[str] = "ip_address"
CONF_PORT: Final[str] = "port"
CONF_PROVIDERS: Final[str] = "providers"
CONF_PLAYERS: Final[str] = "players"
CONF_CORE: Final[str] = "core"
CONF_PATH: Final[str] = "path"
CONF_USERNAME: Final[str] = "username"
CONF_PASSWORD: Final[str] = "password"
CONF_VOLUME_NORMALIZATION: Final[str] = "volume_normalization"
CONF_VOLUME_NORMALIZATION_TARGET: Final[str] = "volume_normalization_target"
CONF_MAX_SAMPLE_RATE: Final[str] = "max_sample_rate"
CONF_EQ_BASS: Final[str] = "eq_bass"
CONF_EQ_MID: Final[str] = "eq_mid"
CONF_EQ_TREBLE: Final[str] = "eq_treble"
CONF_OUTPUT_CHANNELS: Final[str] = "output_channels"
CONF_FLOW_MODE: Final[str] = "flow_mode"
CONF_LOG_LEVEL: Final[str] = "log_level"
CONF_HIDE_GROUP_CHILDS: Final[str] = "hide_group_childs"
CONF_CROSSFADE_DURATION: Final[str] = "crossfade_duration"
CONF_BIND_IP: Final[str] = "bind_ip"
CONF_BIND_PORT: Final[str] = "bind_port"
CONF_PUBLISH_IP: Final[str] = "publish_ip"
CONF_AUTO_PLAY: Final[str] = "auto_play"
CONF_GROUP_PLAYERS: Final[str] = "group_players"
CONF_CROSSFADE: Final[str] = "crossfade"
CONF_GROUP_MEMBERS: Final[str] = "group_members"
CONF_HIDE_PLAYER: Final[str] = "hide_player"
CONF_ENFORCE_MP3: Final[str] = "enforce_mp3"
CONF_SYNC_ADJUST: Final[str] = "sync_adjust"

# config default values
DEFAULT_HOST: Final[str] = "0.0.0.0"
DEFAULT_PORT: Final[int] = 8095

# common db tables
DB_TABLE_TRACK_LOUDNESS: Final[str] = "track_loudness"
DB_TABLE_PLAYLOG: Final[str] = "playlog"
DB_TABLE_ARTISTS: Final[str] = "artists"
DB_TABLE_ALBUMS: Final[str] = "albums"
DB_TABLE_TRACKS: Final[str] = "tracks"
DB_TABLE_ALBUM_TRACKS: Final[str] = "albumtracks"
DB_TABLE_PLAYLISTS: Final[str] = "playlists"
DB_TABLE_RADIOS: Final[str] = "radios"
DB_TABLE_CACHE: Final[str] = "cache"
DB_TABLE_SETTINGS: Final[str] = "settings"
DB_TABLE_THUMBS: Final[str] = "thumbnails"
DB_TABLE_PROVIDER_MAPPINGS: Final[str] = "provider_mappings"

# all other
MASS_LOGO_ONLINE: Final[
    str
] = "https://github.com/home-assistant/brands/raw/master/custom_integrations/mass/icon%402x.png"
ENCRYPT_SUFFIX = "_encrypted_"
SECURE_STRING_SUBSTITUTE = "this_value_is_encrypted"
CONFIGURABLE_CORE_CONTROLLERS = (
    "streams",
    "webserver",
    "players",
    "metadata",
    "cache",
    "music",
    "player_queues",
)
SYNCGROUP_PREFIX: Final[str] = "syncgroup_"
