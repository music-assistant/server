"""
Sendspin Source provider implementation.

Exposes every connected Sendspin client with an active `source` role as a Music
Assistant AudioSource, plus approved inputs that another server holds. Decoded PCM
from the client is pulled through an occupancy-controlled clock bridge so the
capture clock never has to match MA's consumption clock. See README.md for the
design rationale.
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
    """State for one source's active exclusive stream."""

    client_id: str
    player_id: str
    owner_player_id: str
    stream_session_id: str
    playback_session_id: str | None = None
    # Retires the prior generator when the same player reclaims the session.
    generation: int = 0
    bridge: SourceBridge | None = None
    ingest_task: asyncio.Task[None] | None = None
    # Selection time counts toward the source timeout before PCM arrives.
    last_pcm_monotonic: float = field(default_factory=time.monotonic)
    pcm_received: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _SourceClientState:
    """State retained for one source client."""

    session: _SourceSession | None = None
    unwatch: Callable[[], None] | None = None
    signal: SignalState | None = None
    autostart_queue_id: str | None = None
    autostart_queue_session_id: str | None = None
    suppressed_autostart_claim: tuple[str, str | None] | None = None
    selection_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


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
        self._clients: dict[str, _SourceClientState] = {}
        # Playback session of the last stream released per source, to tell a retry from a selection.
        self._released_playback_sessions: dict[str, str] = {}
        self._server_unsubscribe: Callable[[], None] | None = None
        self._unloading = False

    async def loaded_in_mass(self) -> None:
        """Start watching every source client, including ones that are already idle."""
        await super().loaded_in_mass()
        self._unloading = False
        if (sendspin := self._sendspin_provider) is None:
            return
        self._server_unsubscribe = sendspin.server_api.add_event_listener(self._on_server_event)
        for client in sendspin.server_api.connected_clients:
            self._watch_client(client)
        for client in sendspin.server_api.clients:
            if self.get_player_input_sources(client.client_id):
                self._refresh_player_listing(client.client_id)

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        sendspin = self._sendspin_provider
        clients = sendspin.server_api.clients if sendspin is not None else []
        listed_client_ids = [
            client.client_id
            for client in clients
            if self.get_player_input_sources(client.client_id)
        ]
        self._unloading = True
        if self._server_unsubscribe is not None:
            self._server_unsubscribe()
            self._server_unsubscribe = None
        for client_id, state in list(self._clients.items()):
            self._cancel_pending_autostart(client_id)
            self._cancel_pending_autostop(client_id)
            if state.unwatch is not None:
                state.unwatch()
                state.unwatch = None
            await self._teardown_session(client_id)
        self._clients.clear()
        for client_id in listed_client_ids:
            self._refresh_player_listing(client_id)

    async def get_audio_sources(self) -> list[AudioSource]:
        """Return one AudioSource per connected source client or approved held input."""
        if (sendspin := self._sendspin_provider) is None:
            return []
        sources: list[AudioSource] = []
        for client in sendspin.server_api.clients:
            if client.is_connected:
                if self._get_source_role(client) is None:
                    continue
            elif not await self._can_reclaim(client):
                continue
            sources.append(self._audio_source(client))
        return sources

    def get_player_input_sources(self, player_id: str) -> list[AudioSource]:
        """Return the line-in of a connected client that is also a player, on that player."""
        if self._unloading:
            return []
        client = self._get_client(player_id)
        # Checks the connection, not is_connected: that only turns True on the first
        # client/state, which can arrive after the reconnect refresh reads this.
        if (
            client is None
            or client.connection is None
            or self._get_source_role(client) is None
            or "player" not in {role_family(role_id) for role_id in client.negotiated_role_ids}
        ):
            return []
        return [self._audio_source(client)]

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Return StreamDetails for streaming the given source to a queue.

        Side-effect-free: streaming is requested from the client in
        on_source_selected, which the streams controller fires before this
        method on the actual stream request (never on queue preload).
        """
        client = self._get_client(item_id)
        if client is None or not await self._is_selectable(client):
            raise MediaNotFoundError(f"Unknown or unavailable Sendspin source: {item_id}")
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=OUTPUT_FORMAT,
            media_type=media_type,
            stream_type=StreamType.CUSTOM,
            stream_metadata=StreamMetadata(title=self._source_name(item_id)),
        )

    async def on_source_selected(
        self, source_id: str, player_id: str, owner_player_id: str, stream_session_id: str
    ) -> None:
        """Claim the source and ask the client to start streaming."""
        client = self._get_client(source_id)
        if client is None or not await self._is_selectable(client):
            raise MediaNotFoundError(f"Sendspin source is not connected: {source_id}")
        state = self._clients.setdefault(source_id, _SourceClientState())
        # Capture the source's session before waiting so delayed requests keep their
        # identity: a live source plays on the player, so the queue has no session for it.
        queue_session_id = self._player_session_id(owner_player_id)
        try:
            # Serialize handoffs because stopping the previous player can suspend.
            async with state.selection_lock:
                # The client or its role may have changed while waiting for the lock.
                client = self._get_client(source_id)
                if client is None or not await self._is_selectable(client):
                    raise MediaNotFoundError(f"Sendspin source is not connected: {source_id}")
                # Reject only the delayed request from an autostart superseded by the user.
                if state.suppressed_autostart_claim == (owner_player_id, queue_session_id):
                    raise RuntimeError("Superseded autostart stream request")
                autostart_queue_id = state.autostart_queue_id
                if autostart_queue_id == owner_player_id:
                    state.autostart_queue_id = None
                    state.autostart_queue_session_id = None
                    self._cancel_pending_autostart(source_id, cancel_running=False)
                else:
                    autostart_session_id: str | None = None
                    if autostart_queue_id is not None:
                        autostart_session_id = (
                            state.autostart_queue_session_id
                            or self._player_session_id(autostart_queue_id)
                        )
                        state.autostart_queue_id = None
                        state.autostart_queue_session_id = None
                        state.suppressed_autostart_claim = (
                            autostart_queue_id,
                            autostart_session_id,
                        )
                    self._cancel_pending_autostart(source_id)
                    if autostart_queue_id is not None:
                        try:
                            # deselect rather than stopping the queue: the source plays on
                            # the player, so stopping the queue would leave the source
                            # published on it while resetting the queue we are preserving
                            await self.mass.players.deselect_source(
                                autostart_queue_id,
                                provider_instance_id=self.instance_id,
                                source_id=source_id,
                                playback_session_id=autostart_session_id,
                            )
                        except (KeyError, PlayerCommandFailed, PlayerUnavailableError) as err:
                            self.logger.debug(
                                "Failed to release autostart player %s: %s",
                                autostart_queue_id,
                                err,
                            )
                # Keep the running bridge when renderers reopen the URL for the same queue.
                # A new selection of an input another server took starts over to reclaim it.
                if (
                    (live := state.session) is not None
                    and (live.player_id, live.owner_player_id) == (player_id, owner_player_id)
                    and (
                        self._get_source_role(client) is not None
                        or live.playback_session_id == queue_session_id
                    )
                ):
                    live.stream_session_id = stream_session_id
                    live.playback_session_id = queue_session_id
                    live.generation += 1
                    return
                await self._teardown_session(source_id, superseded_by_player_id=player_id)
                if self._unloading or self._clients.get(source_id) is not state:
                    raise PlayerUnavailableError(f"Sendspin source is unloading: {source_id}")
                # Teardown can suspend long enough for the connection role to be recreated.
                client = self._get_client(source_id)
                if client is None or not await self._is_selectable(client):
                    raise MediaNotFoundError(f"Sendspin source is not connected: {source_id}")
                role = self._get_source_role(client)
                # The reconnect handler sends the start once the reclaimed client is back.
                if role is None and not self._request_reclaim(source_id, queue_session_id):
                    raise MediaNotFoundError(f"Sendspin source is not connected: {source_id}")
                state.session = _SourceSession(
                    client_id=source_id,
                    player_id=player_id,
                    owner_player_id=owner_player_id,
                    stream_session_id=stream_session_id,
                    playback_session_id=queue_session_id,
                )
                if role is not None:
                    role.request_start()
        finally:
            self._drop_empty_client_state(source_id, state)

    async def on_source_unselected(
        self, source_id: str, owner_player_id: str, stream_session_id: str
    ) -> None:
        """Release the source when MA tears down its stream."""
        session = self._get_session(source_id)
        # Reject stale callbacks from superseded same-queue requests.
        if session is None or session.stream_session_id != stream_session_id:
            return
        if session.playback_session_id is not None:
            self._released_playback_sessions[source_id] = session.playback_session_id
        await self._teardown_session(source_id)
        if (state := self._clients.get(source_id)) is not None:
            state.suppressed_autostart_claim = None
            self._drop_empty_client_state(source_id, state)

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
        session = self._get_session(streamdetails.item_id)
        if session is None:
            raise AudioError(f"Sendspin source is not selected: {streamdetails.item_id}")
        generation = session.generation
        await self._await_first_audio(session)
        if self._get_session(session.client_id) is not session or session.generation != generation:
            return
        frames_per_chunk = OUTPUT_SAMPLE_RATE * CHUNK_DURATION_MS // 1000
        period = CHUNK_DURATION_MS / 1000
        loop = self.mass.loop
        next_deadline = loop.time()
        while True:
            if (
                self._get_session(session.client_id) is not session
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

    def _audio_source(self, client: SendspinClient) -> AudioSource:
        return AudioSource(
            item_id=client.client_id,
            provider=self.instance_id,
            name=self._source_name(client.client_id),
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

    async def _is_selectable(self, client: SendspinClient) -> bool:
        return self._get_source_role(client) is not None or await self._can_reclaim(client)

    async def _can_reclaim(self, client: SendspinClient) -> bool:
        """Whether this client's input is held by another server and approved on this one."""
        # aiosendspin retains a client only after an another_server or concurrent_attempt goodbye.
        if not client.cleanup_on_mdns_removal:
            return False
        if "source" not in {role_family(role_id) for role_id in client.negotiated_role_ids}:
            return False
        # Only a device this server dialed can be dialed again to take it over.
        if (sendspin := self._sendspin_provider) is None or (
            sendspin.server_api.get_client_url(client.client_id) is None
        ):
            return False
        store = sendspin.server_api.pairing_store
        if await store.record_by_client_id(client.client_id) is not None:
            return True
        trusted = await store.trusted_unpaired(client.client_id)
        return trusted is not None and "source" in trusted.approved_roles

    def _request_reclaim(self, client_id: str, playback_session_id: str | None) -> bool:
        if (
            playback_session_id is not None
            and self._released_playback_sessions.get(client_id) == playback_session_id
        ):
            return False
        if (
            sendspin := self._sendspin_provider
        ) is None or not sendspin.server_api.request_client_playback_connection(client_id):
            return False
        self.logger.info("Taking Sendspin source %s over from another server", client_id)
        return True

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
            if self._get_session(session.client_id) is not session:
                return
            raise AudioError(
                f"Sendspin source {session.client_id} did not start streaming",
                translation_key="no_audio",
                translation_owner=self.translation_owner,
                translation_args=[self._device_name(session.client_id)],
            ) from None

    def _on_server_event(self, server: SendspinServer, event: SendspinEvent) -> None:
        if self._unloading:
            return
        match event:
            case ClientConnectedEvent(client_id):
                # Roles attach after this event, so defer until the next loop turn.
                self.mass.create_task(self._on_client_connected(client_id), eager_start=False)
            case ClientRemovedEvent(client_id) | ClientDisconnectedEvent(client_id):
                self._cancel_pending_autostart(client_id)
                self._cancel_pending_autostop(client_id)
                if (state := self._clients.get(client_id)) is not None:
                    state.signal = None
                    state.autostart_queue_id = None
                    state.autostart_queue_session_id = None
                    if state.session is None:
                        state.suppressed_autostart_claim = None
                    if state.unwatch is not None:
                        state.unwatch()
                        state.unwatch = None
                    self._drop_empty_client_state(client_id, state)
                self._refresh_player_listing(client_id)

    async def _on_client_connected(self, client_id: str) -> None:
        """Re-arm watching and streaming for a client that just (re)connected."""
        if self._unloading:
            return
        client = self._get_client(client_id)
        if client is None:
            return
        self._watch_client(client)
        # The player's source list depends on the source role, which only attached now.
        self._refresh_player_listing(client_id)
        # A reconnect clears the client's start request, so ask again.
        if self._get_session(client_id) is None:
            return
        role = self._get_source_role(client)
        if role is None or role.stream_active:
            return
        self.logger.debug("Re-requesting stream start from %s", client_id)
        role.request_start()

    def _refresh_player_listing(self, client_id: str) -> None:
        """Rebuild the source list of the visible player that lists this client's line-in."""
        if (player := self.mass.players.get_player(client_id)) is not None:
            self.mass.players.trigger_player_update(player.protocol_parent_id or client_id)

    def _watch_client(self, client: SendspinClient) -> None:
        """Subscribe to a source client's events, for signal presence while idle."""
        # Watch negotiated source roles because pairing can activate the role later
        # without reconnecting or emitting another event.
        if "source" not in {role_family(role_id) for role_id in client.negotiated_role_ids}:
            return
        state = self._clients.setdefault(client.client_id, _SourceClientState())
        if state.unwatch is not None:
            return
        state.unwatch = client.add_event_listener(self._on_client_event)

    def _on_client_event(self, client: SendspinClient, event: ClientEvent) -> None:
        client_id = client.client_id
        if isinstance(event, SourceSignalChangedEvent):
            self._on_signal_reported(client_id, event.signal)
            return
        session = self._get_session(client_id)
        if session is None:
            return
        if isinstance(event, SourceStreamStartedEvent):
            self.mass.create_task(self._attach_stream(session, event))
        elif isinstance(event, SourceStreamEndedEvent):
            self.logger.debug("Sendspin source %s ended its stream", client_id)
            # A connected client without a source role sends no more audio, a dropped one may
            # reconnect.
            if client.is_connected and self._get_source_role(client) is None:
                self.logger.info(
                    "Sendspin source %s lost its source role, stopping playback", client_id
                )
                self.mass.create_task(self._release_source(session))

    def _on_signal_reported(self, client_id: str, signal: SignalState) -> None:
        """
        Drive line-in autostart/autostop from a reported signal presence.

        Only transitions act. The first report for a client is recorded silently so a
        server restart or a reconnect with the needle already down starts nothing.
        """
        state = self._clients.setdefault(client_id, _SourceClientState())
        previous = state.signal
        if previous == signal:
            return
        state.signal = signal
        if previous is None:
            return
        self.logger.debug("Sendspin source %s signal %s", client_id, signal.value)
        self._cancel_pending_autostart(client_id)
        self._cancel_pending_autostop(client_id)
        if signal == SignalState.PRESENT:
            self.mass.call_later(
                AUTOSTART_SIGNAL_DEBOUNCE_S,
                self._autostart,
                client_id,
                task_id=self._autostart_timer_id(client_id),
            )
        else:
            self.mass.call_later(
                AUTOSTART_SIGNAL_ABSENT_HOLD_S,
                self._autostop,
                client_id,
                task_id=self._autostop_timer_id(client_id),
            )

    def _cancel_pending_autostart(self, client_id: str, *, cancel_running: bool = True) -> None:
        self._cancel_scheduled_action(
            self._autostart_timer_id(client_id), cancel_running=cancel_running
        )

    def _cancel_pending_autostop(self, client_id: str) -> None:
        self._cancel_scheduled_action(self._autostop_timer_id(client_id))

    async def _autostart(self, client_id: str) -> None:
        """Start playing a source whose signal has stayed present."""
        state = self._clients.get(client_id)
        if state is None or state.signal != SignalState.PRESENT or state.session is not None:
            return
        if (target := await self._resolve_autostart_target(client_id)) is None:
            return
        state = self._clients.get(client_id)
        if state is None or state.signal != SignalState.PRESENT or state.session is not None:
            return
        queue_id, uri = target
        self.logger.info("Line-in signal on %s, starting playback on %s", client_id, queue_id)
        state.suppressed_autostart_claim = None
        state.autostart_queue_id = queue_id
        state.autostart_queue_session_id = None
        completed = False
        try:
            await self.mass.player_queues.play_media(queue_id, uri, option=QueueOption.PLAY)
            completed = True
            if self._clients.get(client_id) is state and state.autostart_queue_id == queue_id:
                state.autostart_queue_session_id = self._player_session_id(queue_id)
        finally:
            if (
                not completed
                and self._clients.get(client_id) is state
                and state.autostart_queue_id == queue_id
            ):
                state.autostart_queue_id = None
                state.autostart_queue_session_id = None
                self._drop_empty_client_state(client_id, state)

    async def _autostop(self, client_id: str) -> None:
        """Stop a source whose signal has stayed absent, e.g. a record that ended."""
        state = self._clients.get(client_id)
        if state is None or state.signal != SignalState.ABSENT or state.session is None:
            return
        self.logger.info("Line-in signal gone on %s, stopping playback", client_id)
        await self._release_source(state.session)

    async def _release_source(self, session: _SourceSession) -> None:
        """Give the source up on the player that owns it, stopping that player."""
        try:
            # Queue stop also cancels pending preload and enqueue timers.
            await self.mass.players.deselect_source(
                session.owner_player_id,
                provider_instance_id=self.instance_id,
                source_id=session.client_id,
                playback_session_id=session.playback_session_id,
            )
        except (KeyError, PlayerCommandFailed, PlayerUnavailableError) as err:
            self.logger.debug("Failed to stop player %s: %s", session.owner_player_id, err)

    def _player_session_id(self, player_id: str) -> str | None:
        """
        Return the id of whatever playback session the given player is running.

        A live source's session belongs to the player; anything else is the queue's.
        Used to tell a superseded autostart request apart from the one that replaced it.

        :param player_id: The player to read the session of.
        """
        if (session := self.mass.players.get_audio_source_session(player_id)) is not None:
            return session.playback_session_id
        return self.mass.player_queues.queue_data(player_id).session_id

    async def _resolve_autostart_target(self, client_id: str) -> tuple[str, str] | None:
        """Return the queue id and source uri to autostart, if the source is configured."""
        # Config entry defaults are not persisted until the user saves the page.
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
        # Keep a grouped target in its active group. A target already playing a live
        # source resolves to no queue, which is not a reason to refuse - it is the
        # player we start on either way.
        queue = self.mass.players.get_active_queue(player)
        # mirror the controller's owner resolution: a sync child starts on its leader and
        # a group member on its group, never on itself - selecting on the child ungroups it
        target_id = (
            queue.queue_id
            if queue
            else (player.state.synced_to or player.state.active_group or player.player_id)
        )
        return target_id, create_uri(MediaType.AUDIO_SOURCE, self.instance_id, client_id)

    async def _attach_stream(
        self, session: _SourceSession, event: SourceStreamStartedEvent
    ) -> None:
        """Route a (re)started source stream into a fresh bridge."""
        if self._get_session(session.client_id) is not session:
            return
        if session.ingest_task is not None:
            session.ingest_task.cancel()
        # Provider options are unresolved when the instance first loads.
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
        bridge = session.bridge
        if bridge is None:
            return
        async for pcm, timestamp_us in handle:
            if self._get_session(session.client_id) is not session or session.bridge is not bridge:
                break
            try:
                bridge.feed(pcm, timestamp_us)
            except ValueError as err:
                self.logger.warning("Dropping malformed chunk from %s: %s", session.client_id, err)
                continue
            session.last_pcm_monotonic = time.monotonic()
            session.pcm_received.set()
        else:
            if self._get_session(session.client_id) is session and session.bridge is bridge:
                bridge.flush()

    async def _teardown_session(
        self, source_id: str, superseded_by_player_id: str | None = None
    ) -> None:
        state = self._clients.get(source_id)
        if state is None or state.session is None:
            return
        session, state.session = state.session, None
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
        self._drop_empty_client_state(source_id, state)

    def _get_session(self, client_id: str) -> _SourceSession | None:
        state = self._clients.get(client_id)
        return state.session if state is not None else None

    def _drop_empty_client_state(self, client_id: str, state: _SourceClientState) -> None:
        if (
            self._clients.get(client_id) is state
            and state.session is None
            and state.unwatch is None
            and state.signal is None
            and state.autostart_queue_id is None
            and state.autostart_queue_session_id is None
            and state.suppressed_autostart_claim is None
            and not state.selection_lock.locked()
        ):
            self._clients.pop(client_id)

    def _cancel_scheduled_action(self, task_id: str, *, cancel_running: bool = True) -> None:
        self.mass.cancel_timer(task_id)
        task = self.mass.get_task(task_id)
        if cancel_running and task is not None and task is not asyncio.current_task():
            self.mass.cancel_task(task_id)

    def _source_name(self, client_id: str) -> str:
        return f"{self.name} ({self._device_name(client_id)})"

    def _device_name(self, client_id: str) -> str:
        """Return the name of the player the user sees for this client."""
        if (player := self.mass.players.get_player(client_id)) is not None:
            if player.protocol_parent_id and (
                parent := self.mass.players.get_player(player.protocol_parent_id)
            ):
                player = parent
            return player.display_name
        # Fall back to the device's own name while it has no registered player.
        client = self._get_client(client_id)
        info = client.info_or_none if client else None
        return info.name if info else client_id

    def _autostart_timer_id(self, client_id: str) -> str:
        return f"{self.instance_id}_autostart_{client_id}"

    def _autostop_timer_id(self, client_id: str) -> str:
        return f"{self.instance_id}_autostop_{client_id}"
