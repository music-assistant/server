"""Constants for the Sony SongPal player provider."""

from __future__ import annotations

# One endpoint URL per line, for devices SSDP does not find.
CONF_MANUAL_ENDPOINTS = "manual_endpoints"

# Input activated when a linked protocol (AirPlay, Chromecast, DLNA) starts playing.
CONF_PLAYBACK_INPUT = "playback_input"

# SongpalException.code when the device is already in the requested power state.
# Home Assistant's songpal const.ERROR_REQUEST_RETRY.
ERROR_REQUEST_RETRY = 40000

SSDP_TIMEOUT = 5
REDISCOVERY_INTERVAL = 120
CONNECT_TIMEOUT = 10
INITIAL_RETRY_DELAY = 10
MAX_RETRY_DELAY = 300

SCALARWEBAPI_ST = "urn:schemas-sony-com:service:ScalarWebAPI:1"
AVTRANSPORT_ST = "urn:schemas-upnp-org:service:AVTransport:1"

MANUFACTURER = "Sony Corporation"
