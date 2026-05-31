"""
Spotify Connect plugin for Music Assistant.

We tie a single player to a single Spotify Connect daemon.
The provider has multi instance support,
so multiple players can be linked to multiple Spotify Connect daemons.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import time
from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING, cast

from aiohttp.web import Response
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    EventType,
    MediaType,
    PlaybackState,
    ProviderFeature,
    ProviderType,
    SourceControl,
    StreamType,
)
from music_assistant_models.errors import (
    AudioError,
    MediaNotFoundError,
    UnsupportedFeaturedException,
)
from music_assistant_models.media_items import AudioFormat, AudioSource, ProviderMapping
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.constants import CONF_ENTRY_WARN_PREVIEW
from music_assistant.helpers.process import AsyncProcess, check_output
from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.spotify.helpers import get_librespot_binary

if TYPE_CHECKING:
    from aiohttp.web import Request
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.event import MassEvent
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType
    from music_assistant.providers.spotify.provider import SpotifyProvider

CONF_MASS_PLAYER_ID = "mass_player_id"
CONF_PUBLISH_NAME = "publish_name"

# Special value for auto player selection
PLAYER_ID_AUTO = "__auto__"

EVENTS_SCRIPT = pathlib.Path(__file__).parent.resolve().joinpath("events.py")

SUPPORTED_FEATURES = {ProviderFeature.AUDIO_SOURCE}

# stable id for the single AudioSource this provider exposes;
# combined with the provider instance_id this forms the persistent uri
AUDIO_SOURCE_ID = "main"

# Max seconds to wait for librespot's 'playing' event after asking Spotify to
# play: the Web API returns 200 even when playback never actually starts.
PLAYBACK_START_TIMEOUT_S = 3.0

# How long to wait after session_connected for a definitive 'playing' or
# 'paused' event before assuming librespot is wedged (typically blocked on
# a pipe write because no consumer is reading the FIFO yet).
SESSION_STALL_TIMEOUT_S = 8.0

# How long the emergency drain reads the FIFO once a stall is detected.
SESSION_STALL_DRAIN_TIMEOUT_S = 2.0

# User-facing message for the "not the active Spotify device" failure.
NOT_ACTIVE_DEVICE_MESSAGE = (
    "Music Assistant is not the active Spotify playback device. "
    "Open the Spotify app, pick Music Assistant as the playback device, "
    "and try again."
)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return SpotifyConnectProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    :param instance_id: id of an existing provider instance (None if new instance setup).
    :param action: [optional] action key called from config entries UI.
    :param values: the (intermediate) raw values for config entries sent with the action.
    """
    return (
        CONF_ENTRY_WARN_PREVIEW,
        ConfigEntry(
            key=CONF_MASS_PLAYER_ID,
            type=ConfigEntryType.STRING,
            label="Connected Music Assistant Player",
            description="The Music Assistant player connected to this Spotify Connect plugin. "
            "When you start playback in the Spotify app to this virtual speaker, "
            "the audio will play on the selected player. "
            "Set to 'Auto' to automatically select a currently playing player, "
            "or the first available player if none is playing.",
            multi_value=False,
            default_value=PLAYER_ID_AUTO,
            options=[
                ConfigValueOption("Auto (prefer playing player)", PLAYER_ID_AUTO),
                *(
                    ConfigValueOption(x.display_name, x.player_id)
                    for x in sorted(
                        mass.players.all_players(False, False), key=lambda p: p.display_name.lower()
                    )
                ),
            ],
            required=True,
        ),
        ConfigEntry(
            key=CONF_PUBLISH_NAME,
            type=ConfigEntryType.STRING,
            label="Name to display in the Spotify app",
            description="How should this Spotify Connect device be named in the Spotify app?",
            default_value="Music Assistant",
        ),
    )


