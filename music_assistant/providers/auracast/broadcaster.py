"""Bumble and LC3 implementation for an Auracast broadcast."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bumble import core, device, hci, transport
from bumble.core import AdvertisingData
from bumble.profiles import bap, le_audio, pbp
from lc3 import BaseError as LC3Error
from lc3 import Encoder

if TYPE_CHECKING:
    from collections.abc import Awaitable

LOGGER = logging.getLogger(__name__)
LC3_BITRATE = 160_000
CONTROLLER_STACK_SETUP_KEY = "_controller_stack"
TEARDOWN_STEP_TIMEOUT = 2.0
DRAIN_TIMEOUT = 2.0
# Errors raised by Bumble, the serial transport (OSError, including timeouts) and liblc3
BROADCAST_ERRORS = (core.BaseBumbleError, LC3Error, OSError)
REQUIRED_LE_FEATURES = {
    "LE Periodic Advertising": hci.LeFeatureMask.LE_PERIODIC_ADVERTISING,
    "LE Extended Advertising": hci.LeFeatureMask.LE_EXTENDED_ADVERTISING,
    "Isochronous Broadcaster": hci.LeFeatureMask.ISOCHRONOUS_BROADCASTER,
}
REQUIRED_HCI_COMMANDS = {
    "LE Create BIG": hci.HCI_LE_CREATE_BIG_COMMAND,
    "LE Terminate BIG": hci.HCI_LE_TERMINATE_BIG_COMMAND,
    "LE Setup ISO Data Path": hci.HCI_LE_SETUP_ISO_DATA_PATH_COMMAND,
    "LE Read Buffer Size V2": hci.HCI_LE_READ_BUFFER_SIZE_V2_COMMAND,
}


@dataclass(frozen=True, slots=True)
class ControllerProbeResult:
    """HCI capabilities and identity discovered on a controller."""

    missing_support: tuple[str, ...]
    controller_stack: str | None

    @property
    def supports_auracast(self) -> bool:
        """Return whether the controller can transmit an Auracast broadcast."""
        return not self.missing_support


def find_missing_auracast_support(controller_device: device.Device) -> tuple[str, ...]:
    """
    Return the LE features and HCI commands a powered-on controller lacks for broadcasting.

    :param controller_device: A Bumble device whose host completed controller initialization.
    """
    missing = [
        name
        for name, feature in REQUIRED_LE_FEATURES.items()
        if not controller_device.supports_le_features(feature)
    ]
    missing.extend(
        name
        for name, op_code in REQUIRED_HCI_COMMANDS.items()
        if not controller_device.host.supports_command(op_code)
    )
    return tuple(missing)


async def probe_controller(serial_port: str, baudrate: int) -> ControllerProbeResult:
    """
    Probe a serial port for a usable Auracast controller.

    Raises the Bumble, serial or timeout errors when the controller cannot be opened.

    :param serial_port: Path to the controller's UART device.
    :param baudrate: UART baud rate.
    """
    controller_transport: Any = None
    controller_device: device.Device | None = None
    try:
        LOGGER.debug("Opening Auracast controller probe on %s at %d baud", serial_port, baudrate)
        controller_transport = await transport.open_transport(f"serial:{serial_port},{baudrate}")
        hci_source, hci_sink = controller_transport
        controller_device = device.Device.from_config_with_hci(
            device.DeviceConfiguration(name="Auracast setup probe"), hci_source, hci_sink
        )
        await asyncio.wait_for(controller_device.power_on(), timeout=5)
        missing_support = find_missing_auracast_support(controller_device)
        version = controller_device.host.local_version
        controller_stack = _format_controller_stack(version) if version is not None else None
        LOGGER.debug(
            "Auracast controller probe on %s completed: missing support=%s, "
            "HCI version=%s, controller stack=%s",
            serial_port,
            missing_support,
            version,
            controller_stack,
        )
        return ControllerProbeResult(missing_support, controller_stack)
    finally:
        if controller_device:
            await _teardown_step(controller_device.power_off(), "powering off probed controller")
        if controller_transport:
            await _teardown_step(controller_transport.close(), "closing probed transport")


def parse_manufacturer_data(value: str | None) -> dict[int, bytes] | None:
    """
    Parse semicolon-separated company IDs and hexadecimal payloads.

    :param value: Entries in ``COMPANY_ID:HEX_DATA`` format, separated by semicolons.
    """
    if not value or not value.strip():
        return None

    entries: dict[int, bytes] = {}
    for raw_entry in value.split(";"):
        entry = raw_entry.strip()
        if not entry:
            raise ValueError("Manufacturer data contains an empty entry")
        company_id_text, separator, hex_data = entry.partition(":")
        if not separator or not company_id_text.strip() or not hex_data.strip():
            raise ValueError("Manufacturer data must use COMPANY_ID:HEX_DATA format")
        try:
            company_id = int(company_id_text.strip(), 0)
        except ValueError as err:
            raise ValueError(f"Invalid manufacturer company ID: {company_id_text}") from err
        if not 0 <= company_id <= 0xFFFF:
            raise ValueError("Manufacturer company ID must be between 0 and 65535")
        try:
            payload = bytes.fromhex(hex_data)
        except ValueError as err:
            raise ValueError("Manufacturer payload must contain valid hexadecimal data") from err
        if len(payload) > 252:
            raise ValueError("Manufacturer payload cannot exceed 252 bytes")
        entries[company_id] = payload
    return entries


@dataclass(slots=True)
class BroadcastConfig:
    """Settings used to create the Bluetooth LE Audio broadcast."""

    name: str
    transport_spec: str
    manufacturer_data: dict[int, bytes] | None = None
    broadcast_id: int = 0
    sample_rate: int = 48_000
    frame_duration_us: int = 10_000
    channels: int = 2
    presentation_delay_us: int = 40_000
    max_transport_latency_ms: int = 65


class AuracastBroadcaster:
    """Create a Bumble BIG and submit interleaved PCM as LC3 ISO packets."""

    def __init__(self, config: BroadcastConfig) -> None:
        """Initialize the broadcaster with its settings."""
        self.config = config
        self._device: device.Device | None = None
        self.controller_stack: str | None = None
        self._transport: Any = None
        self._advertising_set: device.AdvertisingSet | None = None
        self._big: device.Big | None = None
        self._streams: list[device.IsoPacketStream] = []
        self._encoder: Encoder | None = None
        self._frame_bytes = 0
        self._bytes_per_bis = 0
        self._samples_per_frame = 0
        self._pcm_buffer = bytearray()

    @property
    def running(self) -> bool:
        """Return whether the broadcast BIG is active."""
        return self._big is not None

    async def start(self) -> None:
        """Open the UART controller and start the Auracast broadcast."""
        if self.running:
            return

        config = self.config
        self._encoder = Encoder(
            frame_duration_us=config.frame_duration_us,
            sample_rate_hz=config.sample_rate,
            num_channels=config.channels,
        )
        self._frame_bytes = self._encoder.get_frame_bytes(LC3_BITRATE)
        self._bytes_per_bis = self._frame_bytes // config.channels
        self._samples_per_frame = config.sample_rate * config.frame_duration_us // 1_000_000
        self._pcm_buffer.clear()

        started = False
        try:
            LOGGER.debug(
                "Starting Auracast broadcast %r over %s: LC3 bitrate=%d, sample_rate=%d, "
                "channels=%d, broadcast_id=0x%06X",
                config.name,
                config.transport_spec,
                LC3_BITRATE,
                config.sample_rate,
                config.channels,
                config.broadcast_id,
            )
            self._transport = await transport.open_transport(config.transport_spec)
            LOGGER.debug("Opened Auracast HCI transport: %s", config.transport_spec)
            hci_source, hci_sink = self._transport
            self._device = device.Device.from_config_with_hci(
                device.DeviceConfiguration(
                    name=config.name, address=hci.Address.generate_static_address()
                ),
                hci_source,
                hci_sink,
            )
            LOGGER.debug("Powering on Bumble host and Bluetooth controller")
            await self._device.power_on()
            LOGGER.debug(
                "Bumble host initialized: controller HCI version=%s, "
                "supported_commands=0x%X, LE_features=%s",
                self._device.host.local_version,
                self._device.host.local_supported_commands,
                self._device.host.local_le_features,
            )
            await self._read_controller_stack()
            self._validate_controller()
            await self._create_broadcast()
            started = True
        finally:
            if not started:
                await self.stop()

    @property
    def queued_seconds(self) -> float:
        """Return the duration of audio submitted but not yet transmitted."""
        if not self._streams:
            return 0.0
        pending_sdus = self._streams[0].data_packet_queue.pending
        return pending_sdus / len(self._streams) * self.config.frame_duration_us / 1_000_000

    async def send_pcm(self, pcm: bytes) -> None:
        """
        Encode complete PCM frames and submit one SDU per BIS.

        :param pcm: Interleaved 16-bit little-endian PCM; a trailing partial frame is buffered.
        """
        if not self.running or self._encoder is None:
            return
        self._pcm_buffer.extend(pcm)
        bytes_per_pcm_frame = self._samples_per_frame * self.config.channels * 2
        while len(self._pcm_buffer) >= bytes_per_pcm_frame:
            pcm_frame = bytes(self._pcm_buffer[:bytes_per_pcm_frame])
            del self._pcm_buffer[:bytes_per_pcm_frame]
            await self._submit_frame(pcm_frame)

    async def drain(self) -> None:
        """Pad and submit any buffered partial frame, then wait until all SDUs are transmitted."""
        if not self.running or self._big is None:
            return
        if self._pcm_buffer:
            bytes_per_pcm_frame = self._samples_per_frame * self.config.channels * 2
            padding = bytes_per_pcm_frame - len(self._pcm_buffer)
            await self.send_pcm(bytes(padding))
        try:
            await asyncio.wait_for(
                asyncio.gather(*(link.drain() for link in self._big.bis_links)),
                timeout=DRAIN_TIMEOUT,
            )
        except TimeoutError:
            LOGGER.warning("Timed out waiting for the Auracast broadcast queue to drain")

    async def stop(self) -> None:
        """Stop the BIG and close the controller transport."""
        big, self._big = self._big, None
        if big:
            await _teardown_step(big.terminate(), "terminating the Auracast BIG")

        advertising_set, self._advertising_set = self._advertising_set, None
        if advertising_set:
            await _teardown_step(advertising_set.stop(), "stopping Auracast advertising")

        current_device, self._device = self._device, None
        if current_device:
            await _teardown_step(current_device.power_off(), "powering off the controller")

        current_transport, self._transport = self._transport, None
        if current_transport:
            await _teardown_step(current_transport.close(), "closing the controller transport")
        self._streams.clear()
        self._encoder = None
        self._pcm_buffer.clear()

    async def _submit_frame(self, pcm_frame: bytes) -> None:
        """Encode one PCM frame with LC3 and write each channel to its BIS."""
        assert self._encoder is not None
        encoded = self._encoder.encode(pcm_frame, num_bytes=self._frame_bytes, bit_depth=16)
        await asyncio.gather(
            *(
                stream.write(
                    encoded[index * self._bytes_per_bis : (index + 1) * self._bytes_per_bis]
                )
                for index, stream in enumerate(self._streams)
            )
        )

    def _validate_controller(self) -> None:
        """Raise when the controller cannot transmit an Auracast broadcast."""
        assert self._device is not None
        if missing := find_missing_auracast_support(self._device):
            raise core.NotSupportedError(
                f"The Bluetooth controller does not support Auracast broadcasting: "
                f"missing {', '.join(missing)}"
            )

    async def _read_controller_stack(self) -> None:
        """Read the Bluetooth controller's HCI version, vendor, and revisions."""
        if self._device is None:
            return
        parameters = self._device.host.local_version
        if parameters is None:
            supports_read_version = self._device.host.supports_command(
                hci.HCI_READ_LOCAL_VERSION_INFORMATION_COMMAND
            )
            LOGGER.debug(
                "Controller local HCI version information is unavailable "
                "(read-version command supported=%s, supported-command-bitmap=0x%X)",
                supports_read_version,
                self._device.host.local_supported_commands,
            )
            return
        self.controller_stack = _format_controller_stack(parameters)
        LOGGER.debug("Detected Auracast controller stack: %s", self.controller_stack)

    async def _create_broadcast(self) -> None:
        """Configure public broadcast announcements and create the BIG."""
        assert self._device is not None
        config = self.config
        announcement = bap.BroadcastAudioAnnouncement(broadcast_id=config.broadcast_id)
        metadata = le_audio.Metadata(entries=[])
        codec_config = bap.CodecSpecificConfiguration(
            sampling_frequency=bap.SamplingFrequency.FREQ_48000,
            frame_duration=bap.FrameDuration.DURATION_10000_US,
            octets_per_codec_frame=self._bytes_per_bis,
        )
        locations = [bap.AudioLocation.FRONT_LEFT, bap.AudioLocation.FRONT_RIGHT]
        bis = [
            bap.BasicAudioAnnouncement.BIS(
                index=index + 1,
                codec_specific_configuration=bap.CodecSpecificConfiguration(
                    audio_channel_allocation=locations[index]
                ),
            )
            for index in range(config.channels)
        ]
        basic_announcement = bap.BasicAudioAnnouncement(
            presentation_delay=config.presentation_delay_us,
            subgroups=[
                bap.BasicAudioAnnouncement.Subgroup(
                    codec_id=hci.CodingFormat(codec_id=hci.CodecID.LC3),
                    codec_specific_configuration=codec_config,
                    metadata=metadata,
                    bis=bis,
                )
            ],
        )
        public_announcement = pbp.PublicBroadcastAnnouncement(
            features=pbp.PublicBroadcastAnnouncement.Features(0), metadata=metadata
        )
        name_bytes = config.name.encode()
        ad_structures = [
            (AdvertisingData.BROADCAST_NAME, name_bytes),
        ]
        if config.manufacturer_data:
            for company_id, vendor_data in config.manufacturer_data.items():
                ad_structures.append(
                    (
                        AdvertisingData.MANUFACTURER_SPECIFIC_DATA,
                        company_id.to_bytes(2, byteorder="little") + vendor_data,
                    )
                )
        advertising_data = (
            bytes(AdvertisingData(ad_structures))
            + announcement.get_advertising_data()
            + public_announcement.get_advertising_data()
        )
        self._advertising_set = await self._device.create_advertising_set(
            advertising_parameters=device.AdvertisingParameters(
                advertising_event_properties=device.AdvertisingEventProperties(
                    is_connectable=False
                ),
                primary_advertising_interval_min=60,
                primary_advertising_interval_max=60,
                advertising_sid=0,
            ),
            advertising_data=advertising_data,
            periodic_advertising_parameters=device.PeriodicAdvertisingParameters(
                periodic_advertising_interval_min=60,
                periodic_advertising_interval_max=60,
            ),
            periodic_advertising_data=basic_announcement.get_advertising_data(),
            auto_restart=True,
            auto_start=True,
        )
        await self._advertising_set.start_periodic()
        self._big = await self._device.create_big(
            self._advertising_set,
            parameters=device.BigParameters(
                num_bis=config.channels,
                sdu_interval=config.frame_duration_us,
                max_sdu=self._bytes_per_bis,
                max_transport_latency=config.max_transport_latency_ms,
                rtn=4,
            ),
        )
        self._streams = []
        for link in self._big.bis_links:
            await link.setup_data_path(direction=link.Direction.HOST_TO_CONTROLLER)
            self._streams.append(device.IsoPacketStream(link, 64))
        LOGGER.debug(
            "Auracast BIG created: bis_count=%d, max_sdu_per_bis=%d, presentation_delay_us=%d",
            len(self._streams),
            self._bytes_per_bis,
            config.presentation_delay_us,
        )
        LOGGER.info("Broadcasting %s with %d BIS streams", config.name, len(self._streams))


