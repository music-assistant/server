"""All constants for Music Assistant."""

import pathlib

__version__ = "2.0.0"

SCHEMA_VERSION = 19

ROOT_LOGGER_NAME = "music_assistant"

UNKNOWN_ARTIST = "Unknown Artist"
VARIOUS_ARTISTS = "Various Artists"
VARIOUS_ARTISTS_ID = "89ad4ac3-39f7-470e-963a-56509c546377"


RESOURCES_DIR = pathlib.Path(__file__).parent.resolve().joinpath("helpers/resources")

ANNOUNCE_ALERT_FILE = str(RESOURCES_DIR.joinpath("announce.mp3"))
SILENCE_FILE = str(RESOURCES_DIR.joinpath("silence.mp3"))

# if duration is None (e.g. radio stream) = 48 hours
FALLBACK_DURATION = 172800

# Name of the environment-variable to override base_url
BASE_URL_OVERRIDE_ENVNAME = "MASS_BASE_URL"


# config keys
CONF_SERVER_ID = "server_id"
CONF_WEB_IP = "webserver.ip"
CONF_WEB_PORT = "webserver.port"
CONF_DB_LIBRARY = "database.library"
CONF_DB_CACHE = "database.cache"
CONF_PROVIDERS = "providers"
CONF_PLAYERS = "players"
CONF_PATH = "path"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_VOLUME_NORMALISATION = "volume_normalisation"
CONF_VOLUME_NORMALISATION_TARGET = "volume_normalisation_target"
CONF_MAX_SAMPLE_RATE = "max_sample_rate"
CONF_EQ_BASS = "eq_bass"
CONF_EQ_MID = "eq_mid"
CONF_EQ_TREBLE = "eq_treble"
CONF_OUTPUT_CHANNELS = "output_channels"

# config default values
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8095
DEFAULT_DB_LIBRARY = "sqlite:///[storage_path]/library.db"
DEFAULT_DB_CACHE = "sqlite:///[storage_path]/cache.db"

# common db tables
DB_TABLE_TRACK_LOUDNESS = "track_loudness"
DB_TABLE_PLAYLOG = "playlog"
DB_TABLE_ARTISTS = "artists"
DB_TABLE_ALBUMS = "albums"
DB_TABLE_TRACKS = "tracks"
DB_TABLE_PLAYLISTS = "playlists"
DB_TABLE_RADIOS = "radios"
DB_TABLE_CACHE = "cache"
DB_TABLE_SETTINGS = "settings"
DB_TABLE_THUMBS = "thumbnails"
DB_TABLE_PROVIDER_MAPPINGS = "provider_mappings"
