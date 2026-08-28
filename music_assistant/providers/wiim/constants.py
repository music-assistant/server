"""Constants for the WiiM Provider."""

from music_assistant_models.player import PlayerSource

# Player ID prefix to avoid colliding with DLNA (which uses the raw UDN)
PLAYER_ID_PREFIX = "wiim_"

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
        can_play_pause=False,
        can_next_previous=False,
        can_seek=False,
    ),
    "Optical In": PlayerSource(
        id="optical",
        name="Optical In",
        passive=False,
        can_play_pause=False,
        can_next_previous=False,
        can_seek=False,
    ),
    "TV": PlayerSource(
        id="hdmi",
        name="TV",
        passive=False,
        can_play_pause=False,
        can_next_previous=False,
        can_seek=False,
    ),
    "Phono In": PlayerSource(
        id="phono_in",
        name="Phono In",
        passive=False,
        can_play_pause=False,
        can_next_previous=False,
        can_seek=False,
    ),
}

# Reverse lookup: source ID -> SDK display name for select_source commands
SOURCE_ID_TO_INPUT_MODE: dict[str, str] = {v.id: k for k, v in INPUT_MODE_SOURCES.items()}