async def _teardown_step(step: Awaitable[None], description: str) -> None:
    """
    Run one best-effort teardown step, logging a timeout or Bumble/serial failure.

    :param step: The awaitable to run.
    :param description: What the step does, used in log messages.
    """
    try:
        await asyncio.wait_for(step, timeout=TEARDOWN_STEP_TIMEOUT)
    except TimeoutError:
        LOGGER.warning("Timed out %s", description)
    except BROADCAST_ERRORS as err:
        LOGGER.warning("Failed %s: %s", description, err)


def _format_controller_stack(parameters: Any) -> str:
    """Format HCI local-version information for display in device info."""
    try:
        version_name = hci.SpecificationVersion(parameters.hci_version).name
    except ValueError:
        version_name = None
    if version_name:
        version = f"Bluetooth Core {version_name.removeprefix('BLUETOOTH_CORE_').replace('_', '.')}"
    else:
        version = f"HCI version {parameters.hci_version}"
        LOGGER.debug("Bumble does not have a name for HCI version %d", parameters.hci_version)
    return (
        f"{version}; company ID 0x{parameters.company_identifier:04X}; "
        f"HCI revision 0x{parameters.hci_subversion:04X}; "
        f"LMP version {parameters.lmp_version}; "
        f"firmware revision 0x{parameters.lmp_subversion:04X}"
    )
