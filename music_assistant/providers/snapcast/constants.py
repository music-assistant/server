"""Constants for snapcast provider."""

import pathlib

from music_assistant_models.enums import ContentType
from music_assistant_models.media_items.audio_format import AudioFormat

CONF_SERVER_HOST = "snapcast_server_host"
CONF_SERVER_CONTROL_PORT = "snapcast_server_control_port"
CONF_USE_EXTERNAL_SERVER = "snapcast_use_external_server"
CONF_SERVER_BUFFER_SIZE = "snapcast_server_built_in_buffer_size"
CONF_SERVER_CHUNK_MS = "snapcast_server_built_in_chunk_ms"
CONF_SERVER_INITIAL_VOLUME = "snapcast_server_built_in_initial_volume"
CONF_SERVER_TRANSPORT_CODEC = "snapcast_server_built_in_codec"
CONF_SERVER_SEND_AUDIO_TO_MUTED = "snapcast_server_built_in_send_muted"
CONF_STREAM_IDLE_THRESHOLD = "snapcast_stream_idle_threshold"
CONF_STREAM_SAMPLE_RATE = "snapcast_stream_sample_rate"
CONF_STREAM_BIT_DEPTH = "snapcast_stream_bit_depth"

CONF_CATEGORY_GENERIC = "generic"
CONF_CATEGORY_BUILT_IN = "Built-in Snapserver Settings"

CONF_HELP_LINK = (
    "https://raw.githubusercontent.com/badaix/snapcast/refs/heads/master/server/etc/snapserver.conf"
)

DEFAULT_SNAPSERVER_IP = "127.0.0.1"
DEFAULT_SNAPSERVER_PORT = 1705
DEFAULT_SNAPSTREAM_IDLE_THRESHOLD = 60000
DEFAULT_SNAPSERVER_PLUGIN_DIR = "/usr/share/snapserver/plug-ins"
DEFAULT_SNAPSERVER_CONFIG_FILE = "/etc/snapserver.conf"
SHIPPED_SNAPSERVER_CONFIG_FILE = (
    pathlib.Path(__file__).parent / "snapserver" / "snapserver.conf"
).resolve()

# snapserver has no TCP keepalive (https://github.com/snapcast/snapcast/issues/995) and
# never times out abruptly powered-off clients, so we poll lastSeen freshness ourselves.
SNAPCLIENT_LIVENESS_POLL_INTERVAL = 5  # poll_interval (seconds) for snapcast players
SNAPCLIENT_STALE_THRESHOLD = 15  # mark unavailable if lastSeen frozen this long (seconds)

# Socket path template for control script communication
# The {queue_id} placeholder will be replaced with the actual queue ID
CONTROL_SOCKET_PATH_TEMPLATE = "/tmp/ma-snapcast-{queue_id}.sock"  # noqa: S108

MASS_STREAM_PREFIX = "Music Assistant - "
MASS_ANNOUNCEMENT_POSTFIX = " (announcement)"
SNAPWEB_DIR = pathlib.Path(__file__).parent.resolve().joinpath("snapweb")
CONTROL_SCRIPT = pathlib.Path(__file__).parent.resolve().joinpath("control.py")

# Supported PCM formats for the Music Assistant -> Snapserver TCP source.
# 24-bit requires Snapserver packed_s24le ingest support (snapcast/snapcast#1532).
SNAPCAST_SAMPLE_RATES = (48000, 96000, 192000)
SNAPCAST_BIT_DEPTHS = (16, 24)

DEFAULT_SNAPCAST_FORMAT = AudioFormat(
    content_type=ContentType.PCM_S16LE,
    sample_rate=48000,
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


def snapcast_stream_format(sample_rate: int, bit_depth: int) -> AudioFormat:
    """Return the PCM AudioFormat used for the Snapcast TCP source stream."""
    if sample_rate not in SNAPCAST_SAMPLE_RATES:
        sample_rate = DEFAULT_SNAPCAST_FORMAT.sample_rate
    if bit_depth not in SNAPCAST_BIT_DEPTHS:
        bit_depth = DEFAULT_SNAPCAST_FORMAT.bit_depth
    content_type = ContentType.PCM_S24LE if bit_depth == 24 else ContentType.PCM_S16LE
    return AudioFormat(
        content_type=content_type,
        sample_rate=sample_rate,
        bit_depth=bit_depth,
        channels=2,
    )


def snapcast_sampleformat_query(audio_format: AudioFormat) -> str:
    """
    Build the sampleformat query fragment for a Snapcast TCP source URI.

    For 24-bit streams, also enables packed_s24le so ffmpeg's packed s24le
    output can be ingested by Snapserver (see snapcast/snapcast#1532).
    """
    sampleformat = f"{audio_format.sample_rate}:{audio_format.bit_depth}:{audio_format.channels}"
    if audio_format.bit_depth == 24:
        return f"sampleformat={sampleformat}&packed_s24le=true"
    return f"sampleformat={sampleformat}"
