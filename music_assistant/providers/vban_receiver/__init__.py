"""VBAN protocol receiver plugin for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiovban.enums import VBANSampleRate
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_BIND_IP, CONF_BIND_PORT, CONF_ENTRY_WARN_PREVIEW
from music_assistant.helpers.util import get_ip_addresses

from .constants import (
    CONF_AUDIO_CHANNELS,
    CONF_LOG_VBAN_STREAM_STATS,
    CONF_PCM_AUDIO_FORMAT,
    CONF_PCM_SAMPLE_RATE,
    CONF_SENDER_HOST,
    CONF_VBAN_QUEUE_SIZE,
    CONF_VBAN_QUEUE_STRATEGY,
    CONF_VBAN_STREAM_NAME,
    DEFAULT_AUDIO_CHANNELS,
    DEFAULT_PCM_AUDIO_FORMAT,
    DEFAULT_PCM_SAMPLE_RATE,
    DEFAULT_UDP_PORT,
    VBAN_QUEUE_STRATEGIES,
)
from .helpers import get_supported_pcm_formats
from .provider import VBANReceiverProvider
from .vban import AsyncVBANClientMod

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


def _get_vban_sample_rates() -> list[int]:
    """Return supported VBAN sample rates."""
    return [int(member.split("_")[1]) for member in VBANSampleRate.__members__]


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return VBANReceiverProvider(mass, manifest, config)


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
    ip_addresses = await get_ip_addresses(include_ipv6=True)

    def _validate_stream_name(config_value: str) -> bool:
        """Validate stream name."""
        try:
            config_value.encode("ascii")
        except UnicodeEncodeError:
            return False
        return len(config_value) < 17

    return (
        CONF_ENTRY_WARN_PREVIEW,
        ConfigEntry(
            key=CONF_BIND_PORT,
            type=ConfigEntryType.INTEGER,
            default_value=DEFAULT_UDP_PORT,
        ),
        ConfigEntry(
            key=CONF_VBAN_STREAM_NAME,
            type=ConfigEntryType.STRING,
            default_value="Network AUX",
            required=True,
            validate=_validate_stream_name,  # type: ignore[arg-type]
        ),
        ConfigEntry(
            key=CONF_SENDER_HOST,
            type=ConfigEntryType.STRING,
            default_value="127.0.0.1",
            required=True,
        ),
        ConfigEntry(
            key=CONF_PCM_AUDIO_FORMAT,
            type=ConfigEntryType.STRING,
            default_value=DEFAULT_PCM_AUDIO_FORMAT,
            options=[ConfigValueOption(x, title=x) for x in get_supported_pcm_formats()],
            required=True,
        ),
        ConfigEntry(
            key=CONF_PCM_SAMPLE_RATE,
            type=ConfigEntryType.INTEGER,
            default_value=DEFAULT_PCM_SAMPLE_RATE,
            options=[ConfigValueOption(x, title=str(x)) for x in _get_vban_sample_rates()],
            required=True,
        ),
        ConfigEntry(
            key=CONF_AUDIO_CHANNELS,
            type=ConfigEntryType.INTEGER,
            default_value=DEFAULT_AUDIO_CHANNELS,
            options=[ConfigValueOption(x, title=str(x)) for x in list(range(1, 9))],
            required=True,
        ),
        ConfigEntry(
            key=CONF_BIND_IP,
            type=ConfigEntryType.STRING,
            default_value="0.0.0.0",
            options=[ConfigValueOption(x, title=x) for x in {"0.0.0.0", *ip_addresses}],
            advanced=True,
            required=True,
        ),
        ConfigEntry(
            key=CONF_VBAN_QUEUE_STRATEGY,
            type=ConfigEntryType.STRING,
            default_value=next(iter(VBAN_QUEUE_STRATEGIES)),
            options=[ConfigValueOption(x, title=x) for x in VBAN_QUEUE_STRATEGIES],
            advanced=True,
            required=True,
        ),
        ConfigEntry(
            key=CONF_VBAN_QUEUE_SIZE,
            type=ConfigEntryType.INTEGER,
            default_value=AsyncVBANClientMod.default_queue_size,
            advanced=True,
            required=True,
        ),
        ConfigEntry(
            key=CONF_LOG_VBAN_STREAM_STATS,
            type=ConfigEntryType.BOOLEAN,
            default_value=False,
            advanced=True,
            required=True,
        ),
    )
