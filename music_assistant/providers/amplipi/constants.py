"""Constants for the AmpliPi player provider."""

import aiohttp
from pyamplipi.error import AmpliPiError
from pydantic import ValidationError

DOMAIN = "amplipi"

# Errors the pyamplipi client raises when the controller is unreachable, returns an API
# error, or sends an unexpected/garbled response (AmpliPiUnreachableError/APIError extend
# AmpliPiError; a malformed payload surfaces as a pydantic ValidationError or a KeyError on
# a missing top-level field). Caught where an AmpliPi call is best-effort and a failure
# should not abort the surrounding operation.
AMPLIPI_API_ERRORS = (AmpliPiError, aiohttp.ClientError, TimeoutError, ValidationError, KeyError)


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

# AmpliPi stream type used for Music Assistant playback. The "internetradio" type is
# built for continuous HTTP streams and supports reliable play/stop, unlike the
# "fileplayer" type (which is intended for one-shot announcements). Note: AmpliPi has
# no native pause for this type; pause is emulated via stop in the player.
MA_STREAM_TYPE = "internetradio"

# name prefix for the streams Music Assistant creates on the AmpliPi controller; used to
# identify and clean up our own streams (one per source) across reloads.
MA_STREAM_NAME = "Music Assistant"

# AmpliPi stream types for physical passthrough inputs. RCA inputs are first-class streams
# (type="rca", index 0-3, ids 996-999, names "Input 1-4") connected to a source like any
# stream: source.input = "stream=<id>". "aux" is the front-panel auxiliary input. Both are
# pure passthrough: no transport (play/stop endpoints 404, info.state stays "stopped"); MA
# can route them to zones and set volume, but the wired source owns the audio.
RCA_STREAM_TYPE = "rca"
AUX_STREAM_TYPE = "aux"
INPUT_STREAM_TYPES = (RCA_STREAM_TYPE, AUX_STREAM_TYPE)

# Friendly labels for AmpliPi stream types, used to disambiguate selectable sources in the
# UI: native streams can share a name (e.g. a Spotify and an AirPlay endpoint both named
# "AmpliPro 1"), so non-input sources are labelled "<name> (<type label>)".
STREAM_TYPE_LABELS = {
    "spotify": "Spotify",
    "airplay": "AirPlay",
    "pandora": "Pandora",
    "dlna": "DLNA",
    "internetradio": "Internet Radio",
    "plexamp": "Plexamp",
    "bluetooth": "Bluetooth",
    "lms": "LMS",
}

# Stream types that should NOT be offered to the user as selectable sources. "fileplayer"
# is AmpliPi's built-in announcement player ("External Media"), not a real source.
# TODO(announcements): fileplayer is excluded from the source picker, but it is the right
# mechanism for MA's PLAY_ANNOUNCEMENT/TTS feature (one-shot URL playback). When we add
# announcement support, USE the fileplayer stream here rather than surfacing it as a source.
EXCLUDED_SELECTABLE_STREAM_TYPES = ("fileplayer",)

# Music Assistant PlayerSource ids for AmpliPi-side selectable sources are namespaced as
# "stream=<amplipi stream id>", which doubles as the value written to source.input.
SOURCE_ID_STREAM_PREFIX = "stream="

# AmpliPi's volume scale is linear in dB over a wide range (~-80..0 dB), so mapping Music
# Assistant's 0-100 directly onto it leaves the lower half of the slider near-silent.
# Instead we map 0-100 onto a usable dB window (this floor .. 0 dB), mirroring how the
# AirPlay provider maps onto a usable window. Volume 0 -> floor, 100 -> 0 dB (max).
VOLUME_DB_FLOOR = -60
