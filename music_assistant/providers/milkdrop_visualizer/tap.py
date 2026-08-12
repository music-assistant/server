"""
Sendspin side of the MilkDrop visualizer: the audio tap and its lifecycle.

An in-process bridge visualizer role (the pattern the Hue Lights Sync plugin
uses) joins the target player's Sendspin group and turns its PCM into packed
waveform frames. One tap is shared by every viewer of the same target player.
"""

from __future__ import annotations

import asyncio
import hashlib
import struct
from collections import deque
from typing import TYPE_CHECKING, cast

import numpy as np
from aiosendspin.models.core import ClientHelloPayload
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from aiosendspin.models.visualizer import ClientHelloVisualizerSupport
from aiosendspin.server.roles.registry import register_role
from music_assistant_models.enums import PlaybackState

from music_assistant.providers.sendspin.bridge_role import BridgeVisualizerRole

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiosendspin.models.visualizer import BeatTiming
    from aiosendspin.server import SendspinClient
    from aiosendspin.server.roles import AudioChunk

    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.sendspin.player import SendspinBasePlayer
    from music_assistant.providers.sendspin.provider import SendspinProvider

    from .provider import MilkdropVisualizerProvider

MILKDROP_ROLE_ID = "visualizer@_milkdrop"
WAVE_SAMPLES = 1024
# How long a viewerless tap stays attached, so refreshes and the next track
# rejoin instantly instead of hitting the mid-stream join gap.
TAP_LINGER_SECONDS = 600


def get_sendspin_provider(mass: MusicAssistant) -> SendspinProvider | None:
    """Return the loaded Sendspin provider, if available."""
    return cast("SendspinProvider | None", mass.get_provider("sendspin"))


class MilkdropWaveRole(BridgeVisualizerRole):
    """
    Bridge visualizer role that emits raw waveform tails instead of features.

    One 1024-sample mono uint8 offset-binary tail per audio chunk, timestamped
    at chunk end in the server clock domain.
    """

    def __init__(self, client: SendspinClient) -> None:
        """
        Initialize the wave tap role.

        :param client: The Sendspin client this role belongs to.
        """
        super().__init__(client)
        self._wave_cb: Callable[[int, bytes], None] | None = None
        self._mono = np.zeros(0, dtype=np.float32)

    @property
    def role_id(self) -> str:
        """Return role identifier."""
        return MILKDROP_ROLE_ID

    def set_wave_callback(self, wave_cb: Callable[[int, bytes], None]) -> None:
        """
        Set the callback receiving (timestamp_us, 1024 uint8 sample bytes).

        :param wave_cb: Called once per audio chunk after warmup.
        """
        self._wave_cb = wave_cb

    def on_stream_start(self) -> None:
        """Reset the rolling buffer for a new stream (no feature extractor)."""
        self._mono = np.zeros(0, dtype=np.float32)
        if self._on_stream_start_cb:
            self._on_stream_start_cb()

    def on_audio_chunk(self, chunk: AudioChunk) -> None:
        """Append PCM and emit the waveform tail ending at this chunk."""
        if self._wave_cb is None:
            return
        raw = np.frombuffer(chunk.data, dtype="<i2")
        # A truncated chunk must not raise into the push stream's delivery
        # path: drop the dangling sample rather than fail the reshape.
        if raw.size % 2:
            raw = raw[:-1]
        if raw.size == 0:
            return
        mono = raw.reshape(-1, 2).mean(axis=1, dtype=np.float32) / 32768.0
        self._mono = np.concatenate([self._mono, mono])
        if self._mono.size >= WAVE_SAMPLES:
            tail = self._mono[-WAVE_SAMPLES:]
            quantized: np.ndarray = np.rint(np.clip(tail, -1.0, 1.0) * 127.0 + 128.0)
            ts_us = chunk.timestamp_us + chunk.duration_us
            self._wave_cb(ts_us, quantized.astype(np.uint8).tobytes())
            self._mono = self._mono[-WAVE_SAMPLES:]

    def on_stream_clear(self) -> None:
        """Reset the rolling buffer on seek/clear."""
        self._mono = np.zeros(0, dtype=np.float32)
        if self._on_stream_clear_cb:
            self._on_stream_clear_cb()

    def on_stream_end(self) -> None:
        """Reset the rolling buffer at stream end."""
        self._mono = np.zeros(0, dtype=np.float32)
        if self._on_stream_end_cb:
            self._on_stream_end_cb()


register_role(MILKDROP_ROLE_ID, lambda client: MilkdropWaveRole(client=client))


