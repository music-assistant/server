"""Constants for the MPD Player Provider."""

from music_assistant_models.enums import PlaybackState

RECONNECT_DELAY = 5  # seconds between reconnect attempts
ELAPSED_POLL_INTERVAL = 5  # seconds between elapsed time polls while playing
HEALTHCHECK_POLL_INTERVAL = 30  # seconds between liveness polls while the player is idle
COMMAND_TIMEOUT = 10  # seconds before an unanswered MPD command counts as a lost connection
CONNECT_TIMEOUT = 10  # seconds before a connect attempt is given up and retried

MPD_STATE_MAP: dict[str, PlaybackState] = {
    "play": PlaybackState.PLAYING,
    "pause": PlaybackState.PAUSED,
    "stop": PlaybackState.IDLE,
}
