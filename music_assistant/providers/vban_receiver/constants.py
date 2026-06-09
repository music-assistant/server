"""Constants for the VBAN receiver provider."""

from __future__ import annotations

from aiovban.asyncio.util import BackPressureStrategy
from music_assistant_models.enums import ProviderFeature

DEFAULT_UDP_PORT = 6980
DEFAULT_PCM_AUDIO_FORMAT = "S16LE"
DEFAULT_PCM_SAMPLE_RATE = 44100
DEFAULT_AUDIO_CHANNELS = 2

CONF_VBAN_STREAM_NAME = "vban_stream_name"
CONF_SENDER_HOST = "sender_host"
CONF_PCM_AUDIO_FORMAT = "audio_format"
CONF_PCM_SAMPLE_RATE = "sample_rate"
CONF_AUDIO_CHANNELS = "audio_channels"
CONF_VBAN_QUEUE_STRATEGY = "vban_queue_strategy"
CONF_VBAN_QUEUE_SIZE = "vban_queue_size"
CONF_LOG_VBAN_STREAM_STATS = "log_vban_stream_stats"

VBAN_QUEUE_STRATEGIES = {
    "Clear entire queue": BackPressureStrategy.DROP,
    "Clear the oldest half of the queue": BackPressureStrategy.DRAIN_OLDEST,
    "Remove single oldest queue entry": BackPressureStrategy.POP,
}

SUPPORTED_FEATURES = {ProviderFeature.AUDIO_SOURCE}
