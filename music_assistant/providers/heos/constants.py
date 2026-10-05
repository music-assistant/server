"""Constants for HEOS Player Provider."""

from typing import Final

from music_assistant_models.enums import MediaType, PlaybackState
from pyheos import MediaType as HeosMediaType
from pyheos import PlayState as HeosPlayState
from pyheos import const

HEOS_MEDIA_TYPE_TO_MEDIA_TYPE: dict[HeosMediaType | None, MediaType] = {
    HeosMediaType.ALBUM: MediaType.ALBUM,
    HeosMediaType.ARTIST: MediaType.ARTIST,
    HeosMediaType.CONTAINER: MediaType.FOLDER,
    HeosMediaType.GENRE: MediaType.GENRE,
    HeosMediaType.HEOS_SERVER: MediaType.FOLDER,
    HeosMediaType.HEOS_SERVICE: MediaType.FOLDER,
    HeosMediaType.MUSIC_SERVICE: MediaType.FOLDER,
    HeosMediaType.PLAYLIST: MediaType.PLAYLIST,
    HeosMediaType.SONG: MediaType.TRACK,
    HeosMediaType.STATION: MediaType.TRACK,
}

HEOS_PLAY_STATE_TO_PLAYBACK_STATE: dict[HeosPlayState | None, PlaybackState] = {
    HeosPlayState.PLAY: PlaybackState.PLAYING,
    HeosPlayState.PAUSE: PlaybackState.PAUSED,
    HeosPlayState.STOP: PlaybackState.IDLE,
    HeosPlayState.UNKNOWN: PlaybackState.UNKNOWN,
}

HEOS_PASSIVE_SOURCES = [const.MUSIC_SOURCE_AUX_INPUT]


CONF_TIMEOUT: Final[str] = "timeout"
DEFAULT_TIMEOUT: Final = 25.0
CONF_PLAYBACK_TRANSITION_TIMEOUT: Final[str] = "playback_transition_timeout"
DEFAULT_PLAYBACK_TRANSITION_TIMEOUT: Final = 5

CONNECT_MAX_ATTEMPTS: Final = 3
CONNECT_INITIAL_RETRY_DELAY: Final = 5
CONNECT_RETRY_BACKOFF_FACTOR: Final = 1.5

# Gen 1 HEOS hardware (HS1) is limited to 48kHz/16-bit playback. Gen 2 (HS2)
# and newer Denon/Marantz HEOS-enabled receivers support up to 192kHz/24-bit.
# The "HS2" suffix is the canonical Gen 2 indicator; models in this allowlist
# predate it and need to be capped.
NON_HIRES_HEOS_MODELS: Final[tuple[str, ...]] = (
    "HEOS 1",
    "HEOS 3",
    "HEOS 5",
    "HEOS 7",
    "HEOS Amp",
    "HEOS Link",
    "HEOS HomeCinema",
)
