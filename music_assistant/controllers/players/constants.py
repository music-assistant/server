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


class SwitchedOffAction(StrEnum):
    """What a player command does with a player that is switched off at its power control."""

    REFUSE = "refuse"
    """Raise PlayerUnavailableError, as the player can not take the command."""

    IGNORE = "ignore"
    """Return without doing anything, as the command has nothing to act on."""

    CANCEL_POWER_ON = "cancel_power_on"
    """Call off a power on that is under way, otherwise return without doing anything."""

    POWER_ON = "power_on"
    """Power the player on (and wait for it to come online) before running the command."""

    RUN = "run"
    """Run the command as is, for commands that handle the switched off player themselves."""
