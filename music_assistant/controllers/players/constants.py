"""Constants for the Player Controller."""

from enum import StrEnum
from typing import Final

# Waiting for a player lock (see PlayerController.get_player_lock): a debug line once the
# wait exceeds the slow threshold; at the timeout the command fails as the player is busy.
# The timeout outlasts what a command legitimately holds a player for (a play request that
# loads a large playlist, an announcement), so only a holder that is stuck makes it fail.
PLAYER_LOCK_SLOW_THRESHOLD: Final = 5
PLAYER_LOCK_TIMEOUT: Final = 120
# How many times a command takes the locks of a player and its owner (the group or sync
# leader holding it) again when the owner changed while it waited; a player whose owner keeps
# changing makes it fail as busy instead of waiting on.
PLAYER_LOCK_OWNER_ATTEMPTS: Final = 3


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
