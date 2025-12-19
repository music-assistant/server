"""Snapcast Player provider for Music Assistant."""

import re

from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
    ProviderConfig,
)
from music_assistant_models.enums import ConfigEntryType, ProviderFeature
from music_assistant_models.errors import SetupFailedError
from music_assistant_models.provider import ProviderManifest

from music_assistant.helpers.process import check_output
from music_assistant.mass import MusicAssistant
from music_assistant.models import ProviderInstanceType
from music_assistant.providers.snapcast.constants import (
    CONF_CATEGORY_ADVANCED,
    CONF_CATEGORY_BUILT_IN,
    CONF_CATEGORY_GENERIC,
    CONF_HELP_LINK,
    CONF_SERVER_BUFFER_SIZE,
    CONF_SERVER_CHUNK_MS,
    CONF_SERVER_CONTROL_PORT,
    CONF_SERVER_HOST,
    CONF_SERVER_INITIAL_VOLUME,
    CONF_SERVER_SEND_AUDIO_TO_MUTED,
    CONF_SERVER_TRANSPORT_CODEC,
    CONF_STREAM_IDLE_THRESHOLD,
    CONF_USE_EXTERNAL_SERVER,
    DEFAULT_SNAPSERVER_IP,
    DEFAULT_SNAPSERVER_PORT,
    DEFAULT_SNAPSTREAM_IDLE_THRESHOLD,
)
from music_assistant.providers.snapcast.provider import SnapCastProvider

SUPPORTED_FEATURES = {
    ProviderFeature.SYNC_PLAYERS,
    ProviderFeature.REMOVE_PLAYER,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return SnapCastProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    :param instance_id: id of an existing provider instance (None if new instance setup).
    :param action: [optional] action key called from config entries UI.
    :param values: the (intermediate) raw values for config entries sent with the action.
    """
    returncode, output = await check_output("snapserver", "-v")
    snapserver_version = -1
    if returncode == 0:
        # Parse version from output, handling potential noise from library warnings
        # Expected format: "0.27.0" or similar version string
        output_str = output.decode()
        if version_match := re.search(r"(\d+)\.(\d+)\.(\d+)", output_str):
            snapserver_version = int(version_match.group(2))
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
            label="Snapserver buffer size",
            required=False,
            category=CONF_CATEGORY_BUILT_IN,
            hidden=not local_snapserver_present,
            depends_on=CONF_USE_EXTERNAL_SERVER,
            depends_on_value_not=True,
            help_link=CONF_HELP_LINK,
        ),
        ConfigEntry(
            key=CONF_SERVER_CHUNK_MS,
            type=ConfigEntryType.INTEGER,
            range=(10, 100),
            default_value=26,
            label="Snapserver chunk size",
            required=False,
            category=CONF_CATEGORY_BUILT_IN,
            hidden=not local_snapserver_present,
            depends_on=CONF_USE_EXTERNAL_SERVER,
            depends_on_value_not=True,
            help_link=CONF_HELP_LINK,
        ),
        ConfigEntry(
            key=CONF_SERVER_INITIAL_VOLUME,
            type=ConfigEntryType.INTEGER,
            range=(0, 100),
            default_value=25,
            label="Snapserver initial volume",
            required=False,
            category=CONF_CATEGORY_BUILT_IN,
            hidden=not local_snapserver_present,
            depends_on=CONF_USE_EXTERNAL_SERVER,
            depends_on_value_not=True,
            help_link=CONF_HELP_LINK,
        ),
        ConfigEntry(
            key=CONF_SERVER_SEND_AUDIO_TO_MUTED,
            type=ConfigEntryType.BOOLEAN,
            default_value=False,
            label="Send audio to muted clients",
            required=False,
            category=CONF_CATEGORY_BUILT_IN,
            hidden=not local_snapserver_present,
            depends_on=CONF_USE_EXTERNAL_SERVER,
            depends_on_value_not=True,
            help_link=CONF_HELP_LINK,
        ),
        ConfigEntry(
            key=CONF_SERVER_TRANSPORT_CODEC,
            type=ConfigEntryType.STRING,
            options=[
                ConfigValueOption(
                    title="FLAC",
                    value="flac",
                ),
                ConfigValueOption(
                    title="OGG",
                    value="ogg",
                ),
                ConfigValueOption(
                    title="OPUS",
                    value="opus",
                ),
                ConfigValueOption(
                    title="PCM",
                    value="pcm",
                ),
            ],
            default_value="flac",
            label="Snapserver default transport codec",
            required=False,
            category=CONF_CATEGORY_BUILT_IN,
            hidden=not local_snapserver_present,
            depends_on=CONF_USE_EXTERNAL_SERVER,
            depends_on_value_not=True,
            help_link=CONF_HELP_LINK,
        ),
        ConfigEntry(
            key=CONF_USE_EXTERNAL_SERVER,
            type=ConfigEntryType.BOOLEAN,
            default_value=not local_snapserver_present,
            label="Use existing Snapserver",
            required=False,
            category=(
                CONF_CATEGORY_ADVANCED if local_snapserver_present else CONF_CATEGORY_GENERIC
            ),
        ),
        ConfigEntry(
            key=CONF_SERVER_HOST,
            type=ConfigEntryType.STRING,
            default_value=DEFAULT_SNAPSERVER_IP,
            label="Snapcast server ip",
            required=False,
            depends_on=CONF_USE_EXTERNAL_SERVER,
            category=(
                CONF_CATEGORY_ADVANCED if local_snapserver_present else CONF_CATEGORY_GENERIC
            ),
        ),
        ConfigEntry(
            key=CONF_SERVER_CONTROL_PORT,
            type=ConfigEntryType.INTEGER,
            default_value=DEFAULT_SNAPSERVER_PORT,
            label="Snapcast control port",
            required=False,
            depends_on=CONF_USE_EXTERNAL_SERVER,
            category=(
                CONF_CATEGORY_ADVANCED if local_snapserver_present else CONF_CATEGORY_GENERIC
            ),
        ),
        ConfigEntry(
            key=CONF_STREAM_IDLE_THRESHOLD,
            type=ConfigEntryType.INTEGER,
            default_value=DEFAULT_SNAPSTREAM_IDLE_THRESHOLD,
            label="Snapcast idle threshold stream parameter",
            required=True,
            category=CONF_CATEGORY_ADVANCED,
        ),
    )
