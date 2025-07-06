"""Constants for SnapCast."""

from enum import StrEnum

# Configuration constants
CONF_SERVER_BUFFER_SIZE = "snapcast_server_built_in_buffer_size"
CONF_SERVER_CHUNK_MS = "snapcast_server_built_in_chunk_ms"
CONF_SERVER_INITIAL_VOLUME = "snapcast_server_built_in_initial_volume"
CONF_SERVER_TRANSPORT_CODEC = "snapcast_server_built_in_codec"
CONF_SERVER_SEND_AUDIO_TO_MUTED = "snapcast_server_built_in_send_muted"
CONF_STREAM_IDLE_THRESHOLD = "snapcast_stream_idle_threshold"
CONF_USE_EXTERNAL_SERVER = "snapcast_use_external_server"
CONF_SERVER_HOST = "snapcast_server_host"
CONF_SERVER_CONTROL_PORT = "snapcast_server_control_port"


class SnapCastStreamType(StrEnum):
    """Enum for Snapcast Stream Type."""

    MUSIC = "MUSIC"
    ANNOUNCEMENT = "ANNOUNCEMENT"
