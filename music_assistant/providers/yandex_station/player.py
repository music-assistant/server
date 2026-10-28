"""Yandex Station Player — transport control and state management."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

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
from music_assistant_models.errors import (
    PlayerCommandFailed,
    PlayerUnavailableError,
    UnsupportedFeaturedException,
)
from music_assistant_models.player import PlayerSource

from music_assistant.constants import (
    CONF_ENTRY_HTTP_PROFILE_DEFAULT_3,
    CONF_ENTRY_PREFER_WAV_FOR_LIVE_SOURCES_DEFAULT_ENABLED,
    EXTERNAL_PAUSE_IDLE_TIMEOUT,
    create_output_codec_config_entry,
)
from music_assistant.helpers.config_entries import PLAYBACK_TARGET_TYPES
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia

from . import protobuf
from .alice import AliceActivity, AliceTracker, AliceUpdate
from .constants import (
    CONF_INTERCEPT_ENABLED,
    CONF_INTERCEPT_FEATURE_ENABLED,
    CONF_INTERCEPT_MIRROR_VOLUME,
    CONF_INTERCEPT_TARGET,
    CONF_VOICE_CONTROL,
)
from .voice import VoiceRecovery, VoiceRecoveryPhase

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

# The Station's own playback (Yandex Music, radio, ... started through Alice or the app).
STATION_SOURCE_ID = "yandex_station"
# Seconds after an Alice request ends before the stream it stopped is resumed.
VOICE_RESUME_DELAY = 3


def _external_command(name: str, payload: dict[str, Any] | str | None = None) -> dict[str, Any]:
    """Build an externalCommandBypass command for Glagol."""
    data: dict[int, str] = {1: name}
    if payload:
        data[2] = json.dumps(payload) if isinstance(payload, dict) else payload
    return {
        "command": "externalCommandBypass",
        "data": base64.b64encode(protobuf.dumps(data)).decode(),
    }


def _stream_command(url: str, media: PlayerMedia | None, *, audio_client: bool) -> dict[str, Any]:
    """Build the firmware-compatible command for an external audio URL."""
    if not audio_client:
        return _external_command(
            "radio_play",
            {"streamUrl": url, "force_restart_player": True},
        )

    is_hls = urlsplit(url).path.lower().endswith(".m3u8")
    payload: dict[str, Any] = {
        "stream": {
            "url": url,
            # Firmware rejects the command before fetching the URL unless format
            # is "MP3" (or "HLS" for playlists); it then detects WAV, FLAC or AAC
            # from the stream itself (verified on hardware, AlexxIT/YandexStation#802).
            "format": "HLS" if is_hls else "MP3",
            "type": "FmRadio" if is_hls else "Track",
            "offset_ms": 0,
        },
        "set_pause": False,
    }
    if media:
        metadata: dict[str, str] = {}
        if title := getattr(media, "title", None):
            metadata["title"] = title
        if artist := getattr(media, "artist", None):
            metadata["subtitle"] = artist
        if image_url := getattr(media, "image_url", None):
            metadata["art_image_url"] = image_url.removeprefix("https://")
        if metadata:
            payload["metadata"] = metadata
    return _external_command("audio_play", payload)


def _parse_yandex_track_id(raw: str) -> str:
    """
    Extract numeric Yandex Music track ID from Glagol playerState.id.

    Glagol may emit `12345` (plain) or `12345:67890` (track:album).  Strip the
    album suffix so the value is consumable by the yandex_music music provider.
    """
    return raw.split(":", 1)[0].strip()


def _cover_url(extra: dict[str, Any] | None) -> str | None:
    """Return the image URL for a cover template the Station reports, if any."""
    cover = (extra or {}).get("coverURI")
    if not isinstance(cover, str) or not cover:
        return None
    return "https://" + cover.removeprefix("https://").replace("%%", "400x400")


def _reports_handed_stream(player_state: dict[str, Any]) -> bool | None:
    """
    Return whether a report describes a stream handed over with audio_play.

    The Station reports an empty track id for such a stream and the Yandex Music
    track id for its own playback. None means the report does not say.
    """
    track_id = player_state.get("id")
    if not isinstance(track_id, str):
        return None
    return track_id == ""


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

        # Feature advertised by current firmware for cloud-compatible URL playback.
        self._audio_client = False
        # The Music Assistant stream the Station was told to play, until it ends.
        self._external_playing = False
        self._external_audio_client = False
        self._external_media: PlayerMedia | None = None
        # Claimed by each play_media() so a superseded request is dropped.
        self._external_play_generation = 0
        # The generation and owning queue of a play_media() whose stream URL is
        # still resolving, so an Alice request that starts meanwhile can hold it.
        self._resolving_play: tuple[int, str] | None = None
        # stop() calls waiting for the Station; its stopped reports are theirs.
        self._stops_in_flight = 0
        # Becomes True once Glagol reports playing=True during external playback.
        # Used to distinguish the startup window (station fetching stream) from
        # a user-initiated physical pause on the speaker.
        self._external_play_confirmed = False
        # A playing=True update can belong to the source the stream replaces when
        # it cannot be told apart (legacy radio_play, or another MA stream). Only
        # accept it after a non-playing boundary.
        self._external_stop_observed = False
        # Alice's activity, followed through the reported aliceState.
        self._alice = AliceTracker()
        # Recovery of Music Assistant playback an Alice request stopped or delayed.
        self._voice = VoiceRecovery()
        # Track id of the Station's own playback in the last report.
        self._station_track_id = ""
        # Counts playback the Station started on its own; a command response older
        # than such a start must not overwrite it.
        self._station_playback_starts = 0

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
        # Set while the Station is unmuted for a standalone server-action phrase.
        self._station_unmuted_for_phrase: bool = False
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
        self._attr_source_list = [
            PlayerSource(
                id=STATION_SOURCE_ID,
                name="Yandex Station",
                passive=True,
                can_play_pause=True,
                can_seek=True,
                can_next_previous=True,
            )
        ]
        # The Station reports exactly when it plays on its own, so that playback
        # takes over from the Music Assistant queue.
        self._attr_trusts_reported_source = True
        # A paused Station source stops overriding the MA queue after a while.
        self._attr_external_pause_idle_timeout = EXTERNAL_PAUSE_IDLE_TIMEOUT
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

        Default to WAV for prompt queue and live-source playback, with the
        forced_content_length HTTP profile required by the Station.
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
        # so the dropdown is easy to scan.  Non-audio player types
        # (capture-only sources, lights, displays) can never render a
        # track, so those are excluded by type.
        target_options = sorted(
            (
                ConfigValueOption(p.player_id, p.display_name)
                for p in self.mass.players.all_players(return_unavailable=True)
                if p.player_id != self.player_id and p.type in PLAYBACK_TARGET_TYPES
            ),
            key=lambda o: (o.title or "").lower(),
        )
        return [
            create_output_codec_config_entry(default_value="wav"),
            CONF_ENTRY_PREFER_WAV_FOR_LIVE_SOURCES_DEFAULT_ENABLED,
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
            ConfigEntry(
                key=CONF_INTERCEPT_MIRROR_VOLUME,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                required=False,
            ),
        ]

    def update_connection(self, host: str, port: int) -> None:
        """
        Update the station's address after an mDNS change.

        Does not reconnect; call ``async_setup()`` to (re)connect.

        :param host: New IP address of the station.
        :param port: New Glagol port.
        """
        self._device_info["host"] = host
        self._device_info["port"] = port
        self.glagol.device["host"] = host
        self.glagol.device["port"] = port
        self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, host)

    # ── Transport controls ───────────────────────────────────────

    async def power(self, powered: bool) -> None:
        """
        Power the station on (resume playback) or off (return to the home screen).

        :param powered: True to power on, False to power off.
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
        """Resume the Station's own playback, or the MA queue an Alice request stopped."""
        if self._voice.holds_playback:
            # The stream Alice stopped is gone from the Station; play resumes its
            # queue now instead of waiting for her answer or the scheduled resume.
            source = self._voice.source or self.player_id
            self._end_voice_interruption()
            await self.mass.players.cmd_resume(self.player_id, source=source)
            return
        # The user's command supersedes an auto-resume after Alice, whatever its outcome.
        self._end_voice_interruption()
        result = await self.glagol.send({"command": "play"})
        _raise_if_failed(result, "play")

    async def pause(self) -> None:
        """
        Pause the Station's own playback.

        The Station cannot pause a Music Assistant stream, so the player only advertises
        pausing while none plays and the core stops such a stream instead.
        """
        if (
            self._external_playing
            or self._voice.holds_playback
            or self._voice.awaits_resumed_stream
        ):
            # A stream Alice stopped is gone from the Station like a playing one,
            # also while the resume prepares it; pausing it is stopping it, so play
            # resumes its queue afterwards.
            await self.stop()
            return
        self._end_voice_interruption()
        # A pending play_media() must not start playback after the pause.
        self._external_play_generation += 1
        result = await self.glagol.send({"command": "stop"})
        _raise_if_failed(result, "pause")

    async def stop(self) -> None:
        """Stop playback, including a pending play_media() request."""
        self._end_voice_interruption()
        self._external_play_generation += 1
        generation = self._external_play_generation
        station_starts = self._station_playback_starts
        # audio_client sessions stop with the native stop command; legacy bypass
        # sessions only stop when an invalid radio URL replaces them.
        command = (
            _external_command("radio_play", {"streamUrl": "http://0.0.0.0/stop.flac"})
            if self._external_playing and not self._external_audio_client
            else {"command": "stop"}
        )
        self._stops_in_flight += 1
        try:
            result = await self.glagol.send(command)
        finally:
            self._stops_in_flight -= 1
        _raise_if_failed(result, "stop")
        # A newer play_media() owns the session if it started meanwhile, and playback
        # the Station started after the stop is newer than this response.
        if (
            self._external_play_generation == generation
            and self._station_playback_starts == station_starts
        ):
            self._end_external_session()
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
        """Play media using the directive supported by the station firmware."""
        # The resume after Alice asked the core for this stream; whatever overruled
        # that resume meanwhile (the Station's own playback, a new request) stands.
        resumed = self._voice.resuming
        if resumed:
            if self._drop_resumed_stream():
                return
        else:
            self._end_voice_interruption()
        self._external_play_generation += 1
        generation = self._external_play_generation
        _LOGGER.debug("[%s] play_media called: %s", self.player_id, media.title or media.uri)
        if not resumed:
            self._resolving_play = (generation, media.source_id or self.player_id)
        try:
            stream_url = await self.provider.mass.streams.resolve_stream_url(self.player_id, media)
        finally:
            if self._resolving_play is not None and self._resolving_play[0] == generation:
                self._resolving_play = None
        if self._external_play_generation != generation or (
            resumed and self._drop_resumed_stream()
        ):
            return
        if self._voice_control_enabled and self._alice.activity == AliceActivity.REQUEST:
            # Alice is handling a request; start the queue once she is done.
            _LOGGER.debug("[%s] Alice active — deferring play_media", self.player_id)
            self._voice.begin(media.source_id or self.player_id, self._attr_volume_level or 0)
            if self._attr_playback_state != PlaybackState.PLAYING:
                self._attr_playback_state = PlaybackState.PAUSED
                self.update_state()
            return
        _LOGGER.debug("[%s] Stream URL resolved (length=%d)", self.player_id, len(stream_url))

        audio_client = self._audio_client
        command = _stream_command(stream_url, media, audio_client=audio_client)
        directive = "audio_play" if audio_client else "radio_play"

        self._start_external_session(media, audio_client=audio_client)
        try:
            result = await self.glagol.send(command)
            _LOGGER.debug("[%s] %s result: %s", self.player_id, directive, result)
            _raise_if_failed(result, directive)
        except PlayerCommandFailed, asyncio.CancelledError:
            # Only clear the session this call started; a newer play_media
            # may already own the external session.
            if self._external_play_generation == generation:
                self._end_external_session()
            raise

        if self._external_play_generation != generation or not self._external_playing:
            return

        # Legacy bypass playback is optimistic; audio_play is later confirmed
        # by the station's playerState updates.
        self._attr_playback_state = PlaybackState.PLAYING
        self._attr_powered = True
        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time.time()
        self._publish_media(media)
        self.update_state()

    async def play_announcement(
        self, announcement: PlayerMedia, volume_level: int | None = None
    ) -> None:
        """
        Play an announcement and return once it has finished.

        :param announcement: The announcement media to play.
        :param volume_level: Temporary volume for the announcement, restored afterwards.
        """
        # The title is always the literal "Announcement", so Alice TTS cannot
        # speak it; the audio (with optional chime) is streamed from its URI.
        announcement_timeout = 30

        # Adjust volume before announcement if requested
        saved_volume: int | None = None
        if volume_level is not None and volume_level != self._attr_volume_level:
            saved_volume = self._attr_volume_level
            await self.volume_set(volume_level)

        try:
            _LOGGER.debug("[%s] Audio announcement: %s", self.player_id, announcement.uri)
            result = await self.glagol.send(
                _stream_command(announcement.uri, announcement, audio_client=self._audio_client)
            )
            _raise_if_failed(result, "Audio announcement")
            # externalCommandBypass playback doesn't update state.playing,
            # so we can't observe completion — wait by known duration instead.
            duration = await self.mass.streams.get_announcement_duration(announcement)
            wait_time = (
                min(duration + 1, announcement_timeout)
                if duration and duration > 0
                else announcement_timeout
            )
            await asyncio.sleep(wait_time)
        finally:
            if saved_volume is not None:
                await self.volume_set(saved_volume)

    async def say(self, text: str) -> None:
        """
        Make the Station say a phrase in Alice's voice.

        :param text: The phrase to say.
        """
        _raise_if_failed(await self.glagol.send_tts(text), "say")

    async def send_command(self, text: str) -> None:
        """
        Hand a text request to Alice as if the user had spoken it.

        :param text: The request, e.g. "включи свет на кухне".
        """
        _raise_if_failed(await self.glagol.send_text(text), "send_command")

    async def on_unload(self) -> None:
        """Clean up on player unload."""
        self._end_voice_interruption()
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

    @property
    def _intercept_mirror_volume(self) -> bool:
        """Whether Station volume changes are copied to the intercept target."""
        return bool(self._config.get_value(CONF_INTERCEPT_MIRROR_VOLUME, True))

    @property
    def _voice_resume_task_id(self) -> str:
        """Scheduler id of the auto-resume after an Alice request."""
        return f"yandex_station_voice_resume_{self.player_id}"

    def _start_external_session(self, media: PlayerMedia, *, audio_client: bool) -> None:
        """Record the Music Assistant stream the Station is about to be told to play."""
        # A stream that replaces another MA stream is only attributed after a
        # stopped report: both report an empty track id. Legacy radio_play
        # reports carry no track id, so its stream always needs that boundary.
        replaces_stream = self._external_playing and (
            self._attr_playback_state == PlaybackState.PLAYING
        )
        self._external_playing = True
        self._external_audio_client = audio_client
        self._external_media = media
        self._external_play_confirmed = False
        self._external_stop_observed = (
            not replaces_stream
            if audio_client
            else self._attr_playback_state != PlaybackState.PLAYING
        )
        # MA-managed playback reports no source of its own, so the core resolves the
        # owning queue (a universal group stops a paused member whose source differs).
        self._attr_active_source = None
        # The Station cannot pause this stream; the core stops it instead.
        self._attr_supported_features = self._attr_supported_features - {PlayerFeature.PAUSE}

    def _end_external_session(self) -> None:
        """Forget the Music Assistant stream once the Station no longer plays it."""
        self._external_playing = False
        self._external_audio_client = False
        self._external_media = None
        self._external_play_confirmed = False
        self._external_stop_observed = False
        self._attr_supported_features = self._attr_supported_features | {PlayerFeature.PAUSE}

    def _interrupt_for_voice(self) -> None:
        """Record that an Alice request stopped the Music Assistant stream."""
        _LOGGER.debug("[%s] Alice stopped the MA stream", self.player_id)
        # The next track MA is preparing must not start over Alice either.
        self._external_play_generation += 1
        source = self._external_media.source_id if self._external_media else None
        self._end_external_session()
        # Paused, not idle: the core passes a user's stop or pause on to the player
        # only while it is not idle, and that command must cancel the auto-resume.
        self._attr_playback_state = PlaybackState.PAUSED
        # The queue that owned the stream (a group's queue for a group stream) is
        # the one resumed.
        self._voice.begin(source or self.player_id, self._attr_volume_level or 0)

    def _finish_voice_interruption(self, spoke: bool) -> None:
        """
        Schedule the resumption once the request that interrupted playback has ended.

        :param spoke: Whether Alice answered the request.
        """
        if not self._voice.request_ended(spoke=spoke, volume=self._attr_volume_level or 0):
            if self._voice.holds_playback:
                # Resumed once the resume the core is running has returned.
                return
            _LOGGER.debug("[%s] Silent voice command — staying stopped", self.player_id)
            if self._attr_playback_state == PlaybackState.PAUSED:
                self._attr_playback_state = PlaybackState.IDLE
            return
        _LOGGER.debug("[%s] Voice command ended — scheduling auto-resume", self.player_id)
        self.mass.call_later(
            VOICE_RESUME_DELAY, self._resume_after_voice, task_id=self._voice_resume_task_id
        )

    def _hold_resolving_play_for_request(self) -> None:
        """Hold a play_media() whose URL still resolves until the Alice request that just started ends."""
        if not self._voice_control_enabled or self._resolving_play is None:
            return
        generation, source = self._resolving_play
        self._resolving_play = None
        if generation != self._external_play_generation:
            return
        _LOGGER.debug("[%s] Alice request — holding the pending play_media", self.player_id)
        self._external_play_generation += 1
        self._voice.begin(source, self._attr_volume_level or 0)
        if self._attr_playback_state != PlaybackState.PLAYING:
            self._attr_playback_state = PlaybackState.PAUSED

    def _defer_voice_resume_to_new_request(self) -> None:
        """Hand a scheduled auto-resume over to an Alice request that just started."""
        if self._voice.request_started(self._attr_volume_level or 0):
            _LOGGER.debug("[%s] New Alice request — it decides the auto-resume", self.player_id)
            self.mass.cancel_timer(self._voice_resume_task_id)

    def _end_voice_interruption(self) -> None:
        """Drop a pending auto-resume after Alice; something else now decides playback."""
        self._voice.cancel()
        self.mass.cancel_timer(self._voice_resume_task_id)

    def _drop_resumed_stream(self) -> bool:
        """Return True, and record the drop, if the running resume's stream must not play."""
        if not self._voice.drops_resumed_stream:
            return False
        _LOGGER.debug("[%s] Voice resume overruled — dropping its stream", self.player_id)
        self._voice.drop_resumed_stream()
        return True

    async def _resume_after_voice(self) -> None:
        """Resume the Music Assistant queue Alice's request stopped or delayed."""
        if (source := self._voice.start_resume()) is None:
            return
        try:
            await self.mass.players.cmd_resume(self.player_id, source=source)
        except (PlayerCommandFailed, PlayerUnavailableError) as exc:
            _LOGGER.warning("[%s] Resume after voice command failed: %s", self.player_id, exc)
        finally:
            if self._voice.finish_resume():
                # A request Alice answered after the stream was dropped; scheduled
                # only now, so it does not replace the resume that was running.
                self.mass.call_later(
                    VOICE_RESUME_DELAY, self._resume_after_voice, task_id=self._voice_resume_task_id
                )

    # ── Intercept feature ────────────────────────────────────────

    async def _handle_intercept_tick(
        self,
        state: dict[str, Any],
        player_state: dict[str, Any],
        playing: bool,
        alice: AliceUpdate,
    ) -> None:
        """
        Dispatch intercept actions for a single Glagol state update.

        Holds ``_intercept_lock`` for the whole tick so back-to-back WS
        updates (each scheduled as its own background task by the dispatcher)
        can't apply mirror operations out of order — e.g. an older volume
        write completing after a newer one and leaving the target stale.

        ``alice`` is Alice's activity after this update, captured by
        ``_on_glagol_update`` when the update arrived; the tick runs later as
        a background task, after newer updates may have advanced the tracker.
        """
        if self._external_playing:
            return  # our own bypass stream — never intercept

        async with self._intercept_lock:
            track_id = (player_state.get("id") or "").strip()
            progress = int(player_state.get("progress") or 0)

            # Alice activates → unmute Station so her TTS is audible (the
            # Station mixes Alice voice into the music output, so a vol=0
            # Station can drown out / quiet her reply on some firmwares),
            # pause the target once so she's heard cleanly, and skip a
            # new handoff until she goes IDLE — also without a session, or the
            # target would start over her answer.
            if alice.activity == AliceActivity.REQUEST:
                if self._intercept_active:
                    await self._unmute_station_for_alice()
                    if alice.started:
                        await self._pause_target(clear_session=False, clear_debounce=True)
                return

            if self._intercept_active and alice.activity == AliceActivity.PHRASE:
                # A standalone server-action phrase: some firmware ties Alice's
                # speech to the Station volume, so unmute it for the phrase but
                # leave the target playing.
                if self._station_muted_by_intercept:
                    self._station_unmuted_for_phrase = await self._unmute_station_for_alice()
                return

            # Alice done → re-mute Station.  Without this, the Station would
            # resume audible playback and double up with the target.  Same
            # validation reason as in _unmute_station_for_alice(): don't flip
            # the flag unless the device confirms success.
            if (
                self._intercept_active
                and (
                    alice.ended == AliceActivity.REQUEST
                    or (alice.ended == AliceActivity.PHRASE and self._station_unmuted_for_phrase)
                )
                and not self._station_muted_by_intercept
            ):
                try:
                    result = await self.glagol.send({"command": "setVolume", "volume": 0.0})
                    _raise_if_failed(result, "alice-remute")
                    self._station_muted_by_intercept = True
                except Exception as exc:
                    _LOGGER.debug("[%s] alice-remute failed: %s", self.player_id, exc)
            if alice.ended is not None:
                self._station_unmuted_for_phrase = False

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

    async def _unmute_station_for_alice(self) -> bool:
        """
        Restore the Station volume the intercept session muted, so Alice is audible.

        Returns True if the Station confirmed the new volume.
        """
        if not self._station_muted_by_intercept or self._saved_station_volume is None:
            return False
        # YandexGlagol.send() reports failures via {"error": ...} rather than
        # raising, so the result is validated before the mute flag is cleared;
        # otherwise a timeout would leave the Station muted and Alice inaudible.
        try:
            result = await self.glagol.send(
                {"command": "setVolume", "volume": self._saved_station_volume / 100}
            )
            _raise_if_failed(result, "alice-unmute")
        except Exception as exc:
            _LOGGER.debug("[%s] alice-unmute failed: %s", self.player_id, exc)
            return False
        self._station_muted_by_intercept = False
        # Pre-set the mirror baseline so the next state update (with the new
        # non-zero Station volume) doesn't bounce that volume to the target.
        self._last_mirrored_volume = self._saved_station_volume
        return True

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

        Skipped when the player's volume-mirror setting is off; the
        self-mute bookkeeping below still runs so the Station volume is
        restored correctly when the session ends.

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
        if not self._intercept_mirror_volume:
            return
        if target_vol != self._last_mirrored_volume:
            try:
                await self.mass.players.cmd_volume_set(target_id, target_vol)
            except UnsupportedFeaturedException, PlayerUnavailableError:
                _LOGGER.debug(
                    "[%s] target %s does not support volume_set or is unavailable"
                    " — skipping mirror",
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
            except UnsupportedFeaturedException, PlayerUnavailableError:
                _LOGGER.debug(
                    "[%s] target %s does not support seek or is unavailable — skipping mirror",
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

    # ── State updates from Glagol WebSocket ──────────────────────

    def _publish_media(self, media: PlayerMedia) -> None:
        """
        Publish what the player plays as its current media.

        The core's setter merges into the previous media and keeps fields it is
        given empty, so a different item starts from a clean slate instead of
        inheriting the previous item's type or cover.
        """
        current = self._attr_current_media
        self.set_current_media(
            uri=media.uri or "",
            media_type=media.media_type,
            title=media.title,
            artist=media.artist,
            album=media.album,
            image_url=media.image_url,
            duration=media.duration,
            source_id=media.source_id,
            queue_item_id=media.queue_item_id,
            clear_all=current is None or current.uri != (media.uri or ""),
        )

    def _station_track_uri(self, player_state: dict[str, Any]) -> tuple[str, MediaType]:
        """
        Return the URI and media type for the Station's own playback.

        A track becomes an item of the linked Yandex Music instance, which shares the
        Station's account, so the core can add it to favorites; anything else keeps
        the reported id.
        """
        reported_id = str(player_state.get("id") or "")
        instance = cast("YandexStationProvider", self.provider).linked_music_instance
        if instance and reported_id and player_state.get("type") == "Track":
            return f"{instance}://track/{_parse_yandex_track_id(reported_id)}", MediaType.TRACK
        return reported_id, MediaType.UNKNOWN

    def _update_playback_state(
        self,
        playing: bool,
        alice: AliceUpdate,
        stream_reported: bool | None = None,
        progress: float = 0,
        duration: float = 0,
        native_track_id: str = "",
    ) -> None:
        """Apply one Station playback report to the player state."""
        if (
            self._external_playing
            and playing
            and self._external_play_confirmed
            and stream_reported is False
        ):
            # The Station plays its own content without a stop in between, e.g.
            # music Alice was asked for: the stream is gone.
            _LOGGER.debug("[%s] Station playback replaced the MA stream", self.player_id)
            self._external_play_generation += 1
            self._end_external_session()
        if self._external_playing:
            # While Alice handles a request the Station usually only ducks the
            # stream; it is interrupted only once the Station reports it stopped.
            if (
                self._voice_control_enabled
                and alice.activity == AliceActivity.REQUEST
                and not playing
                and not self._stops_in_flight
            ):
                self._interrupt_for_voice()
            elif playing:
                if self._external_audio_client:
                    # The Station reports the stream it was handed with an empty
                    # track id; until then it still reports the source it replaces.
                    if stream_reported and self._external_stop_observed:
                        self._external_play_confirmed = True
                elif self._external_stop_observed:
                    self._external_play_confirmed = True
                self._attr_playback_state = PlaybackState.PLAYING
                self._attr_powered = True
            elif (
                self._external_play_confirmed
                and (alice.activity != AliceActivity.REQUEST or self._stops_in_flight)
                and stream_reported is not False
            ):
                # The stream stopped without Alice: it finished, or the user stopped
                # it on the Station. Either way it is gone; the core resumes the queue.
                _LOGGER.debug("[%s] MA stream stopped on the Station", self.player_id)
                if not (self._external_audio_client and duration and progress >= duration):
                    # Stopped by the user: the next track MA prepares must not start.
                    self._external_play_generation += 1
                self._end_external_session()
                self._attr_playback_state = PlaybackState.IDLE
            else:
                # Startup window: the stream was sent but the Station does not play
                # it yet; the stopped report is the boundary after the source it
                # replaces.
                self._external_stop_observed = True
                self._attr_playback_state = PlaybackState.PLAYING
                self._attr_powered = True
        elif playing and stream_reported:
            # A stream handed to the Station outside a session is one that already
            # ended, still reported for a moment after the stop; it is not the
            # Station's own playback.
            _LOGGER.debug("[%s] Ignoring a report of an ended MA stream", self.player_id)
        elif playing:
            if (
                self._attr_playback_state != PlaybackState.PLAYING
                or native_track_id != self._station_track_id
            ):
                # The Station started playing on its own, e.g. a track Alice was
                # asked for; ducked playback that just continues does not count.
                self._station_playback_starts += 1
                self._end_voice_interruption()
            self._station_track_id = native_track_id
            self._attr_active_source = STATION_SOURCE_ID
            self._attr_playback_state = PlaybackState.PLAYING
            self._attr_powered = True
        elif self._attr_playback_state == PlaybackState.PLAYING:
            # Native stop and pause are the same command; play resumes either.
            self._attr_playback_state = PlaybackState.PAUSED

        if alice.started and alice.activity == AliceActivity.REQUEST:
            self._hold_resolving_play_for_request()
            self._defer_voice_resume_to_new_request()
        if (
            alice.ended == AliceActivity.REQUEST
            and self._voice.phase == VoiceRecoveryPhase.AWAITING_ANSWER
        ):
            self._finish_voice_interruption(alice.spoke)

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

        features = data.get("supported_features")
        if isinstance(features, (list, tuple, set, frozenset)):
            audio_client = "audio_client" in features
            if audio_client != self._audio_client:
                _LOGGER.debug(
                    "[%s] audio_client capability: %s",
                    self.player_id,
                    audio_client,
                )
            self._audio_client = audio_client

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

        # Firmware without audio_client reports no track id for its streams.
        stream_reported = (
            _reports_handed_stream(player_state)
            if self._audio_client or self._external_audio_client
            else None
        )

        _LOGGER.debug(
            "[%s] playing=%s vol=%s prog=%s dur=%s title=%s extra=%s alice=%s "
            "requestID=%s id=%s type=%s playerType=%s",
            self.player_id,
            playing,
            state.get("volume"),
            player_state.get("progress"),
            player_state.get("duration"),
            (player_state.get("title", "") or "")[:30],
            list(extra.keys()) if extra else None,
            state.get("aliceState"),
            extra.get("requestID") if extra else None,
            player_state.get("id"),
            player_state.get("type"),
            player_state.get("playerType"),
        )

        # Volume (0.0-1.0 → 0-100)
        if "volume" in state:
            self._attr_volume_level = round(state["volume"] * 100)

        alice_state = state.get("aliceState", "")
        alice = self._alice.feed(alice_state)

        self._update_playback_state(
            playing,
            alice,
            stream_reported,
            player_state.get("progress", 0),
            player_state.get("duration", 0),
            (player_state.get("id") or "").strip(),
        )

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
            # Keep requested MA metadata. audio_play reports reliable progress,
            # while legacy radio_play can still expose stale native state.
            if self._external_audio_client and stream_reported is not False:
                self._attr_elapsed_time = progress
                self._attr_elapsed_time_last_updated = time.time()
            self._publish_media(self._external_media)
        else:
            self._attr_elapsed_time = progress
            self._attr_elapsed_time_last_updated = time.time()

            # Current media info
            title = player_state.get("title")
            subtitle = player_state.get("subtitle")

            if title or playing:
                uri, media_type = self._station_track_uri(player_state)
                self._publish_media(
                    PlayerMedia(
                        uri=uri,
                        media_type=media_type,
                        title=title or None,
                        artist=subtitle or None,
                        duration=int(duration) if duration else None,
                        image_url=_cover_url(extra),
                        source_id=STATION_SOURCE_ID,
                    )
                )
            elif not playing:
                self._attr_current_media = None

        if self._intercept_enabled and self._intercept_target_player_id:
            self.mass.create_task(self._handle_intercept_tick(state, player_state, playing, alice))

        self.update_state()
