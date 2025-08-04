"""Constants for the Squeezelite player provider."""

from __future__ import annotations

from aioslimproto.models import VisualisationType as SlimVisualisationType

CONF_CLI_TELNET_PORT = "cli_telnet_port"
CONF_CLI_JSON_PORT = "cli_json_port"
CONF_DISCOVERY = "discovery"
CONF_PORT = "port"
DEFAULT_SLIMPROTO_PORT = 3483
CONF_DISPLAY = "display"
CONF_VISUALIZATION = "visualization"


DEFAULT_VISUALIZATION = SlimVisualisationType.NONE