class SpotifyConnectProvider(PluginProvider):
    """Implementation of a Spotify Connect Plugin."""

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize MusicProvider."""
        super().__init__(mass, manifest, config, SUPPORTED_FEATURES)
        # Default player ID from config (PLAYER_ID_AUTO or a specific player_id)
        self._default_player_id: str = (
            cast("str", self.config.get_value(CONF_MASS_PLAYER_ID)) or PLAYER_ID_AUTO
        )
        # Currently active player (the one currently playing or selected)
        self._active_player_id: str | None = None
        self.cache_dir = os.path.join(self.mass.cache_path, self.instance_id)
        self._librespot_bin: str | None = None
        self._stop_called: bool = False
        self._runner_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._librespot_proc: AsyncProcess | None = None
        self._librespot_started = asyncio.Event()
        self.named_pipe = f"/tmp/{self.instance_id}"  # noqa: S108
        connect_name = cast("str", self.config.get_value(CONF_PUBLISH_NAME)) or self.name
        self.logger.debug(
            "Init plugin with name '%s' for player '%s' with instance id '%s'",
            self.name,
            self._default_player_id,
            self.instance_id,
        )
        # _audio_format describes the original Spotify source (Ogg Vorbis 320
        # kbps, as requested via librespot's --bitrate flag) and is what we
        # advertise to clients for source-format display.
        self._audio_format = AudioFormat(
            content_type=ContentType.OGG,
            codec_type=ContentType.VORBIS,
            sample_rate=44100,
            bit_depth=16,
            channels=2,
            bit_rate=320,
        )
        # _decoded_audio_format is what librespot actually pipes into MA after
        # decoding the Ogg Vorbis stream; the streams controller hands this to
        # ffmpeg as the input format so it can read the FIFO correctly.
        self._decoded_audio_format = AudioFormat(
            content_type=ContentType.PCM_S16LE,
            codec_type=ContentType.PCM_S16LE,
            sample_rate=44100,
            bit_depth=16,
            channels=2,
        )
        self._stream_metadata = StreamMetadata(title=f"Spotify Connect | {connect_name}")
        # Web API integration for playback control - must come before _build_audio_source
        # because the AudioSource's capability flags depend on whether Web API is available.
        self._connected_spotify_username: str | None = None
        self._spotify_provider: SpotifyProvider | None = None
        # Playback control capabilities flip True once a matching Spotify music
        # provider becomes available (Web API for play/pause/seek/next/prev).
        # _build_audio_source refreshes the cached AudioSource so the next
        # get_audio_sources call returns the updated capability flags.
        self._audio_source = self._build_audio_source()
        # _in_use_by_queue is the queue currently streaming us. Claimed in
        # on_source_selected (NOT in get_stream_details — that path also runs
        # from queue preload, where claiming would block a later cross-queue
        # handoff). Released in on_source_unselected when the session id
        # matches, or in _clear_active_player on Spotify session_disconnected.
        self._in_use_by_queue: str | None = None
        # _active_session_id is the controller-provided token for the current
        # stream request — used to reject stale on_source_unselected callbacks
        # after a same-queue reconnect supersedes the previous request.
        self._active_session_id: str | None = None
        # tracks librespot's play/pause state from its 'playing' / 'paused' /
        # session_disconnected events; gates the Web API kick in on_source_selected
        # (skip if already playing) and the play_media trigger in the event handler
        self._librespot_playing: bool = False
        # True while MA is the active Spotify Connect device (set/cleared on the
        # session connect/disconnect events); gates get_stream_details.
        self._spotify_session_active: bool = False
        # holds the single in-flight deferred play_media task scheduled from a
        # librespot 'playing' event; cancelled if a 'paused' or 'session_connected'
        # arrives during the debounce so we don't act on stale state from a dying
        # session and end up in a play→pause→reconnect loop
        self._pending_play_media_task: asyncio.Task[None] | None = None
        self._on_unload_callbacks: list[Callable[..., None]] = []
        self._runner_error_count = 0
        self._spotify_device_id: str | None = None
        self._last_session_connected_time: float = 0
        self._last_volume_sent_to_spotify: int | None = None
        # Armed on session_connected, cancelled on the first definitive
        # state event ('playing' / 'paused' / 'session_disconnected'). If
        # neither lands within SESSION_STALL_TIMEOUT_S, the watchdog drains
        # the FIFO to unblock a likely pipe-write deadlock in librespot.
        self._session_watchdog: asyncio.Task[None] | None = None

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._librespot_bin = await get_librespot_binary()
        # Always start the daemon - we always have a default player configured
        self._setup_player_daemon()

        # Subscribe to events
        self._on_unload_callbacks.append(
            self.mass.subscribe(
                self._on_provider_event,
                (EventType.PROVIDERS_UPDATED),
            )
        )
        self._on_unload_callbacks.append(
            self.mass.streams.register_dynamic_route(
                f"/{self.instance_id}",
                self._handle_custom_webservice,
            )
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
        self._stop_called = True
        self._cancel_pending_play_media()
        self._cancel_session_watchdog()
        if self._runner_task and not self._runner_task.done():
            self._runner_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._runner_task
        for callback in self._on_unload_callbacks:
            callback()

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
        request). Keeping this idempotent means preload paths like
        player_queues._load_item can fetch streamdetails without claiming the
        source and blocking a subsequent cross-queue handoff.

        Raises AudioError when MA has no way to acquire the source (librespot
        idle and no Spotify music provider for Web API control, or MA is no
        longer the active Spotify Connect device).
        """
        if source_id != AUDIO_SOURCE_ID:
            raise MediaNotFoundError(f"Unknown AudioSource: {source_id}")
        if not self._librespot_playing and not self._spotify_provider:
            raise AudioError(
                "Spotify Connect cannot be acquired from Music Assistant — "
                "start playback from the Spotify app, or configure the matching "
                "Spotify music provider to enable Web API control"
            )
        # Fail clearly when MA is no longer the active Spotify device: the Web
        # API would accept a play call but never actually start playback on us.
        if not self._librespot_playing and not self._spotify_session_active:
            raise AudioError(NOT_ACTIVE_DEVICE_MESSAGE)
        # NAMED_PIPE (not CUSTOM): the core opens the FIFO with ffmpeg directly
        # using `-re`, which paces the read at native rate. Going through a
        # Python generator + StreamReader would let librespot's pipe backend
        # (which is not realtime-paced) fill an arbitrary-size buffer ahead of
        # us; pause/skip would then take seconds to react.
        # expiration=0: never reuse a cached streamdetails so the active-device
        # check above re-runs on every play attempt.
        return StreamDetails(
            provider=self.instance_id,
            item_id=source_id,
            audio_format=self._audio_format,
            decoded_audio_format=self._decoded_audio_format,
            media_type=MediaType.AUDIO_SOURCE,
            stream_type=StreamType.NAMED_PIPE,
            path=self.named_pipe,
            stream_metadata=self._stream_metadata,
            expiration=0,
        )

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
        # (e.g. Sendspin's spb_… bridges that tear down between streams) — their
        # ID is invalid for play_media / queue lookups once the bridge is gone.
        active_player_id = queue_id

        # If there's already an active player and it's different, kick it out.
        # The lock claim a few lines below replaces the previous queue's claim;
        # the prior stream's on_source_unselected may fire later, but its
        # session-id guard keeps it from clobbering the new claim.
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

        # Claim ownership for this queue. The lock lives here (not in
        # get_stream_details) so preload paths can fetch streamdetails without
        # accidentally blocking a subsequent cross-queue handoff at the actual
        # stream request.
        self._in_use_by_queue = queue_id
        # Record this request's session id so a later on_source_unselected can
        # tell whether it is the live teardown or a stale callback from a
        # superseded same-queue request.
        self._active_session_id = stream_session_id

        # Update the active player
        self._active_player_id = active_player_id
        self.logger.debug("Active player set to: %s", active_player_id)

        # Only persist the selected player as the new default if not in auto mode
        if self._default_player_id != PLAYER_ID_AUTO:
            self._save_last_player_id(active_player_id)

        # MA-initiated: librespot is idle; kick Spotify via Web API.
        # Externally triggered: librespot is already playing → skip.
        if not self._librespot_playing:
            if not self._spotify_provider:
                raise AudioError(
                    "Spotify Connect requires the matching Spotify music provider "
                    "for MA-initiated playback"
                )
            try:
                await self._ensure_active_device(play=True)
            except Exception as err:
                raise AudioError(f"Failed to acquire Spotify Connect via Web API: {err}") from err
            # The Web API returns 200 even when Spotify won't actually start
            # playing on us; confirm via librespot before reporting success.
            if not await self._wait_for_librespot_playing():
                raise AudioError(NOT_ACTIVE_DEVICE_MESSAGE)

    async def on_source_unselected(
        self, source_id: str, queue_id: str, stream_session_id: str
    ) -> None:
        """Release the queue-scoped exclusive claim when MA tears down the stream."""
        if source_id != AUDIO_SOURCE_ID:
            return
        # Reject stale callbacks: only release if this is still the active
        # session. A queue_id check alone is not sufficient — same-queue
        # reconnects (player drops + reopens the same stream URL before the
        # original request's finally fires) would otherwise let the old
        # request's late callback clear the live claim of the new stream.
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
        """Proxy playback control commands to Spotify via the Web API."""
        if source_id != AUDIO_SOURCE_ID:
            return
        # Without an active Spotify session the Web API answers 403 to every
        # transport command; fail fast with the user-facing message instead.
        if not self._librespot_playing and not self._spotify_session_active:
            raise AudioError(NOT_ACTIVE_DEVICE_MESSAGE)
        if action == SourceControl.PLAY:
            await self._on_play()
        elif action == SourceControl.PAUSE:
            await self._on_pause()
        elif action == SourceControl.NEXT:
            await self._on_next()
        elif action == SourceControl.PREVIOUS:
            await self._on_previous()
        elif action == SourceControl.SEEK and value is not None:
            await self._on_seek(value)

    async def on_volume_change(self, source_id: str, volume: int) -> None:
        """Sync the Spotify app's volume slider with the player's new volume."""
        if source_id != AUDIO_SOURCE_ID:
            return
        if not self._librespot_playing and not self._spotify_session_active:
            raise AudioError(NOT_ACTIVE_DEVICE_MESSAGE)
        await self._on_volume(volume)

    def _build_audio_source(self) -> AudioSource:
        """Construct the AudioSource MediaItem with current capability flags."""
        has_web_api = self._spotify_provider is not None
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
            can_play_pause=has_web_api,
            can_seek=has_web_api,
            can_next_previous=has_web_api,
            exclusive=True,
            allow_external_trigger=True,
            # Web API can't reliably cold-start playback (it needs an existing
            # Spotify context), so only allow external entry via the Spotify app.
            can_initiate=False,
        )

    def _get_target_player_id(self) -> str | None:
        """
        Determine the target player ID for playback.

        Returns the player ID to use based on the following priority:
        1. If a player was explicitly selected (source selected on a player), use that
        2. If default is 'auto': prefer playing player, then first available
        3. If a specific default player is configured, use that

        :return: The player ID to use for playback, or None if no player available.
        """
        # If there's an active player (source was selected on a player), use it
        if self._active_player_id:
            # Validate that the active player still exists
            if self.mass.players.get_player(self._active_player_id):
                return self._active_player_id
            # Active player no longer exists, clear it
            self._active_player_id = None

        # Handle auto selection
        if self._default_player_id == PLAYER_ID_AUTO:
            all_players = list(self.mass.players.all_players(False, False))
            # First, try to find a playing player
            for player in all_players:
                if player.state.playback_state == PlaybackState.PLAYING:
                    self.logger.debug("Auto-selecting playing player: %s", player.display_name)
                    return player.player_id
            # Fallback to first available player
            if all_players:
                first_player = all_players[0]
                self.logger.debug(
                    "Auto-selecting first available player: %s", first_player.display_name
                )
                return first_player.player_id
            # No player available
            return None

        # Use the specific default player if configured and it still exists
        if self.mass.players.get_player(self._default_player_id):
            return self._default_player_id
        self.logger.warning(
            "Configured default player '%s' no longer exists", self._default_player_id
        )
        return None

    def _cancel_pending_play_media(self) -> None:
        """Cancel any pending deferred play_media trigger."""
        task = self._pending_play_media_task
        if task is not None and not task.done():
            task.cancel()
        self._pending_play_media_task = None

    def _arm_session_watchdog(self) -> None:
        """Arm the post-session_connected stall watchdog."""
        self._cancel_session_watchdog()
        self._session_watchdog = self.mass.create_task(self._session_watchdog_body())

    def _cancel_session_watchdog(self) -> None:
        """Cancel the stall watchdog."""
        task = self._session_watchdog
        if task is not None and not task.done():
            task.cancel()
        self._session_watchdog = None

    async def _session_watchdog_body(self) -> None:
        """
        Recover librespot when a session_connected isn't followed by a state event.

        The typical cause is a pipe-write deadlock: librespot starts producing
        audio, the FIFO has no consumer yet, the kernel pipe buffer fills, the
        next write blocks, and the main loop is stuck — so no 'playing' or
        'paused' event ever fires. Draining the FIFO unblocks the write; the
        normal flow then resumes on its own.
        """
        try:
            await asyncio.sleep(SESSION_STALL_TIMEOUT_S)
        except asyncio.CancelledError:
            return
        if self._librespot_playing or self._in_use_by_queue:
            return
        self.logger.warning(
            "Spotify Connect session connected but no playing/paused event "
            "within %ss — draining FIFO to unblock librespot",
            SESSION_STALL_TIMEOUT_S,
        )
        await self._emergency_drain_fifo()

    async def _emergency_drain_fifo(self) -> None:
        """Open the FIFO and read+discard briefly to unblock librespot."""
        try:
            fd = os.open(self.named_pipe, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError as err:
            self.logger.debug("Emergency drain: cannot open FIFO: %s", err)
            return
        drained = 0
        deadline = self.mass.loop.time() + SESSION_STALL_DRAIN_TIMEOUT_S
        try:
            while self.mass.loop.time() < deadline:
                # Bail out as soon as librespot recovers or the normal stream
                # pipeline is about to attach; closing our fd before ffmpeg
                # opens avoids two readers splitting the audio bytes.
                if self._librespot_playing or self._in_use_by_queue:
                    break
                try:
                    data = os.read(fd, 65536)
                except BlockingIOError:
                    await asyncio.sleep(0.01)
                    continue
                except OSError:
                    break
                if data == b"":
                    await asyncio.sleep(0.01)
                else:
                    drained += len(data)
        finally:
            with suppress(Exception):
                os.close(fd)
        self.logger.info("Emergency FIFO drain complete: %d bytes", drained)

    async def _deferred_play_media_fire(self) -> None:
        """
        Trigger play_media after a short debounce.

        librespot can emit a stale 'playing' event from a dying session moments
        before reconnecting; firing play_media synchronously on that event lands
        our stream on a pipe that's about to lose its writer. Waiting briefly
        and aborting on a 'paused' or 'session_connected' event in the meantime
        avoids the restart loop.
        """
        try:
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            return
        if not self._librespot_playing or self._in_use_by_queue:
            return
        if not self._connected_spotify_username or not self._spotify_provider:
            await self._check_spotify_provider_match()
        # ensure we're the active Spotify device (no play override — librespot
        # is already playing, we don't want to disrupt it)
        if self._spotify_provider:
            self.mass.create_task(self._ensure_active_device())
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
        """
        Clear the active player and revert to default if configured.

        Called when playback ends to reset the plugin state.
        """
        prev_player_id = self._active_player_id
        self._active_player_id = None
        self._in_use_by_queue = None
        self._active_session_id = None
        self._librespot_playing = False

        if prev_player_id:
            self.logger.debug("Playback ended on player %s, clearing active player", prev_player_id)
            # Trigger update for the player that was using this source
            self.mass.players.trigger_player_update(prev_player_id)

    def _save_last_player_id(self, player_id: str) -> None:
        """Persist the selected player ID to config as the new default."""
        if self._default_player_id == player_id:
            return  # No change needed
        try:
            self.mass.config.set_raw_provider_config_value(
                self.instance_id, CONF_MASS_PLAYER_ID, player_id
            )
            self._default_player_id = player_id
        except Exception as err:
            self.logger.debug("Failed to persist player ID: %s", err)

    async def _check_spotify_provider_match(self) -> None:
        """Check if a Spotify music provider is available with matching username."""
        # Username must be available (set from librespot output)
        if not self._connected_spotify_username:
            return

        # Look for a Spotify music provider with matching username
        for provider in self.mass.get_providers():
            if provider.domain == "spotify" and provider.type == ProviderType.MUSIC:
                # Check if the username matches
                if hasattr(provider, "_sp_user") and provider._sp_user:
                    spotify_username = provider._sp_user.get("id")
                    if spotify_username == self._connected_spotify_username:
                        self.logger.debug(
                            "Found matching Spotify music provider - "
                            "enabling playback control via Web API"
                        )
                        self._spotify_provider = cast("SpotifyProvider", provider)
                        self._update_source_capabilities()
                        return

        # No matching provider found
        if self._spotify_provider is not None:
            self.logger.debug(
                "Spotify music provider no longer available - disabling playback control"
            )
            self._spotify_provider = None
            self._update_source_capabilities()

    def _update_source_capabilities(self) -> None:
        """Rebuild the AudioSource so capability flags reflect Web API availability."""
        self._audio_source = self._build_audio_source()
        # The currently playing queue item carries a SNAPSHOT of the old
        # AudioSource — overwrite it so the new capability flags reach the UI
        # without waiting for the next play_media. Snapshot current_item and
        # re-check identity before the write so a queue advance racing this
        # callback can't stamp the new AudioSource onto an item that has
        # already moved on. Signal the queue update so the frontend re-renders
        # the controls (play/pause, next/prev) live.
        if not self._in_use_by_queue:
            return
        queue_id = self._in_use_by_queue
        queue = self.mass.player_queues.get(queue_id)
        if queue is None:
            return
        current_item = queue.current_item
        if (
            current_item is not None
            and current_item.media_item is not None
            and current_item.media_item.media_type == MediaType.AUDIO_SOURCE
            and current_item.media_item.item_id == AUDIO_SOURCE_ID
            and current_item.media_item.provider == self.instance_id
            and queue.current_item is current_item
        ):
            current_item.media_item = self._audio_source
            self.mass.player_queues.signal_update(queue_id, items_changed=True)
        self.mass.players.trigger_player_update(queue_id)

    async def _on_play(self) -> None:
        """Handle play command via Spotify Web API."""
        if not self._spotify_provider:
            raise UnsupportedFeaturedException(
                "Playback control requires a matching Spotify music provider"
            )
        try:
            await self._ensure_active_device(play=True)
        except Exception as err:
            self.logger.warning("Failed to send play command via Spotify Web API: %s", err)
            raise
        # 200 OK doesn't guarantee playback actually started — confirm.
        if not await self._wait_for_librespot_playing():
            raise AudioError(NOT_ACTIVE_DEVICE_MESSAGE)

    async def _on_pause(self) -> None:
        """Handle pause command via Spotify Web API."""
        if not self._spotify_provider:
            raise UnsupportedFeaturedException(
                "Playback control requires a matching Spotify music provider"
            )
        try:
            await self._spotify_provider._put_data("me/player/pause")
        except Exception as err:
            self.logger.warning("Failed to send pause command via Spotify Web API: %s", err)
            raise

    async def _on_next(self) -> None:
        """Handle next track command via Spotify Web API."""
        if not self._spotify_provider:
            raise UnsupportedFeaturedException(
                "Playback control requires a matching Spotify music provider"
            )
        try:
            await self._spotify_provider._post_data("me/player/next", want_result=False)
        except Exception as err:
            self.logger.warning("Failed to send next track command via Spotify Web API: %s", err)
            raise

    async def _on_previous(self) -> None:
        """Handle previous track command via Spotify Web API."""
        if not self._spotify_provider:
            raise UnsupportedFeaturedException(
                "Playback control requires a matching Spotify music provider"
            )
        try:
            await self._spotify_provider._post_data("me/player/previous")
        except Exception as err:
            self.logger.warning("Failed to send previous command via Spotify Web API: %s", err)
            raise

    async def _on_seek(self, position: int) -> None:
        """Handle seek command via Spotify Web API."""
        if not self._spotify_provider:
            raise UnsupportedFeaturedException(
                "Playback control requires a matching Spotify music provider"
            )
        try:
            # Spotify Web API expects position in milliseconds
            position_ms = position * 1000
            await self._spotify_provider._put_data(f"me/player/seek?position_ms={position_ms}")
        except Exception as err:
            self.logger.warning("Failed to send seek command via Spotify Web API: %s", err)
            raise

    async def _on_volume(self, volume: int) -> None:
        """
        Handle volume change command via Spotify Web API.

        :param volume: Volume level (0-100) from Music Assistant.
        """
        if not self._spotify_provider:
            raise UnsupportedFeaturedException(
                "Volume control requires a matching Spotify music provider"
            )

        # Prevent ping-pong: only send if volume actually changed from what we last sent
        if self._last_volume_sent_to_spotify == volume:
            self.logger.debug("Skipping volume update to Spotify - already at %d%%", volume)
            return

        try:
            # Bypass throttler for volume changes to ensure responsive UI
            async with self._spotify_provider.throttler.bypass():
                await self._spotify_provider._put_data(f"me/player/volume?volume_percent={volume}")
                self._last_volume_sent_to_spotify = volume
        except Exception as err:
            self.logger.warning("Failed to send volume command via Spotify Web API: %s", err)
            raise

    async def _get_spotify_device_id(self) -> str | None:
        """
        Get the Spotify Connect device ID for this instance.

        :return: Device ID if found, None otherwise.
        """
        if not self._spotify_provider:
            return None

        try:
            # Get list of available devices from Spotify Web API
            devices_data = await self._spotify_provider._get_data("me/player/devices")
            devices = devices_data.get("devices", [])

            # Look for our device by name
            connect_name = cast("str", self.config.get_value(CONF_PUBLISH_NAME)) or self.name
            for device in devices:
                if device.get("name") == connect_name and device.get("type") == "Speaker":
                    device_id: str | None = device.get("id")
                    self.logger.debug("Found Spotify Connect device ID: %s", device_id)
                    return device_id

            self.logger.debug(
                "Could not find Spotify Connect device '%s' in available devices", connect_name
            )
            return None
        except Exception as err:
            self.logger.debug("Failed to get Spotify devices: %s", err)
            return None

    async def _wait_for_librespot_playing(self, timeout: float = PLAYBACK_START_TIMEOUT_S) -> bool:
        """
        Wait up to ``timeout`` seconds for librespot to report it is playing.

        :param timeout: Maximum seconds to wait.
        :return: True once playback is confirmed, False if the timeout elapses.
        """
        deadline = self.mass.loop.time() + timeout
        while True:
            if self._librespot_playing:
                return True
            if self.mass.loop.time() >= deadline:
                return False
            await asyncio.sleep(0.1)

    async def _ensure_active_device(self, play: bool | None = None) -> None:
        """
        Make this device the active Spotify player, optionally starting playback.

        :param play: When True, also start playback on this device; when False,
            pause it; when None, leave the current playback state untouched.
        """
        if not self._spotify_provider:
            return
        # cache device ID on first call; subsequent calls reuse it
        if not self._spotify_device_id:
            self._spotify_device_id = await self._get_spotify_device_id()
        if not self._spotify_device_id:
            self.logger.debug("Cannot transfer playback - device ID not found")
            return
        if play is True:
            # Prefer the direct play endpoint: transfer-with-play can leave the
            # device paused for a long time before it actually starts.
            try:
                await self._spotify_provider._put_data(
                    "me/player/play", device_id=self._spotify_device_id
                )
                return
            except Exception as err:
                self.logger.debug(
                    "Direct /me/player/play failed (%s), falling back to transfer-with-play",
                    err,
                )
        data: dict[str, object] = {"device_ids": [self._spotify_device_id]}
        if play is not None:
            data["play"] = play
        try:
            await self._spotify_provider._put_data("me/player", data=data)
        except Exception as err:
            self.logger.debug("Failed to ensure active device: %s", err)
            # Don't raise - this is a best-effort operation

    def _on_provider_event(self, event: MassEvent) -> None:
        """Handle provider added/removed events to check for Spotify provider."""
        # Re-check for matching Spotify provider when providers change
        if self._connected_spotify_username:
            self.mass.create_task(self._check_spotify_provider_match())

    def _process_librespot_stderr_line(self, line: str) -> None:
        """
        Process a single line from librespot stderr output.

        :param line: A line from librespot's stderr output.
        """
        if (
            not self._librespot_started.is_set()
            # Codec/backend-independent readiness signal: librespot reaches this
            # only after audio backend + spirc setup completed without errors.
            and "Connecting to AP" in line
        ):
            self._librespot_started.set()
        if "error sending packet Os" in line:
            return
        if "dropping truncated packet" in line:
            return
        if "couldn't parse packet from " in line:
            return
        if "Authenticated as '" in line:
            # Extract username from librespot authentication message
            # Format: "Authenticated as 'username'"
            try:
                parts = line.split("Authenticated as '")
                if len(parts) > 1:
                    username_part = parts[1].split("'")
                    if len(username_part) > 0 and username_part[0]:
                        username = username_part[0]
                        self._connected_spotify_username = username
                        self.logger.debug("Authenticated to Spotify as: %s", username)
                        # Check for provider match now that we have the username
                        self.mass.create_task(self._check_spotify_provider_match())
                    else:
                        self.logger.warning("Could not parse Spotify username from line: %s", line)
                else:
                    self.logger.warning("Could not parse Spotify username from line: %s", line)
            except Exception as err:
                self.logger.warning("Error parsing Spotify username from line: %s - %s", line, err)
            return
        self.logger.debug("[%s] %s", self.name, line)

    async def _librespot_runner(self) -> None:
        """Run the spotify connect daemon in a background task."""
        assert self._librespot_bin
        self.logger.info("Starting Spotify Connect background daemon [%s]", self.name)
        env = {"MASS_CALLBACK": f"{self.mass.streams.base_url}/{self.instance_id}"}
        await check_output("rm", "-f", self.named_pipe)
        await asyncio.sleep(0.1)
        await check_output("mkfifo", self.named_pipe)
        await asyncio.sleep(0.1)
        try:
            # Get initial volume from default player if available, or use 20 as fallback
            initial_volume = 20
            if self._default_player_id and self._default_player_id != PLAYER_ID_AUTO:
                if _player := self.mass.players.get_player(self._default_player_id):
                    if _player.volume_level:
                        initial_volume = _player.volume_level
            args: list[str] = [
                self._librespot_bin,
                "--name",
                cast("str", self.config.get_value(CONF_PUBLISH_NAME)) or self.name,
                "--cache",
                self.cache_dir,
                "--disable-audio-cache",
                "--bitrate",
                "320",
                "--backend",
                "pipe",
                "--device",
                self.named_pipe,
                "--dither",
                "none",
                # disable volume control
                "--mixer",
                "passthrough",
                "--volume-ctrl",
                "passthrough",
                "--initial-volume",
                str(initial_volume),
                "--enable-volume-normalisation",
                # forward events to the events script
                "--onevent",
                str(EVENTS_SCRIPT),
                "--emit-sink-events",
            ]
            bind_ip = self.mass.streams.bind_ip
            if bind_ip and bind_ip != "0.0.0.0":
                args.extend(["--zeroconf-interface", bind_ip])
            self._librespot_proc = librespot = AsyncProcess(
                args, stdout=False, stderr=True, name=f"librespot[{self.name}]", env=env
            )
            await librespot.start()

            # keep reading logging from stderr until exit
            async for line in librespot.iter_stderr():
                self._process_librespot_stderr_line(line)
        finally:
            await librespot.close()
            self.logger.info("Spotify Connect background daemon stopped for %s", self.name)
            await check_output("rm", "-f", self.named_pipe)
            if not self._librespot_started.is_set():
                self.unload_with_error("Unable to initialize librespot daemon.")
            # auto restart if not stopped manually
            elif not self._stop_called and self._runner_error_count >= 5:
                self.unload_with_error("Librespot daemon failed to start multiple times.")
            elif not self._stop_called:
                self._runner_error_count += 1
                self.mass.call_later(2, self._setup_player_daemon)

    def _setup_player_daemon(self) -> None:
        """Handle setup of the spotify connect daemon for a player."""
        self._librespot_started.clear()
        self._runner_task = self.mass.create_task(self._librespot_runner())

    async def _handle_custom_webservice(self, request: Request) -> Response:  # noqa: PLR0915
        """Handle incoming requests on the custom webservice."""
        json_data = await request.json()
        self.logger.debug("Received metadata on webservice [%s]: \n%s", self.name, json_data)

        event_name = json_data.get("event")

        # handle session connected event
        # extract the connected username and check for matching Spotify provider
        if event_name == "session_connected":
            # Track when session connected for volume event filtering
            self._last_session_connected_time = time.time()
            self._spotify_session_active = True
            self._arm_session_watchdog()
            username = json_data.get("user_name")
            self.logger.debug(
                "Session connected event - username from event: %s, current username: %s",
                username,
                self._connected_spotify_username,
            )
            if username and username != self._connected_spotify_username:
                self.logger.info("Spotify Connect session connected for user: %s", username)
                self._connected_spotify_username = username
                await self._check_spotify_provider_match()
            elif not username:
                self.logger.warning("Session connected event received but no username in payload")

        # Keep _connected_spotify_username/_spotify_provider so Web API control
        # still works and MA can re-acquire after the Spotify app drops the
        # device; provider lifecycle is handled via PROVIDERS_UPDATED.
        if event_name == "session_disconnected":
            self.logger.info("Spotify Connect session disconnected")
            self._spotify_session_active = False
            self._cancel_session_watchdog()
            prev_player_id = self._active_player_id
            self._clear_active_player()
            if prev_player_id:
                self.mass.create_task(self.mass.players.cmd_stop(prev_player_id))

        # NOTE: a transient "paused" event used to clear in_use_by_queue here so
        # MA could take over. In the new AudioSource model, pause is rendered by
        # the queue's seek bar freezing (stream_metadata.elapsed_time stops
        # advancing), and an explicit play_media call from MA replaces the
        # active queue item — closing our stream cleanly through the normal
        # queue lifecycle. Clearing the lock on paused caused churn on the
        # MA-side resume path (each resume created a new play_media session),
        # so we leave it set until session_disconnected or an external stop.

        # 'sink' = audio sink active, 'playing' = playback started — both mean
        # librespot is producing audio. They often arrive in the same tick in
        # either order; treat both as active so downstream gates see consistent
        # state regardless of which lands first.
        if event_name in ("sink", "playing"):
            self._librespot_playing = True
            # Definitive state signal — the watchdog can stand down.
            if event_name == "playing":
                self._cancel_session_watchdog()
        elif event_name == "paused":
            self._librespot_playing = False
            # Definitive state signal — the watchdog can stand down.
            self._cancel_session_watchdog()
            # cancel any deferred play_media — pause is the definitive "don't".
            # We deliberately do NOT cmd_stop the consumer here even though it
            # would make pause UX feel snappier: cmd_stop ends the stream →
            # ffmpeg closes the pipe read fd → librespot gets EPIPE and resets
            # its Spotify Connect session. The reset emits a stale 'playing'
            # before re-syncing to paused, which would loop play_media → pause
            # → cmd_stop forever. Slower consumer-buffer drain is the trade.
            self._cancel_pending_play_media()
        elif event_name == "session_connected":
            # a session reconnect means the previous 'playing' event was from
            # a now-dying session — acting on it would land our stream on a
            # closing pipe; cancel any deferred fire from that event
            self._cancel_pending_play_media()

        # An externally-triggered 'playing' event means Spotify (the app on a
        # phone/desktop) has started playback on us — kick a play_media on the
        # target MA player so the audio actually reaches a speaker. We only
        # fire on 'playing' (not 'sink' — that's just the audio-sink-active
        # signal which fires before actual playback and would race with
        # 'playing' to double-fire play_media). The fire is deferred so a
        # rapid sink/playing/session-connect burst from a reconnecting session
        # can cancel it before we act on a stale state.
        if (
            event_name == "playing"
            and not self._in_use_by_queue
            and (self._pending_play_media_task is None or self._pending_play_media_task.done())
        ):
            self._pending_play_media_task = self.mass.create_task(self._deferred_play_media_fire())

        # parse metadata fields (_stream_metadata is always set in __init__)
        if common_meta := json_data.get("common_metadata_fields", {}):
            uri = common_meta.get("uri", "Unknown")
            title = common_meta.get("name", "Unknown")
            image_url = images[0] if (images := common_meta.get("covers")) else None
            self._stream_metadata.uri = uri
            self._stream_metadata.title = title
            self._stream_metadata.artist = None
            self._stream_metadata.album = None
            self._stream_metadata.image_url = image_url
            self._stream_metadata.description = None
            duration_ms = common_meta.get("duration_ms", 0)
            self._stream_metadata.duration = (
                int(duration_ms) // 1000 if duration_ms is not None else None
            )
            # Reset elapsed time when track changes to prevent showing stale elapsed time
            # from previous track
            self._stream_metadata.elapsed_time = 0
            self._stream_metadata.elapsed_time_last_updated = int(time.time())

        if track_meta := json_data.get("track_metadata_fields", {}):
            if artists := track_meta.get("artists"):
                self._stream_metadata.artist = artists[0]
            self._stream_metadata.album = track_meta.get("album")

        if episode_meta := json_data.get("episode_metadata_fields", {}):
            self._stream_metadata.description = episode_meta.get("description")

        if "position_ms" in json_data:
            self._stream_metadata.elapsed_time = int(json_data["position_ms"]) // 1000
            self._stream_metadata.elapsed_time_last_updated = int(time.time())

        if event_name == "volume_changed" and (volume := json_data.get("volume")):
            # Ignore volume_changed events that fire immediately after session_connect
            # We want to use the volume from MA in that case
            time_since_connect = time.time() - self._last_session_connected_time
            if time_since_connect < 3.0:
                self.logger.debug(
                    "Ignoring initial volume_changed event (%.2fs after session_connect)",
                    time_since_connect,
                )
            elif self._in_use_by_queue:
                # Spotify Connect volume is 0-65535
                volume = int(int(volume) / 65535 * 100)
                self._last_volume_sent_to_spotify = volume
                try:
                    await self.mass.players.cmd_volume_set(self._in_use_by_queue, volume)
                except UnsupportedFeaturedException:
                    self.logger.debug(
                        "Player %s does not support volume control",
                        self._in_use_by_queue,
                    )

        # push metadata update to the active queue item's streamdetails
        if self._in_use_by_queue:
            self.mass.streams.update_stream_metadata(
                self._in_use_by_queue,
                AUDIO_SOURCE_ID,
                self.instance_id,
                self._stream_metadata,
            )

        return Response()
