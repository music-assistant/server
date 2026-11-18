"""Constants for the Player Controller."""

from enum import StrEnum
from typing import Final

from music_assistant_models.enums import PlayerType

from music_assistant.constants import POWER_ON_STARTUP_TIME_RANGE

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
# How many times a command over several players (see PlayerController.get_playback_locks)
# tries to take their locks, each time first waiting for the player found busy the time before;
# one that keeps finding a busy player fails as busy instead of waiting on.
PLAYER_LOCKS_ATTEMPTS: Final = 3

# How long the protocols of a player whose power control cuts its power get to connect to
# its device once the start-up time of the player is over, and to handle that its power
# was cut.
POWER_RESTORE_TIMEOUT: Final = 10
# The longest a power on of a player can wait for it to start up.
MAX_POWER_ON_WAIT: Final = POWER_ON_STARTUP_TIME_RANGE[1] + POWER_RESTORE_TIMEOUT
# How long a power on waits for the power control of a player, or its own protocol, to report
# on before the command goes on: an amplifier or receiver can take a while to report.
POWER_ON_REPORT_TIMEOUT: Final = 15


class PlayerLockPurpose(StrEnum):
    """Lock categories for get_player_lock to serialize commands per player."""

    PLAYBACK = "playback"
    VOLUME = "volume"
    GROUP_VOLUME = "group_volume"


class SwitchedOffAction(StrEnum):
    """
    What a player command does with a player that is switched off at its power control.

    That is a player that is off, or starting up, at a power control that cuts its power.
    """

    REFUSE = "refuse"
    """Raise PlayerUnavailableError, as the player can not take the command."""

    IGNORE = "ignore"
    """Return without doing anything, as the command has nothing to act on."""

    CANCEL_POWER_ON = "cancel_power_on"
    """Call off a power on that is under way, otherwise return without doing anything."""

    POWER_ON = "power_on"
    """Power the player on (and wait for it to start up) before running the command."""

    RUN = "run"
    """Run the command as is, for commands that handle the switched off player themselves."""


CACHE_CATEGORY_PLAYER_POWER = 1

# Player types that must be detached from their (sync)group when they power off.
# A multi-channel player joins a group and leads a sync session exactly like a single speaker.
# A GROUP player has no group of its own to leave: its members are released by its own
# power off, and ungrouping one is defined as powering it off - which is what already
# brought it here.
UNGROUP_ON_POWER_OFF_TYPES = {PlayerType.PLAYER, PlayerType.MULTI_CHANNEL}
