"""Constants for snapcast provider."""

import pathlib
from enum import StrEnum

from music_assistant_models.enums import ContentType
from music_assistant_models.media_items.audio_format import AudioFormat

from music_assistant.constants import create_sample_rates_config_entry

CONF_SERVER_HOST = "snapcast_server_host"
CONF_SERVER_CONTROL_PORT = "snapcast_server_control_port"
CONF_USE_EXTERNAL_SERVER = "snapcast_use_external_server"
CONF_SERVER_BUFFER_SIZE = "snapcast_server_built_in_buffer_size"
CONF_SERVER_CHUNK_MS = "snapcast_server_built_in_chunk_ms"
CONF_SERVER_INITIAL_VOLUME = "snapcast_server_built_in_initial_volume"
CONF_SERVER_TRANSPORT_CODEC = "snapcast_server_built_in_codec"
CONF_SERVER_SEND_AUDIO_TO_MUTED = "snapcast_server_built_in_send_muted"
CONF_STREAM_IDLE_THRESHOLD = "snapcast_stream_idle_threshold"


CONF_CATEGORY_GENERIC = "generic"
CONF_CATEGORY_ADVANCED = "advanced"
CONF_CATEGORY_BUILT_IN = "Built-in Snapserver Settings"

CONF_HELP_LINK = (
    "https://raw.githubusercontent.com/badaix/snapcast/refs/heads/master/server/etc/snapserver.conf"
)

# snapcast has fixed sample rate/bit depth so make this config entry static and hidden
CONF_ENTRY_SAMPLE_RATES_SNAPCAST = create_sample_rates_config_entry(
    supported_sample_rates=[48000], supported_bit_depths=[16], hidden=True
)

DEFAULT_SNAPSERVER_IP = "127.0.0.1"
DEFAULT_SNAPSERVER_PORT = 1705
DEFAULT_SNAPSTREAM_IDLE_THRESHOLD = 60000

# Socket path template for control script communication
# The {queue_id} placeholder will be replaced with the actual queue ID
CONTROL_SOCKET_PATH_TEMPLATE = "/tmp/ma-snapcast-{queue_id}.sock"  # noqa: S108

MASS_STREAM_PREFIX = "Music Assistant - "
MASS_ANNOUNCEMENT_POSTFIX = " (announcement)"
SNAPWEB_DIR = pathlib.Path(__file__).parent.resolve().joinpath("snapweb")
CONTROL_SCRIPT = pathlib.Path(__file__).parent.resolve().joinpath("control.py")

DEFAULT_SNAPCAST_FORMAT = AudioFormat(
    content_type=ContentType.PCM_S16LE,
    sample_rate=48000,
    # TODO: we can also use 32 bits here
    bit_depth=16,
    channels=2,
)

DEFAULT_SNAPCAST_PCM_FORMAT = AudioFormat(
    # the format that is used as intermediate pcm stream,
    # we prefer F32 here to account for volume normalization
    content_type=ContentType.PCM_F32LE,
    sample_rate=48000,
    bit_depth=16,
    channels=2,
)


class SnapCastStreamType(StrEnum):
    """Enum for Snapcast Stream Type."""

    MUSIC = "MUSIC"
    ANNOUNCEMENT = "ANNOUNCEMENT"
