"""
Reusable WLED V2 audio-sync bridge library.

Provides the building blocks for emitting WLED V2 audio-sync UDP packets:
an analyzer that turns Sendspin VisualizerFrames into the WLED V2 fields,
a packet encoder that packs them to 44 bytes, and an asyncio UDP
transport that fans them out to one or many WLED receivers.

This package is intentionally decoupled from Music Assistant specifics so
it can later be extracted as a standalone library.
"""

from __future__ import annotations

from .analyzer import (
    DEFAULT_VISUALIZER_RATE_HZ,
    WLED_FFT_BANDS,
    WledAudioAnalyzer,
)
from .constants import (
    WLED_AUDIOSYNC_DEFAULT_MULTICAST_GROUP,
    WLED_AUDIOSYNC_DEFAULT_PORT,
    WLED_V2_MAGIC_HEADER,
    WLED_V2_PACKET_SIZE,
)
from .encoder import V2_STRUCT_FORMAT, WledV2Frame, encode_v2
from .transport import (
    DEFAULT_ERROR_LOG_INTERVAL_S,
    DEFAULT_MULTICAST_TTL,
    DEFAULT_RESET_AFTER_CONSECUTIVE_ERRORS,
    DestinationKind,
    SocketLike,
    WledV2Transport,
    classify_destination,
)

# Spectrum bounds the bridge requests from Sendspin's visualizer role. The
# WLED V2 protocol assumes ~40 Hz to ~10 kHz log-spaced bins; we ask
# Sendspin to deliver the spectrum already shaped to that range.
DEFAULT_F_MIN = 40.0
DEFAULT_F_MAX = 10_000.0

__all__ = [
    "DEFAULT_ERROR_LOG_INTERVAL_S",
    "DEFAULT_F_MAX",
    "DEFAULT_F_MIN",
    "DEFAULT_MULTICAST_TTL",
    "DEFAULT_RESET_AFTER_CONSECUTIVE_ERRORS",
    "DEFAULT_VISUALIZER_RATE_HZ",
    "V2_STRUCT_FORMAT",
    "WLED_AUDIOSYNC_DEFAULT_MULTICAST_GROUP",
    "WLED_AUDIOSYNC_DEFAULT_PORT",
    "WLED_FFT_BANDS",
    "WLED_V2_MAGIC_HEADER",
    "WLED_V2_PACKET_SIZE",
    "DestinationKind",
    "SocketLike",
    "WledAudioAnalyzer",
    "WledV2Frame",
    "WledV2Transport",
    "classify_destination",
    "encode_v2",
]
