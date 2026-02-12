"""Constants for the MSX Bridge Provider."""

import re

CONF_HTTP_PORT = "http_port"
CONF_OUTPUT_FORMAT = "output_format"
CONF_PLAYER_IDLE_TIMEOUT = "player_idle_timeout"
CONF_SHOW_STOP_NOTIFICATION = "show_stop_notification"
CONF_ABORT_STREAM_FIRST = "abort_stream_first"
CONF_ENABLE_GROUPING = "enable_player_grouping"

DEFAULT_HTTP_PORT = 8099
DEFAULT_OUTPUT_FORMAT = "mp3"
DEFAULT_PLAYER_IDLE_TIMEOUT = 30  # minutes
DEFAULT_SHOW_STOP_NOTIFICATION = False
DEFAULT_ABORT_STREAM_FIRST = False
DEFAULT_ENABLE_GROUPING = True

# Player ID prefix for dynamically registered players
MSX_PLAYER_ID_PREFIX = "msx_"

# Sanitize device_id or IP for use in player_id (alphanumeric + underscore only)
PLAYER_ID_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9_]+")

# Pre-buffer size: accumulate this many bytes before sending HTTP headers to prevent
# MSX stutter/restart when ffmpeg hasn't produced data yet.
PRE_BUFFER_BYTES = 64 * 1024
