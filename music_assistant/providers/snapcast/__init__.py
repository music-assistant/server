"""Snapcast Player provider for Music Assistant."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import SetupFailedError

from music_assistant.helpers.process import check_output

from .provider import SnapcastPlayerProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType


# Configuration constants
CONF_SERVER_BUFFER_SIZE = "snapcast_server_built_in_buffer_size"
CONF_SERVER_CHUNK_MS = "snapcast_server_built_in_chunk_ms"
CONF_SERVER_INITIAL_VOLUME = "snapcast_server_built_in_initial_volume"
CONF_SERVER_TRANSPORT_CODEC = "snapcast_server_built_in_codec"
CONF_SERVER_SEND_AUDIO_TO_MUTED = "snapcast_server_built_in_send_muted"
CONF_STREAM_IDLE_THRESHOLD = "snapcast_stream_idle_threshold"
CONF_USE_EXTERNAL_SERVER = "snapcast_use_external_server"
CONF_SERVER_HOST = "snapcast_server_host"
CONF_SERVER_CONTROL_PORT = "snapcast_server_control_port"


class SnapCastStreamType(StrEnum):
    """Enum for Snapcast Stream Type."""

    MUSIC = "MUSIC"
    ANNOUNCEMENT = "ANNOUNCEMENT"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return SnapcastPlayerProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    returncode, output = await check_output("snapserver", "-v")
    snapserver_version = int(output.decode().split(".")[1]) if returncode == 0 else -1
    local_snapserver_present = snapserver_version >= 27 and snapserver_version != 30
    if returncode == 0 and not local_snapserver_present:
        raise SetupFailedError(
            f"Invalid snapserver version. Expected >= 27 and != 30, got {snapserver_version}"
        )

    return (
        ConfigEntry(
            key=CONF_SERVER_BUFFER_SIZE,
            type=ConfigEntryType.INTEGER,
            range=(200, 6000),
            default_value=1000,
            label="Buffer size (ms)",
            description="Buffer size in milliseconds for snapcast server",
        ),
        ConfigEntry(
            key=CONF_USE_EXTERNAL_SERVER,
            type=ConfigEntryType.BOOLEAN,
            default_value=False,
            label="Use external snapcast server",
            description="Use an external snapcast server instead of the built-in one",
        ),
        ConfigEntry(
            key=CONF_SERVER_HOST,
            type=ConfigEntryType.STRING,
            default_value="127.0.0.1",
            label="Server host",
            description="Host address of external snapcast server",
            depends_on=CONF_USE_EXTERNAL_SERVER,
        ),
        ConfigEntry(
            key=CONF_SERVER_CONTROL_PORT,
            type=ConfigEntryType.INTEGER,
            default_value=1705,
            label="Server control port",
            description="Control port of external snapcast server",
            depends_on=CONF_USE_EXTERNAL_SERVER,
        ),
    )
