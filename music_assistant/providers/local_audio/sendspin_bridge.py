"""Sendspin Bridge for Local Audio Out - streams audio to local soundcards."""

from __future__ import annotations

import asyncio
import hashlib
import sys
import time
import uuid
from contextlib import suppress
from typing import TYPE_CHECKING, Any, TypeVar, cast

import numpy as np
from aiosendspin.models.core import ClientHelloPayload
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, PlayerCommand
from music_assistant_models.enums import IdentifierType
from music_assistant_models.player import DeviceInfo

from music_assistant.helpers.util import join_task
from music_assistant.models.player import Player
from music_assistant.providers.sendspin.bridge_manager import SendspinBridgeManagerBase
from music_assistant.providers.sendspin.bridge_role import (
    BRIDGE_BIT_DEPTH,
    BRIDGE_CHANNELS,
    BRIDGE_ROLE_ID,
    BRIDGE_SAMPLE_RATE,
    BridgePlayerRole,
)
from music_assistant.providers.sendspin.helpers import bridge_client_id_from_uuid

from .constants import (
    AUDIO_BACKEND_ALSA,
    AUDIO_BACKEND_AUTO,
    AUDIO_BACKEND_PULSEAUDIO,
    CACHE_CATEGORY_PREV_STATE,
    CONF_AUDIO_BACKEND,
    CONF_PREWARM_STREAMS,
    DEFAULT_BUFFER_FRAMES,
    DEFAULT_PLAYER_VOLUME,
    DEVICE_UUID_NAMESPACE,
    VOLUME_CONTROL_HARDWARE,
    VOLUME_CONTROL_SOFTWARE,
    volume_pct_to_amplitude,
)

if sys.platform == "linux":
    from .card_profiles import PROFILE_AUTO, conf_card_profile_key, plan_profile_changes
    from .pa_simple import (
        PASimpleStream,
        PAVolumeController,
        enumerate_alsa_devices,
        enumerate_pa_cards,
        enumerate_pa_sinks,
        set_card_profile,
        suspend_resume_sink,
        unmute_playback_switches,
    )
    from .remap_topology import (
        build_remap_sink_argument,
        compute_remap_topology,
        connector_label,
        normalize_card_name,
        remap_zone_suffix,
    )

if TYPE_CHECKING:
    from collections.abc import Callable

    import sounddevice as sd
    from aiosendspin.server import (
        ExternalStreamStartRequest,
        SendspinClient,
        SendspinServer,
    )
    from aiosendspin.server.roles import AudioChunk

    from music_assistant.providers.sendspin.provider import SendspinProvider

    from .provider import LocalAudioProvider


# Chunks arriving more than this many microseconds late are dropped rather than
# played, since writing stale audio would push subsequent chunks further out of
# sync with the server timeline.
_LATE_DROP_THRESHOLD_US = 500_000  # 500 ms

# A sound device that fails again this soon after being reopened is not going to
# settle by itself (a sink that keeps disappearing, a card whose driver stalls),
# so the bridge gives up instead of churning the device for the rest of the
# stream. Measured from the moment the reopen is attempted, which includes the
# open it is waiting on, so it sits well clear of a full _DEVICE_OPEN_TIMEOUT_SECONDS
# cycle to leave a slow-reopening device room to prove itself.
_DEVICE_REOPEN_GUARD_SECONDS: float = 30.0

# How long a writer gets to stop on its own before it is cancelled. It can be
# parked on a device that stopped responding — mid-write, or waiting out an
# open — where the graceful stop sentinel never gets read.
_WRITER_STOP_TIMEOUT_SECONDS: float = 2.0

# An open that has not come back by now is not going to: the device is wedged
# rather than simply gone. Generous enough that a sink resuming from suspend or
# a card waking up still makes it.
_DEVICE_OPEN_TIMEOUT_SECONDS: float = 10.0

_StreamT = TypeVar("_StreamT")


def _now_us() -> int:
    """
    Return current monotonic time in microseconds.

    Uses CLOCK_MONOTONIC_RAW on Linux to match the aiosendspin server clock,
    which avoids NTP slewing poisoning playback scheduling.
    """
    if sys.platform == "linux" and hasattr(time, "CLOCK_MONOTONIC_RAW"):
        return time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW) // 1_000
    return time.monotonic_ns() // 1_000


def get_device_uuid(device_name: str, hostapi_index: int) -> str:
    """Generate a stable UUID for a local audio device."""
    return str(uuid.uuid5(DEVICE_UUID_NAMESPACE, f"{device_name}:{hostapi_index}"))


def identity_seed(device_name: str, device_info: dict[str, Any]) -> str:
    """
    Return the stable identity input for get_device_uuid().

    Uses master_device + the recovered zone suffix when present (a remap
    sink this provider created — see remap_topology.remap_zone_suffix),
    falling back to the device's own PA sink name otherwise (already
    stable, since PulseAudio generates it from the physical bus path, not
    from anything this module names).

    MUST be the single source of truth for this computation. It's called
    both by discover_and_register() (which registers the MA player) and by
    SendspinLocalAudioBridge.start() (which registers the bridge as that
    same player's output protocol) — the two must always resolve to the
    identical UUID for the same device, or MA has a player with no
    attached protocol. Computing this independently in two places is
    exactly how they drifted out of sync previously; do not duplicate it.
    """
    master_device: str | None = device_info.get("master_device")
    zone_suffix = remap_zone_suffix(device_name)
    if master_device and zone_suffix:
        return f"{master_device}::{zone_suffix}"
    return device_name


def short_hardware_tag(bus_identity: str, length: int = 4) -> str:
    """
    Derive a short, stable hex tag from a physical identity string for display purposes.

    Derived from a physical identity string (e.g. master_device), for
    display/grouping purposes only — e.g. hinting that several players
    (front_stereo, rear_stereo, multichannel_stereo, ...) all originate
    from the same physical card, without needing the full
    card_name/label string repeated in each one.

    NOT a replacement for get_device_uuid()'s player identity — that stays
    the actual player_id mechanism. A short tag has real, non-negligible
    collision risk (4 hex digits = 65,536 possible values) and is only
    appropriate as a cosmetic hint, never as anything relied on to be
    truly unique.

    Uses hashlib.sha1 rather than the built-in hash() — Python's built-in
    hash() is randomized per process (PYTHONHASHSEED) unless explicitly
    disabled, so it would produce a different tag every restart, defeating
    the entire point of a stable tag.
    """
    digest = hashlib.sha1(bus_identity.encode("utf-8")).hexdigest()
    return digest[:length]


