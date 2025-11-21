"""Constants for the MusicCast provider."""

from music_assistant.constants import (
    CONF_ENTRY_FLOW_MODE_HIDDEN_DISABLED,
    CONF_ENTRY_HTTP_PROFILE_DEFAULT_2,
    CONF_ENTRY_ICY_METADATA_HIDDEN_DISABLED,
    CONF_ENTRY_OUTPUT_CODEC,
    create_sample_rates_config_entry,
)

# Constants for players
# both the http profile and icy didn't matter for me testing it.
PLAYER_CONFIG_ENTRIES = [
    CONF_ENTRY_OUTPUT_CODEC,
    CONF_ENTRY_HTTP_PROFILE_DEFAULT_2,
    CONF_ENTRY_ICY_METADATA_HIDDEN_DISABLED,
    CONF_ENTRY_FLOW_MODE_HIDDEN_DISABLED,
    create_sample_rates_config_entry(max_sample_rate=192000, max_bit_depth=24),
]
# player id is {device_id}{ZONE_SPLITTER}{zone_name}
PLAYER_ZONE_SPLITTER = "___"  # must be url ok

# Switch to these non netusb sources when leaving a group as a dev
# with multiple zones. Optionally turn device off.
CONF_PLAYER_HANDLE_SOURCE_DISABLED = "handle_source_allowed"
CONF_PLAYER_SWITCH_SOURCE_NON_NET = "main_switch_source"
CONF_PLAYER_TURN_OFF_ON_LEAVE = "turn_off_on_leave"
MAIN_SWITCH_SOURCE_NON_NET = "audio1"
PLAYER_ZONE2_SWITCH_SOURCE_NON_NET = "audio2"
PLAYER_ZONE3_SWITCH_SOURCE_NON_NET = "audio3"
PLAYER_ZONE4_SWITCH_SOURCE_NON_NET = "audio4"


# MusicCast constants
MC_POLL_INTERVAL = 10
MC_PLAY_TITLE = "Music Assistant"

MC_DEVICE_INFO_ENDPOINT = "YamahaExtendedControl/v1/system/getDeviceInfo"
MC_DEVICE_UPNP_ENDPOINT = "MediaRenderer/desc.xml"
# if this is not a constant, we'll have to do some xml parsing
MC_DEVICE_UPNP_CTRL_ENDPOINT = "AVTransport/ctrl"
MC_DEVICE_UPNP_PORT = 49154
MC_NULL_GROUP = "00000000000000000000000000000000"
MC_DEFAULT_ZONE = "main"

MC_SOURCE_MC_LINK = "mc_link"
MC_SOURCE_MAIN_SYNC = "main_sync"
MC_LINK_SOURCES = [MC_SOURCE_MC_LINK, MC_SOURCE_MAIN_SYNC]

MC_PASSIVE_SOURCE_IDS = [MC_SOURCE_MC_LINK]
MC_NETUSB_SOURCE_IDS = [
    "napster",
    "spotify",
    "qobuz",
    "tidal",
    "deezer",
    "amazon_music",
    "alexa",
    "airplay",
    "usb",
    "server",
    "net_radio",
    "bluetooth",
    # these were in aiomusiccast/musiccast_media_content.py:
    "pandora",
    "rhapsody",
    "siriusxm",
    "juke",
    "radiko",
]
MC_CONTROL_SOURCE_IDS = MC_NETUSB_SOURCE_IDS
MC_CONTROL_SOURCE_IDS.append(
    # tuner can be controlled, will change the station
    "tuner",
)
