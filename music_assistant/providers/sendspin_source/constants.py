"""Constants for the Sendspin Source provider."""

from __future__ import annotations

CONF_TARGET_LATENCY = "target_latency"
DEFAULT_TARGET_LATENCY_MS = 500

# Fixed PCM output format for all Sendspin sources. The source's native format is
# only known once the client starts streaming, so the stream declares this format
# upfront and the bridge converts to it.
OUTPUT_SAMPLE_RATE = 48000
OUTPUT_BIT_DEPTH = 16
OUTPUT_CHANNELS = 2

CHUNK_DURATION_MS = 25

# Give up and end the stream after this long without source audio (client never
# started, disconnected, or reports unavailable).
SOURCE_TIMEOUT_S = 30.0

# Hold a new line-in signal this long before starting. A preamp hovering around its
# detection threshold reports present/absent repeatedly.
AUTOSTART_SIGNAL_DEBOUNCE_S = 2.0

# Keep playing this long after the signal disappears, so the gap between tracks or a
# quiet passage does not end the stream.
AUTOSTART_SIGNAL_ABSENT_HOLD_S = 60.0