class SendspinLocalAudioBridge:
    """Manages the Sendspin to local soundcard bridge for a single device."""

    def __init__(
        self,
        provider: LocalAudioProvider,
        device_info: dict[str, Any],
        sendspin_server: SendspinServer,
        backend: str = "auto",
        volume_controller: PAVolumeController | None = None,
        has_remap_children: bool = False,
    ) -> None:
        """
        Initialize the bridge.

        :param provider: The Local Audio provider instance.
        :param device_info: Device info dict — on Linux PA: from enumerate_pa_sinks();
            on Linux ALSA or Darwin: from sounddevice.query_devices() with 'index' added.
        :param sendspin_server: The Sendspin server to register with.
        :param backend: Resolved audio backend: "pulse", "alsa", or "sounddevice".
        :param volume_controller: Shared PAVolumeController for hardware sink volume
            (pulse backend only). If None, falls back to software volume scaling.
        :param has_remap_children: True if this is a master sink with at least
            one module-remap-sink.c child in the current enumeration. Ignored
            for remap sinks themselves.
        """
        self.provider = provider
        self.mass = provider.mass
        self.sendspin_server = sendspin_server
        self.device_info = device_info
        self.device_name: str = device_info["name"]
        self.display_name: str = device_info.get("description", self.device_name)
        self.pa_sink_name: str | None = device_info.get("pa_sink_name")
        self.sample_rate: int = device_info.get("sample_rate", BRIDGE_SAMPLE_RATE)
        self.bit_depth: int = device_info.get("bit_depth", BRIDGE_BIT_DEPTH)
        # The PA sink's actual channel count (e.g. 8 for a 7.1 master, 2 for a
        # remap sink). Used for pa_cvolume_set in PAVolumeController calls —
        # a mismatch against the sink's real channel_map can leave the
        # displayed reference_volume updated while soft_volume (real gain)
        # doesn't change. This is independent of BRIDGE_CHANNELS, which is
        # the audio *stream's* channel count (always 2).
        self.pa_channels: int = device_info.get("max_output_channels", BRIDGE_CHANNELS)
        self.device_index: int | None = device_info.get("index")
        self.backend: str = backend
        self.logger = provider.logger.getChild(f"bridge.{self.display_name}")

        # The shared PAVolumeController, if connected for the pulse backend.
        # Used by _reset_sink_volume() (pin-to-100% fallback, via libpulse
        # instead of a pactl subprocess) for ALL pulse bridges.
        self._shared_volume_controller: PAVolumeController | None = (
            volume_controller if backend == "pulse" else None
        )

        # Hardware per-player volume control is used for:
        #  - remap sinks (module-remap-sink.c): each has its own independent
        #    PA volume that doesn't affect its master or siblings (validated
        #    empirically), or
        #  - base/master sinks with NO remap-sink children in the current
        #    enumeration (e.g. remap addon disabled, or a standalone card):
        #    nothing shares this sink's master output, so hardware volume is
        #    safe here too.
        #
        # A master sink that DOES have remap children must NOT use hardware
        # volume for itself: its PA volume multiplies into every remap sink
        # feeding through it, so driving it from *this* player's volume
        # would silently attenuate every other zone sharing the card. Such
        # masters stay pinned at 100% (via _reset_sink_volume) and use
        # software (numpy) volume scaling for their own player.
        is_remap = device_info.get("is_remap", False)
        self._volume_controller: PAVolumeController | None = (
            self._shared_volume_controller if (is_remap or not has_remap_children) else None
        )
        self._volume_control_mode = (
            VOLUME_CONTROL_HARDWARE
            if self._volume_controller is not None
            else VOLUME_CONTROL_SOFTWARE
        )

        # Volume/mute state — owned by the bridge; persisted to MA cache
        self._volume_level: int = DEFAULT_PLAYER_VOLUME
        self._volume_muted: bool = False
        # Stable UUID for this device — set in start()
        self._device_uuid: str = ""

        self._sendspin_client: SendspinClient | None = None
        self._bridge_client_id: str | None = None
        self._bridge_role: BridgePlayerRole | None = None
        self._is_streaming = False
        self._logged_chunk_fmt: bool = False
        # Queue holds (timestamp_us, pcm_bytes) tuples for scheduled playback.
        # None sentinel signals the writer to stop.
        self._write_queue: asyncio.Queue[tuple[int, bytes] | None] = asyncio.Queue()
        # The writer that currently owns the output device. Registration is the
        # ownership token: a writer only touches shared bridge state while it is
        # still the one registered here, so a stream that replaced it is left alone.
        self._writer_task: asyncio.Task[None] | None = None
        # Monotonic instant of the last output device reopen within the running
        # writer (None when none happened yet), used to tell a one-off device
        # failure from one that keeps repeating.
        self._last_device_reopen: float | None = None
        # Pre-warmed PA stream — opened during start() so PA stream-open
        # latency is paid once at provider init, not at first play. Eliminates
        # the ~200ms cold-start sync offset when all bridges in a group open
        # their PA streams simultaneously for the first time.
        self._pa_stream: PASimpleStream | None = None
        self._lock = asyncio.Lock()

    @property
    def is_registered(self) -> bool:
        """Return whether the bridge is registered with Sendspin."""
        return self._sendspin_client is not None

    async def start(self) -> None:
        """Register the local audio device as an external Sendspin client."""
        hostapi_index: int = self.device_info.get("hostapi", 0)
        self._device_uuid = get_device_uuid(
            identity_seed(self.device_name, self.device_info), hostapi_index
        )
        self._bridge_client_id = bridge_client_id_from_uuid(self._device_uuid)

        if sendspin_prov := self._get_sendspin_provider():
            sendspin_prov.register_bridge_identifiers(
                self._bridge_client_id,
                {IdentifierType.UUID: self._device_uuid},
            )
            # The local audio device player (registered under the bare device
            # uuid) is the player this bridge runs on top of.
            sendspin_prov.register_bridge_underlying_player(
                self._bridge_client_id, self._device_uuid
            )

        # Restore cached volume/mute state from a previous session.
        # Never restore muted=True on startup: a stale cached mute silently
        # kills audio every boot with no visible indication in the MA UI.
        # The user can re-mute intentionally; we should never start muted.
        if last_state := await self.mass.cache.get(
            key=self._device_uuid,
            provider=self.provider.instance_id,
            category=CACHE_CATEGORY_PREV_STATE,
        ):
            self._volume_muted = False
            self._volume_level = last_state[1]

        # On Linux advertise the sink's native format so MA transcodes correctly.
        _depths = sorted({self.bit_depth, BRIDGE_BIT_DEPTH}, reverse=True)
        supported_formats = [
            SupportedAudioFormat(
                codec=AudioCodec.PCM,
                channels=BRIDGE_CHANNELS,
                sample_rate=self.sample_rate,
                bit_depth=d,
            )
            for d in _depths
        ]

        hello = ClientHelloPayload(
            client_id=self._bridge_client_id,
            name=self.display_name,
            version=1,
            supported_roles=[BRIDGE_ROLE_ID, "player@v1"],
            device_info=SendspinDeviceInfo(
                product_name=self.display_name,
                manufacturer="Local Audio",
            ),
            player_support=ClientHelloPlayerSupport(
                supported_formats=supported_formats,
                buffer_capacity=1_000,
                supported_commands=[PlayerCommand.VOLUME, PlayerCommand.MUTE],
            ),
        )

        self.logger.debug(
            "Registering Sendspin bridge for %s (client_id=%s)",
            self.device_name,
            self._bridge_client_id,
        )

        self._sendspin_client = self.sendspin_server.register_external_player(
            hello, on_stream_start=self._on_stream_start
        )

        for role in self._sendspin_client.roles_by_family("player"):
            self.logger.debug("Found player role: %s type=%s", role.role_id, type(role).__name__)
            if isinstance(role, BridgePlayerRole):
                self._bridge_role = role
                break

        if self._bridge_role is None:
            self.logger.error("No BridgePlayerRole found for %s", self.device_name)
            return

        self._bridge_role.set_callbacks(
            on_audio_chunk=self._on_audio_chunk,
            on_volume_change=self._on_volume_change,
            on_mute_change=self._on_mute_change,
            on_stream_start=self._on_bridge_stream_start,
            on_stream_end=self._on_bridge_stream_end,
            initial_volume=self._volume_level,  # restore cached volume for correct slider position
        )
        self._bridge_role.setup_audio_requirements(
            sample_rate=self.sample_rate,
            bit_depth=self.bit_depth,
            channels=BRIDGE_CHANNELS,
        )

        self.logger.info(
            "Sendspin bridge registered for %s (client_id=%s, volume_control=%s)",
            self.device_name,
            self._bridge_client_id,
            self._volume_control_mode,
        )

        if self._volume_controller is not None:
            # Remap sink: apply the restored volume/mute to PA sink hardware
            # directly so the sink starts at the user's last level.
            await self._apply_hardware_volume()
        elif self.backend == "pulse":
            # Master/base sink (or remap sink if PAVolumeController failed to
            # connect): pin PA sink volume to 100% so it doesn't attenuate
            # remap sinks feeding through it; volume is controlled in software
            # for this player. Also undoes any deferred_volume bleed from
            # stream volume into sink hardware volume on pa_simple_new open.
            await self._reset_sink_volume()

        # Pre-warm the PA stream so the latency of pa_simple_new() is paid
        # once at provider init rather than at first play. Without this, each
        # bridge in a sync group opens its PA stream on the first play, and the
        # spread in that per-bridge open latency (~50-200ms) becomes a fixed
        # sync offset that persists for the session. Pre-warming means all
        # streams are already open and idle when the first play starts, so
        # play_at_us scheduling lands all bridges within a much tighter window.
        #
        # Trade-off: a pre-warmed stream is an open (uncorked) sink-input, so
        # the sink stays RUNNING while the provider is active — even with
        # module-suspend-on-idle loaded. On PCI cards the idle cost is
        # negligible; on USB devices (isochronous traffic) or virtualized
        # setups it can matter, hence the config option to disable it and
        # accept per-play stream-open latency instead.
        if (
            self.backend == "pulse"
            and self.pa_sink_name
            and bool(self.provider.config.get_value(CONF_PREWARM_STREAMS))
        ):
            await self._prewarm_pa_stream()

    async def stop(self) -> None:
        """Stop and unregister the Sendspin bridge."""
        async with self._lock:
            await self._stop_streaming()
            if self._sendspin_client and self._bridge_client_id:
                await self.sendspin_server.remove_client(self._bridge_client_id)
                self._sendspin_client = None
                self._bridge_role = None
        # Close the pre-warmed stream if it was never consumed by a writer.
        if self._pa_stream is not None:
            stream = self._pa_stream
            self._pa_stream = None
            with suppress(OSError):
                await self.mass.loop.run_in_executor(None, stream.close)
        self.logger.debug("Sendspin bridge stopped for %s", self.device_name)

    def _get_sendspin_provider(self) -> SendspinProvider | None:
        """Get the Sendspin provider if available."""
        return cast("SendspinProvider | None", self.mass.get_provider("sendspin"))

    def _on_stream_start(self, request: ExternalStreamStartRequest) -> None:
        """Handle stream start request from Sendspin server."""
        self.logger.debug(
            "Sendspin stream start request for %s (reason=%s)",
            self.device_name,
            request.connection_reason,
        )
        self._is_streaming = True

    def _on_bridge_stream_start(self) -> None:
        """Start the audio writer task for a new stream."""
        if self._writer_task is not None and not self._writer_task.done():
            self._writer_task.cancel()
        self._is_streaming = True
        while not self._write_queue.empty():
            self._write_queue.get_nowait()
        # Started on the next loop iteration so the writer is registered before
        # it runs: everything it touches on the bridge is keyed on being the
        # registered writer, which an eager start would leave it short of.
        self._writer_task = self.mass.create_task(self._audio_writer(), eager_start=False)
        self.logger.info("Bridge writer started for %s", self.device_name)

    def _on_bridge_stream_end(self) -> None:
        """Stop streaming when the stream ends."""
        # Clearing the streaming flag is left to the teardown, which is the only
        # thing that knows whether this end still owns the running writer.
        self.mass.create_task(self._stop_streaming_locked(self._writer_task))
        if self._volume_controller is None and self.backend == "pulse":
            # Master/base sink (software volume control): re-pin PA sink
            # volume to 100% after playback ends.
            self.mass.create_task(self._reset_sink_volume())

    async def _prewarm_pa_stream(self) -> None:
        """
        Open the PA stream early so stream-open latency doesn't affect sync.

        Opens a PASimpleStream and writes a single silence chunk to trigger
        the idle->RUNNING ALSA transition (paying the mmap init cost once at
        provider startup rather than at first play). The stream is then held
        open and idle — PA may suspend it via suspend-on-idle, but the ALSA
        driver remains initialised so the first real write reconnects with
        negligible latency. No keepalive writes are used to avoid interfering
        with real audio when the writer takes ownership.
        """
        if not self.pa_sink_name:
            return
        pa_sink_name = self.pa_sink_name
        bytes_per_sample = self.bit_depth // 8
        # One chunk of silence to trigger idle->RUNNING and initialise the
        # ALSA DMA buffer — just enough to pay the mmap init cost.
        silence = bytes(BRIDGE_CHANNELS * bytes_per_sample * 64)  # 64 frames
        try:
            stream = await self.mass.loop.run_in_executor(
                None,
                lambda: PASimpleStream(
                    sink_name=pa_sink_name,
                    app_name=f"music-assistant-{pa_sink_name}",
                    rate=self.sample_rate,
                    channels=BRIDGE_CHANNELS,
                    bit_depth=self.bit_depth,
                ),
            )
        except OSError as err:
            self.logger.debug("PA stream pre-warm failed for %s: %s", pa_sink_name, err)
            return
        # Write one silence chunk to trigger the ALSA DMA init cycle.
        try:
            await self.mass.loop.run_in_executor(None, stream.write, silence)
        except OSError:
            with suppress(OSError):
                await self.mass.loop.run_in_executor(None, stream.close)
            return
        self._pa_stream = stream
        self.logger.debug("PA stream pre-warmed for %s", pa_sink_name)

    async def _reset_sink_volume(self) -> None:
        """
        Pin PA sink hardware volume to 100% via the shared PAVolumeController.

        Called at bridge startup and after each playback session for:
          - Master/base ALSA-card sinks (is_remap=False), where volume control
            is software (per-player) and the underlying PA sink must stay at
            unity so it doesn't attenuate every remap sink feeding through it.
          - The fallback path on remap sinks if PAVolumeController itself
            failed to connect (self._volume_controller is None in that case
            too, so this also covers undoing any deferred_volume bleed from
            stream volume into sink hardware volume).
        No-op on non-PA backends or if PAVolumeController is unavailable.
        """
        if self.backend != "pulse" or not self.pa_sink_name:
            return
        if self._shared_volume_controller is None:
            return
        pa_sink_name = self.pa_sink_name
        controller = self._shared_volume_controller
        try:
            ok = await self.mass.loop.run_in_executor(
                None, controller.set_sink_volume, pa_sink_name, 100, self.pa_channels
            )
            if not ok:
                self.logger.warning("Failed to pin PA sink volume to 100%% for %s", pa_sink_name)
            else:
                self.logger.debug("Pinned PA sink hardware volume to 100%% for %s", pa_sink_name)
        except OSError as err:
            self.logger.warning("Hardware volume control error for %s: %s", pa_sink_name, err)

    def _on_volume_change(self, volume: int) -> None:
        """Sync volume from Sendspin side to bridge state and persist."""
        self._volume_level = volume
        self.mass.create_task(self._save_state())
        if self._volume_controller is not None:
            self.mass.create_task(self._apply_hardware_volume())

    def _on_mute_change(self, muted: bool) -> None:
        """Sync mute from Sendspin side to bridge state and persist."""
        self._volume_muted = muted
        self.mass.create_task(self._save_state())
        if self._volume_controller is not None:
            self.mass.create_task(self._apply_hardware_volume())

    async def _apply_hardware_volume(self) -> None:
        """
        Apply the current volume/mute state to PA sink hardware volume.

        No-op if no PAVolumeController is available (software fallback path).
        """
        if self._volume_controller is None or not self.pa_sink_name:
            return
        target_pct = 0 if self._volume_muted else self._volume_level
        self.logger.debug(
            "apply_hardware_volume %s: target=%d%% (level=%d muted=%s)",
            self.pa_sink_name,
            target_pct,
            self._volume_level,
            self._volume_muted,
        )
        pa_sink_name = self.pa_sink_name
        controller = self._volume_controller
        try:
            ok = await self.mass.loop.run_in_executor(
                None, controller.set_sink_volume, pa_sink_name, target_pct, self.pa_channels
            )
            if not ok:
                self.logger.warning(
                    "Failed to set hardware volume to %d%% for %s", target_pct, pa_sink_name
                )
        except OSError as err:
            self.logger.warning("Hardware volume control error for %s: %s", pa_sink_name, err)

    def _schedule_hardware_volume_reapply(self) -> None:
        """Re-apply the cached hardware volume shortly after the sink starts running."""
        if self._volume_controller is None:
            return

        # module-device-restore may restore a persisted per-sink-name volume
        # when the sink transitions idle->RUNNING — which happens on the first
        # actual write, *after* _apply_hardware_volume() already ran during
        # start() while the sink was idle, silently reverting it. Re-apply
        # shortly after the first write so our cached volume wins, once that
        # one-time revert has had a chance to fire.
        async def _reapply_hardware_volume_once_active() -> None:
            await asyncio.sleep(0.3)
            await self._apply_hardware_volume()

        self.mass.create_task(_reapply_hardware_volume_once_active())

    async def _save_state(self) -> None:
        """Persist current volume/mute state to MA cache."""
        if not self._device_uuid:
            return
        await self.mass.cache.set(
            key=self._device_uuid,
            data=[self._volume_muted, self._volume_level],
            provider=self.provider.instance_id,
            category=CACHE_CATEGORY_PREV_STATE,
        )

    def _on_audio_chunk(self, chunk: AudioChunk) -> None:
        """Enqueue an incoming audio chunk with its playback timestamp."""
        if not self._is_streaming:
            return
        if not self._logged_chunk_fmt:
            self.logger.debug(
                "First chunk: len=%d  sample_rate=%d bit_depth=%d timestamp_us=%d",
                len(chunk.data),
                self.sample_rate,
                self.bit_depth,
                chunk.timestamp_us,
            )
            self._logged_chunk_fmt = True
        self._write_queue.put_nowait((chunk.timestamp_us, chunk.data))

    def _static_delay_us(self) -> int:
        """Return the configured static playback delay in microseconds."""
        if self._bridge_role is not None:
            return self._bridge_role.get_static_delay_us()
        return 0

    def _apply_format_conversion(self, pcm_data: bytes) -> bytes:
        """
        Apply PA format conversion only — no volume scaling.

        Used on the hardware-volume-control path, where mute and volume are
        handled by PAVolumeController directly on the PA sink. Only the
        24-bit -> packed s24le repack is still needed here, since PA expects
        3 bytes/sample but MA delivers 24-bit audio left-justified in 32-bit
        containers.
        """
        if self.bit_depth != 24:
            return pcm_data
        samples = np.frombuffer(pcm_data, dtype=np.int32)
        return samples.view(np.uint8).reshape(-1, 4)[:, 1:].tobytes()

    def _apply_software_volume(self, pcm_data: bytes) -> bytes:
        """
        Apply software volume scaling and format conversion.

        Fallback path used when no PAVolumeController is available (or for
        the ALSA/sounddevice backend, which has no PA hardware volume path).
        """
        if self._volume_muted:
            if self.bit_depth == 24:
                # PA expects packed s24le: 3 bytes/sample, not 4
                return b"\x00" * (len(pcm_data) * 3 // 4)
            return b"\x00" * len(pcm_data)
        volume = self._volume_level
        amplitude = volume_pct_to_amplitude(volume)
        # 1.0 == no-op; skip the scaling pass entirely
        scale: float | None = None if amplitude >= 1.0 else amplitude

        if self.bit_depth == 32:
            if scale is None:
                return pcm_data
            samples = np.frombuffer(pcm_data, dtype=np.int32).copy()
            scaled = np.clip(samples.astype(np.float64) * scale, -2147483648, 2147483647)
            return scaled.astype(np.int32).tobytes()

        if self.bit_depth == 24:
            # MA delivers 24-bit audio left-justified in 32-bit containers.
            # Always repack to packed s24le (bytes 1-3 of each int32).
            samples = np.frombuffer(pcm_data, dtype=np.int32).copy()
            if scale is not None:
                samples = np.clip(
                    samples.astype(np.float64) * scale, -2147483648, 2147483647
                ).astype(np.int32)
            return samples.view(np.uint8).reshape(-1, 4)[:, 1:].tobytes()

        # 16-bit
        if scale is None:
            return pcm_data
        samples_16 = np.frombuffer(pcm_data, dtype=np.int16).copy()
        scaled = np.clip(samples_16.astype(np.float64) * scale, -32768, 32767)
        return scaled.astype(np.int16).tobytes()

    def _prepare_pa_chunk(self, pcm_data: bytes) -> bytes:
        """
        Return chunk data ready to be written to the PA sink.

        Volume is applied in software unless a PAVolumeController drives the
        sink's hardware volume, in which case only the format conversion runs.
        """
        if self._volume_controller is not None:
            return self._apply_format_conversion(pcm_data)
        return self._apply_software_volume(pcm_data)

    async def _wait_for_chunk_time(self, timestamp_us: int) -> bool:
        """
        Sleep until the scheduled playback time for a chunk.

        :param timestamp_us: Server-domain playback timestamp in microseconds.
        :returns: True if the chunk should be played, False if it arrived too
            late and should be dropped.

        The bridge runs in-process with the Sendspin server and both use
        CLOCK_MONOTONIC_RAW (Linux) or monotonic_ns elsewhere, so
        chunk.timestamp_us is already in the local clock domain — no NTP-style
        offset conversion is required.  The static_delay_us configured by the
        user is subtracted so that playback on this device is advanced relative
        to other players, compensating for a slower DAC pipeline.
        """
        play_at_us = timestamp_us - self._static_delay_us()
        now = _now_us()
        wait_us = play_at_us - now
        if wait_us < -_LATE_DROP_THRESHOLD_US:
            self.logger.warning(
                "Dropping late chunk for %s: %d ms behind schedule",
                self.device_name,
                -wait_us // 1_000,
            )
            return False
        if wait_us > 0:
            await asyncio.sleep(wait_us / 1_000_000)
        return True

    async def _audio_writer(self) -> None:
        """Write queued audio to the output device."""
        # Every writer gets its own reopen budget: a device failure on the
        # previous stream says nothing about its health on this one.
        self._last_device_reopen = None
        if self.backend == "pulse":
            await self._audio_writer_pulse()
        else:
            await self._audio_writer_sounddevice()

    async def _get_pa_stream(self) -> PASimpleStream:
        """
        Return a ready PASimpleStream for this bridge's PA sink.

        Uses the pre-warmed stream if available, otherwise opens a new one.
        """
        if self._pa_stream is not None:
            stream = self._pa_stream
            self._pa_stream = None  # take ownership
            self.logger.debug("Using pre-warmed PA stream for %s", self.pa_sink_name)
            return stream
        self.logger.debug(
            "Opening PA stream: sink=%s rate=%d channels=%d bit_depth=%d",
            self.pa_sink_name,
            self.sample_rate,
            BRIDGE_CHANNELS,
            self.bit_depth,
        )
        assert self.pa_sink_name is not None
        pa_sink_name = self.pa_sink_name
        return await self._await_device_open(
            self.mass.loop.run_in_executor(
                None,
                lambda: PASimpleStream(
                    sink_name=pa_sink_name,
                    app_name=f"music-assistant-{pa_sink_name}",
                    rate=self.sample_rate,
                    channels=BRIDGE_CHANNELS,
                    bit_depth=self.bit_depth,
                ),
            ),
            lambda stream: stream.close(),
        )

    async def _audio_writer_pulse(self) -> None:
        """Write queued audio to a PA sink via PASimpleStream (Linux)."""
        stream: PASimpleStream | None = None
        write_future: asyncio.Future[None] | None = None
        try:
            stream = await self._get_pa_stream()
            self.logger.debug("PA stream ready for %s", self.pa_sink_name)
            assert stream is not None

            first_chunk_written = False

            while True:
                item = await self._write_queue.get()
                if item is None or not self._is_streaming:
                    break
                timestamp_us, data = item
                if not await self._wait_for_chunk_time(timestamp_us):
                    continue  # late chunk dropped
                data = self._prepare_pa_chunk(data)
                try:
                    write_future = self.mass.loop.run_in_executor(None, stream.write, data)
                    await write_future
                    write_future = None
                except OSError as err:
                    self.logger.error("PA stream error for %s: %s", self.pa_sink_name, err)
                    write_future = None
                    if (stream := await self._reopen_pa_stream(stream)) is None:
                        # closed by the reopen, so the teardown has nothing to release
                        self._abandon_streaming()
                        return
                    # The reopened sink runs through idle->RUNNING again, so the
                    # cached volume has to win over module-device-restore once more.
                    first_chunk_written = False
                    continue

                if not first_chunk_written:
                    first_chunk_written = True
                    self._schedule_hardware_volume_reapply()

        except asyncio.CancelledError:
            pass
        except OSError as err:
            self.logger.error("Failed to open PA stream for %s: %s", self.pa_sink_name, err)
            self._abandon_streaming()
        except Exception:
            # The sink is this player's only audio path, so a writer dying on
            # anything at all has to take the player out of playback with it.
            self.logger.exception("PA writer for %s failed", self.pa_sink_name)
            self._abandon_streaming()
        finally:
            await self._finish_pa_writer(stream, write_future)

    async def _finish_pa_writer(
        self, stream: PASimpleStream | None, write_future: asyncio.Future[None] | None
    ) -> None:
        """
        Release the PA stream a writer owned and hand back its registration.

        :param stream: The stream the writer got, if it got one.
        :param write_future: A write still in flight, if any.
        """
        if self._writer_task is asyncio.current_task():
            self._is_streaming = False
        if write_future is not None:
            # How the write ended does not matter, only that it is no longer
            # running when the stream underneath it is closed below.
            with suppress(Exception, asyncio.CancelledError):
                await asyncio.shield(write_future)
        if stream is not None:
            with suppress(OSError):
                await asyncio.shield(self.mass.loop.run_in_executor(None, stream.close))
        if self._writer_task is asyncio.current_task():
            self._writer_task = None

    async def _audio_writer_sounddevice(self) -> None:
        """Write queued audio to a sounddevice output stream (ALSA on Linux or Darwin)."""
        import sounddevice as _sd  # noqa: PLC0415

        stream: sd.RawOutputStream | None = None
        try:
            stream = await self._get_sounddevice_stream()
            self.logger.debug("sounddevice stream opened for %s", self.device_name)
            assert stream is not None

            while True:
                item = await self._write_queue.get()
                if item is None or not self._is_streaming:
                    break
                timestamp_us, data = item
                if not await self._wait_for_chunk_time(timestamp_us):
                    continue  # late chunk dropped
                data = self._apply_software_volume(data)
                try:
                    await self.mass.loop.run_in_executor(None, stream.write, data)
                except _sd.PortAudioError as err:
                    self.logger.error("PortAudio error for %s: %s", self.device_name, err)
                    replacement = await self._reopen_sounddevice_stream(stream)
                    if replacement is None:
                        stream = None  # closed by the reopen, nothing left to release
                        self._abandon_streaming()
                        return
                    stream = replacement
        except (_sd.PortAudioError, TimeoutError) as err:
            self.logger.error("Failed to open sounddevice stream for %s: %s", self.device_name, err)
            self._abandon_streaming()
        except Exception:
            # The device is this player's only audio path, so a writer dying on
            # anything at all has to take the player out of playback with it.
            self.logger.exception("Sound device writer for %s failed", self.device_name)
            self._abandon_streaming()
        finally:
            await self._finish_sounddevice_writer(stream)

    async def _finish_sounddevice_writer(self, stream: sd.RawOutputStream | None) -> None:
        """
        Release the sound device a writer owned and hand back its registration.

        :param stream: The device the writer got, if it got one.
        """
        if self._writer_task is asyncio.current_task():
            self._is_streaming = False
        if stream is not None:
            # Closing waits for the device to play out what it still holds,
            # which is not something to do on the event loop.
            with suppress(OSError):
                await asyncio.shield(
                    self.mass.loop.run_in_executor(None, self._close_sounddevice_stream, stream)
                )
        if self._writer_task is asyncio.current_task():
            self._writer_task = None

    async def _get_sounddevice_stream(self) -> sd.RawOutputStream:
        """Open a sounddevice output stream for this device, off the event loop."""
        return await self._await_device_open(
            self.mass.loop.run_in_executor(None, self._create_sounddevice_stream),
            lambda stream: self._close_sounddevice_stream(stream, drain=False),
        )

    async def _await_device_open(
        self, open_future: asyncio.Future[_StreamT], close_stream: Callable[[_StreamT], None]
    ) -> _StreamT:
        """
        Wait for a device to finish opening, for as long as that is worth doing.

        An open that never comes back would otherwise hold the writer forever
        while chunks pile up behind a player still reporting playback.

        :param open_future: The open running off the event loop.
        :param close_stream: How to release the device, used when it arrives
            after nobody is waiting for it any more.
        :return: The opened stream.
        :raises TimeoutError: The device did not open in time.
        """
        try:
            return await join_task(open_future, _DEVICE_OPEN_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            self._release_orphaned_stream(open_future, close_stream)
            raise
        except TimeoutError:
            self._release_orphaned_stream(open_future, close_stream)
            raise TimeoutError(
                f"the device did not open within {_DEVICE_OPEN_TIMEOUT_SECONDS:.0f}s"
            ) from None

    def _release_orphaned_stream(
        self, open_future: asyncio.Future[_StreamT], close_stream: Callable[[_StreamT], None]
    ) -> None:
        """
        Arrange for a device to be released once an open nobody is waiting for finishes.

        The open runs to completion in its own thread whatever the caller does,
        and a device nobody hands back keeps every later open of it failing.

        :param open_future: The open that outlived the caller waiting on it.
        :param close_stream: How to release the device it produces.
        """

        def _close_device(stream: _StreamT) -> None:
            # Nobody is left to hand a failed release to, and the executor
            # future it would surface on is never retrieved.
            with suppress(Exception):
                close_stream(stream)

        def _close_when_open(finished: asyncio.Future[_StreamT]) -> None:
            if finished.cancelled() or finished.exception() is not None:
                return
            self.mass.loop.run_in_executor(None, _close_device, finished.result())

        open_future.add_done_callback(_close_when_open)

    def _create_sounddevice_stream(self) -> sd.RawOutputStream:
        """Open and start a sounddevice output stream, blocking until the device is ready."""
        import sounddevice as _sd  # noqa: PLC0415

        stream = _sd.RawOutputStream(
            device=self.device_index,
            samplerate=self.sample_rate,
            channels=BRIDGE_CHANNELS,
            dtype="int16",
            blocksize=DEFAULT_BUFFER_FRAMES,
        )
        try:
            stream.start()
        except Exception:
            # PortAudio already handed out the device, so it has to be given
            # back before the failure propagates.
            stream.close()
            raise
        return stream

    async def _reopen_pa_stream(self, dead_stream: PASimpleStream) -> PASimpleStream | None:
        """
        Replace a PA stream that failed mid-playback.

        :param dead_stream: The stream that raised on write. Always closed —
            libpulse has no reconnect, so a failed connection stays failed.
        :return: A fresh stream, or None when the device may not be reopened
            or could not be opened.
        """
        with suppress(OSError):
            await self.mass.loop.run_in_executor(None, dead_stream.close)
        if not self._start_device_reopen():
            return None
        try:
            stream = await self._get_pa_stream()
        except OSError as err:
            # covers the bounded open giving up, which reports a TimeoutError
            self.logger.error("Could not reopen PA sink %s: %s", self.pa_sink_name, err)
            return None
        if self._volume_controller is None:
            # Opening a stream can bleed its volume into the sink's hardware
            # volume, which for this path has to stay pinned at unity.
            await self._reset_sink_volume()
        self.logger.warning("Reopened PA sink %s after a write failure", self.pa_sink_name)
        return stream

    async def _reopen_sounddevice_stream(
        self, dead_stream: sd.RawOutputStream
    ) -> sd.RawOutputStream | None:
        """
        Replace a sounddevice output stream that failed mid-playback.

        :param dead_stream: The stream that raised on write; always closed.
        :return: A fresh started stream, or None when the device may not be
            reopened or could not be opened.
        """
        import sounddevice as _sd  # noqa: PLC0415

        # The device just failed, so closing it can block; it goes off the event
        # loop rather than stalling every other player behind it.
        await self.mass.loop.run_in_executor(
            None, lambda: self._close_sounddevice_stream(dead_stream, drain=False)
        )
        if not self._start_device_reopen():
            return None
        try:
            stream = await self._get_sounddevice_stream()
        except (_sd.PortAudioError, TimeoutError) as err:
            self.logger.error("Could not reopen sound device %s: %s", self.device_name, err)
            return None
        self.logger.warning("Reopened sound device %s after a write failure", self.device_name)
        return stream

    def _close_sounddevice_stream(self, stream: sd.RawOutputStream, *, drain: bool = True) -> None:
        """
        Close a sounddevice output stream.

        :param stream: The stream to close.
        :param drain: Let buffered audio play out first. Pass False for a device
            that is no longer responding, where that wait cannot finish.
        """
        if drain:
            with suppress(OSError):
                stream.stop()
        with suppress(OSError):
            stream.close()

    def _start_device_reopen(self) -> bool:
        """
        Register an attempt to reopen the output device after a failure.

        :return: True when the attempt may go ahead, False when the device
            already failed again within the guard window of the previous
            reopen and the bridge should give up on it.
        """
        now = time.monotonic()
        last_reopen = self._last_device_reopen
        if last_reopen is not None and now - last_reopen < _DEVICE_REOPEN_GUARD_SECONDS:
            self.logger.error(
                "Output device %s failed again within %.0fs of being reopened, "
                "giving up and taking it out of the Sendspin session",
                self.device_name,
                _DEVICE_REOPEN_GUARD_SECONDS,
            )
            return False
        self._last_device_reopen = now
        return True

    def _abandon_streaming(self) -> None:
        """
        Give up on this stream: stop accepting chunks and leave the Sendspin session.

        Playback resumes normally on the next stream — the device is only given
        up on for the remainder of this one. Called by a writer a newer stream
        has already replaced, it does nothing: that stream is not this one's to
        give up on.
        """
        if self._writer_task is not asyncio.current_task():
            return
        # The device is this player's only audio path and nothing restarts the
        # writer within a stream, so leaving is what surfaces the silence
        # instead of letting the group hold the player on PLAYING.
        self._is_streaming = False
        # Leaving ends the Sendspin stream, which unwinds straight back into
        # this bridge's own teardown — that must not run inside the writer task
        # calling this, which is on its way out.
        self.mass.create_task(self._leave_sendspin_session(), eager_start=False)

    async def _leave_sendspin_session(self) -> None:
        """
        Take the bridge out of Sendspin playback, keeping it registered.

        A shared group plays on without this device, a solo one stops. The
        client stays registered, so the player survives to be grouped again.
        """
        if not (client := self._sendspin_client):
            return
        try:
            await client.quiesce_to_solo_stopped()
        except Exception as err:
            self.logger.warning(
                "Could not take %s out of its Sendspin session: %s", self.display_name, err
            )

    async def _stop_streaming_locked(self, writer_task: asyncio.Task[None] | None) -> None:
        """
        Serialize streaming teardown.

        :param writer_task: The writer that was running when teardown was
            scheduled. A newer stream having replaced it means this teardown is
            stale and is skipped, so the running writer is left alone.
        """
        async with self._lock:
            if self._writer_task is not writer_task:
                return
            await self._stop_streaming()

    async def _stop_streaming(self) -> None:
        """Stop streaming (internal, called with lock held)."""
        self._is_streaming = False
        # Held as a local across the awaits below: a new stream can install its
        # own writer meanwhile, and this teardown owns only the one it started on.
        if (writer_task := self._writer_task) and not writer_task.done():
            if self.backend == "pulse":
                # PA writer handles CancelledError cleanly
                writer_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await writer_task
            else:
                # sounddevice (ALSA or Darwin): signal gracefully via None sentinel
                while not self._write_queue.empty():
                    self._write_queue.get_nowait()
                self._write_queue.put_nowait(None)
                try:
                    await asyncio.wait_for(writer_task, timeout=_WRITER_STOP_TIMEOUT_SECONDS)
                except TimeoutError:
                    writer_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await writer_task
                except asyncio.CancelledError:
                    pass
            if self._writer_task is writer_task:
                self._writer_task = None
        while not self._write_queue.empty():
            self._write_queue.get_nowait()


class LocalAudioBridgeManager(SendspinBridgeManagerBase[SendspinLocalAudioBridge]):
    """Manages Sendspin bridges for all local audio output devices."""

    def __init__(self, provider: LocalAudioProvider) -> None:
        """Initialize the bridge manager."""
        super().__init__(provider)
        self._discover_lock = asyncio.Lock()
        self._volume_controller: PAVolumeController | None = None
        # player_id (device uuid) -> device info dict from the last enumeration
        self._devices: dict[str, dict[str, Any]] = {}
        self._backend: str = AUDIO_BACKEND_AUTO
        # master sink names with at least one remap-sink child (see _remap_master_sinks)
        self._remap_masters: set[str] = set()
        # sink_name -> PA module index, for sinks local_audio created via
        # _ensure_remap_topology(). Unloaded on stop_all().
        self._loaded_remap_modules: dict[str, int] = {}

    async def discover_and_register(self) -> None:
        """Enumerate output devices, register their players and set up the bridges."""
        if not self.sendspin_server:
            self.logger.debug("Sendspin provider not available, skipping device enumeration")
            return

        async with self._discover_lock:
            # Read audio backend config (Linux only; ignored on Darwin)
            configured_backend: str = str(
                self.provider.config.get_value(CONF_AUDIO_BACKEND) or AUDIO_BACKEND_AUTO
            )
            try:
                resolved_backend, devices = await self.mass.loop.run_in_executor(
                    None, self._enumerate_output_devices, configured_backend
                )
            except Exception as err:
                self.logger.warning("Failed to enumerate audio devices: %s", err)
                return

            self.logger.info(
                "Found %d local audio output device(s) via %s backend",
                len(devices),
                resolved_backend,
            )
            if not devices:
                self.logger.info("No local audio output devices found")
                return

            await self._ensure_volume_controller(resolved_backend)
            if resolved_backend == "pulse":
                devices = await self._apply_card_profiles(devices)
                devices = await self._refresh_after_remap_topology(devices)

            self._backend = resolved_backend
            self._remap_masters = self._remap_master_sinks(devices)
            passthrough_masters = self._remap_masters_with_passthrough(devices)

            device_map: dict[str, dict[str, Any]] = {}
            for device in devices:
                device_name: str = device["name"]
                # A master sink fully covered by its own remap-sink topology
                # (per-zone sinks plus a full-passthrough
                # "_multichannel_stereo" sink) isn't registered as its own
                # player — the passthrough sink already provides "play to
                # all outputs" with independent hardware volume control.
                if not device.get("is_remap", False) and device_name in passthrough_masters:
                    self.logger.debug(
                        "Skipping %s — covered by its remap-sink topology",
                        device.get("description", device_name),
                    )
                    continue
                # Identity is derived from the remap sink's master_device
                # (a property PulseAudio sets natively on every remap sink —
                # not something we write ourselves, so no proplist-parsing
                # risk) plus its zone suffix (recovered via
                # remap_topology.remap_zone_suffix against this sink's own
                # name — reliable since these suffixes are literal strings
                # this module controls). This is independent of the sink's
                # card-index/connector-label naming, which can change for
                # reasons unrelated to this specific device (e.g. a second
                # identical card appearing shifts the first's index) and
                # would otherwise orphan the existing player and silently
                # create a new one in its place. Sinks with no
                # master_device or unrecognized suffix (raw master sinks —
                # 2ch-or-fewer devices, or a multichannel master not yet
                # covered by remap topology) fall back to device_name,
                # which PulseAudio generates from the physical bus path and
                # is already stable on its own. See identity_seed() —
                # SendspinLocalAudioBridge.start() must use the exact same
                # function so the player and its bridge always agree.
                device_map[
                    get_device_uuid(identity_seed(device_name, device), device.get("hostapi", 0))
                ] = device
            self._devices = device_map

            for device_uuid, device in self._devices.items():
                player = self.mass.players.get_player(device_uuid)
                if player is None or player.provider is not self.provider:
                    # not registered yet, or a stale player object left behind by a
                    # previous provider instance (players survive a provider reload):
                    # (re)register so the player is bound to this provider instance
                    player = await self._register_player(
                        device_uuid, self._labeled_display_name(device)
                    )
                if player is None:
                    # registration skipped - the player is disabled
                    continue
                await self.evaluate_bridge(player)

    async def evaluate_bridge(self, player: Player) -> None:
        """
        Reconcile the Sendspin bridge for a player and update the player's availability.

        A local audio player has no playback path other than its bridge, so a
        bridge that should exist but failed to start marks the player
        unavailable (and a successful rebuild restores it).

        :param player: The player to evaluate.
        """
        await super().evaluate_bridge(player)
        if not (self._should_have_bridge(player) and self._lifecycle_allows_bridge(player)):
            return
        available = self._has_bridge(player.player_id)
        if player.available != available:
            player._attr_available = available
            player.update_state()

    async def stop_all(self) -> None:
        """Stop all Sendspin bridges and unload any remap sinks we created."""
        await super().stop_all()
        if self._loaded_remap_modules:
            # Give PA a moment to release active streams on the remap sinks
            # before unloading their modules — unload-module fails if a stream
            # is still open on the sink at the time of the call.
            await asyncio.sleep(0.5)
        if self._volume_controller is not None:
            controller = self._volume_controller
            for sink_name, module_index in self._loaded_remap_modules.items():
                with suppress(OSError):
                    ok = await self.mass.loop.run_in_executor(
                        None, controller.unload_module, module_index
                    )
                    if not ok:
                        self.logger.warning("Failed to unload remap sink %s", sink_name)
            self._loaded_remap_modules.clear()
            with suppress(OSError):
                await self.mass.loop.run_in_executor(None, self._volume_controller.close)
            self._volume_controller = None

    @staticmethod
    def _labeled_display_name(device: dict[str, Any]) -> str:
        """
        Return a device's display name with connector-type and hardware-card labels applied.

        Applies hdmi/analog/usb labeling (see remap_topology.connector_label)
        so a raw master sink registering as its own player — a 2ch-or-fewer
        card, or a multichannel card not yet covered by remap-sink topology —
        doesn't rely solely on PulseAudio's own per-profile description,
        which gives no distinguishing signal when two identical cards
        (same alsa_card_name, same profile) produce identical descriptions.

        Skips adding the connector label if PA's own description already
        mentions it (case-insensitive) — e.g. an HDMI port's description is
        often already "... Digital Stereo (HDMI 2)" — to avoid a redundant
        "(HDMI) (HDMI 2)"-style display name.

        Also appends a short hardware tag (see short_hardware_tag) — but
        only for a raw master sink, derived from its own PA sink name. A
        remap-sink zone's tag is embedded directly into its description by
        compute_remap_topology (positioned before the zone suffix, e.g.
        "Creative_X_Fi_analog_[cbf7]_front_stereo"), so that sorting
        players by name groups every zone of the same physical card
        together — appending another tag here would duplicate it.
        """
        raw_name: str = device.get("description", device["name"])
        label = connector_label(device.get("device_bus"), device["name"])
        if label and label.lower() not in raw_name.lower():
            raw_name = f"{raw_name} ({label.upper()})"
        if device.get("master_device"):
            # Remap-sink zone: tag is already embedded in raw_name above.
            return raw_name
        return f"{raw_name} [{short_hardware_tag(device['name'])}]"

    async def _ensure_volume_controller(self, resolved_backend: str) -> None:
        """
        Lazily connect the shared PAVolumeController for hardware sink volume.

        No-op if not the pulse backend or already connected. Logs and leaves
        it as None on failure — bridges fall back to software volume scaling.
        """
        if resolved_backend != "pulse" or self._volume_controller is not None:
            return
        try:
            self._volume_controller = await self.mass.loop.run_in_executor(None, PAVolumeController)
            self.logger.debug("Hardware volume control connected (PAVolumeController)")
        except OSError as err:
            self.logger.warning(
                "Hardware volume control unavailable, falling back to software volume scaling: %s",
                err,
            )

    @staticmethod
    def _remap_master_sinks(devices: list[dict[str, Any]]) -> set[str]:
        """
        Return master sink names with at least one remap-sink child.

        Such masters must keep software volume control — their PA volume
        multiplies into every remap sink feeding through them. Masters with
        no remap children (e.g. remap addon disabled) can safely use
        hardware volume control like any standalone sink.
        """
        return {d["master_device"] for d in devices if d.get("is_remap") and d.get("master_device")}

    @staticmethod
    def _remap_masters_with_passthrough(devices: list[dict[str, Any]]) -> set[str]:
        """
        Return master sink names with a full-passthrough remap-sink child.

        A passthrough child has the same channel count as its master (a 1:1
        remap, e.g. the "_multichannel_stereo" sink from
        compute_remap_topology). Such masters are fully covered by their remap-sink topology (per-zone
        sinks plus the passthrough for "play to all outputs") and should not
        also be registered as their own player.
        """
        master_channels = {
            d["name"]: d.get("max_output_channels", 0) for d in devices if not d.get("is_remap")
        }
        result: set[str] = set()
        for d in devices:
            master = d.get("master_device")
            if (
                d.get("is_remap")
                and master
                and master_channels.get(master) == d.get("max_output_channels")
            ):
                result.add(master)
        return result

    async def _ensure_remap_topology(self, devices: list[dict[str, Any]]) -> bool:
        """
        Create any missing per-zone/passthrough remap sinks for multi-channel cards.

        For each ALSA-card master sink with more than 2 channels, computes
        the expected remap-sink topology (see remap_topology module) and
        loads module-remap-sink.c for any sink not already present by name.
        Idempotent: sinks that already exist (created by a previous run, or
        matching this naming convention) are left untouched.

        :param devices: Current enumerate_pa_sinks() result.
        :returns: True if any new modules were loaded (caller should
            re-enumerate to pick up the new sinks).
        """
        if self._volume_controller is None:
            return False
        controller = self._volume_controller
        existing_names = {d["name"] for d in devices}
        loaded_any = False

        for device in devices:
            # Master sinks are anything that isn't itself a remap-sink
            # child (is_remap=False) — this is more robust than matching
            # the "driver" field, since PipeWire's PulseAudio-compatibility
            # layer reports a generic "PipeWire" driver string for every
            # sink it manages (ALSA-backed or not), unlike real PulseAudio
            # which reports the literal module name (e.g.
            # "module-alsa-card.c"). alsa_card_name and channel_map being
            # present (checked below) is what actually confirms this is a
            # real ALSA-backed multi-channel sink worth computing a remap
            # topology for, rather than e.g. a virtual/monitor/null sink.
            if device.get("is_remap"):
                continue
            channels: int = device.get("max_output_channels", 0)
            if channels <= 2:
                continue
            alsa_card_name = device.get("alsa_card_name")
            channel_map: list[str] = device.get("channel_map", [])
            if not alsa_card_name or not channel_map:
                continue

            master_sink_name: str = device["name"]
            # Label the connector type (hdmi/analog/usb) so an HDMI output
            # and an analog output on the same physical chip — which share
            # an identical alsa_card_name and would otherwise differ only
            # by an opaque hardware tag — are distinguishable at a glance.
            # Applied unconditionally since it's informative on its own,
            # not just a collision workaround.
            label = connector_label(device.get("device_bus"), master_sink_name)

            # Hardware tag disambiguates identical cards (see
            # short_hardware_tag()'s docstring) — derived from this card's
            # own master sink name, which PulseAudio generates from the
            # physical bus path, so it stays the same across reboots
            # regardless of ALSA card-index enumeration order. Applied
            # unconditionally, not just when a collision is detected: a
            # card's sink-name prefix must never change later purely
            # because a second identical card was added — that would be a
            # silent rename of every remap sink (and, before this change,
            # historically risked drifting the PA sink name out of step
            # with the stable player identity computed in identity_seed(),
            # which does NOT depend on this prefix).
            hardware_tag = short_hardware_tag(master_sink_name)
            card_name = normalize_card_name(alsa_card_name, hardware_tag, label)
            # Untagged prefix for each sink's PA "description" base — the
            # tag itself is embedded separately, before the zone suffix
            # (see compute_remap_topology's hardware_tag param), so player
            # sorting groups every zone of the same physical card together.
            display_prefix = normalize_card_name(alsa_card_name, None, label)
            for spec in compute_remap_topology(
                card_name, channel_map, channels, display_prefix, hardware_tag
            ):
                if spec.sink_name in existing_names:
                    continue
                argument = build_remap_sink_argument(spec, master_sink_name)
                module_index = await self.mass.loop.run_in_executor(
                    None, controller.load_module, "module-remap-sink", argument
                )
                if module_index is None:
                    self.logger.warning("Failed to create remap sink %s", spec.sink_name)
                    continue
                self.logger.info(
                    "Created remap sink %s (master=%s, module=%d)",
                    spec.sink_name,
                    master_sink_name,
                    module_index,
                )
                self._loaded_remap_modules[spec.sink_name] = module_index
                existing_names.add(spec.sink_name)
                loaded_any = True

            # Suspend/resume the master sink to reset the underlying ALSA
            # driver state. Specifically works around the snd_ctxfi mmap bug
            # (kernel 6.12.x, commit 391e69143d0a) where the X-Fi card's DMA
            # stalls after init, causing pa_simple_write to timeout silently.
            # Running this on every ALSA-card master at topology creation time
            # is safe — it only takes ~0.5s and has no effect on cards that
            # don't have the bug.
            await self.mass.loop.run_in_executor(None, suspend_resume_sink, master_sink_name)

            # Force-unmute any "* Playback Switch" ALSA mixer controls on
            # this card. On some multi-instance card setups, one or more
            # channel-enable switches (e.g. surround/center/side) default to
            # muted on the second-enumerated card even though DMA is
            # confirmed running and volume/routing are correct — the card
            # is silently producing no output on those channels. Safe to run
            # unconditionally: a no-op on cards that don't expose these
            # controls or that are already unmuted.
            device_alsa_card_index = device.get("alsa_card_index")
            if device_alsa_card_index:
                unmute_status = await self.mass.loop.run_in_executor(
                    None, unmute_playback_switches, str(device_alsa_card_index)
                )
                if unmute_status.startswith("ok") and "set_failed" not in unmute_status:
                    self.logger.debug(
                        "unmute_playback_switches on ALSA card %s: %s",
                        device_alsa_card_index,
                        unmute_status,
                    )
                elif unmute_status in ("no_libasound", "attach_failed"):
                    # Expected in containerized deployments without direct
                    # /dev/snd access — suspend_resume_sink via PA is
                    # unaffected either way.
                    self.logger.debug(
                        "unmute_playback_switches on ALSA card %s did not "
                        "complete (%s) — likely no direct /dev/snd access "
                        "from this process; suspend_resume_sink via PA is "
                        "unaffected",
                        device_alsa_card_index,
                        unmute_status,
                    )
                else:
                    # open_failed / register_failed / load_failed mean the
                    # process DID have some libasound access but the mixer
                    # sequence broke partway through — a real anomaly, not
                    # the expected no-/dev/snd-access case above.
                    # "set_failed" means a target element was correctly
                    # identified as muted but the actual unmute call itself
                    # reported failure — that element's true state is
                    # unknown, worth surfacing rather than assuming success.
                    self.logger.warning(
                        "unmute_playback_switches on ALSA card %s reported "
                        "%s — surround/center/side channels on this card "
                        "may still be muted; suspend_resume_sink via PA is "
                        "unaffected",
                        device_alsa_card_index,
                        unmute_status,
                    )

            # Pin the master sink to 100% so it never attenuates remap sinks
            # feeding through it. The master has no bridge of its own (it's
            # skipped from player registration), so _reset_sink_volume() is
            # never called for it from a bridge's start() path — we have to
            # do it here explicitly. Without this, module-device-restore or
            # any other PA client can leave the master at an arbitrary volume,
            # stacking an extra hidden attenuation on top of every remap sink.
            with suppress(OSError):
                await self.mass.loop.run_in_executor(
                    None,
                    controller.set_sink_volume,
                    master_sink_name,
                    100,
                    channels,
                )

        if loaded_any:
            # module-device-restore may restore a persisted per-sink-name
            # volume asynchronously shortly after sink creation, which can
            # race with and overwrite _apply_hardware_volume() during
            # bridge.start(). Give it a moment to settle first so our
            # cached-volume application (run after re-enumeration) wins.
            await asyncio.sleep(0.3)

        return loaded_any

    def _bridge_client_id(self, player: Player) -> str | None:
        """Return the Sendspin client_id used to bridge a local audio player."""
        return bridge_client_id_from_uuid(player.player_id)

    def _create_bridge(self, player: Player) -> SendspinLocalAudioBridge:
        """Create a bridge instance for a local audio player."""
        sendspin_server = self.sendspin_server
        assert sendspin_server is not None  # guaranteed by _lifecycle_allows_bridge
        device = self._devices[player.player_id]
        return SendspinLocalAudioBridge(
            cast("LocalAudioProvider", self.provider),
            device,
            sendspin_server,
            backend=self._backend,
            volume_controller=self._volume_controller,
            has_remap_children=device["name"] in self._remap_masters,
        )

    def _should_have_bridge(self, player: Player) -> bool:
        """Return whether a local audio player should have a Sendspin bridge."""
        return player.player_id in self._devices

    async def _register_player(self, device_uuid: str, display_name: str) -> Player | None:
        """
        Register a local audio output device as a regular, visible player.

        The player has no native playback features of its own — the Sendspin
        bridge provides its (single) output protocol — and its enabled flag
        is the on/off toggle for the device.

        :param device_uuid: The stable device UUID, used as player_id.
        :param display_name: The human friendly device name.
        :return: The registered player, or None when the player is disabled
            (registration of disabled players is skipped).
        """
        player = Player(self.provider, device_uuid)
        player._attr_name = display_name
        player._attr_available = True
        player._attr_supported_features = set()  # no direct playback capability
        player._attr_device_info = DeviceInfo(model=display_name, manufacturer="Local Audio")
        player._attr_device_info.add_identifier(IdentifierType.UUID, device_uuid)
        await self.mass.players.register_or_update(player)
        return self.mass.players.get_player(device_uuid)

    async def _refresh_after_remap_topology(
        self, devices: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """
        Create missing remap sinks and re-enumerate if any were added.

        :returns: The original devices list, or a freshly re-enumerated list
            if _ensure_remap_topology() loaded any new modules.
        """
        if not await self._ensure_remap_topology(devices):
            return devices
        try:
            new_devices = await self.mass.loop.run_in_executor(None, enumerate_pa_sinks)
        except (FileNotFoundError, RuntimeError) as err:
            self.logger.warning("Failed to re-enumerate after creating remap sinks: %s", err)
            return devices
        self.logger.info(
            "Found %d local audio output device(s) after creating remap sinks", len(new_devices)
        )
        return new_devices

    async def _apply_card_profiles(self, devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Resolve and apply card profiles for cards local_audio is using.

        Runs the card_profiles resolver (most output channels,
        duplex-preferred; see that module) over every card backing a
        currently-enumerated output sink and activates any decided
        switch. Set-only-if-different by construction — the resolver
        returns no target for an already-correct card — and PA's
        module-card-restore persists an applied switch, so after the
        first successful run every subsequent provider start is a no-op
        that logs "keeping" for each card.

        Per-card user overrides (the settings-page dropdowns) take
        precedence over the automatic policy; a card set to "auto" or
        never configured follows the resolver.

        :param devices: Current enumerate_pa_sinks() result.
        :returns: The original devices list, or a freshly re-enumerated
            list if any profile was switched — a profile switch tears
            down the card's old sinks and creates the new profile's
            sinks, so the pre-switch enumeration is stale for that card.
        """
        try:
            cards = await self.mass.loop.run_in_executor(None, enumerate_pa_cards)
        except (FileNotFoundError, RuntimeError) as err:
            self.logger.debug("Card profile inspection unavailable: %s", err)
            return devices
        # Per-card user overrides from the provider config. Keys are derived
        # from the card's stable PA name (see conf_card_profile_key); a card
        # whose entry is absent (never saved — get_value returns None for
        # unknown keys) or set to "auto" uses the automatic policy.
        overrides: dict[str, str] = {}
        for card in cards:
            value = self.provider.config.get_value(conf_card_profile_key(card.name))
            if value and str(value) != PROFILE_AUTO:
                overrides[card.name] = str(value)
        switched_any = False
        for decision in plan_profile_changes(cards, devices, overrides=overrides):
            if not decision.target_profile:
                self.logger.debug(
                    "Card %s (%s): keeping profile %s (%s)",
                    decision.card_display_name,
                    decision.card_name,
                    decision.current_profile,
                    decision.reason,
                )
                continue
            ok = await self.mass.loop.run_in_executor(
                None, set_card_profile, decision.card_name, decision.target_profile
            )
            if ok:
                switched_any = True
                self.logger.info(
                    "Card %s (%s): switched profile %s -> %s (%s)",
                    decision.card_display_name,
                    decision.card_name,
                    decision.current_profile,
                    decision.target_profile,
                    decision.reason,
                )
            else:
                self.logger.warning(
                    "Card %s (%s): failed to switch profile %s -> %s — "
                    "continuing with the active profile",
                    decision.card_display_name,
                    decision.card_name,
                    decision.current_profile,
                    decision.target_profile,
                )
        if not switched_any:
            return devices
        # A profile switch replaces the card's sinks; give PA a moment to
        # finish creating them (mirrors the settle waits used elsewhere in
        # this file) before re-enumerating.
        await asyncio.sleep(0.5)
        try:
            new_devices = await self.mass.loop.run_in_executor(None, enumerate_pa_sinks)
        except (FileNotFoundError, RuntimeError) as err:
            self.logger.warning("Failed to re-enumerate after switching card profiles: %s", err)
            return devices
        self.logger.info(
            "Found %d local audio output device(s) after switching card profiles",
            len(new_devices),
        )
        return new_devices

    @staticmethod
    def _enumerate_output_devices(backend: str) -> tuple[str, list[dict[str, Any]]]:
        """
        Enumerate available audio output devices for the given backend.

        :param backend: One of AUDIO_BACKEND_AUTO / AUDIO_BACKEND_PULSEAUDIO /
            AUDIO_BACKEND_ALSA (from constants).  On non-Linux the backend is
            always resolved to "sounddevice".
        :returns: Tuple of (resolved_backend, device_list).
        """
        if sys.platform != "linux":
            import sounddevice as _sd  # noqa: PLC0415

            devices: list[dict[str, Any]] = []
            for idx, dev in enumerate(_sd.query_devices()):
                if dev.get("max_output_channels", 0) < 2:
                    continue
                try:
                    test_stream = _sd.RawOutputStream(
                        device=idx,
                        samplerate=BRIDGE_SAMPLE_RATE,
                        channels=BRIDGE_CHANNELS,
                        dtype="int16",
                    )
                    test_stream.close()
                except _sd.PortAudioError:
                    continue
                dev_with_index = dict(dev)
                dev_with_index["index"] = idx
                devices.append(dev_with_index)
            return "sounddevice", devices

        # Linux — dispatch by configured backend
        if backend == AUDIO_BACKEND_ALSA:
            return "alsa", enumerate_alsa_devices()

        if backend == AUDIO_BACKEND_PULSEAUDIO:
            return "pulse", enumerate_pa_sinks()

        # AUTO: try PA, fall back to ALSA
        try:
            sinks = enumerate_pa_sinks()
            if sinks:
                return "pulse", sinks
        except (FileNotFoundError, RuntimeError):  # fmt: skip
            pass
        return "alsa", enumerate_alsa_devices()
