"""
Reusable bridge roles for in-process Sendspin integrations.

Provides a BridgePlayerRole that receives audio from Sendspin's PushStream
and forwards it to an external player via callbacks. This role can be used
by any bridge implementation (AirPlay, etc.) to integrate external players
with Sendspin's synchronization and timing.

Also provides BridgeVisualizerRole and BridgeColorRole for in-process
consumers of the visualization pipeline (e.g. Hue Entertainment): feature
extraction runs against the group's audio and results are delivered via
callbacks instead of a WebSocket connection.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from aiosendspin.models.core import ServerStateMessage
from aiosendspin.models.visualizer import BeatAvailability, StreamStartVisualizer
from aiosendspin.server import VolumeChangedEvent
from aiosendspin.server.roles import AudioRequirements, Role
from aiosendspin.server.roles.registry import register_role
from aiosendspin.server.roles.visualizer.features import VisualizerFeatureExtractor

from music_assistant.mass import LOGGER

if TYPE_CHECKING:
    from aiosendspin.models.core import ServerStatePayload
    from aiosendspin.models.types import ServerMessage
    from aiosendspin.models.visualizer import BeatTiming, ClientHelloVisualizerSupport
    from aiosendspin.server import SendspinClient
    from aiosendspin.server.roles import AudioChunk
    from aiosendspin.server.roles.visualizer.features import ExtractedFrame

# Audio format constants for bridge players.
BRIDGE_SAMPLE_RATE = 44100
BRIDGE_BIT_DEPTH = 16
BRIDGE_CHANNELS = 2
BRIDGE_BYTES_PER_SAMPLE = BRIDGE_BIT_DEPTH // 8

BRIDGE_ROLE_ID = "player@_bridge"
VISUALIZER_BRIDGE_ROLE_ID = "visualizer@_bridge"
COLOR_BRIDGE_ROLE_ID = "color@_bridge"


class BridgePlayerRole(Role):
    """
    Custom Sendspin player role for external player bridges.

    This role receives audio from Sendspin's PushStream and forwards it
    to an external player via callbacks. It bypasses the normal WebSocket
    audio delivery since external players don't have a WebSocket connection.

    Created by the role factory registry. After creation, the bridge must
    call set_callbacks() to wire up audio/volume/stream callbacks.
    """

    def __init__(self, client: SendspinClient) -> None:
        """
        Initialize the bridge player role.

        :param client: The Sendspin client this role belongs to.
        """
        self._client = client
        self._on_audio_chunk_cb: Callable[[AudioChunk], None] | None = None
        self._on_volume_change_cb: Callable[[int], None] | None = None
        self._on_mute_change_cb: Callable[[bool], None] | None = None
        self._on_stream_start_cb: Callable[[], None] | None = None
        self._on_stream_end_cb: Callable[[], None] | None = None
        self._on_explicit_stop_cb: Callable[[], None] | None = None
        self._audio_requirements: AudioRequirements | None = None
        self._volume: int = 100
        self._muted: bool = False
        # Bridge-specific timing reported to PushStream so server scheduling
        # accounts for the downstream player's startup and jitter requirements.
        self._required_lead_time_ms: int = 0
        self._min_buffer_ms: int = 0

    def set_callbacks(
        self,
        *,
        on_audio_chunk: Callable[[AudioChunk], None],
        on_volume_change: Callable[[int], None],
        on_mute_change: Callable[[bool], None],
        on_stream_start: Callable[[], None],
        on_stream_end: Callable[[], None],
        on_explicit_stop: Callable[[], None] | None = None,
        initial_volume: int = 100,
        initial_muted: bool = False,
    ) -> None:
        """
        Wire up bridge callbacks after role creation.

        :param on_audio_chunk: Callback to receive audio chunks.
        :param on_volume_change: Callback when volume level changes.
        :param on_mute_change: Callback when mute state changes.
        :param on_stream_start: Callback when the stream starts.
        :param on_stream_end: Callback when the stream ends.
        :param on_explicit_stop: Callback when playback was ended by an explicit
            user command (stop, or removal from the group), so the bridge can
            silence its player at once instead of keeping its transport warm.
        :param initial_volume: Initial volume level (0-100).
        :param initial_muted: Initial mute state.
        """
        self._on_audio_chunk_cb = on_audio_chunk
        self._on_volume_change_cb = on_volume_change
        self._on_mute_change_cb = on_mute_change
        self._on_stream_start_cb = on_stream_start
        self._on_stream_end_cb = on_stream_end
        self._on_explicit_stop_cb = on_explicit_stop
        self.update_player_state(volume=initial_volume, muted=initial_muted)

    @property
    def role_id(self) -> str:
        """Return role identifier."""
        return BRIDGE_ROLE_ID

    @property
    def role_family(self) -> str:
        """Return role family name."""
        return "player"

    def setup_audio_requirements(
        self,
        sample_rate: int = BRIDGE_SAMPLE_RATE,
        bit_depth: int = BRIDGE_BIT_DEPTH,
        channels: int = BRIDGE_CHANNELS,
    ) -> None:
        """
        Set up audio requirements for bridge PCM format.

        Call with the sink's native rate/depth so MA transcodes to the
        correct format before delivering chunks. Defaults to the bridge
        constants for callers that don't need format negotiation.
        """
        self._audio_requirements = AudioRequirements(
            sample_rate=sample_rate,
            bit_depth=bit_depth,
            channels=channels,
            transformer=None,
        )

    @property
    def preferred_format(self) -> AudioRequirements | None:
        """Return the audio format declared via setup_audio_requirements."""
        return self._audio_requirements

    def get_audio_requirements(self) -> AudioRequirements | None:
        """Return audio requirements for PushStream."""
        return self._audio_requirements

    def set_timing(self, *, required_lead_time_ms: int, min_buffer_ms: int) -> None:
        """
        Configure timing values reported to PushStream for scheduling.

        Call before or during a stream session to adjust the lead time and
        ongoing buffer floor reported to the Sendspin send-ahead calculation.
        Bridge implementations should reflect their downstream player's real
        startup latency (CLI spawn, protocol handshake, device buffer fill)
        and jitter requirements.
        """
        self._required_lead_time_ms = max(0, required_lead_time_ms)
        self._min_buffer_ms = max(0, min_buffer_ms)

    def get_required_lead_time_us(self) -> int:
        """Return bridge startup lead time in microseconds."""
        return self._required_lead_time_ms * 1_000

    def get_min_buffer_us(self) -> int:
        """Return bridge minimum ongoing buffer duration in microseconds."""
        return self._min_buffer_ms * 1_000

    def get_player_volume(self) -> int | None:
        """Return current volume level."""
        return self._volume

    def get_player_muted(self) -> bool | None:
        """Return current mute state."""
        return self._muted

    def set_player_volume(self, volume: int) -> None:
        """
        Set volume and notify bridge.

        The level is on the scale the role reports, and is stored, announced to
        the client and handed to the bridge unchanged.
        """
        self._volume = volume
        self._emit_volume_changed()
        if self._on_volume_change_cb:
            self._on_volume_change_cb(volume)

    def set_player_mute(self, muted: bool) -> None:
        """Set mute state and notify bridge."""
        self._muted = muted
        self._emit_volume_changed()
        if self._on_mute_change_cb:
            self._on_mute_change_cb(muted)

    def update_player_state(self, *, volume: int | None = None, muted: bool | None = None) -> None:
        """
        Adopt volume/mute state the bridged player is already in.

        Use this instead of set_player_volume/set_player_mute for state that came
        from the player's own side (device feedback, a mute applied elsewhere): the
        bridge callbacks are not invoked, so the value is not pushed back at the
        player it was just read from.

        :param volume: Volume level (0-100) the player is at, or None to leave it.
        :param muted: Mute state the player is in, or None to leave it.
        """
        changed = False
        if volume is not None and volume != self._volume:
            self._volume = volume
            changed = True
        if muted is not None and muted != self._muted:
            self._muted = muted
            changed = True
        if changed:
            self._emit_volume_changed()

    def on_audio_chunk(self, chunk: AudioChunk) -> None:
        """Receive audio chunk from PushStream and forward to callback."""
        if self._on_audio_chunk_cb:
            self._on_audio_chunk_cb(chunk)

    def on_connect(self) -> None:
        """Subscribe to PlayerGroupRole on attach."""
        self._subscribe_to_group_role()

    def on_disconnect(self) -> None:
        """Unsubscribe from PlayerGroupRole on detach."""
        self._unsubscribe_from_group_role()

    def has_connection(self) -> bool:
        """Return True to indicate bridge is "connected" for audio purposes."""
        return True

    def supports_preconnect_audio(self) -> bool:
        """Return True -- bridge can receive audio before the stream starts."""
        return True

    def on_stream_start(self) -> None:
        """Log stream start and invoke callback."""
        LOGGER.debug("BridgePlayerRole stream started for client %s", self._client.client_id)
        if self._on_stream_start_cb:
            self._on_stream_start_cb()

    def on_stream_end(self) -> None:
        """Log stream end and invoke the stream-end callback."""
        LOGGER.debug("BridgePlayerRole stream ended for client %s", self._client.client_id)
        if self._on_stream_end_cb:
            self._on_stream_end_cb()

    def notify_explicit_stop(self) -> None:
        """
        Notify the bridge that playback ended on an explicit user command.

        Call this after the stop (or group removal) has ended the stream, so
        the bridge can tear its downstream transport down at once instead of
        keeping it warm for a next track that will not come.
        """
        LOGGER.debug("BridgePlayerRole explicit stop for client %s", self._client.client_id)
        if self._on_explicit_stop_cb:
            self._on_explicit_stop_cb()

    def _emit_volume_changed(self) -> None:
        """Emit VolumeChangedEvent so the SendspinPlayer stays in sync."""
        self.emit_client_event(VolumeChangedEvent(volume=self._volume, muted=self._muted))


class BridgeVisualizerRole(Role):
    """
    Custom Sendspin visualizer role for in-process bridges.

    Runs the server's visualizer feature extraction on the group's audio and
    forwards extracted frames, beat schedules, and stream lifecycle events to
    callbacks instead of sending binary frames over a WebSocket.

    Created by the role factory registry. After creation, the bridge must call
    set_callbacks() and setup_visualizer(), then attach the roles via the
    client's attach_preinitialized_roles(): external clients never get a
    transport attach, so the group-role subscription (which beat schedules
    and lifecycle broadcasts flow through) has to be made explicitly.
    """

    def __init__(self, client: SendspinClient) -> None:
        """
        Initialize the bridge visualizer role.

        :param client: The Sendspin client this role belongs to.
        """
        self._client = client
        self._support: ClientHelloVisualizerSupport | None = None
        self._extractor: VisualizerFeatureExtractor | None = None
        self._beat_availability = BeatAvailability.PENDING
        self._on_frame_cb: Callable[[ExtractedFrame], None] | None = None
        self._on_beats_cb: Callable[[list[BeatTiming]], None] | None = None
        self._on_beats_clear_cb: Callable[[], None] | None = None
        self._on_stream_start_cb: Callable[[], None] | None = None
        self._on_stream_clear_cb: Callable[[], None] | None = None
        self._on_stream_end_cb: Callable[[], None] | None = None

    def set_callbacks(
        self,
        *,
        on_frame: Callable[[ExtractedFrame], None],
        on_beats: Callable[[list[BeatTiming]], None],
        on_beats_clear: Callable[[], None],
        on_stream_start: Callable[[], None],
        on_stream_clear: Callable[[], None],
        on_stream_end: Callable[[], None],
    ) -> None:
        """
        Wire up bridge callbacks after role creation.

        :param on_frame: Callback receiving extracted visualizer frames.
        :param on_beats: Callback receiving beat schedule segments.
        :param on_beats_clear: Callback when the beat schedule is dropped.
        :param on_stream_start: Callback when the stream starts.
        :param on_stream_clear: Callback when buffered stream state is cleared (seek).
        :param on_stream_end: Callback when the stream ends.
        """
        self._on_frame_cb = on_frame
        self._on_beats_cb = on_beats
        self._on_beats_clear_cb = on_beats_clear
        self._on_stream_start_cb = on_stream_start
        self._on_stream_clear_cb = on_stream_clear
        self._on_stream_end_cb = on_stream_end

    def setup_visualizer(self, support: ClientHelloVisualizerSupport) -> None:
        """
        Configure feature extraction from the registered hello's support object.

        :param support: The visualizer support object from the client hello.
        """
        self._support = support

    @property
    def role_id(self) -> str:
        """Return role identifier."""
        return VISUALIZER_BRIDGE_ROLE_ID

    @property
    def role_family(self) -> str:
        """Return role family name."""
        return "visualizer"

    def get_audio_requirements(self) -> AudioRequirements:
        """Return audio requirements for visualizer analysis."""
        return AudioRequirements(
            sample_rate=48_000,
            bit_depth=16,
            channels=2,
            frame_duration_us=25_000,
        )

    def has_connection(self) -> bool:
        """Return True to indicate bridge is "connected" for audio purposes."""
        return True

    def supports_preconnect_audio(self) -> bool:
        """Return True -- bridge receives audio without a transport attach."""
        return True

    def replay_from_pcm_cache(self) -> bool:
        """Replay buffered PCM on late join (visualizer is analysis-only)."""
        return True

    @property
    def wants_beats(self) -> bool:
        """True if the bridge requested beats and beats are not unavailable."""
        return (
            self._support is not None
            and "beat" in self._support.types
            and self._beat_availability is not BeatAvailability.UNAVAILABLE
        )

    def append_beats(self, beats: list[BeatTiming]) -> None:
        """Forward a beat schedule segment to the bridge."""
        if self._on_beats_cb and beats:
            self._on_beats_cb(list(beats))

    def clear_beats(self) -> None:
        """Forward a beat-schedule clear to the bridge."""
        if self._on_beats_clear_cb:
            self._on_beats_clear_cb()

    def set_beat_availability(self, availability: BeatAvailability) -> None:
        """Track beat availability; UNAVAILABLE drops the delivered schedule."""
        self._beat_availability = availability
        if availability is BeatAvailability.UNAVAILABLE:
            self.clear_beats()

    def on_connect(self) -> None:
        """Subscribe to VisualizerGroupRole on attach."""
        self._subscribe_to_group_role()

    def on_disconnect(self) -> None:
        """Unsubscribe from VisualizerGroupRole on detach."""
        self._unsubscribe_from_group_role()

    def on_stream_start(self) -> None:
        """Build a fresh extractor for the new stream and notify the bridge."""
        if self._support is not None:
            req = self.get_audio_requirements()
            self._extractor = VisualizerFeatureExtractor(
                sample_rate=req.sample_rate,
                channels=req.channels,
                config=StreamStartVisualizer.from_support(self._support),
            )
        LOGGER.debug("BridgeVisualizerRole stream started for client %s", self._client.client_id)
        if self._on_stream_start_cb:
            self._on_stream_start_cb()

    def on_audio_chunk(self, chunk: AudioChunk) -> None:
        """Extract features from group audio and forward each frame."""
        if self._extractor is None or self._on_frame_cb is None:
            return
        for frame in self._extractor.process_chunk(chunk.data, chunk.timestamp_us):
            self._on_frame_cb(frame)

    def on_stream_clear(self) -> None:
        """Reset extractor state on seek/clear and notify the bridge."""
        if self._extractor is not None:
            self._extractor.reset()
        if self._on_stream_clear_cb:
            self._on_stream_clear_cb()

    def on_stream_end(self) -> None:
        """Tear down the extractor and notify the bridge."""
        self._extractor = None
        LOGGER.debug("BridgeVisualizerRole stream ended for client %s", self._client.client_id)
        if self._on_stream_end_cb:
            self._on_stream_end_cb()


class BridgeColorRole(Role):
    """
    Custom Sendspin color role for in-process bridges.

    Receives color palette state updates from ColorGroupRole and forwards
    them to a callback instead of sending server/state over a WebSocket.
    """

    def __init__(self, client: SendspinClient) -> None:
        """
        Initialize the bridge color role.

        :param client: The Sendspin client this role belongs to.
        """
        self._client = client
        self._on_color_cb: Callable[[ServerStatePayload], None] | None = None

    def set_callbacks(self, *, on_color: Callable[[ServerStatePayload], None]) -> None:
        """
        Wire up the color update callback after role creation.

        :param on_color: Callback receiving server/state payloads with color updates.
        """
        self._on_color_cb = on_color

    @property
    def role_id(self) -> str:
        """Return role identifier."""
        return COLOR_BRIDGE_ROLE_ID

    @property
    def role_family(self) -> str:
        """Return role family name."""
        return "color"

    def has_connection(self) -> bool:
        """Return True to indicate bridge is "connected"."""
        return True

    def send_message(self, message: ServerMessage) -> None:
        """Intercept outbound server/state color updates and forward to the bridge."""
        if isinstance(message, ServerStateMessage) and self._on_color_cb:
            self._on_color_cb(message.payload)

    def on_connect(self) -> None:
        """Subscribe to ColorGroupRole on attach."""
        self._subscribe_to_group_role()

    def on_disconnect(self) -> None:
        """Unsubscribe from ColorGroupRole on detach."""
        self._unsubscribe_from_group_role()


register_role(BRIDGE_ROLE_ID, lambda client: BridgePlayerRole(client=client))
register_role(VISUALIZER_BRIDGE_ROLE_ID, lambda client: BridgeVisualizerRole(client=client))
register_role(COLOR_BRIDGE_ROLE_ID, lambda client: BridgeColorRole(client=client))
