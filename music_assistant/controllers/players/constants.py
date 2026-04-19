"""Constants for the Player Controller."""

from enum import StrEnum

# Debounce window (seconds) for coalescing rapid-fire cmd_set_members calls
# against the same target player. Home Assistant automations often fire
# several add/remove commands in quick succession; without this window each
# one would trigger a full teardown+restart of protocol sessions (notably
# AirPlay cliraop). 0.3s is tight enough to feel instant and wide enough to
# absorb typical HA automation bursts.
SET_MEMBERS_DEBOUNCE = 0.3

# Prefix for the per-target-player task id used to key the debounce timer.
SET_MEMBERS_TASK_ID_PREFIX = "set_members"


class PlayerLockPurpose(StrEnum):
    """Lock categories for get_player_lock to serialize commands per player."""

    PLAYBACK = "playback"
    VOLUME = "volume"
