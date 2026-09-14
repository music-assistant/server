"""Constants for the Linn / OpenHome Media Provider."""

CONF_NETWORK_SCAN: str = "True"
CALLBACK_URL: str = "/notify_ohm"

# Player ID prefix to avoid colliding with DLNA (which uses the raw UDN)
PLAYER_ID_PREFIX = "ohm_"
