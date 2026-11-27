"""Constants for the WiiM Provider."""

from music_assistant_models.player import PlayerSource

# Player ID prefix to avoid colliding with DLNA (which uses the raw UDN)
PLAYER_ID_PREFIX = "wiim_"

# The slaves of each native set (a stereo pair made in the WiiM app), kept per leader so the
# set is known after a restart while the pair is part of a larger group.
CACHE_CATEGORY_NATIVE_SETS = 1
NATIVE_SET_CACHE_EXPIRATION = 86400 * 3650

# Backend markers so either player class can recognise a group member's backend without a
# circular import, and so detect a cross-backend (mixed) group that must stay read-only.
BACKEND_OFFICIAL = "official"
BACKEND_GENERIC = "generic"

# Passive sources detected via current track URI
SOURCE_AIRPLAY = "airplay"
SOURCE_SPOTIFY = "spotify"
SOURCE_UNKNOWN = "unknown"
SOURCE_NETWORK = "Network"

PASSIVE_SOURCES: dict[str, PlayerSource] = {
    SOURCE_AIRPLAY: PlayerSource(
        id=SOURCE_AIRPLAY,
        name="AirPlay",
        passive=True,
        can_play_pause=True,
        can_next_previous=True,
        can_seek=True,
    ),
    SOURCE_SPOTIFY: PlayerSource(
        id=SOURCE_SPOTIFY,
        name="Spotify",
        passive=True,
        can_play_pause=True,
        can_next_previous=True,
        can_seek=True,
    ),
    SOURCE_UNKNOWN: PlayerSource(
        id=SOURCE_UNKNOWN,
        name="Unknown",
        passive=True,
        can_play_pause=True,
        can_next_previous=True,
        can_seek=True,
    ),
}

# Input modes from device.supported_input_modes (non-passive, user-selectable)
# Keys match the display names returned by the SDK
INPUT_MODE_SOURCES: dict[str, PlayerSource] = {
    SOURCE_NETWORK: PlayerSource(
        id="network",
        name=SOURCE_NETWORK,
        passive=False,
        can_play_pause=True,
        can_next_previous=True,
        can_seek=True,
    ),
    "Bluetooth": PlayerSource(
        id="bluetooth",
        name="Bluetooth",
        passive=False,
        can_play_pause=True,
        can_next_previous=False,
        can_seek=False,
    ),
    "Line In": PlayerSource(
        id="line_in",
        name="Line In",
        passive=False,
        is_input=True,
        can_play_pause=False,
        can_next_previous=False,
        can_seek=False,
    ),
    "Optical In": PlayerSource(
        id="optical",
        name="Optical In",
        passive=False,
        is_input=True,
        can_play_pause=False,
        can_next_previous=False,
        can_seek=False,
    ),
    "TV": PlayerSource(
        id="hdmi",
        name="TV",
        passive=False,
        is_input=True,
        can_play_pause=False,
        can_next_previous=False,
        can_seek=False,
    ),
    "Phono In": PlayerSource(
        id="phono_in",
        name="Phono In",
        passive=False,
        is_input=True,
        can_play_pause=False,
        can_next_previous=False,
        can_seek=False,
    ),
    "Aux In": PlayerSource(
        id="aux_in",
        name="Aux In",
        passive=False,
        is_input=True,
        can_play_pause=False,
        can_next_previous=False,
        can_seek=False,
    ),
}

# Reverse lookup: source ID -> SDK display name for select_source commands
SOURCE_ID_TO_INPUT_MODE: dict[str, str] = {v.id: k for k, v in INPUT_MODE_SOURCES.items()}

# The ids of the sources that are a physical input on the device (including Bluetooth, which the
# device routes itself but does not mark as an input): they keep playing while the device reports
# nothing else, so they outrank any network state left behind by an earlier stream.
DEVICE_INPUT_SOURCE_IDS = frozenset(
    {"bluetooth", "line_in", "optical", "hdmi", "phono_in", "aux_in"}
)

# External streaming services: the device plays them on its own (Tidal Connect, Spotify Connect,
# a DLNA push), so Music Assistant can only mirror what the device reports about them.
SOURCE_TIDAL = "tidal"
SOURCE_QOBUZ = "qobuz"
SOURCE_AMAZON = "amazon"
SOURCE_DEEZER = "deezer"
SOURCE_YOUTUBE = "youtube"
SOURCE_SOUNDCLOUD = "soundcloud"
SOURCE_EXTERNAL = "external"

EXTERNAL_SOURCES: dict[str, PlayerSource] = {
    SOURCE_TIDAL: PlayerSource(
        id=SOURCE_TIDAL,
        name="Tidal",
        passive=True,
        can_play_pause=True,
        can_next_previous=True,
        can_seek=True,
    ),
    SOURCE_QOBUZ: PlayerSource(
        id=SOURCE_QOBUZ,
        name="Qobuz",
        passive=True,
        can_play_pause=True,
        can_next_previous=True,
        can_seek=True,
    ),
    SOURCE_AMAZON: PlayerSource(
        id=SOURCE_AMAZON,
        name="Amazon Music",
        passive=True,
        can_play_pause=True,
        can_next_previous=True,
        can_seek=True,
    ),
    SOURCE_DEEZER: PlayerSource(
        id=SOURCE_DEEZER,
        name="Deezer",
        passive=True,
        can_play_pause=True,
        can_next_previous=True,
        can_seek=True,
    ),
    SOURCE_YOUTUBE: PlayerSource(
        id=SOURCE_YOUTUBE,
        name="YouTube Music",
        passive=True,
        can_play_pause=True,
        can_next_previous=True,
        can_seek=True,
    ),
    SOURCE_SOUNDCLOUD: PlayerSource(
        id=SOURCE_SOUNDCLOUD,
        name="SoundCloud",
        passive=True,
        can_play_pause=True,
        can_next_previous=True,
        can_seek=True,
    ),
    SOURCE_EXTERNAL: PlayerSource(
        id=SOURCE_EXTERNAL,
        name="External source",
        passive=True,
        can_play_pause=True,
        can_next_previous=True,
        can_seek=True,
    ),
}

# Every source that only mirrors what the device is doing (AirPlay, Spotify Connect, an external
# streaming session): Music Assistant has no queue of its own playing there.
REPORTED_SOURCES: dict[str, PlayerSource] = {**PASSIVE_SOURCES, **EXTERNAL_SOURCES}
