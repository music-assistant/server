"""AirPlay Receiver plugin provider implementation."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from contextlib import suppress
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.enums import (
    ContentType,
    EventType,
    ImageType,
    MediaType,
    ProviderFeature,
    SourceControl,
    StreamType,
)
from music_assistant_models.errors import (
    AudioError,
    MediaNotFoundError,
    PlayerCommandFailed,
    PlayerUnavailableError,
    UnsupportedFeaturedException,
)
from music_assistant_models.media_items import (
    AudioFormat,
    AudioSource,
    MediaItemImage,
    ProviderMapping,
)
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata
from zeroconf import Error as ZeroconfError
from zeroconf.asyncio import AsyncServiceInfo

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.config_entries import (
    CONF_CONNECTED_PLAYERS,
    CONF_PUBLISH_NAME_TEMPLATE,
    create_connected_players_entry,
    create_publish_name_template_entry,
    resolve_publish_name,
)
from music_assistant.helpers.named_pipe import AsyncNamedPipeWriter
from music_assistant.helpers.player import (
    PlayerAvailabilityTracker,
    can_start_playback,
    get_available_player,
)
from music_assistant.helpers.process import AsyncProcess, check_output
from music_assistant.helpers.util import (
    get_ip_pton,
    get_publish_ip_candidates,
    interface_name_for_ip,
)
from music_assistant.models.media_capabilities import SourceControlValue
from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.airplay_receiver.helpers import get_shairport_sync_binary
from music_assistant.providers.airplay_receiver.metadata import MdnsService, MetadataReader

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.event import MassEvent
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.player import Player

SUPPORTED_FEATURES = {ProviderFeature.AUDIO_SOURCE}

# seconds the silence nudge waits for the audio pipe's consumer to reattach
AUDIO_PIPE_READER_TIMEOUT = 1.0
# seconds a flush of the AirPlay client must last to count as a pause (a seek resumes at once)
FLUSH_PAUSE_DELAY = 2.0


def airplay_receiver_ports(instance_id: str, player_ids: Iterable[str]) -> dict[str, int]:
    """
    Return the AirPlay port used for each connected player of a receiver instance.

    Deterministically derived from the instance id and player id, so the ports stay
    the same across server restarts (Python's built-in ``hash()`` is salted per
    process). Colliding derivations probe upwards deterministically, staying within
    the 7000-7999 AirPlay 2 range.

    :param instance_id: The provider instance id of the AirPlay receiver.
    :param player_ids: The connected player ids to derive ports for.
    """
    unique_player_ids = sorted(set(player_ids))
    if len(unique_player_ids) > 1000:
        # cannot happen through the UI; guards the probing loop against a
        # malformed stored value hanging startup
        raise ValueError("More connected players than available AirPlay ports")
    ports: dict[str, int] = {}
    claimed: set[int] = set()
    # iterate sorted so probing resolves collisions the same way for any input order
    for player_id in unique_player_ids:
        digest = hashlib.md5(
            f"{instance_id}_{player_id}".encode(), usedforsecurity=False
        ).hexdigest()
        port = 7000 + int(digest, 16) % 1000
        while port in claimed:
            port = 7000 + (port - 7000 + 1) % 1000
        claimed.add(port)
        ports[player_id] = port
    return ports


@dataclass
class _ReceiverDaemon:
    """State for one connected player's shairport-sync receiver."""

    # the connected player this receiver plays on, as stored (also after that player was
    # merged into another one); doubles as the AudioSource item_id and keys the port
    player_id: str
    # player_id sanitized for use in filesystem paths
    safe_player_id: str
    # the name this receiver advertises in the AirPlay device list
    airplay_name: str
    port: int
    audio_pipe: AsyncNamedPipeWriter
    metadata_pipe: AsyncNamedPipeWriter
    config_file: str
    audio_source: AudioSource
    stream_metadata: StreamMetadata
    shairport_proc: AsyncProcess | None = None
    runner_task: asyncio.Task[None] | None = None
    started: asyncio.Event = field(default_factory=asyncio.Event)
    metadata_reader: MetadataReader | None = None
    runner_error_count: int = 0
    stop_called: bool = False
    # Currently active player (the one currently playing or selected)
    active_player_id: str | None = None
    # in_use_by_player: the queue currently streaming us. Claimed in
    # on_source_selected (NOT in get_stream_details — that path also runs
    # from queue preload, where claiming would block a later cross-queue
    # handoff). Released in on_source_unselected when the session id
    # matches, or in _clear_active_player on external session disconnect.
    in_use_by_player: str | None = None
    # active_session_id is the controller-provided token for the current
    # stream request — used to reject stale on_source_unselected callbacks
    # after a same-queue reconnect supersedes the previous request.
    active_session_id: str | None = None
    pending_stop_task: asyncio.Task[None] | None = None
    # the in-flight externally-triggered playback start (awaits the pending stop)
    pending_start_task: asyncio.Task[None] | None = None
    # pauses the player once a flush of the AirPlay client is not followed by a resume
    pending_pause_task: asyncio.Task[None] | None = None
    # the player the source was paused on, which a resume of the AirPlay client plays on again
    paused_player_id: str | None = None
    first_volume_event_received: bool = False  # Track if we've received the first volume event
    # an AirPlay client streams to this receiver (shairport-sync's active state)
    client_active: bool = False
    # the player a failed playback start waits for, until it can play
    waiting_player_id: str | None = None
    unsubscribe_waiting: Callable[[], None] | None = None
    # the mDNS service published on behalf of the running shairport-sync process
    mdns_info: AsyncServiceInfo | None = None
    mdns_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def cover_art_path(self, img_hash: str) -> str:
        """
        Return the provider-scoped image path for this receiver's cover art.

        :param img_hash: Content hash of the current artwork bytes.
        """
        # the player id keeps simultaneous sessions on different receivers from
        # serving each other's artwork through the single provider instance
        return f"cover_art_{self.safe_player_id}_{img_hash}"


