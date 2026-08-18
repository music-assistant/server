"""
MA-facing provider logic for the Spotify Connect plugin.

The provider owns everything Music Assistant sees: the AudioSource, stream
details, target-player selection, playback claims and volume policy. It is
backend-agnostic: all Spotify specifics live behind the
``SpotifyConnectBackend`` contract and reach the provider as normalized
``BackendEvent``s.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Final, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    MediaType,
    PlaybackState,
    ProviderFeature,
    SourceControl,
)
from music_assistant_models.errors import AudioError, LoginFailed, MediaNotFoundError
from music_assistant_models.media_items import AudioSource, ProviderMapping
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.constants import CONF_ENTRY_WARN_PREVIEW
from music_assistant.models.plugin import PluginProvider

from .backends.go_librespot import GoLibrespotBackend
from .backends.soloist import VOLUME_MODE_PLAYER_ONLY, VOLUME_MODE_SYNC_SPOTIFY, SoloistBackend
from .models import BackendEventType

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant

    from .backends.base import SpotifyConnectBackend
    from .models import BackendEvent, BackendTrackMetadata

CONF_MASS_PLAYER_ID = "mass_player_id"
CONF_PUBLISH_NAME = "publish_name"
DEFAULT_PUBLISH_NAME = "Music Assistant"

# Backend selection, collected by the setup flow (stored in setup_data).
CONF_BACKEND = "backend"
BACKEND_GO_LIBRESPOT = "go_librespot"
BACKEND_SOLOIST = "soloist"

# Soloist-specific values collected by the setup flow (see CONF_BACKEND).
CONF_API_KEY = "soloist_api_key"
CONF_SOLOIST_CONSENT = "soloist_download_consent"
CONF_VOLUME_MODE = "volume_mode"

# The selectable volume modes (labels resolve from strings.json), shared
# between the runtime option and the setup flow.
VOLUME_MODE_OPTIONS: Final = [
    ConfigValueOption(VOLUME_MODE_PLAYER_ONLY),
    ConfigValueOption(VOLUME_MODE_SYNC_SPOTIFY),
]

# Special value for auto player selection
PLAYER_ID_AUTO = "__auto__"

SUPPORTED_FEATURES = {ProviderFeature.AUDIO_SOURCE}

# stable id for the single AudioSource this provider exposes;
# combined with the provider instance_id this forms the persistent uri
AUDIO_SOURCE_ID = "main"

# When playback is paused the backend stops writing PCM. If no PCM arrives for
# this long while we're not in a 'playing' state, end the stream (clean EOF) so
# the player leaves the playing state; the next 'playing' event re-streams.
PAUSE_EOF_TIMEOUT_S = 0.5

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


class SpotifyConnectProvider(PluginProvider):
    """Implementation of a Spotify Connect Plugin (backed by a SpotifyConnectBackend)."""

    reload_on_streams_network_change = True

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
        self._backend: SpotifyConnectBackend = self._create_backend()
        self.logger.debug(
            "Init plugin with name '%s' for player '%s' with instance id '%s'",
            self.name,
            self._default_player_id,
            self.instance_id,
        )
        self._stream_metadata = StreamMetadata(title=f"Spotify Connect | {self._publish_name}")
        self._audio_source = self._build_audio_source()
        # _in_use_by_queue is the queue currently streaming us. Claimed in
        # on_source_selected (NOT in get_stream_details — that path also runs
        # from queue preload, where claiming would block a later cross-queue
        # handoff). Released in on_source_unselected when the session id
        # matches, or in _clear_active_player on the backend's 'inactive' event.
        self._in_use_by_queue: str | None = None
        # _active_session_id is the controller-provided token for the current
        # stream request — used to reject stale on_source_unselected callbacks
        # after a same-queue reconnect supersedes the previous request.
        self._active_session_id: str | None = None
        # tracks the backend's play/pause state from its 'playing' / 'paused' /
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
        # The backend selection and the soloist secrets are managed by the setup
        # flow (stored in setup_data) and stay hidden; the volume mode is a
        # visible runtime option for soloist configs.
        is_soloist = self.get_setup_value(CONF_BACKEND) == BACKEND_SOLOIST
        return (
            CONF_ENTRY_WARN_PREVIEW,
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
                # default to the currently effective mode so the options page
                # shows it until the user overrides it here
                default_value=self._resolve_volume_mode(),
                required=False,
                options=VOLUME_MODE_OPTIONS,
                hidden=not is_soloist,
            ),
        )

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        await self._backend.start()

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
        self._cancel_pending_play_media()
        await self._backend.stop()

    @property
    def active_player_id(self) -> str | None:
        """Return the currently active player ID for this plugin."""
        return self._active_player_id

    async def get_audio_sources(self) -> list[AudioSource]:
        """Return the AudioSources this plugin currently exposes."""
        return [self._audio_source]

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
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
        if item_id != AUDIO_SOURCE_ID:
            raise MediaNotFoundError(f"Unknown AudioSource: {item_id}")
        # Only refuse when we can neither resume nor take playback back. If a last
        # context is known we let the stream proceed; on_source_selected then takes
        # playback back (makes us the active device) before audio is pulled.
        if not self._playing and not self._spotify_session_active and not self._last_context_uri:
            raise self._not_active_error()
        # The backend describes how its audio is consumed: CUSTOM (the core pulls
        # PCM from get_audio_stream) or a named pipe read directly by ffmpeg.
        # decoded_audio_format tells the core the PCM format while audio_format
        # keeps the source codec for display; MA resamples to each player's
        # format as needed.
        # expiration=0: never reuse a cached streamdetails so the active-device
        # check above re-runs on every play attempt.
        stream_source = await self._backend.get_stream_source()
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=self._backend.audio_format,
            decoded_audio_format=self._backend.decoded_audio_format,
            media_type=MediaType.AUDIO_SOURCE,
            stream_type=stream_source.stream_type,
            path=stream_source.path,
            stream_metadata=self._stream_metadata,
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
        if streamdetails.item_id != AUDIO_SOURCE_ID:
            raise MediaNotFoundError(f"Unknown AudioSource: {streamdetails.item_id}")
        read_chunk = self._backend.get_audio_reader()
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
                if not self._playing:
                    return
                continue
            if not chunk:
                return  # audio pipe closed (backend exited / restarting)
            yield chunk

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

        # Externally triggered: the backend is already playing → nothing to do.
        # Otherwise acquire playback, then confirm it actually started.
        if not self._playing:
            try:
                if self._spotify_session_active:
                    # Still the active Spotify device (just paused) → resume.
                    await self._backend.resume()
                elif self._last_context_uri:
                    # The user moved the active device away in the Spotify app.
                    # Take playback back by (re)starting the last context on us,
                    # which makes this device the active one again. The track
                    # restarts from its beginning (there is no resume-at-position
                    # play call).
                    self.logger.info("Taking Spotify playback back to Music Assistant")
                    await self._backend.play(
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

        # The backend reports 100% volume until told otherwise; push the player's
        # volume so the Spotify app's absolute volume commands start from the
        # real level.
        await self._sync_player_volume_to_spotify(active_player_id)

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
        """Proxy playback control commands to the backend."""
        if source_id != AUDIO_SOURCE_ID:
            return
        if not self._playing and not self._spotify_session_active:
            raise self._not_active_error()
        try:
            if action == SourceControl.PLAY:
                await self._backend.resume()
            elif action == SourceControl.PAUSE:
                await self._backend.pause()
            elif action == SourceControl.NEXT:
                await self._backend.next()
            elif action == SourceControl.PREVIOUS:
                await self._backend.previous()
            elif action == SourceControl.SEEK and value is not None:
                await self._backend.seek(value * 1000)
        except Exception as err:
            self.logger.warning("Failed to send %s command to backend: %s", action, err)
            raise

    async def on_volume_change(self, source_id: str, volume: int) -> None:
        """Sync the Spotify app's volume slider with the player's new volume."""
        if source_id != AUDIO_SOURCE_ID:
            return
        if not self._playing and not self._spotify_session_active:
            raise self._not_active_error()
        # Prevent ping-pong: only push if the value actually changed from what we
        # last sent to / received from the backend.
        if self._last_volume_sent == volume:
            return
        try:
            await self._push_volume_to_backend(volume)
        except Exception as err:
            self.logger.warning("Failed to send volume command to backend: %s", err)
            raise

    def _create_backend(self) -> SpotifyConnectBackend:
        """Construct the configured Spotify Connect backend implementation."""
        # The backend choice and soloist secrets are collected by the setup flow
        # into setup_data; a config migrated from before the backend choice
        # existed yields None here, which intentionally selects go-librespot
        # (the equality check must keep treating None as the default).
        if self.get_setup_value(CONF_BACKEND) == BACKEND_SOLOIST:
            return SoloistBackend(
                self.mass,
                instance_id=self.instance_id,
                publish_name=self._publish_name,
                name=self.name,
                logger=self.logger,
                event_callback=self._handle_backend_event,
                api_key=cast("str", self.get_setup_value(CONF_API_KEY) or ""),
                consent=bool(self.get_setup_value(CONF_SOLOIST_CONSENT)),
                volume_mode=self._resolve_volume_mode(),
            )
        return GoLibrespotBackend(
            self.mass,
            instance_id=self.instance_id,
            publish_name=self._publish_name,
            name=self.name,
            logger=self.logger,
            event_callback=self._handle_backend_event,
        )

    def _resolve_volume_mode(self) -> str:
        """Return the effective volume mode: the visible option wins over the setup choice."""
        return cast(
            "str",
            self.config.get_value(CONF_VOLUME_MODE)
            or self.get_setup_value(CONF_VOLUME_MODE)
            or VOLUME_MODE_PLAYER_ONLY,
        )

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

        Backends provide a full control surface, so play / pause / seek /
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
                    audio_format=self._backend.audio_format,
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
        Wait up to ``timeout`` seconds for the backend to report it is playing.

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

    async def _stop_paused_player(self, player_id: str) -> None:
        """
        Stop the active player after a pause on a backend without stream EOF.

        :param player_id: The player currently consuming the live source.
        """
        try:
            await self.mass.players.cmd_stop(player_id)
        except Exception as err:
            self.logger.debug("Failed to stop player %s on pause: %s", player_id, err)

    def _cancel_pending_play_media(self) -> None:
        """Cancel any pending deferred play_media trigger."""
        task = self._pending_play_media_task
        if task is not None and not task.done():
            task.cancel()
        self._pending_play_media_task = None

    async def _deferred_play_media_fire(self) -> None:
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

    async def _handle_backend_event(self, event: BackendEvent) -> None:
        """Dispatch a single normalized event received from the backend."""
        if event.type is BackendEventType.CONNECTION_LOST:
            # The backend's Spotify session is gone (e.g. daemon exit). Reset
            # session state so a dead/restarting backend isn't treated as active
            # and controllable; a fresh 'active' event re-establishes it.
            self._playing = False
            self._spotify_session_active = False
            return
        if event.type is BackendEventType.FATAL_ERROR:
            self.unload_with_error(event.error or "Spotify Connect backend failed")
            return
        if event.type is BackendEventType.ERROR:
            # non-fatal backend error: surface it in the log only
            self.logger.warning("Spotify Connect backend error: %s", event.error)
            return
        if event.type is BackendEventType.AUTH_REQUIRED:
            # the backend lost its Spotify login mid-session: stop treating the
            # device as active and unload with an auth error so the UI flags
            # the provider and routes the user through the setup flow
            self._playing = False
            self._spotify_session_active = False
            self.logger.warning(
                "Spotify Connect backend for %s requires (re)authentication", self.name
            )
            self.unload_with_error(
                LoginFailed(
                    "Spotify authentication required",
                    translation_key="soloist_auth_required",
                    translation_owner=self.translation_owner,
                )
            )
            return

        # Remember the latest context/track so we can take playback back if the
        # user moves the active device away in the Spotify app (see on_source_selected).
        if event.context_uri:
            self._last_context_uri = event.context_uri
        if event.track_uri:
            self._last_track_uri = event.track_uri

        if event.type is BackendEventType.SESSION_ACTIVE:
            self._spotify_session_active = True
            self._last_session_active_time = time.time()
            # A (re)activation supersedes any deferred play_media scheduled from a
            # previous session's stale 'playing'; the fresh 'playing' that follows
            # schedules a new one.
            self._cancel_pending_play_media()
            self.logger.info("Spotify Connect session active for %s", self.name)
            # A new session starts at the backend's 100% volume default; push the
            # target player's volume so the Spotify app's slider is correct from
            # device selection, before any playback starts. (In the soloist
            # player_only mode the backend pins 100% and ignores the pushed
            # value — the app slider staying at 100 there is by design.)
            if player_id := self._get_target_player_id():
                await self._sync_player_volume_to_spotify(player_id)
        elif event.type is BackendEventType.SESSION_INACTIVE:
            self.logger.info("Spotify Connect session inactive for %s", self.name)
            self._spotify_session_active = False
            prev_player_id = self._active_player_id
            self._clear_active_player()
            if prev_player_id:
                self.mass.create_task(self.mass.players.cmd_stop(prev_player_id))
            return
        elif event.type is BackendEventType.PLAYING:
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
        elif event.type in (BackendEventType.PAUSED, BackendEventType.STOPPED):
            self._playing = False
            # A pause/stop is the definitive "don't start": cancel a deferred fire
            # from a now-stale 'playing'. The active get_audio_stream sees the PCM
            # stop and ends the stream (clean EOF), so the player leaves the playing
            # state; the next 'playing' event re-fires play_media to resume.
            self._cancel_pending_play_media()
            # A pipe-fed backend keeps delivering silence on pause (no EOF), so
            # the player must be stopped actively; the claim stays so the next
            # 'playing' event resumes playback like the EOF path does.
            if not self._backend.stream_ends_on_pause and (player_id := self._active_player_id):
                self.mass.create_task(self._stop_paused_player(player_id))

        if event.type is BackendEventType.METADATA and event.metadata is not None:
            self._apply_metadata(event.metadata)
        elif event.type is BackendEventType.POSITION and event.position is not None:
            self._stream_metadata.elapsed_time = event.position
            self._stream_metadata.elapsed_time_last_updated = int(time.time())

        if event.type is BackendEventType.VOLUME and event.volume is not None:
            await self._handle_volume_event(event.volume)

        # push metadata update to the active queue item's streamdetails
        if self._in_use_by_queue:
            self.mass.streams.update_stream_metadata(
                self._in_use_by_queue,
                AUDIO_SOURCE_ID,
                self.instance_id,
                self._stream_metadata,
            )

    def _apply_metadata(self, metadata: BackendTrackMetadata) -> None:
        """Update the live StreamMetadata from a normalized metadata event."""
        self._stream_metadata.uri = metadata.track_uri
        if metadata.title:
            self._stream_metadata.title = metadata.title
        self._stream_metadata.artist = metadata.artist
        self._stream_metadata.album = metadata.album
        self._stream_metadata.image_url = metadata.image_url
        self._stream_metadata.description = None
        self._stream_metadata.duration = metadata.duration
        self._stream_metadata.elapsed_time = metadata.position
        self._stream_metadata.elapsed_time_last_updated = int(time.time())

    async def _handle_volume_event(self, volume: int) -> None:
        """
        Apply a Spotify-side volume change to the linked MA player.

        :param volume: The reported volume as a 0-100 percentage.
        """
        # Ignore our own echo: the backend emits a 'volume' event for the value we
        # just pushed in on_volume_change; re-applying it would ping-pong.
        if volume == self._last_volume_sent:
            return
        # Ignore the volume the backend reports right after a session becomes
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

    async def _sync_player_volume_to_spotify(self, player_id: str) -> None:
        """
        Push a player's current volume to the backend (best-effort).

        :param player_id: The MA player whose volume to push.
        """
        player = self.mass.players.get_player(player_id)
        if player is None or player.state.volume_level is None:
            return
        # clamp: the logical volume can be out of range until volume limit
        # enforcement runs
        volume = max(0, min(100, player.state.volume_level))
        # No dedupe against _last_volume_sent here: it holds the last value
        # exchanged with the backend, not the backend's current volume, which
        # resets to its 100% default on a new session or backend restart.
        try:
            await self._push_volume_to_backend(volume)
        except Exception as err:
            self.logger.debug("Failed to sync player volume to Spotify: %s", err)

    async def _push_volume_to_backend(self, volume: int) -> None:
        """
        Send an absolute 0-100 volume to the backend.

        :param volume: Volume percentage to send.
        :raises Exception: If the request to the backend fails.
        """
        previous_volume = self._last_volume_sent
        # Record BEFORE the call: the backend echoes a 'volume' event back, and
        # that echo can arrive over the event stream while we're still awaiting
        # set_volume. Recording up front lets _handle_volume_event dedupe it
        # instead of bouncing it back as a player volume change.
        self._last_volume_sent = volume
        try:
            await self._backend.set_volume(volume)
        except Exception:
            # restore on failure so a retry of this value isn't wrongly deduped
            self._last_volume_sent = previous_volume
            raise
