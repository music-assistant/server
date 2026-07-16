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

# Required hold time for filtering transient PLAYING reports during group startup.
PLAYBACK_CONFIRM_HOLD: Final[float] = 2.0

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


# Provider domains whose live sync session can survive removal of the current
# leader (the protocol promotes another sync_client to leader at the protocol
# level). When the active session is owned by one of these providers the sync
# group can do a seamless leader handoff; otherwise it must dissolve and
# re-form (with a brief audio gap) on leader change.
PROVIDERS_WITH_DYNAMIC_LEADER_SWITCH: Final[tuple[str, ...]] = (
    "airplay",
    "snapcast",
    "sendspin",
)
