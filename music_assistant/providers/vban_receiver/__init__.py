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
            label="Receiver: UDP Port",
            description="The UDP port the VBAN receiver will listen on for connections. "
            "Make sure that this server can be reached "
            "on the given IP and UDP port by remote VBAN senders.",
        ),
        ConfigEntry(
            key=CONF_VBAN_STREAM_NAME,
            type=ConfigEntryType.STRING,
            label="Sender: VBAN Stream Name",
            default_value="Network AUX",
            description="Max 16 ASCII chars.\n"
            "The VBAN stream name to expect from the remote VBAN sender.\n"
            "This MUST match what the remote VBAN sender has set for the session name "
            "otherwise audio streaming will not work.",
            required=True,
            validate=_validate_stream_name,  # type: ignore[arg-type]
        ),
        ConfigEntry(
            key=CONF_SENDER_HOST,
            type=ConfigEntryType.STRING,
            default_value="127.0.0.1",
            label="Sender: VBAN Sender hostname/IP address",
            description="The hostname/IP Address of the remote VBAN SENDER.",
            required=True,
        ),
        ConfigEntry(
            key=CONF_PCM_AUDIO_FORMAT,
            type=ConfigEntryType.STRING,
            default_value=DEFAULT_PCM_AUDIO_FORMAT,
            options=[ConfigValueOption(x, x) for x in get_supported_pcm_formats()],
            label="PCM audio format",
            description="The VBAN PCM audio format to expect from the remote VBAN sender. "
            "This MUST match what the remote VBAN sender has set otherwise audio streaming "
            "will not work.",
            required=True,
        ),
        ConfigEntry(
            key=CONF_PCM_SAMPLE_RATE,
            type=ConfigEntryType.INTEGER,
            default_value=DEFAULT_PCM_SAMPLE_RATE,
            options=[ConfigValueOption(str(x), x) for x in _get_vban_sample_rates()],
            label="PCM sample rate",
            description="The VBAN PCM sample rate to expect from the remote VBAN sender. "
            "This MUST match what the remote VBAN sender has set otherwise audio streaming "
            "will not work.",
            required=True,
        ),
        ConfigEntry(
            key=CONF_AUDIO_CHANNELS,
            type=ConfigEntryType.INTEGER,
            default_value=DEFAULT_AUDIO_CHANNELS,
            options=[ConfigValueOption(str(x), x) for x in list(range(1, 9))],
            label="Channels",
            description="The number of audio channels",
            required=True,
        ),
        ConfigEntry(
            key=CONF_BIND_IP,
            type=ConfigEntryType.STRING,
            default_value="0.0.0.0",
            options=[ConfigValueOption(x, x) for x in {"0.0.0.0", *ip_addresses}],
            label="Receiver: Bind to IP/interface",
            description="Start the VBAN receiver on this specific interface. \n"
            "Use 0.0.0.0 to bind to all interfaces, which is the default. \n"
            "This is an advanced setting that should normally "
            "not be adjusted in regular setups.",
            advanced=True,
            required=True,
        ),
        ConfigEntry(
            key=CONF_VBAN_QUEUE_STRATEGY,
            type=ConfigEntryType.STRING,
            default_value=next(iter(VBAN_QUEUE_STRATEGIES)),
            options=[ConfigValueOption(x, x) for x in VBAN_QUEUE_STRATEGIES],
            label="Receiver: VBAN queue strategy",
            description="What should happen if the receiving queue fills up?",
            advanced=True,
            required=True,
        ),
        ConfigEntry(
            key=CONF_VBAN_QUEUE_SIZE,
            type=ConfigEntryType.INTEGER,
            default_value=AsyncVBANClientMod.default_queue_size,
            label="Receiver: VBAN packets queue size",
            description="This can be increased if MA is running on a very low power device, "
            "otherwise this should not need to be changed.",
            advanced=True,
            required=True,
        ),
        ConfigEntry(
            key=CONF_LOG_VBAN_STREAM_STATS,
            type=ConfigEntryType.BOOLEAN,
            default_value=False,
            label="Log VBAN stream statistics",
            description="Log VBAN stream statistics when DEBUG/VERBOSE logging is enabled. "
            "Target numbers not being hit will result in audio glitches/dropouts and is "
            "indicative of a low power device unable to keep up with the incoming stream rate.",
            advanced=True,
            required=True,
        ),
    )
