"""Constants for the Teufel Raumfeld player provider."""

from __future__ import annotations

import aiohttp
from music_assistant_models.config_entries import ConfigEntry

from music_assistant.constants import (
    CONF_ENTRY_FLOW_MODE,
    CONF_ENTRY_HTTP_PROFILE_FORCED_1,
    CONF_ENTRY_ICY_METADATA_DEFAULT_FULL,
    create_sample_rates_config_entry,
)

# Errors expected when the host is (temporarily) unreachable or a zone/room cannot be
# resolved by hassfeld; used to narrow the try/except around host communication. KeyError is
# one of them: hassfeld looks rooms and zones up in its model of the host, which raises it
# for a room or zone the host just dropped (a regrouping, a speaker going to deep standby).
HOST_ERRORS = (aiohttp.ClientError, OSError, TimeoutError, KeyError)

# Default port of the Raumfeld host webservice (the device running the "RaumfeldHost"
# service). Config keys reuse MA's CONF_IP_ADDRESS / CONF_PORT.
DEFAULT_PORT = 47365

# How long (seconds) to wait for hassfeld to complete its initial discovery
# of rooms/zones before giving up on a single connection attempt.
INITIAL_UPDATE_TIMEOUT = 30

# How long (seconds) unload waits for its cancelled tasks to finish.
UNLOAD_TIMEOUT = 5

# How long (seconds) to wait for a renderer's device description, which is read once per
# room to get the hardware serial the player_id is derived from.
DEVICE_INFO_TIMEOUT = 10

# How long (seconds) to wait for a renderer to answer a state query. Polls run one after the
# other for all players, so a renderer that stops answering must not hold them up for long.
RENDERER_QUERY_TIMEOUT = 5

# How often (seconds) the connection supervisor retries a connect while the host is
# unreachable, and health-checks the host while connected.
RECONNECT_INTERVAL = 30

# How often (seconds) the zones the host reports are compared with the groups Music Assistant
# shows. The comparison reads hassfeld's in-memory model, so it costs no request.
ZONE_WATCH_INTERVAL = 2

# How long (seconds) after Music Assistant changed a group itself the host's zones are left
# alone. A group change takes the host several steps (a room leaves one zone, joins the
# next, a leader hands over), and mirroring one of the states in between would undo it.
GROUP_SETTLE_TIME = 10

# Player id prefix so ids are namespaced and stable-ish per room.
PLAYER_ID_PREFIX = "raumfeld"

# Media-server container listing each device's analog Line-In input, and the MA
# source id used to expose a room's Line-In via PlayerFeature.SELECT_SOURCE.
LINE_IN_OBJECT_ID = "0/Line In"

# The station buttons on a speaker, offered as sources; Raumfeld devices have four.
STATION_BUTTONS = (1, 2, 3, 4)
# How often (seconds) the station buttons are read again, as they can be reassigned in the
# Raumfeld app at any time. A read asks each button for its item; only an item not seen
# before is described, so a read that finds nothing changed costs one call per button.
LIBRARY_REFRESH_INTERVAL = 60
# How long (seconds) the title and kind of a button's item are kept in the cache. A reassigned
# button gets a new object id, so this only bounds how long a renamed item keeps its old title.
BUTTON_ITEM_CACHE_TTL = 300
CACHE_CATEGORY_BUTTON_ITEMS = 1
SOURCE_LINE_IN = "line_in"

# Per-player config entries (MA adds the flow sample-rate entry itself).
PLAYER_CONFIG_ENTRIES = [
    # Flow mode on by default. The zone renderer has no SetNextAVTransportURI, so a stream
    # per queue item tears the transport down at every track boundary: a short gap. One
    # continuous flow stream, relayed by the host to every room in the zone, is gapless - in
    # groups too. Switched off, each item plays as its own stream: a gap between tracks, but
    # the Raumfeld app then shows each track's own elapsed time and duration.
    ConfigEntry.from_dict({**CONF_ENTRY_FLOW_MODE.to_dict(), "default_value": True}),
    # Chunked, hidden. The host's stream relay reconnects and replays the stream from the
    # start whenever the response carries a finite Content-Length (measured, and
    # forced_content_length reproduces it); a chunked response carries none, so the host
    # relays the flow as one continuous connection.
    CONF_ENTRY_HTTP_PROFILE_FORCED_1,
    # ICY on by default. The stream never changes - it is one flow for the whole queue - so
    # the ICY StreamTitle, which the host parses out of the relayed stream, is the only way
    # the Raumfeld app can follow the queue. It carries the title only: the app's clock and
    # duration do not follow along.
    CONF_ENTRY_ICY_METADATA_DEFAULT_FULL,
    # Sample rates, a choice per room. Up to 24-bit/96kHz plays on every model seen so far;
    # 176.4 and 192 kHz play on some (the One S) and drop out on others (a 1st generation
    # One M, and a Connector and Stereo Cubes in one setup). No renderer tells which
    # (GetProtocolInfo lists no rates), so the higher rates are there to switch on per room.
    create_sample_rates_config_entry(
        max_sample_rate=192000,
        max_bit_depth=24,
        safe_max_sample_rate=96000,
        safe_max_bit_depth=24,
    ),
]
