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

from aiosendspin.audio import AsrcSourceBridge
from aiosendspin.audio import AudioFormat as SendspinAudioFormat
from aiosendspin.models.types import role_family
from aiosendspin.server import (
    ClientConnectedEvent,
    ClientDisconnectedEvent,
    ClientRemovedEvent,
    SignalState,
    SourceSignalChangedEvent,
    SourceStreamEndedEvent,
    SourceStreamStartedEvent,
)
from music_assistant_models.enums import (
    ContentType,
    MediaType,
    PlaybackState,
    QueueOption,
    StreamType,
)
from music_assistant_models.errors import (
    AudioError,
    MediaNotFoundError,
    PlayerCommandFailed,
    PlayerUnavailableError,
)
from music_assistant_models.helpers import create_uri
from music_assistant_models.media_items import AudioFormat, AudioSource, ProviderMapping
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.sendspin.constants import (
    CONF_SOURCE_AUTOSTART_INTERRUPT,
    CONF_SOURCE_AUTOSTART_TARGET,
    SOURCE_AUTOSTART_OFF,
)

from .constants import (
    AUTOSTART_SIGNAL_ABSENT_HOLD_S,
    AUTOSTART_SIGNAL_DEBOUNCE_S,
    CHUNK_DURATION_MS,
    COLD_START_TIMEOUT_S,
    CONF_TARGET_LATENCY,
    DEFAULT_TARGET_LATENCY_MS,
    OUTPUT_BIT_DEPTH,
    OUTPUT_CHANNELS,
    OUTPUT_SAMPLE_RATE,
    SOURCE_TIMEOUT_S,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from aiosendspin.audio import SourceBridge
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
    # Bumped when the same queue re-claims its own stream, to retire the generator
    # that was serving the previous request while the session itself lives on.
    generation: int = 0
    bridge: SourceBridge | None = None
    ingest_task: asyncio.Task[None] | None = None
    # Selection time counts toward the timeout so a client that never starts
    # streaming also ends the stream after SOURCE_TIMEOUT_S.
    last_pcm_monotonic: float = field(default_factory=time.monotonic)
    pcm_received: asyncio.Event = field(default_factory=asyncio.Event)


class SendspinSourceProvider(PluginProvider):
    """Expose Sendspin source-role clients as MA AudioSources."""

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
        self._watchers: dict[str, Callable[[], None]] = {}
        self._signals: dict[str, SignalState] = {}
        # Kept apart: a re-selection drops a pending start but must leave a pending
        # stop counting down, or a same-queue reconnect defuses it for good.
        self._pending_autostart: dict[str, asyncio.Task[None]] = {}
        self._pending_autostop: dict[str, asyncio.Task[None]] = {}
        self._server_unsubscribe: Callable[[], None] | None = None

    async def loaded_in_mass(self) -> None:
        """Start watching every source client, including ones that are already idle."""
        await super().loaded_in_mass()
        if (sendspin := self._sendspin_provider) is None:
            return
        self._server_unsubscribe = sendspin.server_api.add_event_listener(self._on_server_event)
        for client in sendspin.server_api.connected_clients:
            self._watch_client(client)

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        if self._server_unsubscribe is not None:
            self._server_unsubscribe()
            self._server_unsubscribe = None
        for unwatch in self._watchers.values():
            unwatch()
        self._watchers.clear()
        for pending in (self._pending_autostart, self._pending_autostop):
            for task in pending.values():
                task.cancel()
            pending.clear()
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
                    allow_external_trigger=True,
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
        client = self._get_client(source_id)
        role = self._get_source_role(client) if client else None
        if client is None or role is None:
            raise MediaNotFoundError(f"Sendspin source is not connected: {source_id}")
        # A queue re-claiming its own stream keeps the running bridge: renderers that
        # open the stream url twice, and same-queue reconnects, would otherwise cost a
        # stop/start of the client and a gap in the audio they are already playing.
        if (live := self._sessions.get(source_id)) is not None and (
            live.player_id,
            live.queue_id,
        ) == (player_id, queue_id):
            live.stream_session_id = stream_session_id
            live.generation += 1
            self._cancel_pending_autostart(source_id)
            return
        # Exclusive per source: supersede only this source's prior session
        # (its generator notices and exits). Other sources keep streaming.
        await self._teardown_session(source_id, superseded_by_player_id=player_id)
        session = _SourceSession(
            client_id=source_id,
            player_id=player_id,
            queue_id=queue_id,
            stream_session_id=stream_session_id,
        )
        self._sessions[source_id] = session
        # A manual selection means the user chose the target, so drop any autostart
        # that was still counting down for this source.
        self._cancel_pending_autostart(source_id)
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
        not stopped) until SOURCE_TIMEOUT_S passes without source audio. Pulling at
        the output rate here makes the controller's own realtime pacer a no-op.
        """
        session = self._sessions.get(streamdetails.item_id)
        if session is None:
            raise AudioError(f"Sendspin source is not selected: {streamdetails.item_id}")
        await self._await_first_audio(session)
        generation = session.generation
        if self._sessions.get(session.client_id) is not session:
            return
        frames_per_chunk = OUTPUT_SAMPLE_RATE * CHUNK_DURATION_MS // 1000
        period = CHUNK_DURATION_MS / 1000
        loop = self.mass.loop
        next_deadline = loop.time()
        while True:
            # Superseded by a newer selection (cross-queue handoff), or replaced by a
            # newer request for this same queue.
            if (
                self._sessions.get(session.client_id) is not session
                or session.generation != generation
            ):
                break
            if time.monotonic() - session.last_pcm_monotonic > SOURCE_TIMEOUT_S:
                self.logger.info(
                    "No audio from Sendspin source %s for %.0fs, ending stream",
                    session.client_id,
                    SOURCE_TIMEOUT_S,
                )
                break
            if (bridge := session.bridge) is None:
                break
            yield bridge.read(frames_per_chunk)
            next_deadline += period
            delay = next_deadline - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            elif -delay * 1_000_000 > bridge.occupancy_us:
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

    async def _await_first_audio(self, session: _SourceSession) -> None:
        """
        Block until the source actually streams, so a failed acquisition raises.

        The silence-hold only makes sense once audio has flowed: a client that never
        answers the start command is a broken source, not a quiet one.
        """
        try:
            async with asyncio.timeout(COLD_START_TIMEOUT_S):
                await session.pcm_received.wait()
        except TimeoutError:
            if self._sessions.get(session.client_id) is not session:
                return
            client = self._get_client(session.client_id)
            info = client.info_or_none if client else None
            raise AudioError(
                f"Sendspin source {session.client_id} did not start streaming",
                translation_key="no_audio",
                translation_owner=self.translation_owner,
                translation_args=[info.name if info else session.client_id],
            ) from None

    def _on_server_event(self, server: SendspinServer, event: SendspinEvent) -> None:
        match event:
            case ClientConnectedEvent(client_id):
                # Roles are attached after this fires, so defer anything needing them.
                # An eager start would run the whole thing back inside this callback.
                self.mass.create_task(self._on_client_connected(client_id), eager_start=False)
            case ClientRemovedEvent(client_id) | ClientDisconnectedEvent(client_id):
                self._cancel_pending_autostart(client_id)
                self._cancel_pending_autostop(client_id)
                self._signals.pop(client_id, None)
                if (unwatch := self._watchers.pop(client_id, None)) is not None:
                    unwatch()

    async def _on_client_connected(self, client_id: str) -> None:
        """Re-arm watching and streaming for a client that just (re)connected."""
        client = self._get_client(client_id)
        if client is None:
            return
        self._watch_client(client)
        # A reconnect clears the client's start request, so ask again.
        if (session := self._sessions.get(client_id)) is not None:
            await self._request_start_on_reconnect(session)

    def _watch_client(self, client: SendspinClient) -> None:
        """Subscribe to a source client's events, for signal presence while idle."""
        if client.client_id in self._watchers:
            return
        # Gate on negotiated rather than active roles: activation follows the pairing
        # state and can happen on an already-connected client, with no event to re-arm on.
        if "source" not in {role_family(role_id) for role_id in client.negotiated_role_ids}:
            return
        self._watchers[client.client_id] = client.add_event_listener(self._on_client_event)

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
        client_id = client.client_id
        if isinstance(event, SourceSignalChangedEvent):
            self._on_signal_reported(client_id, event.signal)
            return
        session = self._sessions.get(client_id)
        if session is None:
            return
        if isinstance(event, SourceStreamStartedEvent):
            self.mass.create_task(self._attach_stream(session, event))
        elif isinstance(event, SourceStreamEndedEvent):
            self.logger.debug("Sendspin source %s ended its stream", client_id)

    def _on_signal_reported(self, client_id: str, signal: SignalState) -> None:
        """
        Drive line-in autostart/autostop from a reported signal presence.

        Only transitions act. The first report for a client is recorded silently so a
        server restart or a reconnect with the needle already down starts nothing.
        """
        previous = self._signals.get(client_id)
        if previous == signal:
            return
        self._signals[client_id] = signal
        if previous is None:
            return
        self.logger.debug("Sendspin source %s signal %s", client_id, signal.value)
        self._cancel_pending_autostart(client_id)
        self._cancel_pending_autostop(client_id)
        if signal == SignalState.PRESENT:
            self._pending_autostart[client_id] = self.mass.create_task(
                self._autostart_after_debounce(client_id)
            )
        else:
            self._pending_autostop[client_id] = self.mass.create_task(
                self._autostop_after_hold(client_id)
            )

    def _cancel_pending_autostart(self, client_id: str) -> None:
        if (task := self._pending_autostart.pop(client_id, None)) is not None:
            task.cancel()

    def _cancel_pending_autostop(self, client_id: str) -> None:
        if (task := self._pending_autostop.pop(client_id, None)) is not None:
            task.cancel()

    async def _autostart_after_debounce(self, client_id: str) -> None:
        """Start playing a source whose signal has stayed present."""
        await asyncio.sleep(AUTOSTART_SIGNAL_DEBOUNCE_S)
        # Untrack before starting, not in a finally: play_media claims the source, and
        # that claim cancels whatever this dict holds for the client.
        self._pending_autostart.pop(client_id, None)
        if client_id in self._sessions:
            # Already streaming, wherever the user put it.
            return
        if (target := await self._resolve_autostart_target(client_id)) is None:
            return
        queue_id, uri = target
        self.logger.info("Line-in signal on %s, starting playback on %s", client_id, queue_id)
        await self.mass.player_queues.play_media(queue_id, uri, option=QueueOption.PLAY)

    async def _autostop_after_hold(self, client_id: str) -> None:
        """Stop a source whose signal has stayed absent, e.g. a record that ended."""
        await asyncio.sleep(AUTOSTART_SIGNAL_ABSENT_HOLD_S)
        self._pending_autostop.pop(client_id, None)
        if (session := self._sessions.get(client_id)) is None:
            return
        self.logger.info("Line-in signal gone on %s, stopping playback", client_id)
        try:
            # Stop the queue rather than the player, so its pending preload/enqueue
            # timers are cancelled too.
            await self.mass.player_queues.stop(session.queue_id)
        except (KeyError, PlayerCommandFailed, PlayerUnavailableError) as err:
            self.logger.debug("Failed to stop queue %s: %s", session.queue_id, err)

    async def _resolve_autostart_target(self, client_id: str) -> tuple[str, str] | None:
        """Return the queue id and source uri to autostart, if the source is configured."""
        # Resolve through the config controller rather than the raw store: the entry's
        # default is what a device that plays its own line-in relies on, and defaults
        # are not persisted until the user saves the page.
        target_player_id = await self.mass.config.get_player_config_value(
            client_id, CONF_SOURCE_AUTOSTART_TARGET, default=SOURCE_AUTOSTART_OFF
        )
        if not target_player_id or target_player_id == SOURCE_AUTOSTART_OFF:
            return None
        player = self.mass.players.get_player(target_player_id)
        if player is None:
            self.logger.warning(
                "Autostart target %s for Sendspin source %s no longer exists",
                target_player_id,
                client_id,
            )
            return None
        # Follow the player into any group it is part of, so a grouped speaker plays
        # the source everywhere rather than dropping out of its group.
        queue = self.mass.players.get_active_queue(player)
        if queue is None:
            self.logger.debug("Autostart target %s has no queue", target_player_id)
            return None
        if (
            not await self.mass.config.get_player_config_value(
                client_id, CONF_SOURCE_AUTOSTART_INTERRUPT, default=True
            )
            and queue.state == PlaybackState.PLAYING
        ):
            self.logger.debug(
                "Not interrupting playback on %s for Sendspin source %s", queue.queue_id, client_id
            )
            return None
        return queue.queue_id, create_uri(MediaType.AUDIO_SOURCE, self.instance_id, client_id)

    async def _attach_stream(
        self, session: _SourceSession, event: SourceStreamStartedEvent
    ) -> None:
        """Route a (re)started source stream into a fresh bridge."""
        if self._sessions.get(session.client_id) is not session:
            return
        if session.ingest_task is not None:
            session.ingest_task.cancel()
        # Fall back to the entry's own default: a provider's options are only resolved
        # once its instance is loaded, so the config it starts up with has no value yet.
        target_latency = (
            cast("int | None", self.config.get_value(CONF_TARGET_LATENCY))
            or DEFAULT_TARGET_LATENCY_MS
        )
        session.bridge = self._create_bridge(event.audio_format, target_latency)
        session.last_pcm_monotonic = time.monotonic()
        session.ingest_task = self.mass.create_task(self._ingest(session, event.handle))

    def _create_bridge(
        self, input_format: SendspinAudioFormat, target_latency_ms: int
    ) -> SourceBridge:
        return AsrcSourceBridge(
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
            except ValueError as err:
                self.logger.warning("Dropping malformed chunk from %s: %s", session.client_id, err)
                continue
            session.last_pcm_monotonic = time.monotonic()
            session.pcm_received.set()

    async def _teardown_session(
        self, source_id: str, superseded_by_player_id: str | None = None
    ) -> None:
        session = self._sessions.pop(source_id, None)
        if session is None:
            return
        if session.ingest_task is not None:
            session.ingest_task.cancel()
        # Stop even when superseding: the replacement session only gets a bridge from a
        # fresh client_stream/start, which the client sends after a stop/start cycle.
        if (client := self._get_client(session.client_id)) is not None and (
            role := self._get_source_role(client)
        ) is not None:
            role.request_stop()
        if superseded_by_player_id is not None and superseded_by_player_id != session.player_id:
            # Ending the generator leaves the handed-off player draining its buffer over
            # the new one, so stop it. A same-player re-claim keeps playing.
            try:
                await self.mass.players.cmd_stop(session.player_id)
            except (PlayerCommandFailed, PlayerUnavailableError) as err:
                self.logger.debug("Failed to stop player %s: %s", session.player_id, err)
