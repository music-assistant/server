"""Yandex Station Player — transport control and state management."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    IdentifierType,
    MediaType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
    QueueOption,
)
from music_assistant_models.errors import PlayerCommandFailed, UnsupportedFeaturedException

from music_assistant.constants import CONF_ENTRY_HTTP_PROFILE_DEFAULT_3, CONF_ENTRY_OUTPUT_CODEC
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia

from . import protobuf
from .constants import (
    CONF_INTERCEPT_ENABLED,
    CONF_INTERCEPT_FEATURE_ENABLED,
    CONF_INTERCEPT_TARGET,
    CONF_VOICE_CONTROL,
)

CONF_ENTRY_VOICE_CONTROL = ConfigEntry(
    key=CONF_VOICE_CONTROL,
    type=ConfigEntryType.BOOLEAN,
    default_value=False,
    required=False,
    advanced=True,
)

if TYPE_CHECKING:
    from .glagol import YandexGlagol
    from .provider import YandexStationProvider

PROV_LOGGER_BASE = "music_assistant.Yandex Station"
_LOGGER = logging.getLogger(f"{PROV_LOGGER_BASE}.player")


def _external_command(name: str, payload: dict[str, Any] | str | None = None) -> dict[str, Any]:
    """Build an externalCommandBypass command for Glagol."""
    data: dict[int, str] = {1: name}
    if payload:
        data[2] = json.dumps(payload) if isinstance(payload, dict) else payload
    return {
        "command": "externalCommandBypass",
        "data": base64.b64encode(protobuf.dumps(data)).decode(),
    }


def _parse_yandex_track_id(raw: str) -> str:
    """
    Extract numeric Yandex Music track ID from Glagol playerState.id.

    Glagol may emit `12345` (plain) or `12345:67890` (track:album).  Strip the
    album suffix so the value is consumable by the yandex_music music provider.
    """
    return raw.split(":", 1)[0].strip()


def _raise_if_failed(result: dict[str, Any] | None, command: str) -> None:
    """
    Raise PlayerCommandFailed if a Glagol send result indicates failure.

    Two distinct failure shapes:
    - Transport error (no response, or ``error`` key set).
    - Device returned a status other than ``"SUCCESS"`` (the device accepted
      the command but rejected its execution — e.g. ``"ERROR"`` with a
      ``message``).  Without this check, transport commands like
      play/pause/stop/seek/volume_set would silently succeed in MA even when
      the device rejected them.
    """
    if not result or result.get("error"):
        err = (result or {}).get("error", "no response")
        msg = f"{command} failed: {err}"
        raise PlayerCommandFailed(msg)
    status = result.get("status")
    if status is not None and status != "SUCCESS":
        message = result.get("message", "")
        detail = f" ({message})" if message else ""
        msg = f"{command} returned non-SUCCESS status: {status}{detail}"
        raise PlayerCommandFailed(msg)


def _update_form(name: str, **kwargs: str) -> dict[str, Any]:
    """Build a serverAction command to invoke a named form/scenario."""
    return {
        "command": "serverAction",
        "serverActionEventPayload": {
            "type": "server_action",
            "name": "update_form",
            "payload": {
                "form_update": {
                    "name": name,
                    "slots": [{"type": "string", "name": k, "value": v} for k, v in kwargs.items()],
                },
                "resubmit": True,
            },
        },
    }


class YandexStationPlayer(Player):
    """Represents a single Yandex Station smart speaker."""

    def __init__(
        self,
        provider: YandexStationProvider,
        player_id: str,
        device_info: dict[str, Any],
        glagol: YandexGlagol,
    ) -> None:
        """Initialize the player."""
        super().__init__(provider, player_id)
        self._device_info = device_info
        self.glagol = glagol

        # Track external (radio_play) playback since Glagol doesn't report it
        self._external_playing = False
        self._external_media: PlayerMedia | None = None
        # Becomes True once Glagol reports playing=True during external playback.
        # Used to distinguish the startup window (station fetching stream) from
        # a user-initiated physical pause on the speaker.
        self._external_play_confirmed = False
        # Set after pause of external playback — play() must re-trigger queue
        self._needs_replay = False
        # Track previous alice state to detect LISTENING→IDLE transitions
        self._prev_alice_state: str = ""
        # Timer for auto-resume after voice command
        self._voice_resume_task: asyncio.Task[None] | None = None
        # Voice command analysis: did Alice speak? Was volume changed?
        self._alice_spoke: bool = False
        self._pre_voice_volume: int = 0

        # Intercept feature state (only meaningful when both provider-level and
        # per-player toggles are ON).  See _maybe_intercept / _handle_intercept_tick.
        self._intercept_active: bool = False
        self._last_intercepted_track_id: str | None = None
        self._last_intercept_time: float = 0.0
        self._last_mirrored_volume: int | None = None
        self._last_progress: int = 0
        self._last_progress_wall: float = 0.0
        # Serialises _maybe_intercept so two near-simultaneous WS updates
        # can't issue duplicate stop/play commands for the same track.
        self._intercept_lock: asyncio.Lock = asyncio.Lock()
        # True after we've issued cmd_pause(target) for the current Alice
        # interaction.  Prevents repeating the pause on every WS tick while
        # Alice is still LISTENING/SPEAKING.  Cleared when alice goes IDLE.
        self._alice_active_pause_sent: bool = False
        # Saved Station volume captured ONCE at intercept-session start;
        # restored at session end (target unavailable, handoff failure,
        # provider unload, etc.).  Also used to unmute Station for Alice
        # TTS during a session.
        self._saved_station_volume: int | None = None
        # True iff we currently hold the Station at vol=0 for intercept.
        # Gates volume mirror (skip vol=0 self-mute) and the alice-state
        # mute lifecycle (only restore when we actually muted).
        self._station_muted_by_intercept: bool = False

        # Static attributes
        self._attr_type = PlayerType.PLAYER
        self._attr_name = device_info.get("name", "Yandex Station")
        self._attr_available = False
        self._attr_powered = True
        self._attr_needs_poll = False  # We get state from WebSocket
        self._attr_supported_features = {
            PlayerFeature.POWER,
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.PLAY_ANNOUNCEMENT,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.PAUSE,
            PlayerFeature.NEXT_PREVIOUS,
            PlayerFeature.SEEK,
        }
        self._attr_device_info = DeviceInfo(
            model=device_info.get("quasar_info", {}).get("platform", "unknown"),
            manufacturer="Yandex",
        )
        # Identifiers help MA auto-link this player with other protocols on the
        # same device (e.g. AirPlay/DLNA receivers exposed by the same speaker):
        # deviceId (stable per speaker) and current IP. MAC, if needed, is
        # resolved by MA core from the IP_ADDRESS identifier.
        if host := device_info.get("host"):
            self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, host)
        if device_id := device_info.get("quasar_info", {}).get("device_id"):
            self._attr_device_info.add_identifier(IdentifierType.UUID, device_id)

    async def async_setup(self) -> None:
        """Set up the Glagol WebSocket connection."""
        self.glagol.update_handler = self._on_glagol_update
        await self.glagol.start()

    async def get_config_entries(self) -> list[ConfigEntry]:
        """
        Return player-specific config entries.

        Yandex Station requires Content-Length in HTTP responses (no chunked encoding),
        so we default to forced_content_length HTTP profile.
        """
        # List players that can receive a redirected track.  We dispatch
        # the handoff via ``mass.player_queues.play_media(queue_id=...)``
        # which routes through the per-player queue and works on any
        # registered player regardless of which playback features it
        # advertises — so a feature filter here only ends up hiding
        # legitimate targets (e.g. AirPlay / DLNA / BT bridges that
        # don't advertise ``PLAY_MEDIA`` directly but accept queued
        # media just fine).  Mirror helpers (volume / pause / seek)
        # already gracefully no-op via ``UnsupportedFeaturedException``
        # when the chosen target lacks them.  We also include
        # currently-unavailable players so the user can preselect a
        # target that's offline at setup time, and sort by display name
        # so the dropdown is easy to scan.
        target_options = sorted(
            (
                ConfigValueOption(p.player_id, p.display_name)
                for p in self.mass.players.all_players(return_unavailable=True)
                if p.player_id != self.player_id
            ),
            key=lambda o: (o.title or "").lower(),
        )
        return [
            CONF_ENTRY_OUTPUT_CODEC,
            CONF_ENTRY_HTTP_PROFILE_DEFAULT_3,
            CONF_ENTRY_VOICE_CONTROL,
            ConfigEntry(
                key=CONF_INTERCEPT_ENABLED,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                required=False,
            ),
            ConfigEntry(
                key=CONF_INTERCEPT_TARGET,
                type=ConfigEntryType.STRING,
                options=target_options,
                required=False,
            ),
        ]

    def update_connection(self, host: str, port: int) -> None:
        """
        Update connection info when mDNS reports new IP.

        Pure mutation — no side effects.  Callers decide whether to (re)connect
        via ``async_setup()`` so a single mDNS update can't trigger
        concurrent/duplicate ``glagol.start()`` calls (the previous version
        scheduled a background reconnect here, while the provider's mDNS
        handler also called ``async_setup()`` for the not-connected branch).

        For an already-connected player, the underlying socket targets the
        old IP and will reconnect via Glagol's auto-reconnect loop if the
        old endpoint is no longer reachable.
        """
        self._device_info["host"] = host
        self._device_info["port"] = port
        self.glagol.device["host"] = host
        self.glagol.device["port"] = port
        self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, host)

    # ── Transport controls ───────────────────────────────────────

    async def power(self, powered: bool) -> None:
        """
        Power on/off the station.

        Power on: resumes playback via player_continue scenario.
        Power off: sends station to home screen via go_home scenario.
        """
        if powered:
            result = await self.glagol.send(
                _update_form("personal_assistant.scenarios.player_continue")
            )
        else:
            result = await self.glagol.send(
                _update_form("personal_assistant.scenarios.quasar.go_home")
            )
        _raise_if_failed(result, "power")
        self._attr_powered = powered
        self.update_state()

    async def play(self) -> None:
        """
        Send PLAY command.

        After external playback was stopped via Alice, native 'play' has nothing
        to resume.  Re-trigger queue playback through MA instead.
        """
        if self._needs_replay:
            self._needs_replay = False
            queue = self.mass.player_queues.get_active_queue(self.player_id)
            if queue:
                await self.mass.player_queues.resume(queue.queue_id)
                return
        result = await self.glagol.send({"command": "play"})
        _raise_if_failed(result, "play")

    async def pause(self) -> None:
        """
        Send PAUSE command.

        Glagol 'stop' only affects the native player, not externalCommandBypass.
        Send a radio_play with invalid URL to replace current bypass stream —
        the station will fail to fetch it and stop. Fully local, no cloud needed.
        """
        was_external = self._external_playing
        if was_external:
            result = await self.glagol.send(
                _external_command("radio_play", {"streamUrl": "http://0.0.0.0/stop.flac"})
            )
        else:
            result = await self.glagol.send({"command": "stop"})
        _raise_if_failed(result, "pause")
        if was_external:
            self._needs_replay = True
        self._external_playing = False
        self._external_media = None
        self._external_play_confirmed = False
        self._cancel_voice_resume()
        self._attr_playback_state = PlaybackState.PAUSED
        self.update_state()

    async def stop(self) -> None:
        """Send STOP command."""
        if self._external_playing:
            result = await self.glagol.send(
                _external_command("radio_play", {"streamUrl": "http://0.0.0.0/stop.flac"})
            )
        else:
            result = await self.glagol.send({"command": "stop"})
        _raise_if_failed(result, "stop")
        self._external_playing = False
        self._external_media = None
        self._external_play_confirmed = False
        self._needs_replay = False
        self._cancel_voice_resume()
        self._attr_playback_state = PlaybackState.IDLE
        self.update_state()

    async def next_track(self) -> None:
        """Send NEXT command."""
        result = await self.glagol.send({"command": "next"})
        _raise_if_failed(result, "next_track")

    async def previous_track(self) -> None:
        """Send PREVIOUS command."""
        result = await self.glagol.send({"command": "prev"})
        _raise_if_failed(result, "previous_track")

    async def seek(self, position: int) -> None:
        """Seek to position in seconds."""
        result = await self.glagol.send({"command": "rewind", "position": position})
        _raise_if_failed(result, "seek")

    async def volume_set(self, volume_level: int) -> None:
        """Set volume level (0-100)."""
        result = await self.glagol.send(
            {"command": "setVolume", "volume": round(volume_level / 100, 2)}
        )
        _raise_if_failed(result, "volume_set")

    async def volume_mute(self, muted: bool) -> None:
        """Mute/unmute. Yandex Station doesn't have native mute, simulate with volume."""
        if muted:
            self._saved_volume = self._attr_volume_level or 0
            await self.volume_set(0)
        elif hasattr(self, "_saved_volume"):
            await self.volume_set(self._saved_volume)
        self._attr_volume_muted = muted
        self.update_state()

    async def play_media(self, media: PlayerMedia) -> None:
        """Play media on the Yandex Station via radio_play command."""
        self._needs_replay = False
        _LOGGER.debug("[%s] play_media called: %s", self.player_id, media.title or media.uri)
        stream_url = await self.provider.mass.streams.resolve_stream_url(self.player_id, media)
        _LOGGER.debug("[%s] Stream URL resolved (length=%d)", self.player_id, len(stream_url))

        payload: dict[str, Any] = {
            "streamUrl": stream_url,
            "force_restart_player": True,
        }

        result = await self.glagol.send(_external_command("radio_play", payload))
        _LOGGER.debug("[%s] radio_play result: %s", self.player_id, result)
        _raise_if_failed(result, "radio_play")

        # Glagol doesn't update playerState for externalCommandBypass playback,
        # so we set state optimistically.
        self._external_playing = True
        self._external_play_confirmed = False
        self._external_media = media
        self._attr_playback_state = PlaybackState.PLAYING
        self._attr_powered = True
        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time.time()
        self.set_current_media(
            uri=media.uri or "",
            title=media.title or "",
            artist=media.artist or "",
            duration=media.duration or None,
            image_url=media.image_url or None,
        )
        self.update_state()

    async def play_announcement(
        self, announcement: PlayerMedia, volume_level: int | None = None
    ) -> None:
        """
        Play announcement by streaming the MA-hosted announcement URL.

        Blocks until the announcement finishes so MA core can properly
        manage ANNOUNCEMENT_IN_PROGRESS state and volume restoration.

        ``announcement.title`` is always the literal string ``"Announcement"``
        (set by MA core for every announcement, regardless of source), so we
        can't synthesise it via Alice TTS. The actual audio (with optional
        pre-announce chime) is at ``announcement.uri``.
        """
        announcement_timeout = 30

        # Adjust volume before announcement if requested
        saved_volume: int | None = None
        if volume_level is not None and volume_level != self._attr_volume_level:
            saved_volume = self._attr_volume_level
            await self.volume_set(volume_level)

        try:
            _LOGGER.debug("[%s] Audio announcement: %s", self.player_id, announcement.uri)
            result = await self.glagol.send(
                _external_command(
                    "radio_play",
                    {"streamUrl": announcement.uri, "force_restart_player": True},
                )
            )
            _raise_if_failed(result, "Audio announcement")
            # externalCommandBypass playback doesn't update state.playing,
            # so we can't observe completion — wait by known duration instead.
            duration = announcement.duration
            wait_time = (
                min(duration + 1, announcement_timeout)
                if duration and duration > 0
                else announcement_timeout
            )
            await asyncio.sleep(wait_time)
        finally:
            if saved_volume is not None:
                await self.volume_set(saved_volume)

    async def on_unload(self) -> None:
        """Clean up on player unload."""
        if self._voice_resume_task:
            self._voice_resume_task.cancel()
        # End any active intercept session so the Station's volume is
        # restored before we tear down — otherwise users unloading the
        # integration mid-session would be left with a Station stuck at
        # vol=0 with no way for our code to fix it.
        if self._intercept_active:
            await self._end_intercept_session(clear_debounce=True)
        await super().on_unload()
        await self.glagol.stop()

    @property
    def _voice_control_enabled(self) -> bool:
        """Whether experimental voice control integration is enabled."""
        return bool(self._config.get_value(CONF_VOICE_CONTROL))

    @property
    def _intercept_feature_enabled(self) -> bool:
        """Provider-level intercept master switch.  Without it intercept is off."""
        return bool(self._provider.config.get_value(CONF_INTERCEPT_FEATURE_ENABLED))

    @property
    def _intercept_enabled(self) -> bool:
        """Both provider-level master switch AND per-player toggle must be ON."""
        return self._intercept_feature_enabled and bool(
            self._config.get_value(CONF_INTERCEPT_ENABLED)
        )

    @property
    def _intercept_target_player_id(self) -> str | None:
        """Configured target player_id for intercept playback (None when unset)."""
        value = self._config.get_value(CONF_INTERCEPT_TARGET)
        return str(value) if value else None

    async def _delayed_resume(self) -> None:
        """Auto-resume MA queue after a voice command that didn't start native player."""
        try:
            await asyncio.sleep(3)
            if self._needs_replay:
                _LOGGER.debug("[%s] Auto-resuming MA queue after voice command", self.player_id)
                self._needs_replay = False
                queue = self.mass.player_queues.get_active_queue(self.player_id)
                if queue:
                    await self.mass.player_queues.resume(queue.queue_id)
        except asyncio.CancelledError:
            pass
        finally:
            self._voice_resume_task = None

    # ── State updates from Glagol WebSocket ──────────────────────

    def _handle_voice_interrupt(self, alice_state: str) -> None:
        """
        Handle Alice activation during bypass playback.

        Intercept-mode voice handling lives in ``_handle_intercept_tick`` —
        this branch is reached only via ``_update_playback_state`` while
        ``_external_playing`` is True (our own ``radio_play`` stream).
        """
        _LOGGER.debug(
            "[%s] Alice active (%s) during bypass — pausing MA queue",
            self.player_id,
            alice_state,
        )
        self._external_playing = False
        self._external_media = None
        self._external_play_confirmed = False
        self._needs_replay = True
        self._attr_playback_state = PlaybackState.PAUSED
        self._alice_spoke = False
        self._pre_voice_volume = self._attr_volume_level or 0
        self._cancel_voice_resume()

    def _handle_voice_end(self, alice_state: str) -> None:
        """Handle end of voice interaction — decide whether to auto-resume."""
        if alice_state == "SPEAKING":
            self._alice_spoke = True

        if (
            self._prev_alice_state in ("LISTENING", "SPEAKING")
            and alice_state == "IDLE"
            and not self._voice_resume_task
        ):
            current_volume = self._attr_volume_level or 0
            volume_changed = current_volume != self._pre_voice_volume

            if self._alice_spoke or volume_changed:
                reason = "speech" if self._alice_spoke else "volume change"
                _LOGGER.debug(
                    "[%s] Voice command ended (%s) — scheduling auto-resume",
                    self.player_id,
                    reason,
                )
                self._voice_resume_task = asyncio.create_task(self._delayed_resume())
            else:
                _LOGGER.debug("[%s] Silent voice command — staying paused", self.player_id)
                self._needs_replay = True

    def _cancel_voice_resume(self) -> None:
        """Cancel any pending auto-resume task from a prior voice interaction."""
        if self._voice_resume_task:
            self._voice_resume_task.cancel()
            self._voice_resume_task = None

    # ── Intercept feature ────────────────────────────────────────

    async def _handle_intercept_tick(
        self,
        state: dict[str, Any],
        player_state: dict[str, Any],
        playing: bool,
        prev_alice_state: str = "",
    ) -> None:
        """
        Dispatch intercept actions for a single Glagol state update.

        Holds ``_intercept_lock`` for the whole tick so back-to-back WS
        updates (each scheduled as its own background task by the dispatcher)
        can't apply mirror operations out of order — e.g. an older volume
        write completing after a newer one and leaving the target stale.

        ``prev_alice_state`` is the value of ``self._prev_alice_state``
        captured by ``_on_glagol_update`` *before* it overwrites the field
        with the current tick's ``aliceState``.  Reading the field here
        directly would return the post-write value (i.e. the *current*
        state) because the dispatcher schedules this coroutine via
        ``mass.create_task`` and the assignment runs synchronously before
        the task is awaited — making the IDLE-edge re-mute branch dead
        code in production.  The caller threads the snapshot in.
        """
        if self._external_playing:
            return  # our own bypass stream — never intercept

        async with self._intercept_lock:
            track_id = (player_state.get("id") or "").strip()
            progress = int(player_state.get("progress") or 0)
            alice_state = state.get("aliceState", "")
            alice_active = alice_state in ("LISTENING", "SPEAKING")

            # Alice activates → unmute Station so her TTS is audible (the
            # Station mixes Alice voice into the music output, so a vol=0
            # Station can drown out / quiet her reply on some firmwares),
            # pause the target once so she's heard cleanly, and skip a
            # new handoff until she goes IDLE.  The pause-once flag is set
            # BEFORE awaiting cmd_pause so two concurrent ticks racing on
            # the lock both see it after the first wins.
            if self._intercept_active and alice_active:
                if self._station_muted_by_intercept and self._saved_station_volume is not None:
                    # YandexGlagol.send() reports failures via {"error": ...}
                    # rather than raising, so a naive try/except wouldn't
                    # catch a timeout or transport error and we'd flip the
                    # mute flag while the Station is still actually muted
                    # (and Alice's reply would be inaudible).  Validate the
                    # result via _raise_if_failed and only mutate state if
                    # the device confirmed success.
                    try:
                        result = await self.glagol.send(
                            {
                                "command": "setVolume",
                                "volume": self._saved_station_volume / 100,
                            }
                        )
                        _raise_if_failed(result, "alice-unmute")
                        self._station_muted_by_intercept = False
                        # Pre-set mirror baseline so the next state-update
                        # tick (with the new non-zero Station volume) doesn't
                        # spuriously bounce that volume to the target.
                        self._last_mirrored_volume = self._saved_station_volume
                    except Exception as exc:
                        _LOGGER.debug("[%s] alice-unmute failed: %s", self.player_id, exc)
                if not self._alice_active_pause_sent:
                    self._alice_active_pause_sent = True
                    await self._pause_target(clear_session=False, clear_debounce=True)
                return

            if not alice_active:
                # Edge LISTENING/SPEAKING → IDLE: re-mute Station now that
                # Alice is done.  Without this, the Station would resume
                # audible playback and double up with the target.  Same
                # validation reason as the alice-unmute branch above:
                # don't flip the flag unless the device confirms success.
                if (
                    self._intercept_active
                    and prev_alice_state in ("LISTENING", "SPEAKING")
                    and not self._station_muted_by_intercept
                ):
                    try:
                        result = await self.glagol.send({"command": "setVolume", "volume": 0.0})
                        _raise_if_failed(result, "alice-remute")
                        self._station_muted_by_intercept = True
                    except Exception as exc:
                        _LOGGER.debug("[%s] alice-remute failed: %s", self.player_id, exc)
                self._alice_active_pause_sent = False

            # Physical pause / "Алиса, пауза" / end-of-queue on a Station
            # with an active intercept session: mirror the pause to the
            # target AND end the session (restore Station volume).  We
            # can't reliably distinguish "transient user pause" from
            # "queue ended for good" from a single playing=False event,
            # so always end the session — leaving it open would strand
            # the Station at vol=0 indefinitely after a queue ends.
            # Quick-resume cost: ~one WS round-trip of native audio
            # before the new session's mute(0) lands; matches v1.4.7
            # baseline behaviour.  Gated on having an established
            # session (debounce non-None) so the very-first WS update
            # with playing=False doesn't fire a spurious cmd_pause.
            if (
                self._intercept_active
                and not playing
                and self._last_intercepted_track_id is not None
            ):
                await self._pause_target(clear_session=True, clear_debounce=True)
                return

            if playing and track_id:
                await self._maybe_intercept_locked(track_id)
                if self._intercept_active:
                    await self._maybe_mirror_seek(progress)
                    await self._maybe_mirror_volume(state.get("volume"))

    async def _maybe_intercept_locked(self, track_id: str) -> None:
        """
        Resolve the track, stop the Station, then play on the target.

        Caller MUST hold ``_intercept_lock``.  Serialisation prevents two
        concurrent Glagol updates from issuing duplicate stop/play commands
        for the same track.  ``now`` is read *inside* the lock so a long
        handoff doesn't leave the next task with a stale timestamp that
        bypasses debounce.

        The debounce timestamp is updated on every attempt — successful or
        not — so failed lookups don't re-run on every WS tick (~1Hz) and
        spam logs.  Failure-path cleanup of a stale prior session keeps the
        new track's debounce intact via ``_pause_target(clear_debounce=False)``.
        """
        now = time.time()
        # Same-track guard:
        #   - Active session (`_intercept_active=True`) + same track_id:
        #     we already handed it off, target is playing it.  Glagol
        #     emits ``playerState`` ~1Hz with the same id for the entire
        #     track duration (3-5min), so without this guard we'd
        #     re-trigger ``play_media(REPLACE)`` every WS tick the 5-second
        #     failure-debounce expired.  In the live logs that manifested
        #     as the target's audio stuttering every ~5 seconds with
        #     repeated ``StreamEnd``/``StreamStart`` cycles.
        #   - Inactive (failed prior attempt) + same track within 5s:
        #     keep the debounce so a quick-fire of a failing lookup
        #     doesn't spam logs / hammer the API.
        if track_id == self._last_intercepted_track_id and (
            self._intercept_active or (now - self._last_intercept_time) < 5
        ):
            return
        new_track = track_id != self._last_intercepted_track_id
        # Mark the attempt up-front so failure paths debounce too.
        self._last_intercepted_track_id = track_id
        self._last_intercept_time = now

        target_id = self._intercept_target_player_id
        if not target_id:
            return

        if not self.mass.get_provider("yandex_music"):
            _LOGGER.warning(
                "[%s] intercept: yandex_music provider not configured",
                self.player_id,
            )
            if new_track and self._intercept_active:
                await self._pause_target(clear_session=True, clear_debounce=False)
            return

        target_player = self.mass.players.get_player(target_id)
        if target_player is None or not getattr(target_player, "available", True):
            _LOGGER.warning(
                "[%s] intercept: target player %s not available",
                self.player_id,
                target_id,
            )
            if new_track and self._intercept_active:
                await self._pause_target(clear_session=True, clear_debounce=False)
            return

        _LOGGER.debug("[%s] intercept: raw playerState.id=%r", self.player_id, track_id)
        parsed_id = _parse_yandex_track_id(track_id)

        # Resolve first — if the track can't be found or the URI is missing
        # we leave the Station playing rather than silencing it for nothing.
        try:
            track = await self.mass.music.get_item(
                media_type=MediaType.TRACK,
                item_id=parsed_id,
                provider_instance_id_or_domain="yandex_music",
            )
        except Exception as exc:
            _LOGGER.warning(
                "[%s] intercept resolve failed for %r: %s",
                self.player_id,
                track_id,
                exc,
            )
            if new_track and self._intercept_active:
                await self._pause_target(clear_session=True, clear_debounce=False)
            return
        track_uri = track.uri
        if not track_uri:
            _LOGGER.warning(
                "[%s] intercept: resolved track has no uri: %r",
                self.player_id,
                track,
            )
            if new_track and self._intercept_active:
                await self._pause_target(clear_session=True, clear_debounce=False)
            return

        # Silence the Station via setVolume(0) only — no `stop` command.
        # Crucial difference from one-shot intercept: a stopped Station
        # halts its own queue and stops emitting playerState updates, so
        # we'd never see the next track Alice queued.  Keeping it muted
        # but playing means Glagol keeps ticking with each new track ID,
        # which we forward to the target → continuous handoff for the
        # whole Alice session.  Saved volume is captured ONCE at session
        # start and restored only when the session actually ends.
        is_session_start = not self._intercept_active
        if is_session_start:
            self._saved_station_volume = getattr(self, "_attr_volume_level", None)
        try:
            # Mute only when not already muted: skips redundant WS traffic
            # for back-to-back track handoffs in the same session, but does
            # re-mute after an alice-active unmute brought the Station
            # back to its saved volume.
            if not self._station_muted_by_intercept:
                # YandexGlagol.send() returns {"error": ...} on transport
                # failures (timeout / disconnected / etc.) instead of
                # raising.  Without _raise_if_failed we'd start the target
                # while the Station was still audible (or never confirmed
                # muted) and flip _station_muted_by_intercept anyway —
                # leaving session state inconsistent with the device.
                mute_result = await self.glagol.send({"command": "setVolume", "volume": 0.0})
                _raise_if_failed(mute_result, "intercept-mute")
                self._station_muted_by_intercept = True
            await self.mass.player_queues.play_media(
                queue_id=target_id,
                media=track_uri,
                option=QueueOption.REPLACE,
            )
        except Exception as exc:
            _LOGGER.warning(
                "[%s] intercept handoff failed for %r: %s",
                self.player_id,
                track_id,
                exc,
            )
            # End the session cleanly: restore Station volume so the user
            # isn't stuck with a muted Station, clear all session flags.
            await self._end_intercept_session(clear_debounce=False)
            return

        self._intercept_active = True
        self._last_mirrored_volume = None
        self._last_progress = 0
        # Anchor seek baseline at *play start*, not at tick start —
        # otherwise a slow handoff makes the target appear to lag and every
        # subsequent progress update looks like a backwards seek.
        self._last_progress_wall = time.time()

    async def _restore_station_volume(self, vol_pct: int) -> None:
        """
        Restore Station volume after we muted it for intercept handoff.

        YandexGlagol.send() reports failures via ``{"error": ...}`` rather
        than raising, so a bare ``try/except`` would silently swallow
        timeouts/disconnects and leave the Station muted with no signal
        to the operator.  We validate via ``_raise_if_failed`` and log
        the failure at WARNING (not DEBUG) so it's visible in normal
        log levels — a stuck-muted Station is user-visible.  The flag
        reset upstream in ``_end_intercept_session`` still fires so the
        session doesn't dangle indefinitely (Station can be unmuted via
        Yandex app or "Алиса, погромче").
        """
        try:
            result = await self.glagol.send({"command": "setVolume", "volume": vol_pct / 100})
            _raise_if_failed(result, "restore_volume")
        except Exception as exc:
            _LOGGER.warning(
                "[%s] failed to restore Station volume to %s%%: %s",
                self.player_id,
                vol_pct,
                exc,
            )

    async def _end_intercept_session(self, *, clear_debounce: bool) -> None:
        """
        End an intercept session: restore Station volume + clear flags.

        Single funnel for session-end side effects.  Idempotent — safe to
        call when no session is active (volume restore is gated by both
        ``_saved_station_volume`` and ``_station_muted_by_intercept``).
        Awaited (not scheduled as a task) so callers can rely on the
        Station's volume being back at the saved baseline before they
        return — important for the next mute-on-handoff to capture a
        fresh baseline if the user manually changed volume mid-session.
        """
        if self._saved_station_volume is not None and self._station_muted_by_intercept:
            await self._restore_station_volume(self._saved_station_volume)
        self._saved_station_volume = None
        self._station_muted_by_intercept = False
        self._intercept_active = False
        if clear_debounce:
            self._last_intercepted_track_id = None
            self._last_intercept_time = 0.0

    async def _maybe_mirror_volume(self, vol: float | None) -> None:
        """
        Mirror Station volume changes to the intercept target player.

        Targets without VOLUME_SET raise ``UnsupportedFeaturedException`` —
        we treat that as a no-op (matches the dropdown's documented
        relaxed-feature contract: only PLAY_MEDIA is required, the rest
        gracefully degrade).

        During an active session the Station is held at vol=0 by us, so
        we skip mirroring that — otherwise the self-induced mute would
        silence the target.  A user-initiated unmute (vol > 0 via the
        Yandex app or "Алиса, громче") clears the self-mute flag and
        updates the saved baseline so we don't restore a stale volume
        when the session ends.
        """
        target_id = self._intercept_target_player_id
        if vol is None or not target_id:
            return
        target_vol = round(vol * 100)
        if self._intercept_active and target_vol == 0:
            return
        # Differentiate "stale pre-mute volume reading from the WS update
        # we're processing" (target_vol == saved_station_volume) from
        # "user manually moved Station volume mid-session"
        # (target_vol != saved).  The first WS update after our mute
        # often still carries the pre-mute volume; only treat actual
        # changes as a user action that should clear the self-mute flag.
        if (
            self._intercept_active
            and target_vol > 0
            and self._station_muted_by_intercept
            and target_vol != self._saved_station_volume
        ):
            self._station_muted_by_intercept = False
            self._saved_station_volume = target_vol
        if target_vol != self._last_mirrored_volume:
            try:
                await self.mass.players.cmd_volume_set(target_id, target_vol)
            except UnsupportedFeaturedException:
                _LOGGER.debug(
                    "[%s] target %s does not support volume_set — skipping mirror",
                    self.player_id,
                    target_id,
                )
            self._last_mirrored_volume = target_vol

    async def _maybe_mirror_seek(self, progress: int) -> None:
        """
        Detect Alice-initiated seek by comparing reported progress to wall clock.

        Targets without SEEK raise ``UnsupportedFeaturedException`` — treated
        as a no-op (see ``_maybe_mirror_volume`` docstring).
        """
        target_id = self._intercept_target_player_id
        now = time.time()
        if self._last_progress_wall == 0 or not target_id:
            self._last_progress = progress
            self._last_progress_wall = now
            return
        expected = self._last_progress + (now - self._last_progress_wall)
        if abs(progress - expected) > 2:
            try:
                await self.mass.players.cmd_seek(target_id, progress)
            except UnsupportedFeaturedException:
                _LOGGER.debug(
                    "[%s] target %s does not support seek — skipping mirror",
                    self.player_id,
                    target_id,
                )
        self._last_progress = progress
        self._last_progress_wall = now

    async def _pause_target(self, *, clear_session: bool, clear_debounce: bool) -> None:
        """
        Pause the target player; optionally clear session / debounce state.

        The two flags are independent because callers want different combos:

        - Alice speaks during intercept:
          ``clear_session=False, clear_debounce=True`` — keep session so the
          next Alice track resumes it, but allow a same-track resume to
          trigger a fresh intercept after she's done.
        - Failed re-intercept of a NEW track during an active session:
          ``clear_session=True, clear_debounce=False`` — drop the stale
          session but keep the new track's debounce so failure isn't retried
          on every WS tick.

        State updates run in a ``finally`` block: if ``cmd_pause`` raises
        (e.g. target went unavailable after the handoff) the cleanup still
        happens, otherwise every later WS update would retry the failing
        path with stale ``_intercept_active`` / debounce values.
        """
        target_id = self._intercept_target_player_id
        if not target_id:
            return
        try:
            await self.mass.players.cmd_pause(target_id)
        except Exception as exc:
            _LOGGER.warning(
                "[%s] intercept: cmd_pause(%s) failed: %s",
                self.player_id,
                target_id,
                exc,
            )
        finally:
            if clear_session:
                # Funnels session-end side effects (volume restore + flag
                # reset) through the single helper so we don't leave the
                # Station stuck at vol=0 after a session-ending pause.
                await self._end_intercept_session(clear_debounce=clear_debounce)
            elif clear_debounce:
                self._last_intercepted_track_id = None
                self._last_intercept_time = 0.0

    def _handle_physical_pause(self) -> None:
        """Handle physical pause pressed on the speaker during external playback."""
        _LOGGER.debug(
            "[%s] Physical pause detected during external playback",
            self.player_id,
        )
        self._external_playing = False
        self._external_media = None
        self._external_play_confirmed = False
        self._needs_replay = True
        self._cancel_voice_resume()
        self._attr_playback_state = PlaybackState.PAUSED

    def _update_playback_state(self, playing: bool, alice_state: str) -> None:
        """Update playback state from Glagol data."""
        if self._external_playing:
            if self._voice_control_enabled and alice_state not in ("IDLE", ""):
                self._handle_voice_interrupt(alice_state)
            elif playing:
                # Station confirmed external stream is actually playing.
                self._external_play_confirmed = True
                self._attr_playback_state = PlaybackState.PLAYING
                self._attr_powered = True
            elif self._external_play_confirmed and alice_state in ("IDLE", ""):
                # Stream was playing, now stopped without voice trigger →
                # user pressed physical pause/stop on the speaker.
                self._handle_physical_pause()
            else:
                # Startup window: radio_play sent but station not yet fetching.
                # Stay optimistically PLAYING until we observe playing=True.
                self._attr_playback_state = PlaybackState.PLAYING
                self._attr_powered = True
        elif playing:
            if self._needs_replay:
                _LOGGER.debug(
                    "[%s] Native player active after voice cmd — accepting",
                    self.player_id,
                )
                self._needs_replay = False
                if self._voice_resume_task:
                    self._voice_resume_task.cancel()
                    self._voice_resume_task = None
            self._attr_playback_state = PlaybackState.PLAYING
            self._attr_powered = True
        else:
            if self._voice_control_enabled and self._needs_replay:
                self._handle_voice_end(alice_state)
            if self._attr_playback_state == PlaybackState.PLAYING:
                self._attr_playback_state = PlaybackState.IDLE

    def _on_glagol_update(self, data: dict[str, Any] | None) -> None:
        """
        Handle state update from Glagol WebSocket.

        Called from the WebSocket receive loop (already in asyncio context).
        """
        if data is None:
            self._attr_available = False
            self.update_state()
            return

        self._attr_available = True

        state = data.get("state", {})
        if not state:
            _LOGGER.debug(
                "[%s] No 'state' in Glagol data, keys: %s", self.player_id, list(data.keys())
            )
            self.update_state()
            return

        player_state = state.get("playerState", {})
        playing = state.get("playing", False)
        extra = player_state.get("extra", {})

        _LOGGER.debug(
            "[%s] playing=%s vol=%s prog=%s dur=%s title=%s extra=%s alice=%s",
            self.player_id,
            playing,
            state.get("volume"),
            player_state.get("progress"),
            player_state.get("duration"),
            (player_state.get("title", "") or "")[:30],
            list(extra.keys()) if extra else None,
            state.get("aliceState"),
        )

        # Volume (0.0-1.0 → 0-100)
        if "volume" in state:
            self._attr_volume_level = round(state["volume"] * 100)

        alice_state = state.get("aliceState", "")
        # Snapshot prev BEFORE the assignment below so _handle_intercept_tick
        # (scheduled via mass.create_task — runs after this function returns)
        # can detect the LISTENING/SPEAKING → IDLE edge.  Reading the field
        # inside the task would always observe the post-assignment value.
        prev_alice_state_snapshot = self._prev_alice_state

        self._update_playback_state(playing, alice_state)

        self._prev_alice_state = alice_state

        # Power detection from alice state (only when not in external playback)
        if not self._external_playing:
            if alice_state == "IDLE" and not playing:
                self._attr_powered = self._attr_powered  # keep user-set value
            else:
                self._attr_powered = True

        # Elapsed time
        progress = player_state.get("progress", 0)
        duration = player_state.get("duration", 0)

        if self._external_playing and self._external_media:
            # Glagol reports stale progress/media from native player.
            # Keep our optimistic elapsed_time ticking and use external media info.
            self.set_current_media(
                uri=self._external_media.uri or "",
                title=self._external_media.title or "",
                artist=self._external_media.artist or "",
                duration=self._external_media.duration or None,
                image_url=self._external_media.image_url or None,
            )
        else:
            self._attr_elapsed_time = progress
            self._attr_elapsed_time_last_updated = time.time()

            # Current media info
            title = player_state.get("title")
            subtitle = player_state.get("subtitle")

            if title or playing:
                self.set_current_media(
                    uri=player_state.get("id", ""),
                    title=title or "",
                    artist=subtitle or "",
                    duration=int(duration) if duration else None,
                )
            elif not playing:
                self._attr_current_media = None

        if self._intercept_enabled and self._intercept_target_player_id:
            self.mass.create_task(
                self._handle_intercept_tick(state, player_state, playing, prev_alice_state_snapshot)
            )

        self.update_state()
