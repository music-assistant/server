"""Constants for the DLNA Receiver plugin provider."""

from __future__ import annotations

# UPnP device and service identifiers
UPNP_DEVICE_TYPE = "urn:schemas-upnp-org:device:MediaRenderer:1"
UPNP_SERVICE_AV_TRANSPORT = "urn:schemas-upnp-org:service:AVTransport:1"
UPNP_SERVICE_RENDERING_CONTROL = "urn:schemas-upnp-org:service:RenderingControl:1"
UPNP_SERVICE_CONNECTION_MANAGER = "urn:schemas-upnp-org:service:ConnectionManager:1"

# SSDP
SSDP_MULTICAST_ADDR = "239.255.255.250"
SSDP_PORT = 1900
SSDP_MAX_AGE = 1800  # seconds

# Config entry keys
CONF_FRIENDLY_NAME = "friendly_name"
CONF_TARGET_PLAYERS = "target_players"
CONF_HTTP_PORT = "http_port"

# Defaults
DEFAULT_FRIENDLY_NAME = "Music Assistant"
DEFAULT_HTTP_PORT = 8298  # UPnP renderer HTTP port (separate from MA stream port)

# Supported MIME types for incoming streams
SUPPORTED_MIME_TYPES = [
    "audio/flac",
    "audio/x-flac",
    "audio/wav",
    "audio/x-wav",
    "audio/mpeg",
    "audio/mp3",
    "audio/mp4",
    "audio/aac",
    "audio/ogg",
    "audio/vorbis",
    "audio/L16",
    "audio/*",
]

# UPnP transport states
TRANSPORT_STATE_STOPPED = "STOPPED"
TRANSPORT_STATE_PLAYING = "PLAYING"
TRANSPORT_STATE_PAUSED = "PAUSED_PLAYBACK"
TRANSPORT_STATE_TRANSITIONING = "TRANSITIONING"
TRANSPORT_STATE_NO_MEDIA = "NO_MEDIA_PRESENT"

# UUID namespace for deterministic UDN generation
UDN_NAMESPACE = "ma-dlna-receiver"
