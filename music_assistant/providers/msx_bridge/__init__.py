"""
MSX Bridge Player Provider for Music Assistant.

Streams music to Smart TVs via the Media Station X (MSX) app.
Runs an embedded HTTP server that MSX connects to for library browsing,
playback control, and audio streaming.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, ProviderFeature

from .constants import (
    CONF_ABORT_STREAM_FIRST,
    CONF_ENABLE_GROUPING,
    CONF_GROUP_STREAM_MODE,
    CONF_HTTP_PORT,
    CONF_OUTPUT_FORMAT,
    CONF_PLAYER_IDLE_TIMEOUT,
    CONF_SHOW_STOP_NOTIFICATION,
    DEFAULT_ABORT_STREAM_FIRST,
    DEFAULT_ENABLE_GROUPING,
    DEFAULT_GROUP_STREAM_MODE,
    DEFAULT_HTTP_PORT,
    DEFAULT_OUTPUT_FORMAT,
    DEFAULT_PLAYER_IDLE_TIMEOUT,
    DEFAULT_SHOW_STOP_NOTIFICATION,
    GROUP_STREAM_MODE_INDEPENDENT,
    GROUP_STREAM_MODE_SHARED,
)
from .provider import MSXBridgeProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    grouping_enabled = bool(config.get_value(CONF_ENABLE_GROUPING, DEFAULT_ENABLE_GROUPING))
    features: set[ProviderFeature] = {ProviderFeature.REMOVE_PLAYER}
    if grouping_enabled:
        features.add(ProviderFeature.SYNC_PLAYERS)
    return MSXBridgeProvider(mass, manifest, config, features)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    return (
        ConfigEntry(
            key=CONF_HTTP_PORT,
            type=ConfigEntryType.INTEGER,
            label="HTTP Server Port",
            required=True,
            default_value=str(DEFAULT_HTTP_PORT),
            description="Port for the MSX HTTP server.",
        ),
        ConfigEntry(
            key=CONF_OUTPUT_FORMAT,
            type=ConfigEntryType.STRING,
            label="Audio Output Format",
            required=True,
            default_value=DEFAULT_OUTPUT_FORMAT,
            description="Audio format for streaming to MSX (mp3, aac, or flac).",
        ),
        ConfigEntry(
            key=CONF_PLAYER_IDLE_TIMEOUT,
            type=ConfigEntryType.INTEGER,
            label="Player Idle Timeout (minutes)",
            required=True,
            default_value=str(DEFAULT_PLAYER_IDLE_TIMEOUT),
            description="Unregister MSX players after this many minutes without activity.",
        ),
        ConfigEntry(
            key=CONF_SHOW_STOP_NOTIFICATION,
            type=ConfigEntryType.BOOLEAN,
            label="Show notification before closing player",
            required=False,
            default_value=DEFAULT_SHOW_STOP_NOTIFICATION,
            description="Show confirmation dialog on MSX when stopping playback from MA.",
        ),
        ConfigEntry(
            key=CONF_ABORT_STREAM_FIRST,
            type=ConfigEntryType.BOOLEAN,
            label="Abort stream before broadcast stop",
            required=False,
            default_value=DEFAULT_ABORT_STREAM_FIRST,
            description=(
                "When stopping: abort stream first, then send WebSocket stop. "
                "May stop playback faster on some TVs."
            ),
        ),
        ConfigEntry(
            key=CONF_ENABLE_GROUPING,
            type=ConfigEntryType.BOOLEAN,
            label="Enable player grouping (experimental)",
            required=False,
            default_value=DEFAULT_ENABLE_GROUPING,
            description=(
                "Experimental: allow grouping multiple MSX TVs to play the same track "
                "simultaneously. Disable if you experience issues with multi-TV setups."
            ),
        ),
        ConfigEntry(
            key=CONF_GROUP_STREAM_MODE,
            type=ConfigEntryType.STRING,
            label="Group Stream Mode",
            required=False,
            default_value=DEFAULT_GROUP_STREAM_MODE,
            options=[
                ConfigValueOption(
                    "Independent (default) - each TV has own stream",
                    GROUP_STREAM_MODE_INDEPENDENT,
                ),
                ConfigValueOption(
                    "Shared Buffer - one ffmpeg, multiple readers (less CPU)",
                    GROUP_STREAM_MODE_SHARED,
                ),
            ],
            description=(
                "How to stream audio to grouped players. "
                "'Independent' creates separate streams per TV (more CPU, no sync). "
                "'Shared Buffer' uses one ffmpeg process for all group members "
                "(less CPU, better sync)."
            ),
        ),
    )
