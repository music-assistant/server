"""Constants."""

from music_assistant.constants import (
    CONF_ENTRY_ENABLE_ICY_METADATA_HIDDEN,
    CONF_ENTRY_FLOW_MODE_ENFORCED,
    CONF_ENTRY_HTTP_PROFILE_FORCED_2,
    CONF_ENTRY_OUTPUT_CODEC,
    create_sample_rates_config_entry,
)

PLAYER_CONFIG_ENTRIES = (
    CONF_ENTRY_OUTPUT_CODEC,
    CONF_ENTRY_HTTP_PROFILE_FORCED_2,
    CONF_ENTRY_ENABLE_ICY_METADATA_HIDDEN,
    CONF_ENTRY_FLOW_MODE_ENFORCED,
    create_sample_rates_config_entry(max_sample_rate=192000, max_bit_depth=24),
)

# switch to these non netusb sources when leaving a group
CONF_PLAYER_SWITCH_SOURCE_NON_NET = "main_switch_source"
CONF_PLAYER_TURN_OFF_ON_LEAVE = "turn_off_on_leave"
MAIN_SWITCH_SOURCE_NON_NET = "audio1"
ZONE2_SWITCH_SOURCE_NON_NET = "audio2"
ZONE3_SWITCH_SOURCE_NON_NET = "audio3"
ZONE4_SWITCH_SOURCE_NON_NET = "audio4"
ZONE_SWITCH_SOURCE_NON_NET = {
    "main": MAIN_SWITCH_SOURCE_NON_NET,
    "zone2": ZONE2_SWITCH_SOURCE_NON_NET,
    "zone3": ZONE3_SWITCH_SOURCE_NON_NET,
    "zone4": ZONE4_SWITCH_SOURCE_NON_NET,
}


CONF_NETWORK_SCAN = "network_scan"

POLL_INTERVAL = 30
ZONE_SPLITTER = "___"  # must be url ok

PLAY_TITLE = "Music Assistant"

# MusicCast constants
NULL_GROUP = "00000000000000000000000000000000"
DEFAULT_ZONE = "main"
ATTR_MC_LINK = "mc_link"
ATTR_MAIN_SYNC = "main_sync"
ATTR_MC_LINK_SOURCES = [ATTR_MC_LINK, ATTR_MAIN_SYNC]

MC_PASSIVE_SOURCE_IDS = [
    "napster",
    "spotify",
    "qobuz",
    "tidal",
    "deezer",
    "amazon_music",
    "alexa",
    "airplay",
    # these don't make sense active, as you need to select media
    "usb",
    "server",
]

MC_CONTROL_SOURCE_IDS = MC_PASSIVE_SOURCE_IDS
MC_CONTROL_SOURCE_IDS.extend(
    [
        "net_radio",
        "bluetooth",
        "tuner",
    ]
)
