"""Constants for the Player Controller."""

from enum import StrEnum


class PlayerLockPurpose(StrEnum):
    """Lock categories for get_player_lock to serialize commands per player."""

    PLAYBACK = "playback"
    VOLUME = "volume"
    GROUP_VOLUME = "group_volume"
