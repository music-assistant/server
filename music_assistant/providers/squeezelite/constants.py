"""Constants for the Squeezelite player provider."""

from __future__ import annotations

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
    advanced=True,
)
CONF_ENTRY_VISUALIZATION = ConfigEntry(
    key=CONF_VISUALIZATION,
    type=ConfigEntryType.STRING,
    default_value=DEFAULT_VISUALIZATION,
    options=[
        ConfigValueOption(x.value, title=x.name.replace("_", " ").title())
        for x in SlimVisualisationType
    ],
    required=False,
    advanced=True,
    depends_on=CONF_DISPLAY,
)
