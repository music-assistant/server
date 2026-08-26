"""AirPlay Receiver plugin provider implementation."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from contextlib import suppress
from dataclasses import dataclass, field
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
    UnsupportedFeaturedException,
)
from music_assistant_models.media_items import (
    AudioFormat,
    AudioSource,
    MediaItemImage,
    ProviderMapping,
)
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.config_entries import (
    CONF_CONNECTED_PLAYERS,
    CONF_PUBLISH_NAME_TEMPLATE,
    create_connected_players_entry,
    create_publish_name_template_entry,
    resolve_publish_name,
)
from music_assistant.helpers.named_pipe import AsyncNamedPipeWriter
from music_assistant.helpers.process import AsyncProcess, check_output
from music_assistant.helpers.util import interface_name_for_ip
from music_assistant.models.plugin import PluginProvider, SourceControlValue
from music_assistant.providers.airplay_receiver.helpers import get_shairport_sync_binary
from music_assistant.providers.airplay_receiver.metadata import MetadataReader

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
    ports: dict[str, int] = {}
    claimed: set[int] = set()
    # iterate sorted so probing resolves collisions the same way for any input order
    for player_id in sorted(player_ids):
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

    # the connected player this receiver plays on; doubles as the AudioSource item_id
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
    first_volume_event_received: bool = False  # Track if we've received the first volume event

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
            self._unsubscribe = self.mass.subscribe(
                self._on_player_event,
                event_filter=(
                    EventType.PLAYER_ADDED,
                    EventType.PLAYER_REMOVED,
                    EventType.PLAYER_CONFIG_UPDATED,
                    EventType.PLAYER_UPDATED,
                ),
                id_filter=self._assigned_player_ids,
            )
        # players register after plugins load, so on a cold boot this typically starts
        # nothing yet: the PLAYER_ADDED events drive the actual daemon startups
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

    async def get_audio_sources(self) -> list[AudioSource]:
        """Return the AudioSources this plugin currently exposes."""
        return [daemon.audio_source for daemon in self._daemons.values()]

    def get_player_audio_sources(self, player_id: str) -> list[AudioSource]:
        """Return the AudioSource bound to the given connected player, if any."""
        daemon = self._daemons.get(player_id)
        return [daemon.audio_source] if daemon else []

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Return StreamDetails for streaming the AirPlay audio to a queue.

        Side-effect-free: ownership is claimed in on_source_selected (which the
        streams controller fires before this method on the actual stream
        request). Keeping this idempotent means preload paths like
        player_queues._load_item can fetch streamdetails without claiming the
        source and blocking a subsequent cross-queue handoff.

        Raises AudioError when no AirPlay client is currently connected.
        """
        daemon = self._daemons.get(item_id)
        if daemon is None:
            raise MediaNotFoundError(f"Unknown AudioSource: {item_id}")
        if not daemon.active_player_id:
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
        """Handle callback when this AudioSource is selected/started on a player."""
        daemon = self._daemons.get(source_id)
        if daemon is None or not player_id:
            return

        # Cache the owner_player_id (user-facing MA player) rather than the protocol-
        # level player_id; protocol bridges (e.g. Sendspin's spb_…) can tear
        # down between streams and their ID is then invalid for play_media.
        active_player_id = owner_player_id

        # If there's already an active player and it's different, kick it out.
        # The lock claim a few lines below replaces the previous queue's claim;
        # the prior stream's on_source_unselected may fire later, but its
        # session-id guard keeps it from clobbering the new claim.
        if daemon.active_player_id and daemon.active_player_id != active_player_id:
            prev_player_id = daemon.active_player_id
            self.logger.info(
                "Source selected on player %s, stopping playback on %s",
                active_player_id,
                prev_player_id,
            )
            try:
                await self.mass.players.cmd_stop(prev_player_id)
            except Exception as err:
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
        if event.event == EventType.PLAYER_REMOVED:
            # permanent removal: stop the daemon; a temporarily unavailable player
            # (which fires only PLAYER_UPDATED) keeps its running daemon so the
            # advertised device identity stays stable across the outage
            async with self._reconcile_lock:
                if event.object_id and (daemon := self._daemons.pop(event.object_id, None)):
                    await self._stop_receiver(daemon)
            return
        await self._reconcile()

    async def _reconcile(self) -> None:
        """
        Align the running receiver daemons with the connected players.

        Starts a daemon for every connected player that is registered, and restarts
        a daemon whose advertised name drifted from the player's current name.
        """
        async with self._reconcile_lock:
            if self._unload_called:
                return
            template = self.get_config_value(CONF_PUBLISH_NAME_TEMPLATE)
            for player_id in self._assigned_player_ids:
                player = self.mass.players.get_player(player_id)
                if player is None:
                    # not (yet) registered: never start a daemon for it; an already
                    # running one is deliberately kept (see _on_player_event)
                    continue
                airplay_name = resolve_publish_name(template, player.display_name)
                daemon = self._daemons.get(player_id)
                if daemon is not None and daemon.airplay_name == airplay_name:
                    continue
                if daemon is not None:
                    # the advertised name follows the player name: restart on rename
                    del self._daemons[player_id]
                    await self._stop_receiver(daemon)
                self._start_receiver(player, airplay_name)

    def _start_receiver(self, player: Player, airplay_name: str) -> None:
        """
        Create the receiver state for a connected player and start its daemon.

        :param player: The (registered) player this receiver plays on.
        :param airplay_name: The name to advertise in the AirPlay device list.
        """
        player_id = player.player_id
        safe_player_id = re.sub(r"[^A-Za-z0-9_.-]", "_", player_id)
        receiver_key = f"{self.instance_id}_{safe_player_id}"
        audio_source = AudioSource(
            # the player id is stable across renames, so the source uri survives them
            item_id=player_id,
            provider=self.instance_id,
            name=f"{self.name} ({player.display_name})",
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
            # passive: only flows when an external AirPlay client is connected
            can_initiate=False,
        )
        daemon = _ReceiverDaemon(
            player_id=player_id,
            safe_player_id=safe_player_id,
            airplay_name=airplay_name,
            port=self._ports[player_id],
            audio_pipe=AsyncNamedPipeWriter(f"/tmp/ma_airplay_audio_{receiver_key}"),  # noqa: S108
            metadata_pipe=AsyncNamedPipeWriter(
                f"/tmp/ma_airplay_metadata_{receiver_key}"  # noqa: S108
            ),
            config_file=f"/tmp/ma_shairport_sync_{receiver_key}.conf",  # noqa: S108
            audio_source=audio_source,
            stream_metadata=StreamMetadata(title=f"AirPlay | {airplay_name}"),
        )
        self._daemons[player_id] = daemon
        self._setup_shairport_daemon(daemon)

    async def _stop_receiver(self, daemon: _ReceiverDaemon) -> None:
        """Stop a receiver's shairport-sync daemon and release its resources."""
        daemon.stop_called = True

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

        # Reset the shairport process reference
        daemon.shairport_proc = None
        daemon.started.clear()

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

            # Open the FIFO before shairport-sync can invoke session-control hooks.
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

            # Clean up pipes and config
            await self._cleanup_pipes_and_config(daemon)

            if daemon.stop_called:
                # deliberately stopped (unload, rename restart or player removal)
                pass
            elif not daemon.started.is_set():
                self.unload_with_error("Unable to initialize shairport-sync daemon.")
            # Auto restart if not stopped manually
            elif daemon.runner_error_count >= 5:
                self.unload_with_error("shairport-sync daemon failed to start multiple times.")
            else:
                daemon.runner_error_count += 1
                self.mass.call_later(2, self._setup_shairport_daemon, daemon)

    def _process_shairport_log_line(self, daemon: _ReceiverDaemon, line: str) -> None:
        """
        Process a log line from shairport-sync stderr.

        :param daemon: The receiver daemon the log line originates from.
        :param line: The log line to process.
        """
        # Check for fatal errors (log them, but process will exit on its own)
        if "fatal error:" in line.lower() or "unknown option" in line.lower():
            self.logger.error("Fatal error from shairport-sync: %s", line)
            return
        # Log connection messages at INFO level, everything else at DEBUG
        if "connection from" in line:
            self.logger.info("AirPlay client connected: %s", line)
        else:
            # Note: Play begin/stop events are now handled via sessioncontrol hooks
            # through the metadata pipe, so we don't need to parse stderr logs
            self.logger.debug(line)
        if not daemon.started.is_set():
            daemon.started.set()

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
        config_content = config_content.replace(
            "{INTERFACE_LINE}", await self._get_mdns_interface_line()
        )

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

    async def _get_mdns_interface_line(self) -> str:
        """
        Build the shairport-sync ``general.interface`` directive, or an empty string.

        When the stream server is bound to a specific interface (not 0.0.0.0), pin
        the AirPlay mDNS advertisement to that same interface so the receiver is
        announced on the intended network instead of an unrelated one (e.g. a
        Docker bridge). Returns an empty string to advertise on all interfaces.
        """
        bind_ip = await self.mass.streams.get_source_ip()
        if not bind_ip:
            return ""
        iface_name = interface_name_for_ip(bind_ip)
        if not iface_name:
            self.logger.debug(
                "No interface found for stream bind IP %s; advertising on all interfaces",
                bind_ip,
            )
            return ""
        return f'\tinterface = "{iface_name}";\n'

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

        # Handle play state changes from sessioncontrol hooks
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
        Handle play state changes from sessioncontrol hooks.

        :param daemon: The receiver daemon the state change originates from.
        :param play_state: The new play state ("playing" or "stopped").
        """
        if play_state == "playing":
            # Reset volume event flag for new playback session
            daemon.first_volume_event_received = False
            # Initiate playback via the standard play_media flow on the target player
            if not daemon.in_use_by_player:
                # an explicitly selected player wins, else the receiver's own player
                target_player_id = daemon.active_player_id or daemon.player_id
                self.logger.info("Starting AirPlay playback on player %s", target_player_id)
                daemon.active_player_id = target_player_id
                self.mass.create_task(self._start_playback(daemon, target_player_id))
        elif play_state == "stopped":
            self.logger.info("AirPlay playback stopped")
            # Reset volume event flag for next session
            daemon.first_volume_event_received = False
            # Get the current player before clearing
            current_player_id = daemon.in_use_by_player
            # Clear active player state (also clears in_use_by_player)
            self._clear_active_player(daemon)
            # Write silence to the pipe so ffmpeg can produce a chunk and notice the
            # stream has stopped; the stop command below closes the generator path.
            self.mass.create_task(self._write_silence_to_unblock_stream(daemon))
            # Track the stop so a new session cannot overtake it.
            if current_player_id:
                daemon.pending_stop_task = self.mass.create_task(
                    self.mass.players.cmd_stop(current_player_id)
                )

    async def _start_playback(self, daemon: _ReceiverDaemon, target_player_id: str) -> None:
        """Start playback after any pending stop completes."""
        pending_stop_task = daemon.pending_stop_task
        if pending_stop_task is not None:
            # Await (even if already done) so a failed stop's exception is retrieved,
            # and continue regardless of how it failed: a stop that can't complete must
            # not keep the next session from starting. The reference is cleared only
            # after the await so concurrent starts (rapid "playing" events before the
            # stream is claimed) all await the same stop instead of racing past it.
            try:
                await pending_stop_task
            except Exception as err:
                self.logger.warning("Failed to stop previous AirPlay playback: %s", err)
            # Don't clear a newer stop that replaced ours while we were awaiting.
            if daemon.pending_stop_task is pending_stop_task:
                daemon.pending_stop_task = None
        await self.mass.player_queues.play_media(target_player_id, str(daemon.audio_source.uri))

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
