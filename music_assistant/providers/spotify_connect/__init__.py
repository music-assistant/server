"""
Spotify Connect plugin for Music Assistant.

We tie a single player to a single Spotify Connect daemon (go-librespot).
The provider has multi instance support, so multiple players can be linked to
multiple Spotify Connect daemons.

go-librespot is driven entirely over its local HTTP+WebSocket API: the WebSocket
``/events`` stream feeds player/session/metadata/volume state into Music Assistant,
and transport + volume commands are issued via the REST endpoints. Playback control
works without a configured Spotify *music* provider or the Spotify Web API.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.enums import (
    ContentType,
    MediaType,
    PlaybackState,
    ProviderFeature,
    SourceControl,
    StreamType,
)
from music_assistant_models.errors import AudioError, MediaNotFoundError
from music_assistant_models.media_items import AudioFormat, AudioSource, ProviderMapping
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.constants import CONF_ENTRY_WARN_PREVIEW
from music_assistant.helpers.process import AsyncProcess
from music_assistant.helpers.util import interface_name_for_ip, select_free_port
from music_assistant.models.plugin import PluginProvider

from .client import GoLibrespotClient
from .helpers import generate_device_id, get_go_librespot_binary

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_MASS_PLAYER_ID = "mass_player_id"
CONF_PUBLISH_NAME = "publish_name"
DEFAULT_PUBLISH_NAME = "Music Assistant"

# Special value for auto player selection
PLAYER_ID_AUTO = "__auto__"

SUPPORTED_FEATURES = {ProviderFeature.AUDIO_SOURCE}

# stable id for the single AudioSource this provider exposes;
# combined with the provider instance_id this forms the persistent uri
AUDIO_SOURCE_ID = "main"

# go-librespot volume scale; we pin volume_steps to this so the daemon's 0..max
# volume maps 1:1 to a 0-100 percentage.
VOLUME_STEPS = 100

# Read size for pulling PCM off the daemon's stdout.
STREAM_READ_CHUNK = 16384

# When playback is paused the daemon stops writing PCM. If no PCM arrives for
# this long while we're not in a 'playing' state, end the stream (clean EOF) so
# the player leaves the playing state; the next 'playing' event re-streams.
PAUSE_EOF_TIMEOUT_S = 0.5

# Port range the go-librespot API server binds to (loopback only, one per instance).
API_PORT_RANGE_START = 38800
API_PORT_RANGE_END = 38900

# Seconds to wait for the daemon to report 'playing' after a resume request.
PLAYBACK_START_TIMEOUT_S = 3.0

# Debounce before acting on an externally-triggered 'playing' event (see
# _deferred_play_media_fire for why).
PLAY_MEDIA_DEBOUNCE_S = 0.5

# Ignore Spotify volume events for this long after a session becomes active, so
# the player's own volume wins over librespot's initial value on (re)connect.
INITIAL_VOLUME_GRACE_S = 3.0

# User-facing message for the "not the active Spotify device" failure.
# {0} is the Spotify Connect device's published name (see _not_active_error).
NOT_ACTIVE_DEVICE_MESSAGE = (
    "'{0}' is not the active Spotify playback device. "
    "Open the Spotify app, select it as the playback device, and try again."
)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return SpotifyConnectProvider(mass, manifest, config)


class SpotifyConnectProvider(PluginProvider):
    """Implementation of a Spotify Connect Plugin (backed by go-librespot)."""

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize MusicProvider."""
        super().__init__(mass, manifest, config, SUPPORTED_FEATURES)
        # Configured default player (PLAYER_ID_AUTO or a specific player id)
        self._default_player_id: str = (
            cast("str", self.get_setup_value(CONF_MASS_PLAYER_ID)) or PLAYER_ID_AUTO
        )
        self._publish_name = (
            cast("str", self.get_setup_value(CONF_PUBLISH_NAME)) or DEFAULT_PUBLISH_NAME
        )
        # Currently active player (the one currently playing or selected)
        self._active_player_id: str | None = None
        self.cache_dir = os.path.join(self.mass.cache_path, self.instance_id)
        self._binary: str | None = None
        self._api_port: int = 0
        self._client: GoLibrespotClient | None = None
        self._stop_called: bool = False
        self._daemon_task: asyncio.Task[None] | None = None
        self._events_task: asyncio.Task[None] | None = None
        self._proc: AsyncProcess | None = None
        self._restart_error_count = 0
        self.logger.debug(
            "Init plugin with name '%s' for player '%s' with instance id '%s'",
            self.name,
            self._default_player_id,
            self.instance_id,
        )
        # _audio_format is the original Spotify source codec (Ogg Vorbis 320 kbps),
        # advertised to clients for display. _decoded_audio_format is the raw PCM
        # go-librespot actually writes to its stdout after decoding — what
        # get_audio_stream yields and what the streams controller hands ffmpeg as
        # the input format. We always emit the source's own format here; MA is
        # responsible for converting it to whatever each player needs.
        self._audio_format = AudioFormat(
            content_type=ContentType.OGG,
            codec_type=ContentType.VORBIS,
            sample_rate=44100,
            bit_depth=16,
            channels=2,
            bit_rate=320,
        )
        self._decoded_audio_format = AudioFormat(
            content_type=ContentType.PCM_S16LE,
            codec_type=ContentType.PCM_S16LE,
            sample_rate=44100,
            bit_depth=16,
            channels=2,
        )
        self._stream_metadata = StreamMetadata(title=f"Spotify Connect | {self._publish_name}")
        self._audio_source = self._build_audio_source()
        # _in_use_by_queue is the queue currently streaming us. Claimed in
        # on_source_selected (NOT in get_stream_details — that path also runs
        # from queue preload, where claiming would block a later cross-queue
        # handoff). Released in on_source_unselected when the session id
        # matches, or in _clear_active_player on the daemon's 'inactive' event.
        self._in_use_by_queue: str | None = None
        # _active_session_id is the controller-provided token for the current
        # stream request — used to reject stale on_source_unselected callbacks
        # after a same-queue reconnect supersedes the previous request.
        self._active_session_id: str | None = None
        # tracks the daemon's play/pause state from its 'playing' / 'paused' /
        # 'inactive' events; gates the resume kick in on_source_selected (skip if
        # already playing) and the play_media trigger in the event handler.
        self._playing: bool = False
        # True while MA is the active Spotify Connect device (set on 'active',
        # cleared on 'inactive'); gates get_stream_details and transport commands.
        self._spotify_session_active: bool = False
        # holds the single in-flight deferred play_media task scheduled from a
        # 'playing' event; cancelled when a 'paused' / 'stopped' / 'active' event
        # arrives during the debounce so we don't act on stale state from a dying
        # session.
        self._pending_play_media_task: asyncio.Task[None] | None = None
        self._last_session_active_time: float = 0
        self._last_volume_sent: int | None = None
        # Last context/track URIs seen on the event stream. Used to take playback
        # back (make ourselves the active Spotify device) when the user switched
        # the active device away in the Spotify app and then presses play in MA.
        self._last_context_uri: str | None = None
        self._last_track_uri: str | None = None

    @property
    def instance_name_postfix(self) -> str | None:
        """Return the advertised device name as the multi-instance postfix."""
        return self._publish_name if self._publish_name != DEFAULT_PUBLISH_NAME else None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return runtime options for this provider."""
        return (CONF_ENTRY_WARN_PREVIEW,)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._binary = get_go_librespot_binary()
        self._api_port = await select_free_port(
            API_PORT_RANGE_START, API_PORT_RANGE_END, host="127.0.0.1"
        )
        self._client = GoLibrespotClient(
            self.mass, f"http://127.0.0.1:{self._api_port}", self.logger
        )
        # Two self-healing supervisors: one keeps the daemon process alive, the
        # other keeps the events websocket connected (reconnecting across daemon
        # restarts). The events runner resets the daemon's restart backoff once
        # the websocket is healthy again.
        self._daemon_task = self.mass.create_task(self._daemon_runner())
        self._events_task = self.mass.create_task(self._events_runner())

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
        self._stop_called = True
        self._cancel_pending_play_media()
        for task in (self._events_task, self._daemon_task):
            if task and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    @property
    def active_player_id(self) -> str | None:
        """Return the currently active player ID for this plugin."""
        return self._active_player_id

    async def get_audio_sources(self) -> list[AudioSource]:
        """Return the AudioSources this plugin currently exposes."""
        return [self._audio_source]

    async def get_stream_details(self, source_id: str, queue_id: str) -> StreamDetails:
        """
        Return StreamDetails for streaming the Spotify Connect audio.

        Side-effect-free: ownership is claimed in on_source_selected (which the
        streams controller fires before this method on the actual stream
        request). Keeping this idempotent means preload paths can fetch
        streamdetails without claiming the source and blocking a cross-queue
        handoff.

        Raises AudioError when MA is not the active Spotify Connect device, since
        playback can only be acquired while a Spotify session is connected to us
        (entry must come from the Spotify app — see can_initiate below).
        """
        if source_id != AUDIO_SOURCE_ID:
            raise MediaNotFoundError(f"Unknown AudioSource: {source_id}")
        # Only refuse when we can neither resume nor take playback back. If a last
        # context is known we let the stream proceed; on_source_selected then takes
        # playback back (makes us the active device) before audio is pulled.
        if not self._playing and not self._spotify_session_active and not self._last_context_uri:
            raise self._not_active_error()
        # CUSTOM: the core pulls PCM from get_audio_stream. Reading the daemon's
        # stdout means a consumer is always attached, so go-librespot's non-blocking
        # pipe open succeeds, and it lets us end the stream cleanly when playback
        # pauses so the player leaves the playing state. decoded_audio_format tells
        # the core the PCM is s16le while audio_format keeps the Ogg/Vorbis source
        # codec for display; MA resamples to each player's format as needed.
        # `-fflags nobuffer` keeps ffmpeg from buffering ahead on the resample path
        # (we back-pressure the daemon to realtime in get_audio_stream).
        # expiration=0: never reuse a cached streamdetails so the active-device
        # check above re-runs on every play attempt.
        return StreamDetails(
            provider=self.instance_id,
            item_id=source_id,
            audio_format=self._audio_format,
            decoded_audio_format=self._decoded_audio_format,
            media_type=MediaType.AUDIO_SOURCE,
            stream_type=StreamType.CUSTOM,
            stream_metadata=self._stream_metadata,
            extra_input_args=["-fflags", "nobuffer"],
            expiration=0,
        )

    async def get_audio_stream(
        self,
        streamdetails: StreamDetails,
        seek_position: int = 0,
    ) -> AsyncGenerator[bytes]:
        """
        Yield raw PCM from the go-librespot daemon's stdout for the live AudioSource.

        We rate-pace the read at the source's native rate so go-librespot (whose
        pipe backend is not realtime-paced) is back-pressured to ~realtime and
        stays only a fraction of a second ahead. Without this, on the ffmpeg
        resample path (when the player's PCM format differs from ours) nothing
        throttles the daemon — it races seconds ahead and pause/skip lag badly.

        When playback pauses the daemon stops writing PCM; we then end the stream
        (clean EOF) so the consuming player leaves the playing state. The next
        ``playing`` event re-triggers playback. ``seek_position`` is ignored —
        seeking is handled upstream by Spotify, not by replaying the bytestream.
        """
        if streamdetails.item_id != AUDIO_SOURCE_ID:
            raise MediaNotFoundError(f"Unknown AudioSource: {streamdetails.item_id}")
        proc = self._proc
        if proc is None:
            raise AudioError("Spotify Connect daemon is not running")
        fmt = self._decoded_audio_format
        bytes_per_second = fmt.sample_rate * fmt.channels * (fmt.bit_depth // 8)
        loop = self.mass.loop
        start = loop.time()
        streamed = 0
        while True:
            try:
                chunk = await asyncio.wait_for(
                    proc.read(STREAM_READ_CHUNK), timeout=PAUSE_EOF_TIMEOUT_S
                )
            except TimeoutError:
                # No PCM for a while. If playback is no longer active (paused /
                # stopped / session gone) end the stream so the player goes idle;
                # a brief buffering gap while still playing just keeps waiting.
                if not self._playing:
                    return
                continue
            if not chunk:
                return  # daemon stdout closed (process exited / restarting)
            yield chunk
            # pace at native rate → back-pressure the daemon to ~realtime
            streamed += len(chunk)
            ahead = streamed / bytes_per_second - (loop.time() - start)
            if ahead > 0:
                await asyncio.sleep(ahead)

    async def on_source_selected(
        self,
        source_id: str,
        player_id: str,
        queue_id: str,
        stream_session_id: str,
    ) -> None:
        """Handle callback when this AudioSource has been selected/started on a player."""
        if source_id != AUDIO_SOURCE_ID or not player_id:
            return

        # Cache the queue_id (== user-facing MA player) rather than the
        # protocol-level player_id. Some protocol players are ephemeral bridges
        # whose ID is invalid for play_media / queue lookups once torn down.
        active_player_id = queue_id

        # If there's already a different active player, kick it out. The claim
        # below replaces the previous queue's claim; the prior stream's
        # on_source_unselected may fire later, but its session-id guard keeps it
        # from clobbering the new claim.
        if self._active_player_id and self._active_player_id != active_player_id:
            prev_player_id = self._active_player_id
            self.logger.info(
                "Source selected on player %s, stopping playback on %s",
                active_player_id,
                prev_player_id,
            )
            try:
                await self.mass.players.cmd_stop(prev_player_id)
            except Exception as err:
                self.logger.debug("Failed to stop previous player %s: %s", prev_player_id, err)

        # Claim ownership for this queue.
        self._in_use_by_queue = queue_id
        self._active_session_id = stream_session_id
        self._active_player_id = active_player_id
        self.logger.debug("Active player set to: %s", active_player_id)

        # Only persist the selected player as the new default if not in auto mode
        if self._default_player_id != PLAYER_ID_AUTO:
            self._save_last_player_id(active_player_id)

        # Externally triggered: the daemon is already playing → nothing to do.
        # Otherwise acquire playback, then confirm it actually started.
        if not self._playing:
            assert self._client is not None
            try:
                if self._spotify_session_active:
                    # Still the active Spotify device (just paused) → resume.
                    await self._client.resume()
                elif self._last_context_uri:
                    # The user moved the active device away in the Spotify app.
                    # Take playback back by (re)starting the last context on us,
                    # which makes go-librespot the active device again. The track
                    # restarts from its beginning (go-librespot has no
                    # resume-at-position play call).
                    self.logger.info("Taking Spotify playback back to Music Assistant")
                    await self._client.play(
                        self._last_context_uri, skip_to_uri=self._last_track_uri
                    )
                else:
                    raise self._not_active_error()
            except AudioError:
                raise
            except Exception as err:
                raise AudioError(f"Failed to acquire Spotify Connect: {err}") from err
            if not await self._wait_for_playing():
                raise self._not_active_error()

    async def on_source_unselected(
        self, source_id: str, queue_id: str, stream_session_id: str
    ) -> None:
        """Release the queue-scoped exclusive claim when MA tears down the stream."""
        if source_id != AUDIO_SOURCE_ID:
            return
        # Reject stale callbacks: only release if this is still the active
        # session. A queue_id check alone is not sufficient — same-queue
        # reconnects would otherwise let an old request's late callback clear
        # the live claim of the new stream.
        if self._active_session_id != stream_session_id:
            return
        self._active_session_id = None
        if self._in_use_by_queue == queue_id:
            self._in_use_by_queue = None

    async def on_source_control(
        self,
        source_id: str,
        action: SourceControl,
        value: int | None = None,
    ) -> None:
        """Proxy playback control commands to go-librespot's REST API."""
        if source_id != AUDIO_SOURCE_ID:
            return
        if not self._playing and not self._spotify_session_active:
            raise self._not_active_error()
        assert self._client is not None
        try:
            if action == SourceControl.PLAY:
                await self._client.resume()
            elif action == SourceControl.PAUSE:
                await self._client.pause()
            elif action == SourceControl.NEXT:
                await self._client.next()
            elif action == SourceControl.PREVIOUS:
                await self._client.prev()
            elif action == SourceControl.SEEK and value is not None:
                await self._client.seek(value * 1000)
        except Exception as err:
            self.logger.warning("Failed to send %s command to go-librespot: %s", action, err)
            raise

    async def on_volume_change(self, source_id: str, volume: int) -> None:
        """Sync the Spotify app's volume slider with the player's new volume."""
        if source_id != AUDIO_SOURCE_ID:
            return
        if not self._playing and not self._spotify_session_active:
            raise self._not_active_error()
        # Prevent ping-pong: only push if the value actually changed from what we
        # last sent to / received from the daemon.
        if self._last_volume_sent == volume:
            return
        assert self._client is not None
        # Record BEFORE the call: go-librespot echoes a 'volume' event back, and
        # that echo can arrive over the WS while we're still awaiting set_volume.
        # Recording up front lets _handle_volume_event dedupe it instead of
        # bouncing it back as a player volume change.
        previous_volume = self._last_volume_sent
        self._last_volume_sent = volume
        try:
            await self._client.set_volume(round(volume / 100 * VOLUME_STEPS))
        except Exception as err:
            # restore on failure so a retry of this value isn't wrongly deduped
            self._last_volume_sent = previous_volume
            self.logger.warning("Failed to send volume command to go-librespot: %s", err)
            raise

    def _not_active_error(self) -> AudioError:
        """Build the localized 'not the active Spotify device' error, naming this device."""
        return AudioError(
            NOT_ACTIVE_DEVICE_MESSAGE.format(self._publish_name),
            translation_key="not_active_device",
            translation_args=[self._publish_name],
            translation_owner=self.translation_owner,
        )

    def _build_audio_source(self) -> AudioSource:
        """
        Construct the AudioSource MediaItem.

        go-librespot exposes a full REST control API, so play / pause / seek /
        next / previous are always available while a session is active — the
        capability flags are static (no dependency on the Spotify Web API).
        """
        return AudioSource(
            item_id=AUDIO_SOURCE_ID,
            provider=self.instance_id,
            name=self.name,
            provider_mappings={
                ProviderMapping(
                    item_id=AUDIO_SOURCE_ID,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=self._audio_format,
                )
            },
            can_play_pause=True,
            can_seek=True,
            can_next_previous=True,
            exclusive=True,
            allow_external_trigger=True,
            # Cold-start from MA is unreliable (Spotify needs an existing
            # playback context), so only allow external entry via the Spotify app.
            can_initiate=False,
        )

    def _get_target_player_id(self) -> str | None:
        """
        Determine the target player ID for playback.

        Priority: an explicitly selected player; else (auto) a currently playing
        player then the first available; else the configured default player.

        :return: The player ID to use for playback, or None if none available.
        """
        if self._active_player_id:
            if self.mass.players.get_player(self._active_player_id):
                return self._active_player_id
            self._active_player_id = None

        if self._default_player_id == PLAYER_ID_AUTO:
            all_players = list(self.mass.players.all_players(False, False))
            for player in all_players:
                if player.state.playback_state == PlaybackState.PLAYING:
                    self.logger.debug("Auto-selecting playing player: %s", player.display_name)
                    return player.player_id
            if all_players:
                first_player = all_players[0]
                self.logger.debug(
                    "Auto-selecting first available player: %s", first_player.display_name
                )
                return first_player.player_id
            return None

        if self.mass.players.get_player(self._default_player_id):
            return self._default_player_id
        self.logger.warning(
            "Configured default player '%s' no longer exists", self._default_player_id
        )
        return None

    async def _wait_for_playing(self, timeout: float = PLAYBACK_START_TIMEOUT_S) -> bool:
        """
        Wait up to ``timeout`` seconds for the daemon to report it is playing.

        :param timeout: Maximum seconds to wait.
        :return: True once playback is confirmed, False if the timeout elapses.
        """
        deadline = self.mass.loop.time() + timeout
        while True:
            if self._playing:
                return True
            if self.mass.loop.time() >= deadline:
                return False
            await asyncio.sleep(0.1)

    def _cancel_pending_play_media(self) -> None:
        """Cancel any pending deferred play_media trigger."""
        task = self._pending_play_media_task
        if task is not None and not task.done():
            task.cancel()
        self._pending_play_media_task = None

    async def _deferred_play_media_fire(self) -> None:
        """
        Trigger play_media after a short debounce.

        The daemon can emit a stale 'playing' from a dying session just before it
        reconnects; acting on it immediately would start a stream for a session
        that is about to be replaced. Debouncing — and cancelling the task on a
        later 'paused' / 'stopped' / 'active' event — avoids a play→stop→replay loop.
        """
        try:
            await asyncio.sleep(PLAY_MEDIA_DEBOUNCE_S)
        except asyncio.CancelledError:
            return
        if not self._playing or self._in_use_by_queue:
            return
        target_player_id = self._get_target_player_id()
        if not target_player_id:
            self.logger.warning(
                "Spotify Connect playback started but no player available. "
                "Select this source on a player to start playback."
            )
            return
        self.logger.info(
            "Starting Spotify Connect playback [%s] on player %s",
            self.instance_id,
            target_player_id,
        )
        self._active_player_id = target_player_id
        self.mass.create_task(
            self.mass.player_queues.play_media(target_player_id, str(self._audio_source.uri))
        )

    def _clear_active_player(self) -> None:
        """Clear the active player and reset playback state when a session ends."""
        prev_player_id = self._active_player_id
        self._active_player_id = None
        self._in_use_by_queue = None
        self._active_session_id = None
        self._playing = False
        if prev_player_id:
            self.logger.debug("Playback ended on player %s, clearing active player", prev_player_id)
            self.mass.players.trigger_player_update(prev_player_id)

    def _save_last_player_id(self, player_id: str) -> None:
        """Persist the selected player ID as the new default."""
        if self._default_player_id == player_id:
            return
        try:
            self._update_setup_data(CONF_MASS_PLAYER_ID, player_id)
            self._default_player_id = player_id
        except Exception as err:
            self.logger.debug("Failed to persist player ID: %s", err)

    def _write_config(self) -> None:
        """
        Write the go-librespot ``config.yml`` for this instance.

        go-librespot reads a YAML config; JSON is valid YAML, so we emit JSON to
        sidestep an extra dependency and any string-quoting pitfalls (the device
        name is user-provided). The config dir doubles as the credential/device
        cache so the Spotify Connect device stays paired across restarts.
        """
        os.makedirs(self.cache_dir, exist_ok=True)
        initial_volume = 50
        if self._default_player_id != PLAYER_ID_AUTO:
            # use the state's logical (0-100) volume: it matches the Spotify volume
            # scale and is also resolved for players that redirect their volume
            # control (e.g. universal players), where the raw attribute is None
            if (player := self.mass.players.get_player(self._default_player_id)) and (
                player.state.volume_level is not None
            ):
                initial_volume = player.state.volume_level
        config: dict[str, Any] = {
            "device_name": self._publish_name,
            "device_type": "speaker",
            "device_id": generate_device_id(self.instance_id),
            "bitrate": 320,
            "audio_backend": "pipe",
            # write decoded PCM to the daemon's stdout, which we capture and
            # forward (the process pipe is always attached, so the daemon's
            # non-blocking pipe open never fails for lack of a reader). s16le is
            # the Spotify source representation; MA converts it per player.
            "audio_output_pipe": "/dev/stdout",
            "audio_output_pipe_format": "s16le",
            # external_volume: don't let go-librespot attenuate the PCM — MA / the
            # target player owns the actual volume. We still receive 'volume'
            # events and push volume back so the Spotify app slider stays in sync.
            "external_volume": True,
            "volume_steps": VOLUME_STEPS,
            "initial_volume": initial_volume,
            "zeroconf_enabled": True,
            "credentials": {"type": "zeroconf", "zeroconf": {"persist_credentials": True}},
            "server": {"enabled": True, "address": "127.0.0.1", "port": self._api_port},
        }
        # Advertise the Spotify Connect device only on the interface the streams
        # server binds to, so it lands on the right network on multi-homed hosts.
        # go-librespot selects advertise interfaces by name, so map the IP to one.
        bind_ip = self.mass.streams.bind_ip
        if bind_ip and bind_ip != "0.0.0.0":
            if iface_name := interface_name_for_ip(bind_ip):
                config["zeroconf_interfaces_to_advertise"] = [iface_name]
            else:
                self.logger.debug(
                    "No interface found for stream bind IP %s; advertising on all interfaces",
                    bind_ip,
                )
        config_file = os.path.join(self.cache_dir, "config.yml")
        with open(config_file, "w", encoding="utf-8") as fileobj:
            json.dump(config, fileobj, indent=2)

    async def _daemon_runner(self) -> None:
        """Run and supervise the go-librespot daemon, restarting it if it exits."""
        assert self._binary
        # Loop forever; unload() cancels this task and the explicit stop-check below
        # handles a graceful exit without a restart.
        while True:
            self._write_config()
            proc: AsyncProcess | None = None
            try:
                # stdout carries the decoded PCM (audio_output_pipe=/dev/stdout) and
                # is consumed by get_audio_stream; stderr carries the daemon's logs,
                # read here. Because the process pipe owns stdout from spawn there is
                # always a reader, so go-librespot's non-blocking pipe open never
                # fails for lack of a consumer.
                self._proc = proc = AsyncProcess(
                    [self._binary, "--config_dir", self.cache_dir],
                    stdout=True,
                    stderr=True,
                    name=f"go-librespot[{self.name}]",
                )
                await proc.start()
                self.logger.info("Started Spotify Connect background daemon [%s]", self.name)
                async for line in proc.iter_stderr():
                    self.logger.debug("[%s] %s", self.name, line)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self.logger.warning("go-librespot daemon error [%s]: %s", self.name, err)
            finally:
                if proc:
                    await proc.close()
                # The daemon — and thus the Spotify session — is gone. Reset session
                # state so a dead/restarting daemon isn't treated as active and
                # controllable; a fresh 'active' event re-establishes it on reconnect.
                self._proc = None
                self._playing = False
                self._spotify_session_active = False
            if self._stop_called:
                break
            self.logger.info("Spotify Connect background daemon stopped for %s", self.name)
            self._restart_error_count += 1
            if self._restart_error_count >= 5:
                self.unload_with_error("go-librespot daemon failed to start multiple times.")
                return
            await asyncio.sleep(2)

    async def _events_runner(self) -> None:
        """Keep the go-librespot events websocket connected, reconnecting as needed."""
        assert self._client is not None
        while not self._stop_called:
            try:
                if not await self._client.wait_until_ready():
                    await asyncio.sleep(2)
                    continue
                # A live websocket means the daemon is healthy: reset the restart
                # backoff counter the daemon supervisor uses.
                self._restart_error_count = 0
                await self._client.listen_events(self._handle_event)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self.logger.debug("go-librespot events websocket dropped: %s", err)
            if not self._stop_called:
                await asyncio.sleep(2)

    async def _handle_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Dispatch a single go-librespot websocket event."""
        self.logger.debug("Received event [%s]: %s %s", self.name, event_type, data)
        # Remember the latest context/track so we can take playback back if the
        # user moves the active device away in the Spotify app (see on_source_selected).
        if context_uri := data.get("context_uri"):
            self._last_context_uri = context_uri
        if track_uri := data.get("uri"):
            self._last_track_uri = track_uri

        if event_type == "active":
            self._spotify_session_active = True
            self._last_session_active_time = time.time()
            # A (re)activation supersedes any deferred play_media scheduled from a
            # previous session's stale 'playing'; the fresh 'playing' that follows
            # schedules a new one.
            self._cancel_pending_play_media()
            self.logger.info("Spotify Connect session active for %s", self.name)
        elif event_type == "inactive":
            self.logger.info("Spotify Connect session inactive for %s", self.name)
            self._spotify_session_active = False
            prev_player_id = self._active_player_id
            self._clear_active_player()
            if prev_player_id:
                self.mass.create_task(self.mass.players.cmd_stop(prev_player_id))
            return
        elif event_type == "playing":
            self._playing = True
            # Externally triggered playback: kick a play_media on the target MA
            # player so the audio reaches a speaker. Deferred so a rapid
            # playing/active burst from a reconnecting session can cancel it.
            if not self._in_use_by_queue and (
                self._pending_play_media_task is None or self._pending_play_media_task.done()
            ):
                self._pending_play_media_task = self.mass.create_task(
                    self._deferred_play_media_fire()
                )
        elif event_type in ("paused", "stopped"):
            self._playing = False
            # A pause/stop is the definitive "don't start": cancel a deferred fire
            # from a now-stale 'playing'. The active get_audio_stream sees the PCM
            # stop and ends the stream (clean EOF), so the player leaves the playing
            # state; the next 'playing' event re-fires play_media to resume.
            self._cancel_pending_play_media()

        self._apply_metadata(event_type, data)

        if event_type == "volume":
            await self._handle_volume_event(data)

        # push metadata update to the active queue item's streamdetails
        if self._in_use_by_queue:
            self.mass.streams.update_stream_metadata(
                self._in_use_by_queue,
                AUDIO_SOURCE_ID,
                self.instance_id,
                self._stream_metadata,
            )

    def _apply_metadata(self, event_type: str, data: dict[str, Any]) -> None:
        """Update the live StreamMetadata from a 'metadata' or 'seek' event."""
        if event_type == "metadata":
            self._stream_metadata.uri = data.get("uri")
            if name := data.get("name"):
                self._stream_metadata.title = name
            artists = data.get("artist_names") or []
            self._stream_metadata.artist = artists[0] if artists else None
            self._stream_metadata.album = data.get("album_name")
            self._stream_metadata.image_url = data.get("album_cover_url")
            self._stream_metadata.description = None
            duration_ms = data.get("duration")
            self._stream_metadata.duration = duration_ms // 1000 if duration_ms else None
            self._stream_metadata.elapsed_time = int(data.get("position", 0)) // 1000
            self._stream_metadata.elapsed_time_last_updated = int(time.time())
        elif event_type == "seek":
            self._stream_metadata.elapsed_time = int(data.get("position", 0)) // 1000
            self._stream_metadata.elapsed_time_last_updated = int(time.time())

    async def _handle_volume_event(self, data: dict[str, Any]) -> None:
        """Apply a Spotify-side volume change to the linked MA player."""
        value = data.get("value")
        max_value = data.get("max") or VOLUME_STEPS
        if value is None:
            return
        volume = int(int(value) / int(max_value) * 100)
        # Ignore our own echo: go-librespot emits a 'volume' event for the value we
        # just pushed in on_volume_change; re-applying it would ping-pong.
        if volume == self._last_volume_sent:
            return
        # Ignore the volume that librespot reports right after a session becomes
        # active — the player's own volume should win in that window.
        if time.time() - self._last_session_active_time < INITIAL_VOLUME_GRACE_S:
            self.logger.debug("Ignoring initial volume_changed event after session active")
            return
        if not self._in_use_by_queue:
            return
        previous_volume = self._last_volume_sent
        self._last_volume_sent = volume
        try:
            await self.mass.players.cmd_volume_set(self._in_use_by_queue, volume)
        except Exception as err:
            # Volume sync is best-effort: the player may not support volume, or the
            # command may fail. Restore the cached value so a retry isn't wrongly
            # deduped, and never let it bubble up and drop the events loop.
            self._last_volume_sent = previous_volume
            self.logger.debug("Could not set volume on %s: %s", self._in_use_by_queue, err)
