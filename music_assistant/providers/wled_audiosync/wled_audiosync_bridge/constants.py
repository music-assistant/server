"""
WLED V2 audio-sync protocol constants.

These describe the bytes on the wire — independent of Music Assistant. The
byte-exact layout was validated against a real-hardware packet capture; see
the parent provider's README.md §5 for the field-by-field walkthrough.
"""

from __future__ import annotations

# Default UDP port the WLED AudioReactive usermod listens on for V2 sync.
WLED_AUDIOSYNC_DEFAULT_PORT = 11988
# Conventional IPv4 multicast group for V2 audio-sync, observed in the
# reference capture. Senders that don't already have a specific unicast
# destination should default to this group.
WLED_AUDIOSYNC_DEFAULT_MULTICAST_GROUP = "239.0.0.1"
# Total wire size of a V2 payload. Field bytes sum to 40; the actual on-wire
# layout is 44 bytes due to two natural-alignment padding regions.
WLED_V2_PACKET_SIZE = 44
# Six-byte magic header (the literal string "00002" plus a NUL byte). Always
# emitted by the encoder and always present in observed packets.
WLED_V2_MAGIC_HEADER = b"00002\x00"
