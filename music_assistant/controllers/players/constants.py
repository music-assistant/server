"""Constants for the Player Controller."""

from enum import StrEnum
from typing import Final

# Waiting for a player lock (see PlayerController.get_player_lock): a debug line once the
# wait exceeds the slow threshold and a warning at the timeout, after which a command runs
# without the lock. A strict acquisition keeps waiting up to the strict timeout and fails.
PLAYER_LOCK_SLOW_THRESHOLD: Final = 5
PLAYER_LOCK_TIMEOUT: Final = 30
PLAYER_LOCK_STRICT_TIMEOUT: Final = 120


class PlayerLockPurpose(StrEnum):
    """Lock categories for get_player_lock to serialize commands per player."""

    PLAYBACK = "playback"
    VOLUME = "volume"
    GROUP_VOLUME = "group_volume"
