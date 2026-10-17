"""Web Kiosk Player implementation."""

from __future__ import annotations

import math
import re
import secrets
import time
from collections.abc import Iterator
from contextlib import AbstractAsyncContextManager, contextmanager
from typing import TYPE_CHECKING, Any, NamedTuple, cast
from uuid import NAMESPACE_URL, uuid5

from music_assistant_models.enums import IdentifierType, PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import (
    MusicAssistantError,
    PlayerUnavailableError,
    QueueEmpty,
    UnsupportedFeaturedException,
)
from music_assistant_models.player import DeviceInfo

from music_assistant.constants import CONF_ENTRY_OUTPUT_CODEC_DEFAULT_MP3
from music_assistant.models.player import Player, PlayerMedia

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry

    from music_assistant.mass import MusicAssistant

    from .ownership import VerifiedSendspinClaim
    from .provider import WebKioskProvider

# Seconds the clock runs on without browser evidence: two position reports.
CLOCK_HOLD_AFTER = 10.0
# Loads of the next queue item before giving up when queue edits keep removing it.
SUCCESSOR_LOAD_ATTEMPTS = 3
QUEUE_PAGE_SIZE = 500


class TransportMark(NamedTuple):
    """The play, pause and stop commands the player had handled at one moment."""

    commands: int
    paused: bool


def playback_lock(mass: MusicAssistant, player_id: str) -> AbstractAsyncContextManager[None]:
    """
    Return the strict playback lock for state checks followed by playback commands.

    Music Assistant serializes playback commands per player and its group with this
    lock. Reading playback state and dispatching a command under it makes a stale
    browser report or a concurrent stop see the outcome of the other operation.

    :param mass: The Music Assistant instance.
    :param player_id: The player whose playback is read and changed.
    """
    return mass.players.get_group_and_player_lock(player_id, strict=True)


