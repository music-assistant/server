"""Base/builtin provider with support for players using slimproto."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, ProviderFeature

from music_assistant.constants import CONF_PORT

from .constants import (
    CONF_CLI_JSON_PORT,
    CONF_CLI_TELNET_PORT,
    CONF_DISCOVERY,
    DEFAULT_SLIMPROTO_PORT,
)
from .provider import SqueezelitePlayerProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.SYNC_PLAYERS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return SqueezelitePlayerProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # ruff: noqa: ARG001
    return (
        ConfigEntry(
            key=CONF_CLI_TELNET_PORT,
            type=ConfigEntryType.INTEGER,
            default_value=9090,
            label="Classic Squeezebox CLI Port",
            description="Some slimproto based players require the presence of the telnet CLI "
            " to request more information. \n\n"
            "By default this CLI is hosted on port 9090 but some players also accept "
            "a different port. Set to 0 to disable this functionality.\n\n"
            "Commands allowed on this interface are very limited and just enough to satisfy "
            "player compatibility, so security risks are minimized to practically zero."
            "You may safely disable this option if you have no players that rely on this feature "
            "or you dont care about the additional metadata.",
            category="advanced",
        ),
        ConfigEntry(
            key=CONF_CLI_JSON_PORT,
            type=ConfigEntryType.INTEGER,
            default_value=9000,
            label="JSON-RPC CLI/API Port",
            description="Some slimproto based players require the presence of the JSON-RPC "
            "API from LMS to request more information. For example to fetch the album cover "
            "and other metadata. \n\n"
            "This JSON-RPC API is compatible with Logitech Media Server but not all commands "
            "are implemented. Just enough to satisfy player compatibility. \n\n"
            "By default this JSON CLI is hosted on port 9000 but most players also accept "
            "it on a different port. Set to 0 to disable this functionality.\n\n"
            "You may safely disable this option if you have no players that rely on this feature "
            "or you dont care about the additional metadata.",
            category="advanced",
        ),
        ConfigEntry(
            key=CONF_DISCOVERY,
            type=ConfigEntryType.BOOLEAN,
            default_value=True,
            label="Enable Discovery server",
            description="Broadcast discovery packets for slimproto clients to automatically "
            "discover and connect to this server. \n\n"
            "You may want to disable this feature if you are running multiple slimproto servers "
            "on your network and/or you don't want clients to auto connect to this server.",
            category="advanced",
        ),
        ConfigEntry(
            key=CONF_PORT,
            type=ConfigEntryType.INTEGER,
            default_value=DEFAULT_SLIMPROTO_PORT,
            label="Slimproto port",
            description="The TCP/UDP port to run the slimproto sockets server. "
            "The default is 3483 and using a different port is not supported by "
            "hardware squeezebox players. Only adjust this port if you want to "
            "use other slimproto based servers side by side with (squeezelite) software players.",
        ),
    )
