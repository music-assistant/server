"""Sync Group Player constants."""

from __future__ import annotations

from typing import Final

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, PlayerFeature

SGP_PREFIX: Final[str] = "syncgroup_"

CONF_ENTRY_SGP_NOTE = ConfigEntry(
    key="sgp_note",
    type=ConfigEntryType.ALERT,
    label="Sync groups allow you to group compatible players together to play audio in sync. "
    "Players can only be grouped together if they support the same sync protocol",
    required=False,
)

CONF_MEMBERS_FILTER: Final[str] = "members_filter"


EXTRA_FEATURES_FROM_MEMBERS: Final[set[PlayerFeature]] = {
    PlayerFeature.ENQUEUE,
    PlayerFeature.GAPLESS_PLAYBACK,
    PlayerFeature.VOLUME_SET,
    PlayerFeature.VOLUME_MUTE,
    PlayerFeature.MULTI_DEVICE_DSP,
}
