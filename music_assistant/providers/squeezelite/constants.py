"""Constants for the Squeezelite player provider."""

from __future__ import annotations

from dataclasses import dataclass

from aioslimproto.client import PlayerState as SlimPlayerState
from aioslimproto.models import VisualisationType as SlimVisualisationType
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, PlaybackState, RepeatMode

CONF_CLI_TELNET_PORT = "cli_telnet_port"
CONF_CLI_JSON_PORT = "cli_json_port"
CONF_DISCOVERY = "discovery"
CONF_PORT = "port"
DEFAULT_SLIMPROTO_PORT = 3483
CONF_DISPLAY = "display"
CONF_VISUALIZATION = "visualization"

DEFAULT_PLAYER_VOLUME = 20
DEFAULT_VISUALIZATION = SlimVisualisationType.NONE

# sync constants
MIN_DEVIATION_ADJUST = 8  # 5 milliseconds
MIN_REQ_PLAYPOINTS = 8  # we need at least 8 measurements
DEVIATION_JUMP_IGNORE = 500  # ignore a sudden unrealistic jump
MAX_SKIP_AHEAD_MS = 800  # 0.8 seconds

STATE_MAP = {
    SlimPlayerState.BUFFERING: PlaybackState.PLAYING,
    SlimPlayerState.BUFFER_READY: PlaybackState.PLAYING,
    SlimPlayerState.PAUSED: PlaybackState.PAUSED,
    SlimPlayerState.PLAYING: PlaybackState.PLAYING,
    SlimPlayerState.STOPPED: PlaybackState.IDLE,
}

REPEATMODE_MAP = {RepeatMode.OFF: 0, RepeatMode.ONE: 1, RepeatMode.ALL: 2}

CONF_ENTRY_DISPLAY = ConfigEntry(
    key=CONF_DISPLAY,
    type=ConfigEntryType.BOOLEAN,
    default_value=False,
    required=False,
    label="Enable display support",
    description="Enable/disable native display support on squeezebox or squeezelite32 hardware.",
    category="advanced",
)
CONF_ENTRY_VISUALIZATION = ConfigEntry(
    key=CONF_VISUALIZATION,
    type=ConfigEntryType.STRING,
    default_value=DEFAULT_VISUALIZATION,
    options=[
        ConfigValueOption(title=x.name.replace("_", " ").title(), value=x.value)
        for x in SlimVisualisationType
    ],
    required=False,
    label="Visualization type",
    description="The type of visualization to show on the display "
    "during playback if the device supports this.",
    category="advanced",
    depends_on=CONF_DISPLAY,
)


@dataclass
class SyncPlayPoint:
    """Simple structure to describe a Sync Playpoint."""

    timestamp: float
    sync_master: str
    diff: int
