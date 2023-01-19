"""All constants for Music Assistant."""

import pathlib

__version__ = "2.0.0"

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
CONF_WEB_HOST = "webserver/host"
CONF_WEB_PORT = "webserver/port"
CONF_DB_URL = "database/url"

# config default values
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8095