class ViewerQueue:
    """
    Outbound queue for one viewer.

    Bounded so a stalled browser cannot stall the tap, but control messages
    (stream/clear, stream/end) are never dropped: losing one would leave the
    viewer animating stale audio after a seek or track change.
    """

    def __init__(self, capacity: int = 4096) -> None:
        """
        Initialize the queue.

        :param capacity: Maximum number of pending items before eviction kicks in.
        """
        self._items: deque[bytes | str] = deque()
        self._capacity = capacity
        self._wakeup = asyncio.Event()

    def push(self, item: bytes | str) -> None:
        """Enqueue an item, evicting the oldest waveform frame when full."""
        if len(self._items) >= self._capacity:
            for index, queued in enumerate(self._items):
                if isinstance(queued, bytes):
                    del self._items[index]
                    break
            else:
                self._items.popleft()
        self._items.append(item)
        self._wakeup.set()

    async def get(self) -> bytes | str:
        """Wait for and return the next item."""
        while not self._items:
            self._wakeup.clear()
            await self._wakeup.wait()
        return self._items.popleft()


class Tap:
    """One in-process tap client shared by all viewers of the same target player."""

    def __init__(self, client_id: str) -> None:
        """
        Initialize the tap.

        :param client_id: Sendspin client id of the hidden tap player.
        """
        self.client_id = client_id
        self.queues: set[ViewerQueue] = set()
        self.frames_seen = False
        # Beat frames with their scheduled timestamps, so viewers that attach
        # mid-track still receive the rest of the track's downbeats.
        self.beats: deque[tuple[int, bytes]] = deque(maxlen=4096)
        # Rolling history of packed waveform frames (~100s at 40fps), replayed
        # to a connecting viewer. Without it a late viewer only receives frames
        # stamped at the production cursor, which on long-lead players runs
        # tens of seconds ahead of what is audible.
        self.ring: deque[bytes] = deque(maxlen=4096)

    def fan_out(self, frame: bytes | str) -> None:
        """Deliver a packed frame to every attached viewer queue."""
        for queue in self.queues:
            queue.push(frame)


