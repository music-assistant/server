"""Constants for the AmpliPi player provider."""

DOMAIN = "amplipi"

CONF_HOST = "host"

# AmpliPi zone source_id sentinels (mirrors the AmpliPi server constants):
# a zone connected to a source uses its source_id (0..3),
# SOURCE_DISCONNECTED means "powered on but no source connected" (zone is silent),
# ZONE_OFF means the zone is "off" (used to model MA's power state).
SOURCE_DISCONNECTED = -1
ZONE_OFF = -2

# AmpliPi has no push interface, so we poll the controller for state updates.
POLL_INTERVAL = 5

# values of Source.input that indicate the source is free/unassigned.
FREE_SOURCE_INPUTS = ("", "None", None)
