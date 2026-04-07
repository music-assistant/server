"""
DLNA Receiver — Music Assistant Plugin Provider.

Exposes Music Assistant as a UPnP/DLNA MediaRenderer so that external
applications (Qobuz, BubbleUPnP, foobar2000, mconnect, etc.) can discover
and cast audio streams to any MA player.

Architecture
~~~~~~~~~~~~
1. SSDP advertisement  — announces virtual MediaRenderers on the LAN
2. UPnP HTTP server     — serves device/service XML descriptions and
                          accepts SOAP control actions (AVTransport,
                          RenderingControl, ConnectionManager)
3. PluginSource bridge  — received audio URL is fed into the MA streaming
                          pipeline as a PluginSource, routed to the
                          corresponding target player

Multi-player mode
~~~~~~~~~~~~~~~~~
When ``target_players`` contains multiple comma-separated player_id values
(or the special value ``*``), the provider creates one virtual DLNA
renderer per player, each with a unique UDN and HTTP port.  DLNA control
points see each renderer as a separate device — e.g.
"Music Assistant — Kitchen", "Music Assistant — Living Room".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import ConfigEntryType

from .constants import (
    CONF_BIND_IP,
    CONF_FRIENDLY_NAME,
    CONF_HTTP_PORT,
    CONF_TARGET_PLAYERS,
    DEFAULT_FRIENDLY_NAME,
    DEFAULT_HTTP_PORT,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    return (
        ConfigEntry(
            key=CONF_FRIENDLY_NAME,
            type=ConfigEntryType.STRING,
            label="Friendly name prefix",
            description=(
                "Prefix for DLNA renderer names shown on the network. "
                "Player name is appended automatically in multi-player mode."
            ),
            default_value=DEFAULT_FRIENDLY_NAME,
            required=True,
        ),
        ConfigEntry(
            key=CONF_TARGET_PLAYERS,
            type=ConfigEntryType.STRING,
            label="Target players",
            description=(
                "Comma-separated MA player_ids to expose as DLNA renderers. "
                'Use "*" to auto-create a renderer for every MA player. '
                "Leave empty for a single renderer without a fixed target."
            ),
            required=False,
        ),
        ConfigEntry(
            key=CONF_BIND_IP,
            type=ConfigEntryType.STRING,
            label="Bind IP address",
            description=(
                "IP address to bind the UPnP HTTP server and SSDP listener. "
                "Leave empty to auto-detect."
            ),
            required=False,
        ),
        ConfigEntry(
            key=CONF_HTTP_PORT,
            type=ConfigEntryType.INTEGER,
            label="HTTP base port",
            description=(
                "Base port for UPnP HTTP servers. In multi-player mode, "
                "each renderer uses an incrementing port (8298, 8299, …)."
            ),
            default_value=DEFAULT_HTTP_PORT,
            required=True,
        ),
    )


async def setup(
    mass: MusicAssistant,
    manifest: ProviderManifest,
    config: ProviderConfig,
) -> ProviderInstanceType:
    """Set up the DLNA Receiver provider."""
    from .provider import DLNAReceiverProvider

    return DLNAReceiverProvider(mass, manifest, config)