def _positive_seconds(value: object) -> float | None:
    """Return a duration in seconds, or None when the value is missing."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value)


def _queue_span(order: list[str], first: str, last: str) -> list[str] | None:
    """Return the queue item IDs from first to last, or None if they are not in that order."""
    try:
        start = order.index(first)
        end = order.index(last, start + 1)
    except ValueError:
        return None
    return order[start : end + 1]


def media_timeline(media: object) -> tuple[float | None, float]:
    """
    Return the song length and where the served audio starts in that song.

    After a seek, Music Assistant hands the player a stream that begins at the
    seek point, so ``stream_duration`` is only the remainder. The audio element
    clock still starts at 0. Callers add this offset when they draw the song.
    """
    duration = _positive_seconds(getattr(media, "duration", None))
    stream = _positive_seconds(getattr(media, "stream_duration", None))
    if duration is None:
        return stream, 0.0
    if stream is not None and stream < duration:
        return duration, duration - stream
    return duration, 0.0


def browser_device_info(user_agent: str) -> tuple[str, str, str | None]:
    """Describe browser software without guessing the physical device manufacturer."""
    agent = user_agent[:1024]
    name, vendor, version = "Web browser", "Unknown", None
    for browser, manufacturer, pattern in (
        ("Samsung Internet", "Samsung", r"SamsungBrowser/([\d.]+)"),
        ("Edge", "Microsoft", r"Edg(?:e|A|iOS)?/([\d.]+)"),
        ("Opera", "Opera", r"(?:OPR|Opera)/([\d.]+)"),
        ("Firefox", "Mozilla", r"(?:Firefox|FxiOS)/([\d.]+)"),
        ("Chrome", "Google Inc.", r"(?:Chrome|CriOS)/([\d.]+)"),
        ("Safari", "Apple", r"Version/([\d.]+).*Safari/"),
    ):
        if match := re.search(pattern, agent):
            name, vendor, version = browser, manufacturer, match.group(1)[:40]
            break
    model = f"{name} {version.split('.')[0]}" if version else name
    for marker, platform in (
        ("Android", "Android"),
        ("iPhone", "iOS"),
        ("iPad", "iPadOS"),
        ("Tizen", "Tizen"),
        ("Web0S", "webOS"),
        ("webOS", "webOS"),
        (r"Cr[O]S", "ChromeOS"),
        ("Windows", "Windows"),
        ("Macintosh", "macOS"),
        ("Linux", "Linux"),
    ):
        if re.search(marker, agent):
            model += f" ({platform})"
            break
    return model, vendor, version


class WebKioskPlayer(Player):
    """Represents a web browser kiosk as a Music Assistant player."""

    current_stream_url: str | None = None
    # sendspin-js 5 client id (base64url public key). None until the browser announces it.
    sendspin_client_id: str | None = None
    html5_requested: bool = False
    output_format: str = "mp3"
    _skip_ws_depth: int = 0
    _accepted_position: bool = False
    _attr_elapsed_time: float | None = None
    _attr_elapsed_time_last_updated: float | None = None
    # Monotonic time of the last browser evidence for the clock: a play start,
    # resume, seek or position report.
    _clock_evidence_at: float | None = None
    _ws_ever_connected: bool = False
    _track_started_at: float = 0.0

    def __init__(
        self,
        provider: WebKioskProvider,
        player_id: str,
        name: str = "Web Kiosk",
        output_format: str = "mp3",
        *,
        ip_address: str | None = None,
    ) -> None:
        """Initialize the Web Kiosk Player."""
        super().__init__(provider, player_id)
        self._attr_name = name
        self._attr_type = PlayerType.PLAYER
        self._attr_supported_features = {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.ENQUEUE,
            PlayerFeature.PAUSE,
            PlayerFeature.SEEK,
            # Muting the media element works even where its volume is read-only.
            PlayerFeature.VOLUME_MUTE,
        }
        self._attr_device_info = DeviceInfo(
            model="Web browser",
            manufacturer="Unknown",
        )
        self._attr_device_info.add_identifier(
            IdentifierType.UUID, str(uuid5(NAMESPACE_URL, f"music-assistant:web-kiosk:{player_id}"))
        )
        if ip_address:
            self._attr_device_info.ip_address = ip_address
        self._attr_available = True
        self._attr_powered = True
        self._attr_volume_level = 100
        self._attr_volume_muted = False
        self._playback_speed = 1.0
        self.output_format = output_format
        self.sendspin_fallback = False
        self.html5_requested = False
        self.sendspin_client_id = None
        self.sendspin_claim: VerifiedSendspinClaim | None = None
        self._skip_ws_depth = 0
        self._accepted_position = False
        self._track_end_handled = False
        self._playback_generation = secrets.token_hex(16)
        self._enqueued_media: PlayerMedia | None = None
        self._command_count = 0
        self._commanded_state = PlaybackState.IDLE

    @property
    def playback_speed(self) -> float:
        """Return the media-seconds per browser stream-second for this playback."""
        return self._playback_speed

    @property
    def playback_generation(self) -> str:
        """Return the identity of the current playback instance."""
        return self._playback_generation

    @property
    def supported_features(self) -> set[PlayerFeature]:
        """Return the supported features; native playback yields to an active Sendspin output."""
        if self._uses_sendspin_output():
            return self._attr_supported_features - {PlayerFeature.PLAY_MEDIA}
        return self._attr_supported_features

    @property
    def default_output_protocol_domain(self) -> str | None:
        """Prefer the linked Sendspin output while the browser runs in Sendspin mode."""
        return "sendspin" if self._uses_sendspin_output() else None

    @property
    def enqueued_media(self) -> PlayerMedia | None:
        """Return the next queue item Music Assistant handed over, if any."""
        return self._enqueued_media

    @property
    def needs_poll(self) -> bool:
        """Return if the player needs to be polled for state updates."""
        return True

    @property
    def poll_interval(self) -> int:
        """Return poll interval in seconds."""
        return 5 if self.playback_state == PlaybackState.PLAYING else 30

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return per-player config entries — codec is configurable per kiosk."""
        return [CONF_ENTRY_OUTPUT_CODEC_DEFAULT_MP3]

    def on_protocol_player_updated(
        self, protocol_player: Player, changed_values: dict[str, tuple[Any, Any]]
    ) -> None:
        """Adopt playback arriving on this browser's linked Sendspin output."""
        # Music Assistant marks a protocol active only for playback it starts or
        # groups itself. Playback a Sendspin group starts on this browser is not
        # adopted by the core, so the kiosk would keep reporting its idle native
        # output without this.
        if (
            not self.sendspin_fallback
            and self.sendspin_client_id == protocol_player.player_id
            and protocol_player.protocol_parent_id == self.player_id
            and protocol_player.state.playback_state
            in (PlaybackState.PLAYING, PlaybackState.PAUSED)
        ):
            self.set_active_output_protocol(protocol_player.player_id)
        super().on_protocol_player_updated(protocol_player, changed_values)

    def on_ws_connected(self) -> None:
        """Mark player as available when a WebSocket client connects."""
        self._ws_ever_connected = True
        if not self._attr_available:
            self._attr_available = True
            self.update_state()

    def on_ws_disconnected(self) -> None:
        """Mark the player unavailable when its last WebSocket client disconnects."""
        self._attr_available = False
        self.update_state()

    def set_volume_control_supported(self, supported: bool) -> None:
        """Expose volume control only when the connected browser can apply it."""
        if supported == (PlayerFeature.VOLUME_SET in self._attr_supported_features):
            return
        if supported:
            self._attr_supported_features.add(PlayerFeature.VOLUME_SET)
        else:
            self._attr_supported_features.discard(PlayerFeature.VOLUME_SET)
        self.update_state()

    def set_html5_requested(self, requested: bool) -> None:
        """
        Record whether a connected tab of this device asked for HTML5 playback.

        :param requested: True when native output must be used for this device.
        """
        if requested == self.html5_requested:
            return
        self.html5_requested = requested
        if self.initialized.is_set():
            self.update_state()

    def update_browser_info(self, user_agent: str) -> None:
        """Update model, browser vendor and version from the connected browser."""
        model, vendor, version = browser_device_info(user_agent)
        self._attr_device_info.model = model
        self._attr_device_info.manufacturer = vendor
        self._attr_device_info.software_version = version
        if self.initialized.is_set():
            self.update_state()

    async def on_config_updated(self) -> None:
        """Publish configured-name changes even when the effective name stays the same."""
        await super().on_config_updated()
        provider = cast("WebKioskProvider", self.provider)
        if provider.http_server:
            provider.http_server.broadcast_player_name(self)

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA command — store media for the kiosk to fetch."""
        self.logger.info("play_media on %s: uri=%s", self.display_name, media.uri)
        self._playback_generation = secrets.token_hex(16)
        self._enqueued_media = None
        self.current_stream_url = media.uri
        self._attr_current_media = media
        item = None
        if media.source_id:
            if media.queue_item_id:
                item = self.mass.player_queues.get_item(media.source_id, media.queue_item_id)
            elif queue := self.mass.player_queues.get(media.source_id):
                item = queue.current_item
        speed = item.extra_attributes.get("playback_speed", 1.0) if item else 1.0
        self._playback_speed = (
            float(speed)
            if isinstance(speed, (int, float))
            and not isinstance(speed, bool)
            and math.isfinite(speed)
            and speed > 0
            else 1.0
        )
        self._attr_playback_state = PlaybackState.PLAYING
        self._attr_elapsed_time = 0.0
        self._attr_elapsed_time_last_updated = time.time()
        self._clock_evidence_at = time.monotonic()
        self._track_started_at = time.monotonic()
        self._accepted_position = False
        self._track_end_handled = False
        self.update_state()

        if not self._skip_ws_notify:
            song_length, stream_start = media_timeline(media)
            cast("WebKioskProvider", self.provider).notify_play_started(
                self.player_id,
                playback_generation=self.playback_generation,
                title=media.title,
                artist=media.artist,
                image_url=media.image_url,
                duration=song_length,
                start=stream_start,
                playback_speed=self.playback_speed,
                media=media,
            )

    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """Keep the next queue item until the current browser stream ends."""
        self._enqueued_media = media

    async def start_enqueued_media(self) -> bool:
        """
        Start the queue item that follows the finished one, if there is one.

        Reporting it as the current media lets the queue controller follow the
        transition and hand over the item after it. Returns False when nothing
        follows, so the player can report IDLE.
        """
        media = self._enqueued_media
        self._enqueued_media = None
        current = self.current_media
        if current is None or not current.source_id or not current.queue_item_id:
            # Without a finished queue item to count from, play the handover
            # only while the queue's stream server would still serve it.
            if media is None or (
                media.source_id
                and media.queue_item_id
                and not self.mass.player_queues.is_current_window_item(
                    media.source_id, media.queue_item_id
                )
            ):
                return False
            await self.play_media(media)
            return True
        queue_id = current.source_id
        successor = self.mass.player_queues.get_next_item(queue_id, current.queue_item_id)
        if successor is None:
            return False
        if media is not None and (media.source_id, media.queue_item_id) == (
            queue_id,
            successor.queue_item_id,
        ):
            await self.play_media(media)
            return True
        # The handed-over item is a copy of the queue that a later edit can
        # leave stale, and a track can end before it arrives. Music Assistant
        # hands over a replacement a second later and skips it once the player
        # is IDLE, so ask the queue for the next item here.
        self.logger.debug(
            "Handed-over item %s does not follow in the queue, loading %s",
            media.queue_item_id if media else None,
            successor.queue_item_id,
        )
        generation = self._playback_generation
        mark = self.transport_mark()
        for _attempt in range(SUCCESSOR_LOAD_ATTEMPTS):
            order = self._queue_order(queue_id)
            loaded = await self._load_queue_successor(queue_id, current.queue_item_id)
            if not self.still_plays(generation):
                # Other media replaced the finished stream, or another player
                # instance took over this player ID; either one owns it now.
                return True
            if loaded is None:
                return False
            # The queue can change while the item loads: an edit can remove the
            # item or move another one in front of it. The loaded item still
            # follows the finished one as the loader chose it (skipping
            # unplayable items) only if the queue between them is unchanged.
            if (loaded_id := loaded.queue_item_id) is None:
                break
            span = _queue_span(order, current.queue_item_id, loaded_id)
            if span is not None and span == _queue_span(
                self._queue_order(queue_id), current.queue_item_id, loaded_id
            ):
                break
            self.logger.debug("Queue changed while item %s loaded", loaded.queue_item_id)
        else:
            return False
        await self.play_media(loaded)
        if self.wants_pause(mark):
            await self.pause()
        return True

    def transport_mark(self) -> TransportMark:
        """Return the play, pause and stop commands this player has handled so far."""
        return TransportMark(self._command_count, self._attr_playback_state == PlaybackState.PAUSED)

    def wants_pause(self, mark: TransportMark) -> bool:
        """
        Return whether playback that just started should be paused.

        The last play, pause or stop since the mark decides; without one the
        pause state at the mark does.

        :param mark: The transport mark taken before the playback was prepared.
        """
        if self._command_count != mark.commands:
            paused = self._commanded_state == PlaybackState.PAUSED
        else:
            paused = mark.paused
        return paused and self._attr_playback_state != PlaybackState.IDLE

    def still_plays(self, generation: str) -> bool:
        """
        Return whether this player is still registered and still runs the given playback.

        :param generation: The playback generation captured before an await.
        """
        return (
            generation == self._playback_generation
            and self.mass.players.get_player(self.player_id) is self
        )

    def take_over_output(self) -> None:
        """
        Continue the playback of the active output protocol as this player's own.

        From now on playback commands reach this player instead of the output,
        so a command issued while the output is torn down is not lost. Starts a
        new playback generation.
        """
        state = self.state
        self._playback_generation = secrets.token_hex(16)
        self._enqueued_media = None
        self._attr_current_media = state.current_media
        self._attr_playback_state = state.playback_state
        self._attr_elapsed_time = state.elapsed_time
        self._attr_elapsed_time_last_updated = state.elapsed_time_last_updated
        # No browser stream plays this yet: no evidence to hold the clock on,
        # and no start time to check reports against.
        self._clock_evidence_at = None
        self._track_started_at = 0.0
        self.set_active_output_protocol("native")

    def abandon_takeover(self, generation: str) -> None:
        """
        Drop the playback taken over from an output when no stream replaced it.

        :param generation: The playback generation that take_over_output() started.
        """
        if not self.still_plays(generation):
            return
        self._playback_generation = secrets.token_hex(16)
        self._attr_playback_state = PlaybackState.IDLE
        self._attr_current_media = None
        self._attr_elapsed_time = None
        self._attr_elapsed_time_last_updated = None
        self._clock_evidence_at = None
        self.update_state()

    def can_refresh_stream(self) -> bool:
        """Return whether the current media is a queue stream that can be rebuilt."""
        return self._refreshable_stream() is not None

    async def refresh_stream(self) -> bool:
        """Rebuild a finite queued HTML5 stream at its current song position."""
        # Other media (an announcement, a plain URL) has no queue stream to
        # rebuild. Decide that without the playback lock: an announcement holds
        # it until this player reports IDLE, which needs the page to play it.
        if self._refreshable_stream() is None:
            return False
        # Read media, position and pause state only once a pending track change
        # has finished, and keep the lock through the seek and re-pause.
        async with playback_lock(self.mass, self.player_id):
            if (stream := self._refreshable_stream()) is None:
                return False
            duration, start, position = stream
            mark = self.transport_mark()
            with self.suppress_ws_notify():
                await self.mass.players.cmd_seek(
                    self.player_id, min(int(start + position * self._playback_speed), int(duration))
                )
                # A pause during the seek (queue pause takes no lock) is kept too.
                if self.wants_pause(mark):
                    await self.mass.players.cmd_pause(self.player_id)
            return True

    async def play(self) -> None:
        """Handle PLAY (resume) command."""
        self.logger.info("play (resume) on %s", self.display_name)
        self._command_count += 1
        self._commanded_state = PlaybackState.PLAYING
        if self._attr_playback_state == PlaybackState.PAUSED:
            await self._resume_from_pause()
            return
        self._attr_playback_state = PlaybackState.PLAYING
        self._attr_elapsed_time_last_updated = time.time()
        self._clock_evidence_at = time.monotonic()
        self.update_state()

    async def pause(self) -> None:
        """Handle PAUSE command — pause playback, keep stream alive for resume."""
        self.logger.info("pause on %s", self.display_name)
        self._command_count += 1
        self._commanded_state = PlaybackState.PAUSED
        if self._attr_playback_state == PlaybackState.PLAYING:
            self._attr_elapsed_time = self._clock_position(time.time())
        self._attr_playback_state = PlaybackState.PAUSED
        self._attr_elapsed_time_last_updated = time.time()
        self.update_state()
        if not self._skip_ws_notify:
            cast("WebKioskProvider", self.provider).notify_play_paused(self.player_id)

    async def stop(self) -> None:
        """Handle STOP command."""
        self.logger.info("stop on %s", self.display_name)
        self._command_count += 1
        self._commanded_state = PlaybackState.IDLE
        self._playback_generation = secrets.token_hex(16)
        self._enqueued_media = None
        self._attr_playback_state = PlaybackState.IDLE
        self._attr_current_media = None
        self._attr_elapsed_time = None
        self._attr_elapsed_time_last_updated = None
        self._clock_evidence_at = None
        self.current_stream_url = None
        self.update_state()
        cast("WebKioskProvider", self.provider).notify_play_stopped(self.player_id)

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command."""
        if PlayerFeature.VOLUME_SET not in self.supported_features:
            raise UnsupportedFeaturedException("Use the device volume controls")
        self._attr_volume_level = volume_level
        self.update_state()
        if not self._skip_ws_notify:
            cast("WebKioskProvider", self.provider).notify_volume(self.player_id, volume_level)

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME_MUTE command without changing the volume level."""
        self._attr_volume_muted = muted
        self.update_state()
        if not self._skip_ws_notify:
            cast("WebKioskProvider", self.provider).notify_mute(self.player_id, muted)

    async def seek(self, position_seconds: int) -> None:
        """Handle SEEK command — send seek action to the kiosk via WebSocket."""
        self._attr_elapsed_time = float(position_seconds)
        self._attr_elapsed_time_last_updated = time.time()
        self._clock_evidence_at = time.monotonic()
        if self._track_started_at > 0:
            self._track_started_at = time.monotonic() - float(position_seconds)
        self.update_state()
        if not self._skip_ws_notify:
            cast("WebKioskProvider", self.provider).notify_seek(self.player_id, position_seconds)

    def update_position(self, position: float) -> None:
        """
        Update elapsed time from a WebSocket position report.

        Only accepts updates while PLAYING — late reports arriving after
        pause() would overwrite the correctly accumulated elapsed_time.
        """
        if self._attr_playback_state != PlaybackState.PLAYING:
            return
        normalized = max(0.0, float(position))
        if self._track_started_at > 0:
            age = time.monotonic() - self._track_started_at
            if normalized > age + 2.0:
                if not self._accepted_position:
                    return
                self._track_started_at = time.monotonic() - normalized
        self._accepted_position = True
        duration = self._served_duration()
        if duration is not None:
            normalized = min(normalized, duration)
        self._attr_elapsed_time = normalized
        # elapsed_time_last_updated is compared against time.time() by MA core
        # (corrected_elapsed_time) — must stay wall-clock.
        self._attr_elapsed_time_last_updated = time.time()
        self._clock_evidence_at = time.monotonic()
        self.update_state()

    def note_seek(self, position: float) -> None:
        """Trust a kiosk-initiated seek even before the first position report."""
        if self._attr_playback_state != PlaybackState.PLAYING:
            return
        self._accepted_position = True
        if self._track_started_at > 0:
            self._track_started_at = time.monotonic() - max(0.0, float(position))
        self.update_position(position)

    async def poll(self) -> None:
        """
        Poll player for state updates.

        Raises PlayerUnavailableError if the player was marked unavailable
        (e.g. WS disconnected while playing — kiosk likely went offline).
        """
        if not self._attr_available:
            raise PlayerUnavailableError(
                f"Web kiosk {self.display_name} is offline (WebSocket disconnected)",
                translation_key="player_offline",
                translation_owner=self.translation_owner,
                translation_args=[self.display_name],
            )
        if (
            self._attr_playback_state != PlaybackState.PLAYING
            or self._attr_elapsed_time is None
            or self._attr_elapsed_time_last_updated is None
        ):
            return
        now = time.time()
        position = self._clock_position(now)
        running = self._attr_elapsed_time + (now - self._attr_elapsed_time_last_updated)
        if position == running:
            # Music Assistant extrapolates the same position from the last update.
            return
        self._attr_elapsed_time = position
        self._attr_elapsed_time_last_updated = now
        self.update_state()

    @contextmanager
    def suppress_ws_notify(self) -> Iterator[None]:
        """Suppress MA→kiosk WebSocket echo while the kiosk is driving playback."""
        self._skip_ws_depth += 1
        try:
            yield
        finally:
            self._skip_ws_depth = max(0, self._skip_ws_depth - 1)

    def playback_reached_end(self) -> bool:
        """
        Return whether playback has reached the end of the current song.

        Position reports are seconds into the file Music Assistant is serving.
        After a seek that file is only the remainder, so the song position is
        the seek origin plus that file position. Reports arrive every few
        seconds, so the end counts from a few seconds short of the duration.
        """
        media = self._attr_current_media
        if media is None:
            return False
        song, start = media_timeline(media)
        if song is None:
            return True
        file_pos = float(self._attr_elapsed_time or 0)
        return start + file_pos * self._playback_speed >= song - 8

    @property
    def _skip_ws_notify(self) -> bool:
        """True while at least one suppress_ws_notify() context is active."""
        return self._skip_ws_depth > 0

    def _clock_position(self, now: float) -> float | None:
        """Return the position at wall-clock time now, held and capped as the browser allows."""
        if self._attr_elapsed_time is None or self._attr_elapsed_time_last_updated is None:
            return self._attr_elapsed_time
        running = now - self._attr_elapsed_time_last_updated
        # The browser reports while it plays HTML5. After two missed reports
        # (a reload, Sendspin mode, a stalled stream) the position holds until
        # the browser reports again instead of running on with the wall clock.
        if self._clock_evidence_at is not None:
            silent = time.monotonic() - self._clock_evidence_at
            running -= min(max(0.0, silent - CLOCK_HOLD_AFTER), max(0.0, running))
        position = max(0.0, self._attr_elapsed_time + running)
        if (duration := self._served_duration()) is not None:
            position = min(position, duration)
        return position

    async def _load_queue_successor(
        self, queue_id: str, finished_item_id: str
    ) -> PlayerMedia | None:
        """Return the next queue item as Music Assistant prepares it for a player, if any."""
        # The queue controller owns item preparation (resume position, skipping
        # unplayable items, playback speed across episodes). Use its loader, as
        # its own handover does, instead of converting a bare queue item.
        queues = self.mass.player_queues
        try:
            item = await queues.load_next_queue_item(queue_id, finished_item_id)
            return await queues.player_media_from_queue_item(item)
        except QueueEmpty:
            return None
        except (MusicAssistantError, KeyError) as err:
            self.logger.warning("Cannot load the next item of queue %s: %s", queue_id, err)
            return None

    def _queue_order(self, queue_id: str) -> list[str]:
        """Return the IDs of the queue's items in play order."""
        order: list[str] = []
        while page := self.mass.player_queues.items(
            queue_id, limit=QUEUE_PAGE_SIZE, offset=len(order)
        ):
            order.extend(item.queue_item_id for item in page)
            if len(page) < QUEUE_PAGE_SIZE:
                break
        return order

    def _refreshable_stream(self) -> tuple[float, float, float] | None:
        """Return song length, stream start and position of a rebuildable queue stream."""
        media = self.current_media
        if not media or not media.queue_item_id or not media.source_id:
            return None
        duration, start = media_timeline(media)
        position = self.corrected_elapsed_time
        if not duration or position is None or position < 1:
            return None
        return duration, start, position

    def _served_duration(self) -> float | None:
        """
        Return the length in seconds of the audio served to the kiosk, if known.

        The kiosk reports its position within that audio, which is shorter than the
        media item itself when playback starts at a seek position.
        """
        if (media := self._attr_current_media) is None:
            return None
        duration = media.stream_duration or media.duration
        if not isinstance(duration, (int, float)) or duration <= 0:
            return None
        return float(duration) / self._playback_speed

    def _uses_sendspin_output(self) -> bool:
        """Return whether the browser asked for Sendspin and its linked output can play."""
        client_id = self.sendspin_client_id
        if not client_id or self.sendspin_fallback or self.html5_requested:
            return False
        if all(link.output_protocol_id != client_id for link in self.linked_output_protocols):
            return False
        protocol = self.mass.players.get_player(client_id)
        return bool(protocol and protocol.available_for_playback)

    async def _resume_from_pause(self) -> None:
        """Resume playback after pause — tell the kiosk to unpause its audio element."""
        self._attr_playback_state = PlaybackState.PLAYING
        self._attr_elapsed_time_last_updated = time.time()
        self._clock_evidence_at = time.monotonic()
        self.update_state()
        if not self._skip_ws_notify:
            cast("WebKioskProvider", self.provider).notify_play_resumed(self.player_id)