class TapManager:
    """Creates, shares and tears down the waveform taps."""

    def __init__(self, provider: MilkdropVisualizerProvider) -> None:
        """
        Initialize the tap manager.

        :param provider: The loaded MilkDrop visualizer provider instance.
        """
        self.mass = provider.mass
        self.logger = provider.logger.getChild("tap")
        # One shared tap per target player id, refcounted by viewer queues.
        self._taps: dict[str, Tap] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, target: SendspinBasePlayer) -> Tap:
        """
        Return the shared tap for a target player, creating it on first use.

        :param target: The Sendspin player whose group to tap.
        """
        async with self._lock:
            if (existing := self._taps.get(target.player_id)) is not None:
                return existing
            # Stable id: reconnects and multiple viewers reuse one hidden tap
            # player. Hash the full id rather than slicing its tail, so two
            # players sharing the same last characters cannot collide onto one
            # Sendspin client id.
            digest = hashlib.blake2s(target.player_id.encode(), digest_size=6).hexdigest()
            tap = Tap(f"milkdrop-{digest}")
            viz_client = self._register_client(tap)
            await target.api.group.add_client(viz_client)
            self._taps[target.player_id] = tap
            self.logger.info(
                "Waveform tap %s attached to group of %s (state=%s)",
                tap.client_id,
                target.display_name,
                target.playback_state,
            )
            if target.playback_state == PlaybackState.PLAYING:
                self.mass.create_task(self._late_join_watchdog(target, tap, viz_client))
            return tap

    def schedule_release(self, target_player_id: str) -> None:
        """
        Start the linger countdown for a tap whose viewer just left.

        Keyed per target with abort_existing, so a target never accumulates
        countdowns: an earlier one would otherwise still be sleeping and could
        tear down a tap that a later viewer created.

        :param target_player_id: The player whose tap may now be idle.
        """
        self.mass.create_task(
            self._linger(target_player_id),
            task_id=f"milkdrop_linger_{target_player_id}",
            abort_existing=True,
        )

    async def close(self) -> None:
        """Tear down every live tap."""
        async with self._lock:
            sendspin = get_sendspin_provider(self.mass)
            for tap in self._taps.values():
                if sendspin is not None:
                    await sendspin.server_api.remove_client(tap.client_id)
            self._taps.clear()

    def pending_beat_frames(self, tap: Tap) -> list[bytes]:
        """
        Return the tap's beat frames that are still in the future.

        :param tap: The tap whose beat schedule to filter.
        """
        sendspin = get_sendspin_provider(self.mass)
        if sendspin is None or not tap.beats:
            return []
        now_us = sendspin.server_api.clock.now_us()
        return [frame for ts_us, frame in tap.beats if ts_us > now_us]

    async def _linger(self, target_player_id: str) -> None:
        """
        Remove a tap once it has been viewerless for the linger window.

        The linger keeps the tap in the group across refreshes and track
        changes during a viewing session, so subsequent streams are covered
        from their start (avoiding the mid-stream join gap).
        """
        tap = self._taps.get(target_player_id)
        if tap is None or tap.queues:
            return
        await asyncio.sleep(TAP_LINGER_SECONDS)
        async with self._lock:
            tap = self._taps.get(target_player_id)
            if tap is None or tap.queues:
                return
            self._taps.pop(target_player_id, None)
            if (sendspin := get_sendspin_provider(self.mass)) is not None:
                await sendspin.server_api.remove_client(tap.client_id)
            self.logger.info("Waveform tap %s removed (viewers gone)", tap.client_id)

    def _register_client(self, tap: Tap) -> SendspinClient:
        """Register the in-process tap client and wire its callbacks."""

        def on_wave(ts_us: int, samples: bytes) -> None:
            tap.frames_seen = True
            frame = struct.pack(">Bq", 22, ts_us) + samples
            tap.ring.append(frame)
            tap.fan_out(frame)

        def on_beats(beats: list[BeatTiming]) -> None:
            # Once per track, so cheap enough to keep: without it there is no
            # way to tell a missing beat schedule from a viewer-side problem.
            self.logger.debug(
                "Tap %s received a schedule of %s beat(s), %s downbeat(s)",
                tap.client_id,
                len(beats),
                sum(1 for beat in beats if beat.is_downbeat),
            )
            for beat in beats:
                frame = struct.pack(">BqB", 17, beat.timestamp_us, 1 if beat.is_downbeat else 0)
                tap.beats.append((beat.timestamp_us, frame))
                tap.fan_out(frame)

        def on_stream_boundary(message: str) -> None:
            # Buffered frames and the beat schedule both belong to the audio
            # that just ended; drop them so viewers do not replay stale state.
            tap.ring.clear()
            tap.beats.clear()
            tap.fan_out(message)

        sendspin = get_sendspin_provider(self.mass)
        if sendspin is None:
            msg = "Sendspin provider is not available"
            raise RuntimeError(msg)
        # Taps must never surface as MA players (no group chips, no UI churn).
        sendspin.register_headless_client(tap.client_id)
        support = ClientHelloVisualizerSupport(buffer_capacity=65536, rate_max=60, types=["beat"])
        hello = ClientHelloPayload(
            client_id=tap.client_id,
            name="MilkDrop Visualizer",
            version=1,
            supported_roles=[MILKDROP_ROLE_ID],
            device_info=SendspinDeviceInfo(
                manufacturer="Music Assistant", product_name="MilkDrop Visualizer"
            ),
            visualizer_support=support,
        )
        viz_client = sendspin.server_api.register_external_player(
            hello, on_stream_start=lambda _req: None
        )
        role = cast("MilkdropWaveRole", viz_client.roles_by_family("visualizer")[0])
        role.set_wave_callback(on_wave)
        role.set_callbacks(
            on_frame=lambda _frame: None,
            on_beats=on_beats,
            on_beats_clear=lambda: None,
            on_stream_start=lambda: None,
            # Forward stream boundaries so viewers drop buffered future frames
            # immediately on skip/seek/stop instead of draining them.
            on_stream_clear=lambda: on_stream_boundary('{"type": "stream/clear"}'),
            on_stream_end=lambda: on_stream_boundary('{"type": "stream/end"}'),
        )
        role.setup_visualizer(support)
        viz_client.attach_preinitialized_roles()
        return viz_client

    async def _late_join_watchdog(
        self, target: SendspinBasePlayer, tap: Tap, viz_client: SendspinClient
    ) -> None:
        """
        Re-kick a tap that joined an active stream but received no audio.

        Workaround for an aiosendspin quirk: joining a running stream does not
        always wire a fresh in-process role into ongoing chunk delivery, so
        frames would only start at the next stream start. Delete this once it
        is fixed upstream.
        """
        await asyncio.sleep(2.5)
        if tap.frames_seen or not tap.queues:
            return
        # close() (a provider reload) may have removed this tap during the sleep;
        # re-adding then would leave a stray client with no manager entry.
        if self._taps.get(target.player_id) is not tap:
            return
        self.logger.info("Waveform tap %s got no audio after late join, re-kicking", tap.client_id)
        try:
            await target.api.group.remove_client(viz_client)
            await target.api.group.add_client(viz_client)
        except Exception:
            self.logger.exception("Waveform tap %s re-kick failed", tap.client_id)
            return
        await asyncio.sleep(2.5)
        if not tap.frames_seen and tap.queues:
            self.logger.warning(
                "Waveform tap %s still idle after re-kick; pause/resume playback to activate",
                tap.client_id,
            )
