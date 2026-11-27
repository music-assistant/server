"""Sync Group Player constants."""

from __future__ import annotations

from typing import Final

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, PlayerFeature

SGP_PREFIX: Final[str] = "syncgroup_"

# Grace period (seconds) before a sync group dissolves itself after the queue
# naturally finishes (playback_state transitions to IDLE without an explicit
# stop). A short grace window absorbs end-of-track gaps and a quick "play next"
# from the user without disrupting the live sync session.
IDLE_GRACE_SECONDS: Final[float] = 10.0

# Maximum seconds to wait for the sync leader to confirm it has actually started
# playing after a (re)form. The group's playback lock is held for the duration so
# a concurrent (un)group command can't race a start that is still in flight at the
# device — which would otherwise strand a player playing outside the group.
PLAYBACK_START_TIMEOUT: Final[float] = 5.0

# Debounce window (seconds) between removing the sync leader and re-forming the
# group with a new one. Cascaded unjoins (e.g. a Home Assistant automation
# ungrouping several rooms in short succession) each re-arm the window, so the
# group re-forms and resumes playback exactly once with the final member list
# instead of restarting for every removal.
REFORM_DEBOUNCE_SECONDS: Final[float] = 2.0

# How long a member may keep reporting the leader it was released from, also when that
# leader did not answer the release. A Sonos leader that gets a group command right after
# its AirPlay output stopped answers none for some 40 seconds, and lets its members go then.
STUCK_MEMBER_TIMEOUT: Final[float] = 30.0

CONF_ENTRY_SGP_NOTE = ConfigEntry(
    key="sgp_note",
    type=ConfigEntryType.ALERT,
    required=False,
)

CONF_ALLOWED_MEMBERS: Final[str] = "allowed_members"


EXTRA_FEATURES_FROM_MEMBERS: Final[set[PlayerFeature]] = {
    PlayerFeature.ENQUEUE,
    PlayerFeature.GAPLESS_PLAYBACK,
    PlayerFeature.VOLUME_SET,
    PlayerFeature.VOLUME_MUTE,
    PlayerFeature.MULTI_DEVICE_DSP,
}
# the subset above that is fanned out to every member rather than sent to the sync leader
VOLUME_FEATURES_FROM_MEMBERS: Final[set[PlayerFeature]] = {
    PlayerFeature.VOLUME_SET,
    PlayerFeature.VOLUME_MUTE,
}

# Transport features the group forwards to its sync leader, for a source the
# leader plays itself (e.g. a device-native input or streaming service).
FEATURES_FROM_LEADER: Final[set[PlayerFeature]] = {
    PlayerFeature.PAUSE,
    PlayerFeature.SEEK,
    PlayerFeature.NEXT_PREVIOUS,
}
