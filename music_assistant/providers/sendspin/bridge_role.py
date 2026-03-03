"""Reusable bridge player role for external player bridges.

Provides a BridgePlayerRole that receives audio from Sendspin's PushStream
and forwards it to an external player via callbacks. This role can be used
by any bridge implementation (AirPlay, etc.) to integrate external players
with Sendspin's synchronization and timing.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from aiosendspin.server.roles import AudioRequirements, Role
from aiosendspin.server.roles.registry import register_role

from music_assistant.mass import LOGGER

if TYPE_CHECKING:
    from aiosendspin.server import SendspinClient
    from aiosendspin.server.roles import AudioChunk

# Audio format constants for bridge players.
BRIDGE_SAMPLE_RATE = 44100
BRIDGE_BIT_DEPTH = 16
BRIDGE_CHANNELS = 2
BRIDGE_BYTES_PER_SAMPLE = BRIDGE_BIT_DEPTH // 8

BRIDGE_ROLE_ID = "player@_bridge"


class BridgePlayerRole(Role):
    """Custom Sendspin player role for external player bridges.

    This role receives audio from Sendspin's PushStream and forwards it
    to an external player via callbacks. It bypasses the normal WebSocket
    audio delivery since external players don't have a WebSocket connection.

    Created by the role factory registry. After creation, the bridge must
    call set_callbacks() to wire up audio/volume/stream callbacks.
    """

    def __init__(self, client: SendspinClient) -> None:
        """Initialize the bridge player role.

        :param client: The Sendspin client this role belongs to.
        """
        self._client = client
        self._on_audio_chunk_cb: Callable[[AudioChunk], None] | None = None
        self._on_volume_change_cb: Callable[[int, bool], None] | None = None
        self._on_stream_start_cb: Callable[[], None] | None = None
        self._on_stream_end_cb: Callable[[], None] | None = None
        self._audio_requirements: AudioRequirements | None = None
        self._volume: int = 100
        self._muted: bool = False

    def set_callbacks(
        self,
        *,
        on_audio_chunk: Callable[[AudioChunk], None],
        on_volume_change: Callable[[int, bool], None],
        on_stream_start: Callable[[], None],
        on_stream_end: Callable[[], None],
        initial_volume: int = 100,
    ) -> None:
        """Wire up bridge callbacks after role creation.

        :param on_audio_chunk: Callback to receive audio chunks.
        :param on_volume_change: Callback when volume or mute state changes.
        :param on_stream_start: Callback when the stream starts.
        :param on_stream_end: Callback when the stream ends.
        :param initial_volume: Initial volume level (0-100).
        """
        self._on_audio_chunk_cb = on_audio_chunk
        self._on_volume_change_cb = on_volume_change
        self._on_stream_start_cb = on_stream_start
        self._on_stream_end_cb = on_stream_end
        self._volume = initial_volume

    @property
    def role_id(self) -> str:
        """Return role identifier."""
        return BRIDGE_ROLE_ID

    @property
    def role_family(self) -> str:
        """Return role family name."""
        return "player"

    def setup_audio_requirements(self) -> None:
        """Set up audio requirements for bridge PCM format."""
        self._audio_requirements = AudioRequirements(
            sample_rate=BRIDGE_SAMPLE_RATE,
            bit_depth=BRIDGE_BIT_DEPTH,
            channels=BRIDGE_CHANNELS,
            transformer=None,  # Raw PCM, no encoding
        )

    def get_audio_requirements(self) -> AudioRequirements | None:
        """Return audio requirements for PushStream."""
        return self._audio_requirements

    def get_player_volume(self) -> int | None:
        """Return current volume level."""
        return self._volume

    def get_player_muted(self) -> bool | None:
        """Return current mute state."""
        return self._muted

    def set_player_volume(self, volume: int) -> None:
        """Set volume and notify bridge."""
        self._volume = volume
        if self._on_volume_change_cb:
            self._on_volume_change_cb(volume, self._muted)

    def set_player_mute(self, muted: bool) -> None:
        """Set mute state and notify bridge."""
        self._muted = muted
        if self._on_volume_change_cb:
            self._on_volume_change_cb(self._volume, muted)

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


register_role(BRIDGE_ROLE_ID, lambda client: BridgePlayerRole(client=client))
