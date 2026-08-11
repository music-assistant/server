"""
Sendspin Source provider implementation.

Exposes every connected Sendspin client with an active `source` role as a Music
Assistant AudioSource. Decoded PCM from the client is pulled through an
occupancy-controlled clock bridge so the capture clock never has to match MA's
consumption clock. See README.md for the design rationale.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from aiosendspin.audio import AsrcSourceBridge, SourceBridge
from aiosendspin.audio import AudioFormat as SendspinAudioFormat
from aiosendspin.server import (
    ClientConnectedEvent,
    SourceSignalChangedEvent,
    SourceStreamEndedEvent,
    SourceStreamStartedEvent,
)
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import AudioError, MediaNotFoundError
from music_assistant_models.media_items import AudioFormat, AudioSource, ProviderMapping
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.models.plugin import PluginProvider

from .constants import (
    CHUNK_DURATION_MS,
    CONF_TARGET_LATENCY,
    OUTPUT_BIT_DEPTH,
    OUTPUT_CHANNELS,
    OUTPUT_SAMPLE_RATE,
    SOURCE_TIMEOUT_S,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from aiosendspin.server import (
        ClientEvent,
        SendspinClient,
        SendspinEvent,
        SendspinServer,
        SourceStream,
    )
    from aiosendspin.server.roles import SourceV1Role
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.sendspin.provider import SendspinProvider

OUTPUT_FORMAT = AudioFormat(
    content_type=ContentType.PCM_S16LE,
    sample_rate=OUTPUT_SAMPLE_RATE,
    bit_depth=OUTPUT_BIT_DEPTH,
    channels=OUTPUT_CHANNELS,
)
BRIDGE_OUTPUT_FORMAT = SendspinAudioFormat(
    sample_rate=OUTPUT_SAMPLE_RATE,
    bit_depth=OUTPUT_BIT_DEPTH,
    channels=OUTPUT_CHANNELS,
)


@dataclass
class _SourceSession:
    """State for the single active (exclusive) source stream."""

    client_id: str
    player_id: str
    queue_id: str
    stream_session_id: str
    unsubscribes: list[Callable[[], None]] = field(default_factory=list)
    bridge: SourceBridge | None = None
    ingest_task: asyncio.Task[None] | None = None
    # Selection time counts toward the timeout so a client that never starts
    # streaming also ends the stream after SOURCE_TIMEOUT_S.
    last_pcm_monotonic: float = field(default_factory=time.monotonic)


class SendspinSourceProvider(PluginProvider):
    """Expose Sendspin source-role clients as MA AudioSources."""

    _asrc_unavailable_logged: bool = False

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature] | None = None,
    ) -> None:
        """Initialize the provider."""
        super().__init__(mass, manifest, config, supported_features)
        # Exclusivity is per source, so each source streams independently.
        self._sessions: dict[str, _SourceSession] = {}

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        for source_id in list(self._sessions):
            await self._teardown_session(source_id)

    async def get_audio_sources(self) -> list[AudioSource]:
        """Return one AudioSource per connected client with an active source role."""
        if (sendspin := self._sendspin_provider) is None:
            return []
        sources: list[AudioSource] = []
        for client in sendspin.server_api.connected_clients:
            if self._get_source_role(client) is None:
                continue
            info = client.info_or_none
            name = info.name if info else client.client_id
            sources.append(
                AudioSource(
                    item_id=client.client_id,
                    provider=self.instance_id,
                    name=name,
                    provider_mappings={
                        ProviderMapping(
                            item_id=client.client_id,
                            provider_domain=self.domain,
                            provider_instance=self.instance_id,
                            audio_format=OUTPUT_FORMAT,
                        )
                    },
                    can_play_pause=False,
                    can_seek=False,
                    can_next_previous=False,
                    exclusive=True,
                    allow_external_trigger=False,
                    can_initiate=True,
                )
            )
        return sources

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Return StreamDetails for streaming the given source to a queue.

        Side-effect-free: streaming is requested from the client in
        on_source_selected, which the streams controller fires before this
        method on the actual stream request (never on queue preload).
        """
        client = self._get_client(item_id)
        if client is None or self._get_source_role(client) is None:
            raise MediaNotFoundError(f"Unknown or unavailable Sendspin source: {item_id}")
        info = client.info_or_none
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=OUTPUT_FORMAT,
            media_type=media_type,
            stream_type=StreamType.CUSTOM,
            stream_metadata=StreamMetadata(title=info.name if info else item_id),
        )

    async def on_source_selected(
        self, source_id: str, player_id: str, queue_id: str, stream_session_id: str
    ) -> None:
        """Claim the source and ask the client to start streaming."""
        sendspin = self._sendspin_provider
        client = self._get_client(source_id)
        role = self._get_source_role(client) if client else None
        if sendspin is None or client is None or role is None:
            raise MediaNotFoundError(f"Sendspin source is not connected: {source_id}")
        # Exclusive per source: supersede only this source's prior session
        # (its generator notices and exits). Other sources keep streaming.
        await self._teardown_session(source_id, superseded_by_player_id=player_id)
        session = _SourceSession(
            client_id=source_id,
            player_id=player_id,
            queue_id=queue_id,
            stream_session_id=stream_session_id,
            unsubscribes=[
                client.add_event_listener(self._on_client_event),
                sendspin.server_api.add_event_listener(self._on_server_event),
            ],
        )
        self._sessions[source_id] = session
        role.request_start()

    async def on_source_unselected(
        self, source_id: str, queue_id: str, stream_session_id: str
    ) -> None:
        """Release the source when MA tears down its stream."""
        session = self._sessions.get(source_id)
        # Reject stale callbacks from superseded same-queue requests.
        if session is None or session.stream_session_id != stream_session_id:
            return
        await self._teardown_session(source_id)

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """
        Yield fixed-format PCM pulled from the source's clock bridge.

        The pull cadence of this loop is the master clock: the bridge converts
        the client's drifting capture stream to it and pads silence on underrun,
        so the stream keeps playing through gaps (an unplugged line-in is silent,
        not stopped) until SOURCE_TIMEOUT_S passes without source audio.
        """
        session = self._sessions.get(streamdetails.item_id)
        if session is None:
            raise AudioError(f"Sendspin source is not selected: {streamdetails.item_id}")
        frames_per_chunk = OUTPUT_SAMPLE_RATE * CHUNK_DURATION_MS // 1000
        silence = bytes(frames_per_chunk * (OUTPUT_BIT_DEPTH // 8) * OUTPUT_CHANNELS)
        period = CHUNK_DURATION_MS / 1000
        loop = self.mass.loop
        next_deadline = loop.time()
        while True:
            # Superseded by a newer selection (cross-queue handoff or reconnect).
            if self._sessions.get(session.client_id) is not session:
                break
            if time.monotonic() - session.last_pcm_monotonic > SOURCE_TIMEOUT_S:
                self.logger.info(
                    "No audio from Sendspin source %s for %.0fs, ending stream",
                    session.client_id,
                    SOURCE_TIMEOUT_S,
                )
                break
            bridge = session.bridge
            yield bridge.read(frames_per_chunk) if bridge else silence
            next_deadline += period
            delay = next_deadline - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            elif bridge is None or -delay * 1_000_000 > bridge.occupancy_us:
                # Consumer stalled beyond the buffered audio. Catch-up reads past this
                # point would fabricate silence into the timeline, so re-anchor instead.
                next_deadline = loop.time()

    @property
    def _sendspin_provider(self) -> SendspinProvider | None:
        return cast("SendspinProvider | None", self.mass.get_provider("sendspin"))

    def _get_client(self, client_id: str) -> SendspinClient | None:
        if (sendspin := self._sendspin_provider) is None:
            return None
        return sendspin.server_api.get_client(client_id)

    @staticmethod
    def _get_source_role(client: SendspinClient) -> SourceV1Role | None:
        roles = client.roles_by_family("source")
        return cast("SourceV1Role", roles[0]) if roles else None

    def _on_server_event(self, server: SendspinServer, event: SendspinEvent) -> None:
        match event:
            case ClientConnectedEvent(client_id) if (
                session := self._sessions.get(client_id)
            ) is not None:
                # A reconnect clears the client's start request, so ask again. Roles
                # are re-attached after this fires, hence the hop through a task.
                self.mass.create_task(self._request_start_on_reconnect(session))

    async def _request_start_on_reconnect(self, session: _SourceSession) -> None:
        if self._sessions.get(session.client_id) is not session:
            return
        client = self._get_client(session.client_id)
        role = self._get_source_role(client) if client else None
        if role is None or role.stream_active:
            return
        self.logger.debug("Re-requesting stream start from %s", session.client_id)
        role.request_start()

    def _on_client_event(self, client: SendspinClient, event: ClientEvent) -> None:
        session = self._sessions.get(client.client_id)
        if session is None:
            return
        if isinstance(event, SourceStreamStartedEvent):
            self.mass.create_task(self._attach_stream(session, event))
        elif isinstance(event, SourceStreamEndedEvent):
            self.logger.debug("Sendspin source %s ended its stream", session.client_id)
        elif isinstance(event, SourceSignalChangedEvent):
            self.logger.debug(
                "Sendspin source %s reports signal %s", session.client_id, event.signal.value
            )

    async def _attach_stream(
        self, session: _SourceSession, event: SourceStreamStartedEvent
    ) -> None:
        """Route a (re)started source stream into a fresh bridge."""
        if self._sessions.get(session.client_id) is not session:
            return
        if session.ingest_task is not None:
            session.ingest_task.cancel()
        target_latency = cast("int", self.config.get_value(CONF_TARGET_LATENCY))
        session.bridge = self._create_bridge(event.audio_format, target_latency)
        session.last_pcm_monotonic = time.monotonic()
        session.ingest_task = self.mass.create_task(self._ingest(session, event.handle))

    def _create_bridge(
        self, input_format: SendspinAudioFormat, target_latency_ms: int
    ) -> SourceBridge:
        try:
            return AsrcSourceBridge(
                input_format=input_format,
                output_format=BRIDGE_OUTPUT_FORMAT,
                target_latency_ms=target_latency_ms,
            )
        except ImportError:
            if not self._asrc_unavailable_logged:
                self._asrc_unavailable_logged = True
                self.logger.warning(
                    "soxr is not available, falling back to the simple source bridge"
                )
            return SourceBridge(
                input_format=input_format,
                output_format=BRIDGE_OUTPUT_FORMAT,
                target_latency_ms=target_latency_ms,
            )

    async def _ingest(self, session: _SourceSession, handle: SourceStream) -> None:
        """Feed decoded source chunks into the session's bridge until the stream ends."""
        async for pcm, timestamp_us in handle:
            if self._sessions.get(session.client_id) is not session or session.bridge is None:
                break
            try:
                session.bridge.feed(pcm, timestamp_us)
            except ValueError:
                self.logger.exception("Dropping malformed chunk from %s", session.client_id)
                continue
            session.last_pcm_monotonic = time.monotonic()

    async def _teardown_session(
        self, source_id: str, superseded_by_player_id: str | None = None
    ) -> None:
        session = self._sessions.pop(source_id, None)
        if session is None:
            return
        if session.ingest_task is not None:
            session.ingest_task.cancel()
        for unsubscribe in session.unsubscribes:
            unsubscribe()
        if (client := self._get_client(session.client_id)) is not None and (
            role := self._get_source_role(client)
        ) is not None:
            try:
                role.request_stop()
            except Exception as err:
                self.logger.debug("Failed to send stop to %s: %s", session.client_id, err)
        if superseded_by_player_id is not None and superseded_by_player_id != session.player_id:
            # Ending the generator leaves the handed-off player draining its buffer over
            # the new one, so stop it. A same-player re-claim keeps playing.
            try:
                await self.mass.players.cmd_stop(session.player_id)
            except Exception as err:
                self.logger.debug("Failed to stop player %s: %s", session.player_id, err)
