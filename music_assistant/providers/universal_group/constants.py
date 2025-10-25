"""Universal Group Player constants."""

from __future__ import annotations

from typing import Final

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import INTERNAL_PCM_FORMAT, create_sample_rates_config_entry

UGP_PREFIX: Final[str] = "ugp_"


CONF_ENTRY_SAMPLE_RATES_UGP = create_sample_rates_config_entry(
    max_sample_rate=96000, max_bit_depth=24, hidden=True
)
CONFIG_ENTRY_UGP_NOTE = ConfigEntry(
    key="ugp_note",
    type=ConfigEntryType.ALERT,
    label="Please note that although the Universal Group "
    "allows you to group any player, it will not (and can not) enable audio sync "
    "between players of different ecosystems. It is advised to always use native "
    "player groups or sync groups when available for your player type(s) and use "
    "the Universal Group only to group players of different ecosystems/protocols.",
    required=False,
)


UGP_FORMAT = AudioFormat(
    content_type=INTERNAL_PCM_FORMAT.content_type,
    sample_rate=INTERNAL_PCM_FORMAT.sample_rate,
    bit_depth=INTERNAL_PCM_FORMAT.bit_depth,
)