class AirPlayReceiverProvider(PluginProvider):
    """Implementation of an AirPlay Receiver Plugin."""

    reload_on_streams_network_change = True

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize MusicProvider."""
        super().__init__(mass, manifest, config, SUPPORTED_FEATURES)
        self._shairport_bin: str | None = None
        self._daemons: dict[str, _ReceiverDaemon] = {}
        # players whose daemon gave up permanently; skipped by reconcile until the
        # player comes back or the provider reloads
        self._failed_player_ids: set[str] = set()
        # seeded where the player events are subscribed, so no event is missed in between
        self._availability: PlayerAvailabilityTracker
        self._reconcile_lock = asyncio.Lock()
        self._unload_called = False
        self._unsubscribe: Callable[[], None] | None = None
        # the connected players are immutable per load: config changes reload the provider
        self._assigned_player_ids: tuple[str, ...] = tuple(
            cast("list[str]", self.get_config_value(CONF_CONNECTED_PLAYERS) or [])
        )
        # One unique AirPlay 2 (7000+) port per connected player. The ports must be
        # stable across restarts: the AirPlay provider uses them to recognize (and
        # ignore) our own shairport-sync advertisements in discovery.
        self._ports = airplay_receiver_ports(self.instance_id, self._assigned_player_ids)
        # _audio_format describes the original AirPlay source (ALAC at 44.1/16,
        # the protocol-native format AirPlay senders use) and is what we
        # advertise to clients for source-format display.
        self._audio_format = AudioFormat(
            content_type=ContentType.ALAC,
            codec_type=ContentType.ALAC,
            sample_rate=44100,
            bit_depth=16,
            channels=2,
        )
        # _decoded_audio_format is what shairport-sync actually pipes into MA
        # after decoding the ALAC stream; the streams controller hands this to
        # ffmpeg as the input format so it can read the FIFO correctly.
        self._decoded_audio_format = AudioFormat(
            content_type=ContentType.PCM_S16LE,
            codec_type=ContentType.PCM_S16LE,
            sample_rate=44100,
            bit_depth=16,
            channels=2,
        )

    @property
    def airplay_ports(self) -> set[int]:
        """Return the AirPlay ports of the currently running receiver daemons."""
        return {daemon.port for daemon in self._daemons.values()}

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return runtime options for this provider."""
        return (
            create_connected_players_entry(
                self.mass, cast("list[str]", self.get_config_value(CONF_CONNECTED_PLAYERS) or [])
            ),
            create_publish_name_template_entry(self.get_config_value(CONF_PUBLISH_NAME_TEMPLATE)),
        )

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._shairport_bin = await get_shairport_sync_binary()

    async def loaded_in_mass(self) -> None:
        """Start the receiver daemons and follow the connected players' lifecycle."""
        await super().loaded_in_mass()
        if self._assigned_player_ids:
            self._availability = PlayerAvailabilityTracker(self.mass)
            self._subscribe_player_events()
        # players come online after plugins load, so on a cold boot this typically starts
        # nothing yet: the events of players becoming available drive the daemon startups
        await self._reconcile()

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
        self._unload_called = True
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        async with self._reconcile_lock:
            daemons = list(self._daemons.values())
            self._daemons.clear()
        if daemons:
            await asyncio.gather(*(self._stop_receiver(daemon) for daemon in daemons))
            # drop the standing source entries from the players' cached source lists
            for daemon in daemons:
                self.mass.players.trigger_player_update(daemon.player_id)

    async def get_audio_sources(self) -> list[AudioSource]:
        """Return the AudioSources this plugin currently exposes."""
        return [daemon.audio_source for daemon in self._daemons.values()]

    def get_player_audio_sources(self, player_id: str) -> list[AudioSource]:
        """Return the AudioSource bound to the given connected player, if any."""
        return [
            daemon.audio_source
            for stored_id, daemon in self._daemons.items()
            if self.mass.players.resolve_player_id(stored_id) == player_id
        ]

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Return StreamDetails for streaming the AirPlay audio to a queue.

        Side-effect-free: ownership is claimed in on_source_selected (which the
        streams controller fires before this method on the actual stream
        request). Keeping this idempotent means preload paths like
        player_queues._load_item can fetch streamdetails without claiming the
        source and blocking a subsequent cross-queue handoff.

        :raises AudioError: When no AirPlay client streams to the receiver.
        """
        daemon = self._daemons.get(item_id)
        if daemon is None:
            raise MediaNotFoundError(f"Unknown AudioSource: {item_id}")
        if not daemon.client_active:
            raise AudioError(
                "AirPlay receiver has no active client — start playback from your "
                "AirPlay-capable device first"
            )
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=self._audio_format,
            decoded_audio_format=self._decoded_audio_format,
            media_type=MediaType.AUDIO_SOURCE,
            stream_type=StreamType.NAMED_PIPE,
            path=daemon.audio_pipe.path,
            stream_metadata=daemon.stream_metadata,
        )

    async def on_source_control(
        self,
        source_id: str,
        action: SourceControl,
        value: SourceControlValue = None,
    ) -> None:
        """
        Handle source control commands (no-op: AirPlay receiver is passive).

        The AudioSource advertises no control capabilities, so MA will not invoke
        any actions here. Override exists only to satisfy the contract.
        """
        del source_id, action

    async def on_source_selected(
        self,
        source_id: str,
        player_id: str,
        owner_player_id: str,
        stream_session_id: str,
    ) -> None:
        """
        Handle callback when this AudioSource is selected/started on a player.

        :raises RuntimeError: When no AirPlay client streams to the receiver.
        """
        daemon = self._daemons.get(source_id)
        if daemon is None or not player_id:
            return
        if not daemon.client_active:
            # raised before claiming, so the streams controller cleanly refuses the request
            raise RuntimeError("No AirPlay client is streaming to this receiver")
        # the source plays now, which settles a start that waits for its player
        self._stop_waiting(daemon)

        # Cache the owner_player_id (user-facing MA player) rather than the protocol-
        # level player_id; protocol bridges (e.g. Sendspin's spb_…) can tear
        # down between streams and their ID is then invalid for play_media.
        active_player_id = owner_player_id

        # If another player is still streaming the source, kick it out.
        # The lock claim a few lines below replaces the previous queue's claim;
        # the prior stream's on_source_unselected may fire later, but its
        # session-id guard keeps it from clobbering the new claim.
        if daemon.in_use_by_player and daemon.in_use_by_player != active_player_id:
            prev_player_id = daemon.in_use_by_player
            self.logger.info(
                "Source selected on player %s, stopping playback on %s",
                active_player_id,
                prev_player_id,
            )
            try:
                await self.mass.players.cmd_stop(prev_player_id)
            except Exception as err:  # noqa: BLE001 - a failing stop must not block the new source
                self.logger.debug("Failed to stop previous player %s: %s", prev_player_id, err)

        # Claim ownership for this queue. The lock lives here (not in
        # get_stream_details) so preload paths can fetch streamdetails without
        # accidentally blocking a subsequent cross-queue handoff at the actual
        # stream request.
        daemon.in_use_by_player = owner_player_id
        # Record this request's session id so a later on_source_unselected can
        # tell whether it is the live teardown or a stale callback from a
        # superseded same-queue request.
        daemon.active_session_id = stream_session_id

        # Update the active player
        daemon.active_player_id = active_player_id
        self.logger.debug("Active player set to: %s", active_player_id)

    async def on_source_unselected(
        self, source_id: str, owner_player_id: str, stream_session_id: str
    ) -> None:
        """Release the queue-scoped exclusive claim when MA tears down the stream."""
        daemon = self._daemons.get(source_id)
        if daemon is None:
            return
        # Reject stale callbacks: only release if this is still the active
        # session. A owner_player_id check alone is not sufficient — same-queue
        # reconnects (player drops + reopens the same stream URL before the
        # original request's finally fires) would otherwise let the old
        # request's late callback clear the live claim of the new stream.
        if daemon.active_session_id != stream_session_id:
            return
        daemon.active_session_id = None
        if daemon.in_use_by_player == owner_player_id:
            daemon.in_use_by_player = None

    async def resolve_image(self, path: str) -> bytes:
        """
        Resolve an image from an image path.

        This returns raw bytes of the cover art image received from AirPlay metadata.

        :param path: The image path, carrying the receiver's player id and the
            current cover art content hash suffix.
        """
        for daemon in self._daemons.values():
            if not (daemon.metadata_reader and daemon.metadata_reader.cover_art_bytes):
                continue
            current_hash = hashlib.md5(
                daemon.metadata_reader.cover_art_bytes, usedforsecurity=False
            ).hexdigest()[:8]
            # Only serve when the suffix matches the current artwork's hash, so a
            # stale request can't cache new bytes under an old hash key.
            if path == daemon.cover_art_path(current_hash):
                return daemon.metadata_reader.cover_art_bytes
        return b""

    async def _on_player_event(self, event: MassEvent) -> None:
        """Reconcile the receiver daemons after a connected player's lifecycle event."""
        if self._unload_called:
            return
        # a removal passes the tracker too, so the player's return is recognized
        came_available = self._availability.became_available(event)
        if event.event == EventType.PLAYER_REMOVED and event.object_id:
            if self.mass.players.resolve_player_id(event.object_id) != event.object_id:
                # merged into another player: the receiver plays on that one from now on
                self._subscribe_player_events()
                await self._reconcile()
                return
            # permanent removal: stop the daemon; a temporarily unavailable player
            # (which fires only PLAYER_UPDATED) keeps its running daemon so the
            # advertised device identity stays stable across the outage
            async with self._reconcile_lock:
                for stored_id in self._stored_ids_of(event.object_id):
                    if daemon := self._daemons.pop(stored_id, None):
                        # the session may be consumed by ANOTHER player (cross-select or
                        # sync-group owner); release it so that player is not left bound
                        # to a source that can no longer stream
                        self._clear_active_player(daemon)
                        await self._stop_receiver(daemon)
            return
        if came_available and event.object_id:
            # a player that comes back earns a permanently failed daemon a fresh start
            self._failed_player_ids.difference_update(self._stored_ids_of(event.object_id))
        await self._reconcile()

    async def _reconcile(self) -> None:
        """
        Align the running receiver daemons with the connected players.

        Starts a daemon for every connected player that is available, and restarts
        a daemon whose advertised name drifted from the player's current name. A player
        keeps one receiver, also when several connected players were merged into it.
        """
        async with self._reconcile_lock:
            if self._unload_called:
                return
            template = self.get_config_value(CONF_PUBLISH_NAME_TEMPLATE)
            receiver_ids = self._receiver_ids.values()
            for stored_id in self._daemons.keys() - receiver_ids:
                # merged into another connected player, whose own receiver stays
                merged = self._daemons.pop(stored_id)
                self._clear_active_player(merged)
                await self._stop_receiver(merged)
                self.mass.players.trigger_player_update(stored_id)
            for player_id in receiver_ids:
                if player_id in self._failed_player_ids:
                    # this daemon gave up permanently; blocked from restarts until the
                    # player comes back or the provider reloads
                    continue
                player = get_available_player(self.mass, player_id)
                if player is None:
                    # not available (yet): never start a daemon for it; an already
                    # running one is deliberately kept (see _on_player_event)
                    continue
                airplay_name = resolve_publish_name(template, player.name)
                daemon = self._daemons.get(player_id)
                if daemon is not None and daemon.airplay_name == airplay_name:
                    continue
                if daemon is not None:
                    # the advertised name follows the player name: restart on rename.
                    # A live session is released first so the consuming player's queue
                    # is not left held by a source the replaced daemon cannot stream
                    # (unload and player removal already release via the controller).
                    self._clear_active_player(daemon)
                    del self._daemons[player_id]
                    await self._stop_receiver(daemon)
                self._start_receiver(player_id, player, airplay_name)
                # the standing source entry feeds the player's cached source list
                self.mass.players.trigger_player_update(player_id)

    @property
    def _connected_player_ids(self) -> tuple[str, ...]:
        """Return the ids the connected players answer to now, also after one was merged."""
        return tuple(self._receiver_ids)

    @property
    def _receiver_ids(self) -> dict[str, str]:
        """
        Return the stored id that keys the receiver of each connected player, by its id now.

        That is the id of the player itself when it is connected, else the first connected
        player that was merged into it: the receiver keeps the port and name it had.
        """
        receiver_ids: dict[str, str] = {}
        for stored_id in self._assigned_player_ids:
            player_id = self.mass.players.resolve_player_id(stored_id)
            if player_id not in receiver_ids or stored_id == player_id:
                receiver_ids[player_id] = stored_id
        return receiver_ids

    def _stored_ids_of(self, player_id: str) -> set[str]:
        """
        Return the stored ids of the connected players that answer to a player id now.

        :param player_id: Id of a player.
        """
        return {
            stored_id
            for stored_id in self._assigned_player_ids
            if self.mass.players.resolve_player_id(stored_id) == player_id
        }

    def _subscribe_player_events(self) -> None:
        """Follow the lifecycle events of the connected players."""
        if self._unsubscribe is not None:
            self._unsubscribe()
        self._unsubscribe = self.mass.subscribe(
            self._on_player_event,
            event_filter=(
                EventType.PLAYER_ADDED,
                EventType.PLAYER_REMOVED,
                EventType.PLAYER_CONFIG_UPDATED,
                EventType.PLAYER_UPDATED,
            ),
            id_filter=self._connected_player_ids,
        )

    def _start_receiver(self, player_id: str, player: Player, airplay_name: str) -> None:
        """
        Create the receiver state for a connected player and start its daemon.

        :param player_id: The connected player as stored, which keys the receiver and its port.
        :param player: The (available) player this receiver plays on.
        :param airplay_name: The name to advertise in the AirPlay device list.
        """
        safe_player_id = re.sub(r"[^A-Za-z0-9_.-]", "_", player_id)
        receiver_key = f"{self.instance_id}_{safe_player_id}"
        runtime_path = self.mass.runtime_path
        audio_source = AudioSource(
            # the player id is stable across renames, so the source uri survives them
            item_id=player_id,
            provider=self.instance_id,
            name=self.name,
            owner_player_id=player_id,
            provider_mappings={
                ProviderMapping(
                    item_id=player_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=self._audio_format,
                )
            },
            can_play_pause=False,
            can_seek=False,
            can_next_previous=False,
            exclusive=True,
            allow_external_trigger=True,
            # passive: only selectable while an AirPlay client streams (see _set_client_active)
            can_initiate=False,
        )
        daemon = _ReceiverDaemon(
            player_id=player_id,
            safe_player_id=safe_player_id,
            airplay_name=airplay_name,
            port=self._ports[player_id],
            audio_pipe=AsyncNamedPipeWriter(f"{runtime_path}/ma_airplay_audio_{receiver_key}"),
            metadata_pipe=AsyncNamedPipeWriter(
                f"{runtime_path}/ma_airplay_metadata_{receiver_key}"
            ),
            config_file=f"{runtime_path}/ma_shairport_sync_{receiver_key}.conf",
            audio_source=audio_source,
            # no title until a track plays, so the source shows by its own name
            stream_metadata=StreamMetadata(title=""),
        )
        self._daemons[player_id] = daemon
        self._setup_shairport_daemon(daemon)

    async def _stop_receiver(self, daemon: _ReceiverDaemon) -> None:
        """Stop a receiver's shairport-sync daemon and release its resources."""
        daemon.stop_called = True
        # before the awaits below, so no waiting start can be spawned meanwhile
        self._stop_waiting(daemon)

        # a pending stop or deferred start must not wake after the teardown and
        # act on the replaced daemon's session; cancel BOTH before awaiting either
        # (awaiting first could let the other progress meanwhile), and await so a
        # cancellation-delayed player command cannot land after the daemon is gone
        pending_tasks = [
            task
            for task in (
                daemon.pending_stop_task,
                daemon.pending_start_task,
                daemon.pending_pause_task,
            )
            if task and not task.done()
        ]
        for pending_task in pending_tasks:
            pending_task.cancel()
        for pending_task in pending_tasks:
            with suppress(asyncio.CancelledError):
                await pending_task
        daemon.pending_stop_task = None
        daemon.pending_start_task = None
        daemon.pending_pause_task = None

        # Stop metadata reader
        if daemon.metadata_reader:
            await daemon.metadata_reader.stop()
            daemon.metadata_reader = None

        # Stop shairport-sync process
        if daemon.runner_task and not daemon.runner_task.done():
            daemon.runner_task.cancel()
            with suppress(asyncio.CancelledError):
                await daemon.runner_task
            daemon.runner_task = None

        # the runner withdraws the service when its process exits, but a stop that lands
        # while the runner already closes the process cuts that teardown short
        await self._unpublish_mdns_service(daemon)

        # Reset the shairport process reference
        daemon.shairport_proc = None
        daemon.started.clear()

    async def _give_up_receiver(self, daemon: _ReceiverDaemon, error: str) -> None:
        """
        Permanently stop a single failed receiver, leaving the other receivers running.

        :param daemon: The receiver whose daemon failed permanently.
        :param error: The daemon's failure description.
        """
        async with self._reconcile_lock:
            # a rename restart, player removal or unload may have replaced or
            # stopped the receiver meanwhile; only the live one gives up
            if self._daemons.get(daemon.player_id) is not daemon:
                return
            del self._daemons[daemon.player_id]
            self._failed_player_ids.add(daemon.player_id)
            self.logger.warning(
                "Giving up on AirPlay receiver '%s' for player %s: %s "
                "Other players are unaffected; reload the provider to retry.",
                daemon.airplay_name,
                daemon.player_id,
                error,
            )
            # release a consuming player so it is not left bound to a dead source
            self._clear_active_player(daemon)
            await self._stop_receiver(daemon)
        # drop the standing source entry from the player's cached source list
        self.mass.players.trigger_player_update(daemon.player_id)

    def _setup_shairport_daemon(self, daemon: _ReceiverDaemon) -> None:
        """Handle setup of the shairport-sync daemon for a receiver."""
        # a delayed restart can fire after the receiver was stopped or replaced
        if daemon.stop_called or self._daemons.get(daemon.player_id) is not daemon:
            return
        daemon.started.clear()
        daemon.runner_task = self.mass.create_task(self._shairport_runner(daemon))

    async def _shairport_runner(self, daemon: _ReceiverDaemon) -> None:
        """Run a receiver's shairport-sync daemon in a background task."""
        assert self._shairport_bin
        self.logger.info("Starting AirPlay Receiver background daemon for %s", daemon.airplay_name)
        await self._setup_pipes_and_config(daemon)

        try:
            args: list[str] = [
                self._shairport_bin,
                "--configfile",
                daemon.config_file,
            ]
            daemon.shairport_proc = shairport = AsyncProcess(
                args, stderr=True, name=f"shairport-sync[{daemon.airplay_name}]"
            )

            # Open the FIFO before shairport-sync starts writing metadata to it.
            daemon.metadata_reader = MetadataReader(
                daemon.metadata_pipe.path, self.logger, partial(self._on_metadata_update, daemon)
            )
            await daemon.metadata_reader.start()

            await shairport.start()

            # Check if process started successfully
            await asyncio.sleep(0.1)
            if shairport.returncode is not None:
                self.logger.error(
                    "shairport-sync exited immediately with code %s", shairport.returncode
                )
                return

            # Keep reading logging from stderr until exit
            self.logger.debug("Starting to read shairport-sync stderr")
            async for stderr_line in shairport.iter_stderr():
                line = stderr_line.strip()
                self._process_shairport_log_line(daemon, line)

        finally:
            await shairport.close()
            self.logger.info(
                "AirPlay Receiver background daemon stopped for %s (exit code: %s)",
                daemon.airplay_name,
                shairport.returncode,
            )

            # Stop metadata reader
            if daemon.metadata_reader:
                await daemon.metadata_reader.stop()

            # nothing answers on the advertised port any more
            await self._unpublish_mdns_service(daemon)

            # Clean up pipes and config
            await self._cleanup_pipes_and_config(daemon)

            self._handle_runner_exit(daemon)

    def _handle_runner_exit(self, daemon: _ReceiverDaemon) -> None:
        """
        Decide how to follow up on a receiver's daemon exit (restart, give up or unload).

        :param daemon: The receiver daemon whose shairport-sync process exited.
        """
        if daemon.stop_called:
            # deliberately stopped (unload, rename restart or player removal)
            return
        if not daemon.started.is_set():
            # the binary produced no output at all: an environment-level problem
            # every receiver would hit alike
            self.unload_with_error("Unable to initialize shairport-sync daemon.")
        # Auto restart if not stopped manually
        elif daemon.runner_error_count >= 5:
            # deferred task (no eager start): this runs on the daemon's own
            # runner task, which the give-up is about to stop
            self.mass.create_task(
                self._give_up_receiver(
                    daemon, "shairport-sync daemon failed to start multiple times."
                ),
                eager_start=False,
            )
        else:
            daemon.runner_error_count += 1
            self.mass.call_later(2, self._setup_shairport_daemon, daemon)

    def _process_shairport_log_line(self, daemon: _ReceiverDaemon, line: str) -> None:
        """
        Process a log line from shairport-sync stderr.

        :param daemon: The receiver daemon the log line originates from.
        :param line: The log line to process.
        """
        # any output proves the binary runs: a subsequent exit is then handled by
        # the supervised restart path (per receiver) instead of unloading the
        # provider as uninitializable
        if not daemon.started.is_set():
            daemon.started.set()
        # Check for fatal errors (log them, but process will exit on its own)
        if "fatal error:" in line.lower() or "unknown option" in line.lower():
            self.logger.error("Fatal error from shairport-sync: %s", line)
            return
        # Log connection messages at INFO level, everything else at DEBUG
        if "connection from" in line:
            self.logger.info("AirPlay client connected: %s", line)
        else:
            # Note: Play begin/stop events arrive through the metadata pipe,
            # so we don't need to parse stderr logs
            self.logger.debug(line)

    async def _setup_pipes_and_config(self, daemon: _ReceiverDaemon) -> None:
        """
        Set up named pipes and configuration file for shairport-sync.

        :raises: OSError if pipe or config file creation fails.
        """
        # Remove any existing pipes and config
        await self._cleanup_pipes_and_config(daemon)

        # Create named pipes for audio and metadata
        await daemon.audio_pipe.create()
        await daemon.metadata_pipe.create()

        # Create configuration file
        await self._create_config_file(daemon)

    async def _cleanup_pipes_and_config(self, daemon: _ReceiverDaemon) -> None:
        """Clean up named pipes and configuration file."""
        await daemon.audio_pipe.remove()
        await daemon.metadata_pipe.remove()
        await check_output("rm", "-f", daemon.config_file)

    async def _create_config_file(self, daemon: _ReceiverDaemon) -> None:
        """Create a receiver's shairport-sync configuration file from the template."""
        # Read template
        template_path = os.path.join(os.path.dirname(__file__), "bin", "shairport-sync.conf")

        def _read_template() -> str:
            with open(template_path, encoding="utf-8") as f:
                return f.read()

        template = await asyncio.to_thread(_read_template)

        # Replace placeholders. The name lands inside a quoted libconfig string:
        # escape it so a quote or backslash in a player name cannot break the config.
        safe_name = daemon.airplay_name.replace("\\", "\\\\").replace('"', '\\"')
        config_content = template.replace("{AIRPLAY_NAME}", safe_name)
        config_content = config_content.replace("{METADATA_PIPE}", daemon.metadata_pipe.path)
        config_content = config_content.replace("{AUDIO_PIPE}", daemon.audio_pipe.path)
        config_content = config_content.replace("{PORT}", str(daemon.port))

        # Set default volume based on the connected player's current volume if available
        # Convert player volume (0-100) to AirPlay volume (-30.0 to 0.0 dB)
        player_volume = 100  # Default to 100%
        if _player := self.mass.players.get_player(daemon.player_id):
            if _player.volume_level is not None:
                player_volume = _player.volume_level
        # Map 0-100 to -30.0...0.0
        airplay_volume = (player_volume / 100.0) * 30.0 - 30.0
        config_content = config_content.replace("{DEFAULT_VOLUME}", f"{airplay_volume:.1f}")

        # Write config file
        def _write_config() -> None:
            with open(daemon.config_file, "w", encoding="utf-8") as f:
                f.write(config_content)

        await asyncio.to_thread(_write_config)

    async def _publish_mdns_service(
        self, daemon: _ReceiverDaemon, proc: AsyncProcess | None, service: MdnsService
    ) -> None:
        """
        Publish, or refresh, the mDNS service a receiver's shairport-sync handed over.

        :param daemon: The receiver the service belongs to.
        :param proc: The shairport-sync process that handed the service over.
        :param service: The service to publish.
        """
        async with daemon.mdns_lock:
            # the process may have exited, and withdrawn its service, in the meantime
            if proc is None or proc is not daemon.shairport_proc or proc.closed:
                return
            service_type = f"{service.service_type}.local."
            # the instance name is a single DNS label of at most 63 bytes, and zeroconf
            # splits names at every dot: a one dot leader looks the same as a dot
            name = service.name.replace(".", "\u2024").encode()[:63].decode("utf-8", "ignore")
            # senders only learn the address Music Assistant is reached on, not those of
            # unrelated interfaces (e.g. Docker or VPN), unless no interface of this host
            # holds that address (e.g. a port-forwarded publish IP)
            ip = await self.mass.streams.get_source_ip() or self.mass.streams.publish_ip
            if interface_name_for_ip(ip):
                ips: tuple[str, ...] = (ip,)
            else:
                ips = await get_publish_ip_candidates(include_ipv6=True)
            aiozc = self.mass.discovery.aiozc
            try:
                info = AsyncServiceInfo(
                    service_type,
                    f"{name}.{service_type}",
                    port=service.port,
                    properties=service.txt,
                    addresses=[await get_ip_pton(address) for address in ips],
                    # a host name of our own: other mDNS responders on this host also
                    # answer for its host name, with addresses senders may not reach
                    server=f"ma-airplay-receiver-{self.mass.server_id[:8]}.local.",
                )
                if daemon.mdns_info is None:
                    # no probing: the name derives from this host's MAC address, and a
                    # restart would otherwise trip over the previous process's goodbye
                    announcing = await aiozc.async_register_service(
                        info, cooperating_responders=True
                    )
                else:
                    announcing = await aiozc.async_update_service(info)
                daemon.mdns_info = info
                # hold the lock until the announcements are out: one sent after a later
                # goodbye would bring the withdrawn service back in the senders' caches
                await announcing
            except (ZeroconfError, OSError) as err:
                self.logger.warning(
                    "Unable to announce AirPlay receiver '%s' on the network: %s",
                    daemon.airplay_name,
                    err,
                )

    async def _unpublish_mdns_service(self, daemon: _ReceiverDaemon) -> None:
        """Withdraw the mDNS service published for a receiver, if any."""
        async with daemon.mdns_lock:
            if daemon.mdns_info is None:
                return
            info, daemon.mdns_info = daemon.mdns_info, None
            try:
                # wait for the goodbyes, so a receiver started right after (e.g. on a reload)
                # announces its service after them
                await (await self.mass.discovery.aiozc.async_unregister_service(info))
            except (ZeroconfError, OSError) as err:
                # e.g. zeroconf already closed at shutdown
                self.logger.debug("Unable to withdraw mDNS service %s: %s", info.name, err)

    async def _write_silence_to_unblock_stream(self, daemon: _ReceiverDaemon) -> None:
        """
        Write silence to a receiver's audio pipe to unblock ffmpeg.

        When shairport-sync stops writing but ffmpeg is still reading,
        writing silence will cause ffmpeg to output a chunk, which lets the
        outer consumer make forward progress so the queue's cmd_stop can
        close the stream cleanly.

        We write enough silence to ensure ffmpeg outputs at least one chunk.
        PCM_S16LE format: 2 bytes per sample, 2 channels, 44100 Hz
        Writing 1 second of silence = 44100 * 2 * 2 = 176400 bytes
        """
        self.logger.debug("Writing silence to audio pipe to unblock stream")
        silence = b"\x00" * 176400  # 1 second of silence in PCM_S16LE stereo 44.1kHz
        # the consumer reopens the pipe shortly after shairport-sync drops it, so the
        # nudge waits for it to come back instead of landing in that gap
        if not await daemon.audio_pipe.wait_for_reader(AUDIO_PIPE_READER_TIMEOUT):
            self.logger.debug("No reader on the audio pipe, skipping the silence write")
            return
        await daemon.audio_pipe.write(silence)

    def _clear_active_player(self, daemon: _ReceiverDaemon) -> None:
        """
        Clear a receiver's active player.

        Called when playback ends to reset the receiver's session state.
        """
        prev_player_id = daemon.active_player_id
        source_session = (
            self.mass.players.get_audio_source_session(prev_player_id) if prev_player_id else None
        )
        daemon.active_player_id = None
        daemon.in_use_by_player = None
        daemon.active_session_id = None

        if prev_player_id:
            self.logger.debug("Playback ended on player %s, clearing active player", prev_player_id)
            # the player is not playing us any more, so it should stop saying it is
            self.mass.create_task(
                self.mass.players.deselect_source(
                    prev_player_id,
                    stop_playback=False,
                    provider_instance_id=self.instance_id,
                    source_id=daemon.player_id,
                    playback_session_id=(
                        source_session.playback_session_id if source_session else None
                    ),
                )
            )

    def _on_metadata_update(self, daemon: _ReceiverDaemon, metadata: dict[str, Any]) -> None:
        """
        Handle metadata updates from a receiver's shairport-sync daemon.

        :param daemon: The receiver daemon the update originates from.
        :param metadata: Dictionary containing metadata updates.
        """
        self.logger.log(VERBOSE_LOG_LEVEL, "Received metadata update: %s", metadata)

        # the metadata reader outlives the cancelled tasks for a moment during
        # teardown; a stopped daemon must not schedule new work from late events
        if daemon.stop_called:
            return

        if "mdns_service" in metadata:
            self.mass.create_task(
                self._publish_mdns_service(daemon, daemon.shairport_proc, metadata["mdns_service"])
            )
            return

        # Handle play state changes (shairport-sync's active state begin/end)
        if "play_state" in metadata:
            self._handle_play_state_change(daemon, metadata["play_state"])
            return

        # Handle metadata start (new track starting)
        if "metadata_start" in metadata:
            return

        # Handle volume changes from AirPlay client
        if "volume" in metadata and daemon.in_use_by_player:
            self._handle_volume_change(daemon, metadata["volume"])

        # Update source metadata fields
        self._update_source_metadata(daemon, metadata)

        # Handle cover art updates
        self._update_cover_art(daemon, metadata)

        # Push the metadata update through to the active queue item's streamdetails
        if daemon.in_use_by_player:
            self.mass.players.update_source_metadata(
                daemon.in_use_by_player,
                daemon.player_id,
                self.instance_id,
                daemon.stream_metadata,
            )

    def _handle_play_state_change(self, daemon: _ReceiverDaemon, play_state: str) -> None:
        """
        Handle the start, pause, resume or end of an AirPlay client's playback.

        :param daemon: The receiver daemon the state change originates from.
        :param play_state: The new play state ("playing", "flushed", "resumed" or "stopped").
        """
        if play_state == "flushed":
            if (player_id := daemon.in_use_by_player) and daemon.pending_pause_task is None:
                daemon.pending_pause_task = self.mass.create_task(
                    self._pause_after_flush(daemon, player_id)
                )
            return
        if play_state == "resumed":
            self._cancel_pending_pause(daemon)
            if not (player_id := daemon.paused_player_id):
                return
            daemon.paused_player_id = None
            if daemon.pending_start_task and not daemon.pending_start_task.done():
                return
            self.logger.info("AirPlay playback resumed, playing on player %s", player_id)
            daemon.active_player_id = player_id
            daemon.pending_start_task = self.mass.create_task(
                self._start_playback(daemon, player_id)
            )
            return
        if play_state == "playing":
            # Reset volume event flag for new playback session
            daemon.first_volume_event_received = False
            self._set_client_active(daemon, True)
            # a new client session replaces a start that still waits for its player
            self._stop_waiting(daemon)
            # Initiate playback via the standard play_media flow on the target player
            if not daemon.in_use_by_player:
                if daemon.pending_start_task and not daemon.pending_start_task.done():
                    # a start is already in flight; a second one would double play_media
                    # and leave the first task untracked for teardown cancellation
                    return
                target_player_id = self.mass.players.resolve_player_id(daemon.player_id)
                self.logger.info("Starting AirPlay playback on player %s", target_player_id)
                daemon.active_player_id = target_player_id
                daemon.pending_start_task = self.mass.create_task(
                    self._start_playback(daemon, target_player_id)
                )
        elif play_state == "stopped":
            self.logger.info("AirPlay playback stopped")
            self._cancel_pending_pause(daemon)
            daemon.paused_player_id = None
            # Reset volume event flag for next session
            daemon.first_volume_event_received = False
            # Get the current player before clearing
            current_player_id = daemon.in_use_by_player
            # Clear active player state (also clears in_use_by_player)
            self._clear_active_player(daemon)
            self._stop_waiting(daemon)
            self._set_client_active(daemon, False)
            # Write silence to the pipe so ffmpeg can produce a chunk and notice the
            # stream has stopped; the stop command below closes the generator path.
            self.mass.create_task(self._write_silence_to_unblock_stream(daemon))
            # Track the stop so a new session cannot overtake it.
            if current_player_id:
                daemon.pending_stop_task = self.mass.create_task(
                    self.mass.players.cmd_stop(current_player_id)
                )

    async def _pause_after_flush(self, daemon: _ReceiverDaemon, player_id: str) -> None:
        """
        Show the player as paused once a flush of the AirPlay client is not followed by a resume.

        :param daemon: The receiver whose AirPlay client flushed.
        :param player_id: The player streaming the receiver's source.
        """
        await asyncio.sleep(FLUSH_PAUSE_DELAY)
        daemon.pending_pause_task = None
        if daemon.in_use_by_player != player_id:
            return
        daemon.paused_player_id = player_id
        try:
            await self.mass.players.pause_audio_source(
                player_id, daemon.player_id, self.instance_id
            )
        except Exception as err:  # noqa: BLE001 - player code, a resume still plays on
            self.logger.debug("Failed to pause player %s: %s", player_id, err)
            return
        # shairport-sync writes nothing while paused, so the stream of the silenced player
        # would wait for audio until it stalls
        await self._write_silence_to_unblock_stream(daemon)

    def _cancel_pending_pause(self, daemon: _ReceiverDaemon) -> None:
        """Cancel the pause a flush of the AirPlay client scheduled, if it is still waiting."""
        if daemon.pending_pause_task is not None:
            daemon.pending_pause_task.cancel()
            daemon.pending_pause_task = None

    async def _start_playback(self, daemon: _ReceiverDaemon, target_player_id: str) -> None:
        """Start playback after any pending stop completes, or once the player can play."""
        pending_stop_task = daemon.pending_stop_task
        if pending_stop_task is not None:
            # Await (even if already done) so a failed stop's exception is retrieved,
            # and continue regardless of how it failed: a stop that can't complete must
            # not keep the next session from starting. The reference is cleared only
            # after the await so concurrent starts (rapid "playing" events before the
            # stream is claimed) all await the same stop instead of racing past it.
            try:
                await pending_stop_task
            except Exception as err:  # noqa: BLE001 - a failed stop must not block the next session
                self.logger.warning("Failed to stop previous AirPlay playback: %s", err)
            # Don't clear a newer stop that replaced ours while we were awaiting.
            if daemon.pending_stop_task is pending_stop_task:
                daemon.pending_stop_task = None
        try:
            await self.mass.player_queues.play_media(target_player_id, str(daemon.audio_source.uri))
        except PlayerUnavailableError, PlayerCommandFailed:
            if can_start_playback(self.mass, target_player_id):
                raise
            # the AirPlay client starts no new session while it keeps streaming, so the
            # start waits for the player (or a member of a group player) to come back,
            # unless another player took the source meanwhile
            if daemon.client_active and not daemon.in_use_by_player:
                self.logger.info(
                    "Player %s cannot play right now, AirPlay playback starts once it can",
                    target_player_id,
                )
                self._wait_for_player(daemon, target_player_id)

    def _wait_for_player(self, daemon: _ReceiverDaemon, player_id: str) -> None:
        """
        Start a receiver's playback on a player as soon as that player can play.

        :param daemon: The receiver whose playback could not start.
        :param player_id: The player to start the playback on.
        """
        self._stop_waiting(daemon)
        daemon.waiting_player_id = player_id
        # any player can be a member that makes a group player playable
        daemon.unsubscribe_waiting = self.mass.subscribe(
            partial(self._on_waiting_player_event, daemon),
            event_filter=(EventType.PLAYER_ADDED, EventType.PLAYER_UPDATED),
        )

    def _stop_waiting(self, daemon: _ReceiverDaemon) -> None:
        """Stop waiting for a player to start a receiver's playback on."""
        daemon.waiting_player_id = None
        if daemon.unsubscribe_waiting is not None:
            daemon.unsubscribe_waiting()
            daemon.unsubscribe_waiting = None

    def _on_waiting_player_event(self, daemon: _ReceiverDaemon, _event: MassEvent) -> None:
        """Start the playback that waits for its player, once that player can play."""
        # an event dispatched before the wait ended can still arrive afterwards
        player_id = daemon.waiting_player_id
        if player_id is None or not can_start_playback(self.mass, player_id):
            return
        self._stop_waiting(daemon)
        self.logger.info("Player %s can play again, starting AirPlay playback", player_id)
        daemon.pending_start_task = self.mass.create_task(self._start_playback(daemon, player_id))

    def _set_client_active(self, daemon: _ReceiverDaemon, active: bool) -> None:
        """
        Record whether an AirPlay client streams to a receiver.

        While one does, the receiver's source can be selected on a player.

        :param daemon: The receiver the AirPlay client streams to.
        :param active: Whether an AirPlay client streams to the receiver.
        """
        if daemon.client_active == active:
            return
        daemon.client_active = active
        daemon.audio_source = replace(daemon.audio_source, can_initiate=active)
        # the standing source entry feeds the connected player's cached source list
        self.mass.players.trigger_player_update(daemon.player_id)

    def _handle_volume_change(self, daemon: _ReceiverDaemon, volume: int) -> None:
        """
        Handle volume changes from AirPlay client (iOS/macOS device).

        ignore_volume_control = "yes" means shairport-sync doesn't do software volume control,
        but we still receive volume level changes from the client to apply to the player.

        :param daemon: The receiver daemon the volume change originates from.
        :param volume: The new volume level (0-100).
        """
        # Skip the first volume event as it's the initial sync from default_airplay_volume
        # We don't want to override the player's current volume on startup
        if not daemon.first_volume_event_received:
            daemon.first_volume_event_received = True
            self.logger.debug(
                "Received initial AirPlay volume (%s%%), skipping to preserve player volume",
                volume,
            )
            return

        # Type check: ensure we have a valid player ID; queue_id == player_id by convention
        player_id = daemon.in_use_by_player
        if not player_id:
            return

        self.logger.debug(
            "AirPlay client volume changed to %s%%, applying to player %s",
            volume,
            player_id,
        )
        try:
            self.mass.create_task(self.mass.players.cmd_volume_set(player_id, volume))
        except UnsupportedFeaturedException:
            self.logger.debug("Player %s does not support volume control", player_id)

    def _update_source_metadata(self, daemon: _ReceiverDaemon, metadata: dict[str, Any]) -> None:
        """
        Update a receiver's source metadata fields from AirPlay metadata.

        :param daemon: The receiver daemon the update originates from.
        :param metadata: Dictionary containing metadata updates.
        """
        # Update individual metadata fields
        if "title" in metadata:
            daemon.stream_metadata.title = metadata["title"]

        if "artist" in metadata:
            daemon.stream_metadata.artist = metadata["artist"]

        if "album" in metadata:
            daemon.stream_metadata.album = metadata["album"]

        if "duration" in metadata:
            daemon.stream_metadata.duration = metadata["duration"]

        if "elapsed_time" in metadata:
            daemon.stream_metadata.elapsed_time = metadata["elapsed_time"]
            # Always set elapsed_time_last_updated to current time when we receive elapsed_time
            daemon.stream_metadata.elapsed_time_last_updated = time.time()

    def _update_cover_art(self, daemon: _ReceiverDaemon, metadata: dict[str, Any]) -> None:
        """
        Update a receiver's cover art image URL from AirPlay metadata.

        :param daemon: The receiver daemon the update originates from.
        :param metadata: Dictionary containing metadata updates.
        """
        if (
            "cover_art_timestamp" in metadata
            and daemon.metadata_reader
            and daemon.metadata_reader.cover_art_bytes
        ):
            # Use a content hash in the path so each unique image gets its own
            # thumbnail cache entry (the thumbnail cache is keyed on provider+path).
            img_hash = hashlib.md5(
                daemon.metadata_reader.cover_art_bytes, usedforsecurity=False
            ).hexdigest()[:8]
            image = MediaItemImage(
                type=ImageType.THUMB,
                path=daemon.cover_art_path(img_hash),
                provider=self.instance_id,
                remotely_accessible=False,
            )
            daemon.stream_metadata.image_url = self.mass.metadata.get_image_url(image)
        elif daemon.metadata_reader and daemon.metadata_reader.cover_art_bytes:
            if not daemon.stream_metadata.image_url:
                img_hash = hashlib.md5(
                    daemon.metadata_reader.cover_art_bytes, usedforsecurity=False
                ).hexdigest()[:8]
                image = MediaItemImage(
                    type=ImageType.THUMB,
                    path=daemon.cover_art_path(img_hash),
                    provider=self.instance_id,
                    remotely_accessible=False,
                )
                daemon.stream_metadata.image_url = self.mass.metadata.get_image_url(image)
