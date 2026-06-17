"""
Reusable synchronizer role for external visualization bridges.

Provides a SynchronizerRole that receives audio from Sendspin's PushStream,
computes visualization features (loudness, peak frequency, spectrum), and
forwards them to an external consumer via callbacks. This role can be used
by any bridge implementation (e.g. Hue Entertainment) that needs real-time
audio analysis data without a WebSocket connection.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from aiosendspin.models.visualizer import (
    ClientHelloVisualizerSupport,
    StreamStartVisualizer,
)
from aiosendspin.server.roles import AudioRequirements, Role
from aiosendspin.server.roles.registry import register_role
from aiosendspin.server.roles.visualizer.features import (
    ExtractedFrame,
    VisualizerFeatureExtractor,
)

from music_assistant.mass import LOGGER

if TYPE_CHECKING:
    from aiosendspin.server import SendspinClient
    from aiosendspin.server.roles import AudioChunk

SYNC_SAMPLE_RATE = 48_000
SYNC_BIT_DEPTH = 16
SYNC_CHANNELS = 2
SYNC_FRAME_DURATION_US = 25_000

SYNC_ROLE_ID = "visualizer@_sync"


class SynchronizerRole(Role):
    """
    Custom Sendspin visualizer role for external synchronization bridges.

    Receives audio from PushStream, computes visualization features using
    VisualizerFeatureExtractor, and delivers results via callbacks instead
    of binary WebSocket messages.

    Created by the role factory registry. After creation, the bridge must
    call set_callbacks() to wire up the visualization data delivery.
    """

    def __init__(self, client: SendspinClient) -> None:
        """
        Initialize the synchronizer role.

        :param client: The Sendspin client this role belongs to.
        """
        self._client = client
        self._on_visualization_data_cb: Callable[[ExtractedFrame], None] | None = None
        self._on_stream_start_cb: Callable[[], None] | None = None
        self._on_stream_end_cb: Callable[[], None] | None = None
        self._extractor: VisualizerFeatureExtractor | None = None
        self._stream_config: StreamStartVisualizer | None = None
        self._audio_requirements: AudioRequirements | None = None

    # -- public API --

    def set_callbacks(
        self,
        *,
        on_visualization_data: Callable[[ExtractedFrame], None],
        on_stream_start: Callable[[], None],
        on_stream_end: Callable[[], None],
    ) -> None:
        """Wire up bridge callbacks after role creation."""
        self._on_visualization_data_cb = on_visualization_data
        self._on_stream_start_cb = on_stream_start
        self._on_stream_end_cb = on_stream_end

    @property
    def role_id(self) -> str:
        """Return role identifier."""
        return SYNC_ROLE_ID

    @property
    def role_family(self) -> str:
        """Return role family name."""
        return "visualizer"

    def setup_audio_requirements(self) -> None:
        """Set up audio requirements for visualization analysis."""
        self._audio_requirements = AudioRequirements(
            sample_rate=SYNC_SAMPLE_RATE,
            bit_depth=SYNC_BIT_DEPTH,
            channels=SYNC_CHANNELS,
            frame_duration_us=SYNC_FRAME_DURATION_US,
        )

    def setup_stream_config(self) -> None:
        """Initialize stream config from client hello visualizer support."""
        support_raw = self._client.info.visualizer_support
        if support_raw is None:
            LOGGER.warning(
                "SynchronizerRole: no visualizer support in hello for client %s",
                self._client.client_id,
            )
            return
        payload: dict[str, object] = support_raw.to_dict()
        if "types" not in payload:
            payload["types"] = ["loudness", "f_peak"]
        if "rate_max" not in payload:
            payload["rate_max"] = 30
        support = ClientHelloVisualizerSupport.from_dict(payload)
        self._stream_config = StreamStartVisualizer.from_support(support)

    def get_audio_requirements(self) -> AudioRequirements | None:
        """Return audio requirements for PushStream."""
        return self._audio_requirements

    def has_connection(self) -> bool:
        """Return True — bridge is always 'connected' for audio purposes."""
        return True

    def supports_preconnect_audio(self) -> bool:
        """Return True — bridge can receive audio before the stream starts."""
        return True

    # -- Role lifecycle hooks --

    def on_connect(self) -> None:
        """Subscribe to VisualizerGroupRole on attach."""
        self.setup_stream_config()
        self._subscribe_to_group_role()

    def on_disconnect(self) -> None:
        """Unsubscribe from VisualizerGroupRole on detach."""
        self._unsubscribe_from_group_role()
        self._extractor = None
        self._stream_config = None

    def on_stream_start(self) -> None:
        """Create fresh extractor and invoke callback."""
        if self._stream_config is None:
            self.setup_stream_config()
        if self._stream_config is None:
            return
        req = self.get_audio_requirements()
        if req is None:
            return
        self._extractor = VisualizerFeatureExtractor(
            sample_rate=req.sample_rate,
            channels=SYNC_CHANNELS,
            config=self._stream_config,
        )
        LOGGER.debug("SynchronizerRole stream started for client %s", self._client.client_id)
        if self._on_stream_start_cb:
            self._on_stream_start_cb()

    def on_audio_chunk(self, chunk: AudioChunk) -> None:
        """Process audio chunk and forward visualization data via callback."""
        if self._extractor is None or self._stream_config is None:
            return
        frames = self._extractor.process_chunk(chunk.data, chunk.timestamp_us)
        if self._on_visualization_data_cb:
            for frame in frames:
                self._on_visualization_data_cb(frame)

    def on_stream_clear(self) -> None:
        """Reset extractor state at stream boundaries."""
        if self._extractor is not None:
            self._extractor.reset()

    def on_stream_end(self) -> None:
        """Reset extractor and invoke callback."""
        self._extractor = None
        LOGGER.debug("SynchronizerRole stream ended for client %s", self._client.client_id)
        if self._on_stream_end_cb:
            self._on_stream_end_cb()


register_role(SYNC_ROLE_ID, lambda client: SynchronizerRole(client=client))
