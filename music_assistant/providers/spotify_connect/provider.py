"""
MA-facing provider logic for the Spotify Connect plugin.

The provider owns everything Music Assistant sees: one Spotify Connect device
(a backend daemon plus its AudioSource) per connected player, stream details,
playback claims and volume policy. It is backend-agnostic: all Spotify
specifics live behind the ``SpotifyConnectBackend`` contract and reach the
provider as normalized ``BackendEvent``s.
"""

from __future__ import annotations

import asyncio
import re
import time
from contextlib import suppress
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Final, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    EventType,
    MediaType,
    ProviderFeature,
    RepeatMode,
    SourceControl,
)
from music_assistant_models.errors import AudioError, MediaNotFoundError
from music_assistant_models.media_items import (
    AudioSource,
    ProviderMapping,
)
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.constants import CONF_CROSSFADE_DURATION
from music_assistant.helpers.config_entries import (
    CONF_CONNECTED_PLAYERS,
    CONF_PUBLISH_NAME_TEMPLATE,
    create_connected_players_entry,
    create_publish_name_template_entry,
    resolve_publish_name,
)
from music_assistant.models.plugin import PluginProvider, SourceControlValue

from .base import (
    AUDIO_QUALITY_LOSSLESS,
    AUDIO_QUALITY_OPTIONS,
)
from .go_librespot import GoLibrespotBackend
from .helpers import get_go_librespot_binary
from .models import BackendEventType
from .soloist import (
    VOLUME_MODE_PLAYER_ONLY,
    VOLUME_MODE_SYNC_SPOTIFY,
    SoloistBackend,
    SoloistBinaryManager,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.event import MassEvent
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.player import Player

    from .base import SpotifyConnectBackend
    from .models import BackendEvent, BackendPlaybackOptions, BackendTrackMetadata

# Backend selection, collected by the setup flow (stored in setup_data).
CONF_BACKEND = "backend"
BACKEND_GO_LIBRESPOT = "go_librespot"
BACKEND_SOLOIST = "soloist"

# Soloist-specific values collected by the setup flow (see CONF_BACKEND).
CONF_API_KEY = "soloist_api_key"
CONF_SOLOIST_CONSENT = "soloist_download_consent"
CONF_VOLUME_MODE = "volume_mode"

# Playback behavior applied by the Spotify engine itself (both backends).
CONF_LOUDNESS_NORMALIZATION = "loudness_normalization"
MAX_CROSSFADE_DURATION = 12  # seconds, matching the Spotify apps' slider
CONF_AUDIO_QUALITY = "audio_quality"

AUDIO_QUALITY_VALUES: Final = {option.value for option in AUDIO_QUALITY_OPTIONS}

# The selectable volume modes (labels resolve from strings.json); a runtime
# option on the provider's settings page, not part of the setup flow.
VOLUME_MODE_OPTIONS: Final = [
    ConfigValueOption(VOLUME_MODE_PLAYER_ONLY),
    ConfigValueOption(VOLUME_MODE_SYNC_SPOTIFY),
]

SUPPORTED_FEATURES = {ProviderFeature.AUDIO_SOURCE}

# When playback is paused the backend stops writing PCM. If no PCM arrives for
# this long while we're not in a 'playing' state, end the stream (clean EOF) so
# the player leaves the playing state; the next 'playing' event re-streams.
PAUSE_EOF_TIMEOUT_S = 0.5

# A stop after a pause runs while the player's playback lock is held, so a slow one
# delays whatever the user does next; warn when it takes longer than this.
SLOW_STOP_WARN_S = 10.0

# Seconds to wait for the backend to report 'playing' after a resume request.
PLAYBACK_START_TIMEOUT_S = 3.0

# Debounce before acting on an externally-triggered 'playing' event (see
# _deferred_play_media_fire for why).
PLAY_MEDIA_DEBOUNCE_S = 0.5

# Ignore Spotify volume events for this long after a session becomes active, so
# the player's own volume wins over the backend's initial value on (re)connect.
INITIAL_VOLUME_GRACE_S = 3.0

# User-facing message for the "not the active Spotify device" failure.
# {0} is the Spotify Connect device's published name (see _not_active_error).
NOT_ACTIVE_DEVICE_MESSAGE = (
    "'{0}' is not the active Spotify playback device. "
    "Open the Spotify app, select it as the playback device, and try again."
)


@dataclass
class _PlayerDaemon:
    """State for one connected player's Spotify Connect daemon."""

    # the connected player this daemon plays on; doubles as the AudioSource item_id
    player_id: str
    # player_id sanitized for use in filesystem paths and identity keys
    safe_player_id: str
    # the name this daemon advertises as its device name in the Spotify app
    publish_name: str
    stream_metadata: StreamMetadata
    backend: SpotifyConnectBackend = field(init=False)
    audio_source: AudioSource = field(init=False)
    stop_called: bool = False
    # Currently active player (the one currently playing or selected)
    active_player_id: str | None = None
    # in_use_by_player is the queue currently streaming us. Claimed in
    # on_source_selected (NOT in get_stream_details — that path also runs
    # from queue preload, where claiming would block a later cross-queue
    # handoff). Released in on_source_unselected when the session id
    # matches, or in _clear_active_player on the backend's 'inactive' event.
    in_use_by_player: str | None = None
    # active_session_id is the controller-provided token for the current
    # stream request — used to reject stale on_source_unselected callbacks
    # after a same-queue reconnect supersedes the previous request.
    active_session_id: str | None = None
    # tracks the backend's play/pause state from its 'playing' / 'paused' /
    # 'inactive' events; gates the resume kick in on_source_selected (skip if
    # already playing) and the play_media trigger in the event handler.
    playing: bool = False
    # True while MA is the active Spotify Connect device (set on 'active',
    # cleared on 'inactive'); gates get_stream_details and transport commands.
    spotify_session_active: bool = False
    # holds the single in-flight deferred play_media task scheduled once the
    # session is both active and playing; cancelled when a later event makes
    # that state stale.
    pending_play_media_task: asyncio.Task[None] | None = None
    # holds the in-flight stop of a paused player (pipe-fed backends
    # only); the stop dispatches right away, but a 'playing' event cancels
    # it while it is still in flight (a slow player can hold it for up to
    # 10s), so a resume is never killed by a stop landing late.
    pending_pause_stop_task: asyncio.Task[None] | None = None
    last_session_active_time: float = 0
    last_volume_sent: int | None = None
    # Last context/track URIs seen on the event stream. Used to take playback
    # back (make ourselves the active Spotify device) when the user switched
    # the active device away in the Spotify app and then presses play in MA.
    last_context_uri: str | None = None
    last_track_uri: str | None = None
    # Latest playback options reported by the backend. Cached on every
    # OPTIONS_CHANGED — an externally triggered session reports them before
    # the queue claim exists — and pushed to the queue once claimed in
    # on_source_selected. Cleared when the session ends.
    last_playback_options: BackendPlaybackOptions | None = None


class SpotifyConnectProvider(PluginProvider):
    """Implementation of a Spotify Connect Plugin (backed by SpotifyConnectBackends)."""

    reload_on_streams_network_change = True

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize MusicProvider."""
        super().__init__(mass, manifest, config, SUPPORTED_FEATURES)
        self._daemons: dict[str, _PlayerDaemon] = {}
        self._reconcile_lock = asyncio.Lock()
        self._unload_called = False
        self._unsubscribe: Callable[[], None] | None = None
        # the connected players are immutable per load: config changes reload the provider
        self._assigned_player_ids: tuple[str, ...] = tuple(
            cast("list[str]", self.get_config_value(CONF_CONNECTED_PLAYERS) or [])
        )

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return runtime options for this provider."""
        # The backend selection and the soloist secrets are managed by the setup
        # flow (stored in setup_data) and stay hidden; the volume mode is a
        # visible runtime option for soloist configs.
        is_soloist = self.get_setup_value(CONF_BACKEND) == BACKEND_SOLOIST
        return (
            create_connected_players_entry(
                self.mass, cast("list[str]", self.get_config_value(CONF_CONNECTED_PLAYERS) or [])
            ),
            create_publish_name_template_entry(self.get_config_value(CONF_PUBLISH_NAME_TEMPLATE)),
            ConfigEntry(
                key=CONF_BACKEND,
                type=ConfigEntryType.STRING,
                default_value=BACKEND_GO_LIBRESPOT,
                required=False,
                hidden=True,
            ),
            ConfigEntry(
                key=CONF_API_KEY,
                type=ConfigEntryType.SECURE_STRING,
                required=False,
                hidden=True,
            ),
            ConfigEntry(
                key=CONF_SOLOIST_CONSENT,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                required=False,
                hidden=True,
            ),
            ConfigEntry(
                key=CONF_VOLUME_MODE,
                type=ConfigEntryType.STRING,
                default_value=VOLUME_MODE_PLAYER_ONLY,
                required=False,
                options=VOLUME_MODE_OPTIONS,
                hidden=not is_soloist,
            ),
            ConfigEntry(
                key=CONF_CROSSFADE_DURATION,
                type=ConfigEntryType.INTEGER,
                range=(0, MAX_CROSSFADE_DURATION),
                default_value=0,
                required=False,
            ),
            ConfigEntry(
                key=CONF_LOUDNESS_NORMALIZATION,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                required=False,
            ),
            ConfigEntry(
                key=CONF_AUDIO_QUALITY,
                type=ConfigEntryType.STRING,
                default_value=AUDIO_QUALITY_LOSSLESS,
                required=False,
                options=AUDIO_QUALITY_OPTIONS,
            ),
        )

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # Surface a broken engine setup as a load error (like the per-instance
        # model did through its single backend start): the soloist binary is
        # installed/verified once here — the per-daemon starts hit the manager's
        # recently-verified fast path — and go-librespot must be on PATH.
        if self.get_setup_value(CONF_BACKEND) == BACKEND_SOLOIST:
            await SoloistBinaryManager(self.mass).ensure_fresh(
                bool(self.get_setup_value(CONF_SOLOIST_CONSENT))
            )
        else:
            get_go_librespot_binary()

    async def loaded_in_mass(self) -> None:
        """Start the Connect daemons and follow the connected players' lifecycle."""
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
            await asyncio.gather(*(self._stop_daemon(daemon) for daemon in daemons))
            # drop the standing source entries from the players' cached source lists
            for daemon in daemons:
                self.mass.players.trigger_player_update(daemon.player_id)

    async def get_audio_sources(self) -> list[AudioSource]:
        """Return the AudioSources this plugin currently exposes."""
        return [daemon.audio_source for daemon in self._daemons.values()]

    def get_player_audio_sources(self, player_id: str) -> list[AudioSource]:
        """Return the AudioSource bound to the given connected player, if any."""
        daemon = self._daemons.get(player_id)
        return [daemon.audio_source] if daemon else []

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Return StreamDetails for streaming the Spotify Connect audio.

        Side-effect-free: ownership is claimed in on_source_selected (which the
        streams controller fires before this method on the actual stream
        request). Keeping this idempotent means preload paths can fetch
        streamdetails without claiming the source and blocking a cross-queue
        handoff.

        Raises AudioError when MA is not the active Spotify Connect device and
        no previous playback context is known to resume — the user then has to
        start playback from the Spotify app once.
        """
        daemon = self._daemons.get(item_id)
        if daemon is None:
            raise MediaNotFoundError(f"Unknown AudioSource: {item_id}")
        # Only refuse when we can neither resume nor take playback back. If a last
        # context is known we let the stream proceed; on_source_selected then takes
        # playback back (makes us the active device) before audio is pulled.
        if not daemon.playing and not daemon.spotify_session_active and not daemon.last_context_uri:
            raise self._not_active_error(daemon)
        # The backend describes how its audio is consumed: CUSTOM (the core pulls
        # PCM from get_audio_stream) or a named pipe read directly by ffmpeg.
        # decoded_audio_format tells the core the PCM format while audio_format
        # keeps the source codec for display; MA resamples to each player's
        # format as needed.
        # expiration=0: never reuse a cached streamdetails so the active-device
        # check above re-runs on every play attempt.
        stream_source = await daemon.backend.get_stream_source()
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=daemon.backend.audio_format,
            decoded_audio_format=daemon.backend.decoded_audio_format,
            media_type=MediaType.AUDIO_SOURCE,
            stream_type=stream_source.stream_type,
            path=stream_source.path,
            stream_metadata=daemon.stream_metadata,
            extra_input_args=stream_source.extra_input_args,
            expiration=0,
        )

    async def get_audio_stream(
        self,
        streamdetails: StreamDetails,
        seek_position: int = 0,
    ) -> AsyncGenerator[bytes]:
        """
        Yield raw PCM from the backend's audio pipe for the live AudioSource.

        Only used for backends with a CUSTOM stream source (NAMED_PIPE backends
        are read directly by the streams controller). When playback pauses the
        backend stops writing PCM; we then end the stream (clean EOF) so the
        consuming player leaves the playing state. The next ``playing`` event
        re-triggers playback.

        :param streamdetails: The StreamDetails of the AudioSource being streamed.
        :param seek_position: Ignored — seeking is handled upstream by Spotify,
            not by replaying the bytestream.
        """
        daemon = self._daemons.get(streamdetails.item_id)
        if daemon is None:
            raise MediaNotFoundError(f"Unknown AudioSource: {streamdetails.item_id}")
        read_chunk = daemon.backend.get_audio_reader()
        if read_chunk is None:
            raise AudioError("Spotify Connect daemon is not running")
        # No pacing here: the streams controller's realtime pacer (ffmpeg readrate
        # with a small initial burst) is the single pacing authority for live
        # sources. Backpressure through the audio pipe bounds how far the backend
        # (whose pipe backend is not realtime-paced) runs ahead, while the burst
        # headroom absorbs scheduling jitter that would otherwise underrun the
        # player. Pacing a second time here would pin the feed to exactly realtime
        # and starve that headroom.
        while True:
            try:
                chunk = await asyncio.wait_for(read_chunk(), timeout=PAUSE_EOF_TIMEOUT_S)
            except TimeoutError:
                # No PCM for a while. If playback is no longer active (paused /
                # stopped / session gone) end the stream so the player goes idle;
                # a brief buffering gap while still playing just keeps waiting.
                if not daemon.playing:
                    return
                continue
            if not chunk:
                return  # audio pipe closed (backend exited / restarting)
            yield chunk

    def delivers_normalized_audio(self, streamdetails: StreamDetails) -> bool:
        """
        Return whether Spotify applies loudness normalization to this source.

        :param streamdetails: Stream details of the active Spotify Connect source.
        """
        return self._resolve_loudness_normalization()

    def delivers_crossfaded_audio(self, streamdetails: StreamDetails) -> bool:
        """
        Return whether Spotify applies crossfade to this source.

        :param streamdetails: Stream details of the active Spotify Connect source.
        """
        return self._resolve_crossfade_ms() > 0

    async def on_source_selected(
        self,
        source_id: str,
        player_id: str,
        owner_player_id: str,
        stream_session_id: str,
    ) -> None:
        """Handle callback when this AudioSource has been selected/started on a player."""
        daemon = self._daemons.get(source_id)
        if daemon is None or not player_id:
            return

        # Cache the owner_player_id (== user-facing MA player) rather than the
        # protocol-level player_id. Some protocol players are ephemeral bridges
        # whose ID is invalid for play_media / queue lookups once torn down.
        active_player_id = owner_player_id
        prev_player_id = (
            daemon.active_player_id if daemon.active_player_id != active_player_id else None
        )

        # Claim ownership for this queue BEFORE kicking the previous player: the
        # awaited stop below can complete the old stream's teardown, and only an
        # already-replaced session id lets on_source_unselected's stale-guard
        # reject that teardown — otherwise it releases the Spotify session this
        # handover is about to use.
        daemon.in_use_by_player = owner_player_id
        daemon.active_session_id = stream_session_id
        daemon.active_player_id = active_player_id
        self.logger.debug("Active player set to: %s", active_player_id)

        # If a different player was consuming the source, kick it out (the source
        # is exclusive).
        if prev_player_id:
            self.logger.info(
                "Source selected on player %s, stopping playback on %s",
                active_player_id,
                prev_player_id,
            )
            try:
                await self.mass.players.cmd_stop(prev_player_id)
            except Exception as err:
                self.logger.debug("Failed to stop previous player %s: %s", prev_player_id, err)

        # Push the options the session reported before this claim existed, so the
        # queue mirrors the session's shuffle/repeat state from the start.
        if daemon.last_playback_options is not None:
            self.mass.players.update_source_options(
                owner_player_id,
                daemon.player_id,
                self.instance_id,
                shuffle_enabled=daemon.last_playback_options.shuffle,
                repeat_mode=daemon.last_playback_options.repeat,
            )

        # Externally triggered: the backend is already playing → nothing to do.
        # Otherwise acquire playback, then confirm it actually started.
        if not daemon.playing:
            try:
                if daemon.spotify_session_active:
                    # Still the active Spotify device (just paused) → resume.
                    await daemon.backend.resume()
                elif daemon.last_context_uri:
                    # The user moved the active device away in the Spotify app.
                    # Take playback back by (re)starting the last context on us,
                    # which makes this device the active one again. The track
                    # restarts from its beginning (there is no resume-at-position
                    # play call).
                    self.logger.info("Taking Spotify playback back to Music Assistant")
                    await daemon.backend.play(
                        daemon.last_context_uri, skip_to_uri=daemon.last_track_uri
                    )
                else:
                    raise self._not_active_error(daemon)
            except AudioError:
                raise
            except Exception as err:
                raise AudioError(f"Failed to acquire Spotify Connect: {err}") from err
            if not await self._wait_for_playing(daemon):
                raise self._not_active_error(daemon)

        # The backend reports 100% volume until told otherwise; push the player's
        # volume so the Spotify app's absolute volume commands start from the
        # real level.
        await self._sync_player_volume_to_spotify(daemon, active_player_id)

    async def on_source_unselected(
        self, source_id: str, owner_player_id: str, stream_session_id: str
    ) -> None:
        """Release the queue-scoped exclusive claim when MA tears down the stream."""
        daemon = self._daemons.get(source_id)
        if daemon is None:
            return
        # Reject stale callbacks: only release if this is still the active
        # session. A owner_player_id check alone is not sufficient — same-queue
        # reconnects would otherwise let an old request's late callback clear
        # the live claim of the new stream.
        if daemon.active_session_id != stream_session_id:
            return
        daemon.active_session_id = None
        if daemon.in_use_by_player == owner_player_id:
            daemon.in_use_by_player = None
        if daemon.playing:
            # MA-side stop/queue-clear: release the Spotify session so the app
            # drops the device as its playback target — the daemon would
            # otherwise keep playing into a pipe nobody consumes and the app
            # would stay tethered to the device. (Teardowns caused by a
            # Spotify-side pause, deselect or a player handoff never reach
            # here: those cleared the playing flag or replaced the session id first.)
            try:
                await daemon.backend.deactivate()
            except Exception as err:
                self.logger.debug("Failed to release Spotify session on stream teardown: %s", err)

    async def on_source_released(self, source_id: str, player_id: str) -> None:
        """Release the Spotify session when a player is done with this source."""
        daemon = self._daemons.get(source_id)
        if daemon is None or daemon.active_player_id != player_id:
            return
        if not daemon.spotify_session_active:
            return
        # Released whether or not a stream is still winding down: a paused source
        # already ended its stream, so its teardown released nothing and the
        # Spotify app would stay tethered to a player that has moved on.
        #
        # Let the player go first. The backend answers a deactivate with the same
        # 'inactive' event a deselect in the Spotify app produces, and that stops
        # the player we were on - which by now is playing whatever took our place.
        daemon.active_player_id = None
        try:
            await daemon.backend.deactivate()
        except Exception as err:
            self.logger.debug("Failed to release Spotify session: %s", err)

    async def on_source_control(
        self,
        source_id: str,
        action: SourceControl,
        value: SourceControlValue = None,
    ) -> None:
        """Proxy playback control commands to the backend."""
        daemon = self._daemons.get(source_id)
        if daemon is None:
            return
        if not daemon.playing and not daemon.spotify_session_active:
            raise self._not_active_error(daemon)
        try:
            if action == SourceControl.PLAY:
                await daemon.backend.resume()
            elif action == SourceControl.PAUSE:
                await daemon.backend.pause()
            elif action == SourceControl.NEXT:
                await daemon.backend.next()
            elif action == SourceControl.PREVIOUS:
                await daemon.backend.previous()
            elif (
                action == SourceControl.SEEK
                # tolerate float positions from internal callers; bool is an int
                # subclass, so a misrouted toggle must not become a 1-second seek
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
            ):
                await daemon.backend.seek(int(value) * 1000)
            elif action == SourceControl.SHUFFLE and isinstance(value, bool):
                # strict bool: None or a misrouted enum (bool(RepeatMode.OFF) is
                # True) must not silently toggle shuffle
                await daemon.backend.set_shuffle(value)
            elif action == SourceControl.REPEAT and isinstance(value, RepeatMode):
                await daemon.backend.set_repeat(value)
        except Exception as err:
            self.logger.warning("Failed to send %s command to backend: %s", action, err)
            raise

    async def on_volume_change(self, source_id: str, volume: int) -> None:
        """Sync the Spotify app's volume slider with the player's new volume."""
        daemon = self._daemons.get(source_id)
        if daemon is None:
            return
        if not daemon.playing and not daemon.spotify_session_active:
            raise self._not_active_error(daemon)
        # Prevent ping-pong: only push if the value actually changed from what we
        # last sent to / received from the backend.
        if daemon.last_volume_sent == volume:
            return
        try:
            await self._push_volume_to_backend(daemon, volume)
        except Exception as err:
            self.logger.warning("Failed to send volume command to backend: %s", err)
            raise

    async def _on_player_event(self, event: MassEvent) -> None:
        """Reconcile the Connect daemons after a connected player's lifecycle event."""
        if self._unload_called:
            return
        if event.event == EventType.PLAYER_REMOVED:
            # permanent removal: stop the daemon; a temporarily unavailable player
            # (which fires only PLAYER_UPDATED) keeps its running daemon so the
            # advertised device identity stays stable across the outage
            async with self._reconcile_lock:
                if event.object_id and (daemon := self._daemons.pop(event.object_id, None)):
                    # the session may be consumed by ANOTHER player (cross-select or
                    # sync-group owner); release it so that player is not left bound
                    # to a source that can no longer stream
                    self._clear_active_player(daemon)
                    await self._stop_daemon(daemon)
            return
        await self._reconcile()

    async def _reconcile(self) -> None:
        """
        Align the running Connect daemons with the connected players.

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
                publish_name = resolve_publish_name(template, player.display_name)
                daemon = self._daemons.get(player_id)
                if daemon is not None and daemon.publish_name == publish_name:
                    continue
                if daemon is not None:
                    # the advertised name follows the player name: restart on rename.
                    # A live session is released first so the consuming player's queue
                    # is not left held by a source the replaced daemon cannot stream
                    # (unload and player removal already release via the controller).
                    self._clear_active_player(daemon)
                    del self._daemons[player_id]
                    await self._stop_daemon(daemon)
                await self._start_daemon(player, publish_name)
                # the standing source entry feeds the player's cached source list
                self.mass.players.trigger_player_update(player_id)

    async def _start_daemon(self, player: Player, publish_name: str) -> None:
        """
        Create the daemon state for a connected player and start its backend.

        :param player: The (registered) player this daemon plays on.
        :param publish_name: The device name to advertise in the Spotify app.
        """
        player_id = player.player_id
        daemon = _PlayerDaemon(
            player_id=player_id,
            safe_player_id=re.sub(r"[^A-Za-z0-9_.-]", "_", player_id),
            publish_name=publish_name,
            stream_metadata=StreamMetadata(title=f"Spotify Connect | {publish_name}"),
        )
        daemon.backend = self._create_backend(daemon, player.display_name)
        daemon.audio_source = self._build_audio_source(daemon, player.display_name)
        self._daemons[player_id] = daemon
        try:
            await daemon.backend.start()
        except Exception as err:
            # a daemon that cannot start makes the whole provider unusable
            # (shared engine setup); surface it like a backend fatal error
            self._daemons.pop(player_id, None)
            self.unload_with_error(err)

    async def _stop_daemon(self, daemon: _PlayerDaemon) -> None:
        """Stop a daemon's backend and cancel its pending tasks."""
        daemon.stop_called = True
        pending_tasks = [
            task
            for task in (daemon.pending_play_media_task, daemon.pending_pause_stop_task)
            if task is not None and not task.done()
        ]
        self._cancel_pending_play_media(daemon)
        self._cancel_pending_pause_stop(daemon)
        # await the cancelled tasks so no late player command outlives the daemon
        for task in pending_tasks:
            with suppress(asyncio.CancelledError):
                await task
        await daemon.backend.stop()

    def _create_backend(self, daemon: _PlayerDaemon, player_name: str) -> SpotifyConnectBackend:
        """
        Construct the configured Spotify Connect backend implementation.

        :param daemon: The daemon state the backend belongs to.
        :param player_name: The connected player's display name (log labels).
        """
        # One backend per connected player: the identity key derives the
        # per-player credential/cache dirs and the stable Spotify device id.
        identity_key = f"{self.instance_id}_{daemon.safe_player_id}"
        log_label = f"{self.name}/{player_name}"
        event_callback = partial(self._handle_backend_event, daemon)
        # The backend choice and soloist secrets are collected by the setup flow
        # into setup_data; a config migrated from before the backend choice
        # existed yields None here, which intentionally selects go-librespot
        # (the equality check must keep treating None as the default).
        if self.get_setup_value(CONF_BACKEND) == BACKEND_SOLOIST:
            return SoloistBackend(
                self.mass,
                identity_key=identity_key,
                publish_name=daemon.publish_name,
                name=log_label,
                logger=self.logger,
                event_callback=event_callback,
                api_key=cast("str", self.get_setup_value(CONF_API_KEY) or ""),
                consent=bool(self.get_setup_value(CONF_SOLOIST_CONSENT)),
                volume_mode=self._resolve_volume_mode(),
                crossfade_ms=self._resolve_crossfade_ms(),
                loudness_normalization=self._resolve_loudness_normalization(),
                audio_quality=self._resolve_audio_quality(),
            )
        return GoLibrespotBackend(
            self.mass,
            identity_key=identity_key,
            publish_name=daemon.publish_name,
            name=log_label,
            logger=self.logger,
            event_callback=event_callback,
            crossfade_ms=self._resolve_crossfade_ms(),
            loudness_normalization=self._resolve_loudness_normalization(),
            audio_quality=self._resolve_audio_quality(),
        )

    def _resolve_volume_mode(self) -> str:
        """Return the configured volume mode (the provider options page is the only source)."""
        return cast(
            "str",
            self.config.get_value(CONF_VOLUME_MODE) or VOLUME_MODE_PLAYER_ONLY,
        )

    def _resolve_crossfade_ms(self) -> int:
        """Return the configured crossfade duration in milliseconds (0 = disabled)."""
        value = cast("int | None", self.config.get_value(CONF_CROSSFADE_DURATION))
        return max(0, min(int(value or 0), MAX_CROSSFADE_DURATION)) * 1000

    def _resolve_loudness_normalization(self) -> bool:
        """Return whether Spotify's loudness normalization should be enabled."""
        value = self.config.get_value(CONF_LOUDNESS_NORMALIZATION)
        return True if value is None else bool(value)

    def _resolve_audio_quality(self) -> str:
        """Return the configured streaming quality tier."""
        value = self.config.get_value(CONF_AUDIO_QUALITY)
        if value in AUDIO_QUALITY_VALUES:
            return cast("str", value)
        return AUDIO_QUALITY_LOSSLESS

    def _not_active_error(self, daemon: _PlayerDaemon) -> AudioError:
        """Build the localized 'not the active Spotify device' error, naming the device."""
        return AudioError(
            NOT_ACTIVE_DEVICE_MESSAGE.format(daemon.publish_name),
            translation_key="not_active_device",
            translation_args=[daemon.publish_name],
            translation_owner=self.translation_owner,
        )

    def _build_audio_source(self, daemon: _PlayerDaemon, player_name: str) -> AudioSource:
        """
        Construct the AudioSource MediaItem for a daemon.

        Backends provide a full control surface, so play / pause / seek /
        next / previous are always available while a session is active — the
        capability flags are static (no dependency on the Spotify Web API).
        Ordering the session is only offered by backends implementing the
        queue-session verbs.

        :param daemon: The daemon state the source belongs to.
        :param player_name: The connected player's display name.
        """
        return AudioSource(
            # the player id is stable across renames, so the source uri survives them
            item_id=daemon.player_id,
            provider=self.instance_id,
            name=f"{self.name} ({player_name})",
            provider_mappings={
                ProviderMapping(
                    item_id=daemon.player_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=daemon.backend.audio_format,
                )
            },
            can_play_pause=True,
            can_seek=True,
            can_next_previous=True,
            can_shuffle=daemon.backend.supports_queue_control,
            can_repeat=daemon.backend.supports_queue_control,
            exclusive=True,
            allow_external_trigger=True,
            # Browsable/startable from MA: playback resumes the last known
            # Spotify context (claiming active device status). Without any
            # prior context a localized error points the user to the app.
            can_initiate=True,
        )

    async def _wait_for_playing(
        self, daemon: _PlayerDaemon, timeout: float = PLAYBACK_START_TIMEOUT_S
    ) -> bool:
        """
        Wait up to ``timeout`` seconds for the backend to report it is playing.

        :param daemon: The daemon whose backend was asked to play.
        :param timeout: Maximum seconds to wait.
        :return: True once playback is confirmed, False if the timeout elapses.
        """
        deadline = self.mass.loop.time() + timeout
        while True:
            if daemon.playing:
                return True
            if self.mass.loop.time() >= deadline:
                return False
            await asyncio.sleep(0.1)

    async def _stop_paused_player(self, player_id: str) -> None:
        """
        Stop the active player after a pause on a backend without stream EOF.

        :param player_id: The player currently consuming the live source.
        """
        self.logger.debug("Stopping player %s after pause", player_id)
        started = self.mass.loop.time()
        try:
            await self.mass.players.cmd_stop(player_id)
        except Exception as err:
            self.logger.debug("Failed to stop player %s on pause: %s", player_id, err)
            return
        # a timeout around the stop is not enforceable: the process cleanup it waits on
        # can swallow the cancellation (see AsyncProcess.close), so a slow stop is reported
        if (elapsed := self.mass.loop.time() - started) > SLOW_STOP_WARN_S:
            self.logger.warning("Stopping player %s took %.1f seconds", player_id, elapsed)
        else:
            self.logger.debug("Player %s stopped after pause", player_id)

    def _schedule_play_media(self, daemon: _PlayerDaemon) -> None:
        """Schedule playback when Spotify is active and no player owns the source."""
        if (
            not daemon.playing
            or not daemon.spotify_session_active
            or daemon.in_use_by_player
            or (
                daemon.pending_play_media_task is not None
                and not daemon.pending_play_media_task.done()
            )
        ):
            return
        daemon.pending_play_media_task = self.mass.create_task(
            self._deferred_play_media_fire(daemon)
        )

    def _cancel_pending_play_media(self, daemon: _PlayerDaemon) -> None:
        """Cancel any pending deferred play_media trigger."""
        task = daemon.pending_play_media_task
        if task is not None and not task.done():
            task.cancel()
        daemon.pending_play_media_task = None

    def _schedule_pause_stop(self, daemon: _PlayerDaemon, player_id: str) -> None:
        """
        Dispatch the stop of the paused player, replacing a still-pending one.

        :param daemon: The daemon whose consuming player paused.
        :param player_id: The player currently consuming the live source.
        """
        self._cancel_pending_pause_stop(daemon)
        task = self.mass.create_task(self._stop_paused_player(player_id))
        daemon.pending_pause_stop_task = task
        task.add_done_callback(partial(self._on_pause_stop_done, daemon))

    def _cancel_pending_pause_stop(self, daemon: _PlayerDaemon) -> None:
        """Cancel any pending deferred stop of a paused player."""
        task = daemon.pending_pause_stop_task
        if task is not None and not task.done():
            task.cancel()
        daemon.pending_pause_stop_task = None

    def _on_pause_stop_done(self, daemon: _PlayerDaemon, task: asyncio.Task[None]) -> None:
        """Drop the pause-stop handle once its task finished (unless already replaced)."""
        if daemon.pending_pause_stop_task is task:
            daemon.pending_pause_stop_task = None

    async def _deferred_play_media_fire(self, daemon: _PlayerDaemon) -> None:
        """
        Trigger play_media after a short debounce.

        The backend can emit a stale 'playing' from a dying session just before it
        reconnects; acting on it immediately would start a stream for a session
        that is about to be replaced. Debouncing — and cancelling the task on a
        later 'paused' / 'stopped' / 'active' event — avoids a play→stop→replay loop.
        """
        try:
            await asyncio.sleep(PLAY_MEDIA_DEBOUNCE_S)
        except asyncio.CancelledError:
            return
        if not daemon.playing or daemon.in_use_by_player:
            return
        # an explicitly selected player wins, else the daemon's own connected player
        target_player_id = daemon.active_player_id or daemon.player_id
        self.logger.info(
            "Starting Spotify Connect playback [%s] on player %s",
            daemon.publish_name,
            target_player_id,
        )
        daemon.active_player_id = target_player_id
        # awaited inline so the tracked deferred task covers the whole start and
        # a daemon teardown can still cancel an in-flight source selection
        await self.mass.player_queues.play_media(target_player_id, str(daemon.audio_source.uri))

    def _clear_active_player(self, daemon: _PlayerDaemon) -> None:
        """Clear the active player and reset playback state when a session ends."""
        prev_player_id = daemon.active_player_id
        source_session = (
            self.mass.players.get_audio_source_session(prev_player_id) if prev_player_id else None
        )
        daemon.active_player_id = None
        daemon.in_use_by_player = None
        daemon.active_session_id = None
        daemon.playing = False
        if prev_player_id:
            self.logger.debug("Playback ended on player %s, clearing active player", prev_player_id)
            # the player is not playing us any more, so it should stop saying it is;
            # the stop itself is scheduled separately by the caller
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

    async def _handle_backend_event(self, daemon: _PlayerDaemon, event: BackendEvent) -> None:
        """Dispatch a single normalized event received from a daemon's backend."""
        if event.type is BackendEventType.CONNECTION_LOST:
            # The backend's Spotify session is gone (e.g. daemon exit). Reset
            # session state so a dead/restarting backend isn't treated as active
            # and controllable; a fresh 'active' event re-establishes it.
            daemon.playing = False
            daemon.spotify_session_active = False
            # stale options must not outlive the session they belong to
            daemon.last_playback_options = None
            return
        if event.type is BackendEventType.FATAL_ERROR:
            # a deliberately stopped daemon (unload, rename restart, player
            # removal) must not tear down the whole provider
            if not daemon.stop_called:
                self.unload_with_error(event.error or "Spotify Connect backend failed")
            return
        if event.type is BackendEventType.ERROR:
            # non-fatal backend error: surface it in the log only
            self.logger.warning("Spotify Connect backend error: %s", event.error)
            return

        self._remember_context_uris(daemon, event)

        if event.type is BackendEventType.QUEUE_CHANGED:
            # queue snapshots are not consumed yet (full queue-item mirroring comes later)
            return
        if event.type is BackendEventType.OPTIONS_CHANGED:
            # an options report is no reason to re-push the (unchanged) stream metadata below
            self._handle_options_changed(daemon, event)
            return

        if event.type is BackendEventType.SESSION_ACTIVE:
            daemon.spotify_session_active = True
            daemon.last_session_active_time = time.time()
            # A (re)activation supersedes any deferred play_media from a previous
            # session. Reconcile afterwards because 'playing' may arrive first.
            self._cancel_pending_play_media(daemon)
            self.logger.info("Spotify Connect session active for %s", daemon.publish_name)
            # A new session starts at the backend's 100% volume default; push the
            # target player's volume so the Spotify app's slider is correct from
            # device selection, before any playback starts. (In the soloist
            # player_only mode the backend pins 100% and ignores the pushed
            # value — the app slider staying at 100 there is by design.)
            await self._sync_player_volume_to_spotify(
                daemon, daemon.active_player_id or daemon.player_id
            )
            self._schedule_play_media(daemon)
        elif event.type is BackendEventType.SESSION_INACTIVE:
            self.logger.info("Spotify Connect session inactive for %s", daemon.publish_name)
            daemon.spotify_session_active = False
            # stale options must not outlive the session they belong to
            daemon.last_playback_options = None
            prev_player_id = daemon.active_player_id
            self._clear_active_player(daemon)
            if prev_player_id:
                self._schedule_pause_stop(daemon, prev_player_id)
            return
        elif event.type is BackendEventType.PLAYING:
            daemon.playing = True
            # A resume can arrive while the pause-stop is still in flight on a
            # slow player; cancel it so it doesn't kill the restarted stream.
            # (a stop that already completed is fine: play_media below restarts)
            self._cancel_pending_pause_stop(daemon)
            # Externally triggered playback: kick a play_media on the target MA
            # player so the audio reaches a speaker. Deferred so a rapid
            # playing/active burst from a reconnecting session can cancel it.
            # Only while the session is active: a daemon playing without being
            # the active Connect device (e.g. right after a deactivate) must
            # not grab MA players in a loop.
            self._schedule_play_media(daemon)
        elif event.type in (BackendEventType.PAUSED, BackendEventType.STOPPED):
            was_playing = daemon.playing
            daemon.playing = False
            # A pause/stop is the definitive "don't start": cancel a deferred fire
            # from a now-stale 'playing'. On a backend whose stream ends on pause the
            # active get_audio_stream sees the PCM stop and ends the stream (clean
            # EOF), so the player leaves the playing state and the next 'playing'
            # event re-fires play_media to resume.
            self._cancel_pending_play_media(daemon)
            # A backend without a stream end on pause never signals EOF, so
            # the player must be stopped actively; the claim stays so the next
            # 'playing' event resumes playback like the EOF path does. Only the
            # playing→paused transition fires it: the backend reports a pause
            # through multiple events (state delta + snapshot).
            if (
                was_playing
                and not daemon.backend.stream_ends_on_pause
                and (player_id := daemon.active_player_id)
            ):
                self._schedule_pause_stop(daemon, player_id)

        if event.type is BackendEventType.METADATA and event.metadata is not None:
            self._apply_metadata(daemon, event.metadata)
        elif event.type is BackendEventType.POSITION and event.position is not None:
            daemon.stream_metadata.elapsed_time = event.position
            daemon.stream_metadata.elapsed_time_last_updated = int(time.time())

        if event.type is BackendEventType.VOLUME and event.volume is not None:
            await self._handle_volume_event(daemon, event.volume)

        # push metadata update to the active queue item's streamdetails
        if daemon.in_use_by_player:
            self.mass.players.update_source_metadata(
                daemon.in_use_by_player,
                daemon.player_id,
                self.instance_id,
                daemon.stream_metadata,
            )

    def _remember_context_uris(self, daemon: _PlayerDaemon, event: BackendEvent) -> None:
        """
        Memoize the latest context/track URIs seen on the event stream.

        Used to take playback back (make MA the active Spotify device) when the user
        switched the active device away in the Spotify app and then presses play in MA
        (see ``on_source_selected``).

        :param daemon: The daemon the event originates from.
        :param event: The backend event to read the URIs from.
        """
        if event.context_uri:
            daemon.last_context_uri = event.context_uri
        if event.track_uri:
            daemon.last_track_uri = event.track_uri

    def _handle_options_changed(self, daemon: _PlayerDaemon, event: BackendEvent) -> None:
        """
        Cache the session's playback options and mirror them onto the consuming queue.

        :param daemon: The daemon the event originates from.
        :param event: The OPTIONS_CHANGED event to handle.
        """
        if event.options is None:
            return
        # cache regardless of claim state: an externally triggered session reports its
        # options before the queue claim exists; on_source_selected pushes the cached
        # value once claimed
        daemon.last_playback_options = event.options
        if not daemon.in_use_by_player:
            return
        self.mass.players.update_source_options(
            daemon.in_use_by_player,
            daemon.player_id,
            self.instance_id,
            shuffle_enabled=event.options.shuffle,
            repeat_mode=event.options.repeat,
        )

    def _apply_metadata(self, daemon: _PlayerDaemon, metadata: BackendTrackMetadata) -> None:
        """Update a daemon's live StreamMetadata from a normalized metadata event."""
        daemon.stream_metadata.uri = metadata.track_uri
        if metadata.title:
            daemon.stream_metadata.title = metadata.title
        daemon.stream_metadata.artist = metadata.artist
        daemon.stream_metadata.album = metadata.album
        daemon.stream_metadata.image_url = metadata.image_url
        daemon.stream_metadata.description = None
        daemon.stream_metadata.duration = metadata.duration
        daemon.stream_metadata.elapsed_time = metadata.position
        daemon.stream_metadata.elapsed_time_last_updated = int(time.time())

    async def _handle_volume_event(self, daemon: _PlayerDaemon, volume: int) -> None:
        """
        Apply a Spotify-side volume change to the linked MA player.

        :param daemon: The daemon the volume change originates from.
        :param volume: The reported volume as a 0-100 percentage.
        """
        # Ignore our own echo: the backend emits a 'volume' event for the value we
        # just pushed in on_volume_change; re-applying it would ping-pong.
        if volume == daemon.last_volume_sent:
            return
        # Ignore the volume the backend reports right after a session becomes
        # active — the player's own volume should win in that window.
        if time.time() - daemon.last_session_active_time < INITIAL_VOLUME_GRACE_S:
            self.logger.debug("Ignoring initial volume_changed event after session active")
            return
        if not daemon.in_use_by_player:
            return
        previous_volume = daemon.last_volume_sent
        daemon.last_volume_sent = volume
        try:
            await self.mass.players.cmd_volume_set(daemon.in_use_by_player, volume)
        except Exception as err:
            # Volume sync is best-effort: the player may not support volume, or the
            # command may fail. Restore the cached value so a retry isn't wrongly
            # deduped, and never let it bubble up and drop the events loop.
            daemon.last_volume_sent = previous_volume
            self.logger.debug("Could not set volume on %s: %s", daemon.in_use_by_player, err)

    async def _sync_player_volume_to_spotify(self, daemon: _PlayerDaemon, player_id: str) -> None:
        """
        Push a player's current volume to the backend (best-effort).

        :param daemon: The daemon to push the volume to.
        :param player_id: The MA player whose volume to push.
        """
        player = self.mass.players.get_player(player_id)
        if player is None or player.state.volume_level is None:
            return
        # clamp: the logical volume can be out of range until volume limit
        # enforcement runs
        volume = max(0, min(100, player.state.volume_level))
        # No dedupe against last_volume_sent here: it holds the last value
        # exchanged with the backend, not the backend's current volume, which
        # resets to its 100% default on a new session or backend restart.
        try:
            await self._push_volume_to_backend(daemon, volume)
        except Exception as err:
            self.logger.debug("Failed to sync player volume to Spotify: %s", err)

    async def _push_volume_to_backend(self, daemon: _PlayerDaemon, volume: int) -> None:
        """
        Send an absolute 0-100 volume to the backend.

        :param daemon: The daemon to send the volume to.
        :param volume: Volume percentage to send.
        :raises Exception: If the request to the backend fails.
        """
        previous_volume = daemon.last_volume_sent
        # Record BEFORE the call: the backend echoes a 'volume' event back, and
        # that echo can arrive over the event stream while we're still awaiting
        # set_volume. Recording up front lets _handle_volume_event dedupe it
        # instead of bouncing it back as a player volume change.
        daemon.last_volume_sent = volume
        try:
            await daemon.backend.set_volume(volume)
        except Exception:
            # restore on failure so a retry of this value isn't wrongly deduped
            daemon.last_volume_sent = previous_volume
            raise
