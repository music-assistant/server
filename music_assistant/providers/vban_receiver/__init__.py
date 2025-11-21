"""VBAN protocol receiver plugin for Music Assistant."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import TYPE_CHECKING, cast

from aiovban.asyncio.util import BackPressureStrategy
from aiovban.enums import VBANSampleRate
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import SetupFailedError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamMetadata

from music_assistant.constants import (
    CONF_BIND_IP,
    CONF_BIND_PORT,
    CONF_ENTRY_WARN_PREVIEW,
)
from music_assistant.helpers.util import (
    get_ip_addresses,
)
from music_assistant.models.plugin import PluginProvider, PluginSource

from .vban import AsyncVBANClientMod

if TYPE_CHECKING:
    from aiovban.asyncio.device import VBANDevice
    from aiovban.asyncio.streams import VBANIncomingStream
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

DEFAULT_UDP_PORT = 6980
DEFAULT_PCM_AUDIO_FORMAT = "S16LE"
DEFAULT_PCM_SAMPLE_RATE = 44100
DEFAULT_AUDIO_CHANNELS = 2

CONF_VBAN_STREAM_NAME = "vban_stream_name"
CONF_SENDER_HOST = "sender_host"
CONF_PCM_AUDIO_FORMAT = "audio_format"
CONF_PCM_SAMPLE_RATE = "sample_rate"
CONF_AUDIO_CHANNELS = "audio_channels"
CONF_VBAN_QUEUE_STRATEGY = "vban_queue_strategy"
CONF_VBAN_QUEUE_SIZE = "vban_queue_size"

VBAN_QUEUE_STRATEGIES = {
    "Clear entire queue": BackPressureStrategy.DROP,
    "Clear the oldest half of the queue": BackPressureStrategy.DRAIN_OLDEST,
    "Remove single oldest queue entry": BackPressureStrategy.POP,
}

SUPPORTED_FEATURES = {ProviderFeature.AUDIO_SOURCE}


def _get_supported_pcm_formats() -> dict[str, int]:
    """Return supported PCM formats."""
    pcm_formats = {}
    for content_type in ContentType.__members__:
        if match := re.match(r"PCM_([S|F](\d{2})LE)", content_type):
            pcm_formats[match.group(1)] = int(match.group(2))
    return pcm_formats


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
    ip_addresses = await get_ip_addresses()

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
            options=[ConfigValueOption(x, x) for x in _get_supported_pcm_formats()],
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
            category="advanced",
            required=True,
        ),
        ConfigEntry(
            key=CONF_VBAN_QUEUE_STRATEGY,
            type=ConfigEntryType.STRING,
            default_value=next(iter(VBAN_QUEUE_STRATEGIES)),
            options=[ConfigValueOption(x, x) for x in VBAN_QUEUE_STRATEGIES],
            label="Receiver: VBAN queue strategy",
            description="What should happen if the receiving queue fills up?",
            category="advanced",
            required=True,
        ),
        ConfigEntry(
            key=CONF_VBAN_QUEUE_SIZE,
            type=ConfigEntryType.INTEGER,
            default_value=AsyncVBANClientMod.default_queue_size,
            label="Receiver: VBAN packets queue size",
            description="This can be increased if MA is running on a very low power device, "
            "otherwise this should not need to be changed.",
            category="advanced",
            required=True,
        ),
    )


class VBANReceiverProvider(PluginProvider):
    """Implementation of a VBAN protocol receiver plugin."""

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize MusicProvider."""
        super().__init__(mass, manifest, config, SUPPORTED_FEATURES)
        self._bind_port: int = cast("int", self.config.get_value(CONF_BIND_PORT))
        self._bind_ip: str = cast("str", self.config.get_value(CONF_BIND_IP))
        self._sender_host: str = cast("str", self.config.get_value(CONF_SENDER_HOST))
        self._vban_stream_name: str = cast("str", self.config.get_value(CONF_VBAN_STREAM_NAME))
        self._pcm_audio_format: str = cast("str", self.config.get_value(CONF_PCM_AUDIO_FORMAT))
        self._pcm_sample_rate: int = cast("int", self.config.get_value(CONF_PCM_SAMPLE_RATE))
        self._audio_channels: int = cast("int", self.config.get_value(CONF_AUDIO_CHANNELS))
        self._vban_queue_strategy: BackPressureStrategy = VBAN_QUEUE_STRATEGIES[
            cast("str", self.config.get_value(CONF_VBAN_QUEUE_STRATEGY))
        ]
        self._vban_queue_size: int = cast("int", self.config.get_value(CONF_VBAN_QUEUE_SIZE))

        self._vban_receiver: AsyncVBANClientMod | None = None
        self._vban_sender: VBANDevice | None = None
        self._vban_stream: VBANIncomingStream | None = None

        self._source_details = PluginSource(
            id=self.instance_id,
            name=f"{self.manifest.name}: {self._vban_stream_name}",
            passive=False,
            can_play_pause=False,
            can_seek=False,
            can_next_previous=False,
            audio_format=AudioFormat(
                content_type=ContentType(self._pcm_audio_format.lower()),
                codec_type=ContentType(self._pcm_audio_format.lower()),
                sample_rate=self._pcm_sample_rate,
                bit_depth=_get_supported_pcm_formats()[self._pcm_audio_format],
                channels=self._audio_channels,
            ),
            metadata=StreamMetadata(
                title=self._vban_stream_name,
                artist=self._sender_host,
            ),
            stream_type=StreamType.CUSTOM,
        )

    @property
    def instance_name_postfix(self) -> str | None:
        """Return a (default) instance name postfix for this provider instance."""
        return self._vban_stream_name

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._vban_receiver = AsyncVBANClientMod(
            default_queue_size=self._vban_queue_size, ignore_audio_streams=False
        )
        try:
            self._udp_socket_task = asyncio.create_task(
                self._vban_receiver.listen(
                    address=self._bind_ip, port=self._bind_port, controller=self
                )
            )
        except OSError as err:
            raise SetupFailedError(f"Failed to start VBAN receiver plugin: {err}") from err

        self._vban_sender = self._vban_receiver.register_device(self._sender_host)
        if self._vban_sender:
            self._vban_stream = self._vban_sender.receive_stream(
                self._vban_stream_name, back_pressure_strategy=self._vban_queue_strategy
            )

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
        self.logger.debug("Unloading plugin")
        if self._vban_receiver:
            self.logger.debug("Closing UDP transport")
            self._vban_receiver.close()
            with suppress(asyncio.CancelledError):
                await self._udp_socket_task

        self._vban_receiver = None
        self._vban_sender = None
        self._vban_stream = None
        await asyncio.sleep(0.1)

    def get_source(self) -> PluginSource:
        """Get (audio)source details for this plugin."""
        return self._source_details

    @property
    def active_player(self) -> bool:
        """Report the active player status."""
        return bool(self._source_details.in_use_by)

    async def get_audio_stream(self, player_id: str) -> AsyncGenerator[bytes, None]:
        """Yield raw PCM chunks from the VBANIncomingStream queue."""
        self.logger.debug(
            "Getting VBAN PCM audio stream for Player: %s//Stream: %s//Config: %s",
            player_id,
            self._vban_stream_name,
            self._source_details.audio_format.output_format_str,
        )
        while (
            self._source_details.in_use_by
            and self._vban_stream
            and not self._udp_socket_task.done()
        ):
            try:
                packet = await self._vban_stream.get_packet()
            except asyncio.QueueShutDown:  # type: ignore[attr-defined]
                self.logger.error(
                    "Found VBANIncomingStream queue shut down when attempting to get VBAN packet"
                )
                break

            yield packet.body.data
