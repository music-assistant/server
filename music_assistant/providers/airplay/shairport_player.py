"""AirPlay player for shairport-sync receivers, including playback other senders run on them."""

from __future__ import annotations

import asyncio
import hashlib
import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, cast

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, MediaType, PlaybackState, PlayerFeature
from music_assistant_models.errors import MediaNotFoundError, PlayerCommandFailed
from music_assistant_models.player import PlayerSource

from music_assistant.constants import EXTERNAL_PAUSE_IDLE_TIMEOUT
from music_assistant.models.protocol_backed_player import ProtocolBackedPlayer

from .constants import (
    CONF_SHAIRPORT_MQTT_TOPIC,
    EXTERNAL_STATE_GRACE_S,
    FALLBACK_VOLUME,
    RECEIVER_SESSION_ACTIVE_BIT,
    SHAIRPORT_SYNC_SESSION_BIT,
)
from .helpers import convert_airplay_volume
from .player import GenericAirPlayPlayer

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncServiceInfo

    from .provider import AirPlayProvider
    from .shairport_mqtt import ShairportMqttClient
    from .stream import AirPlayStream

EXTERNAL_SOURCE_ID: Final[str] = "airplay"
# How long a remote command waits for the receiver to report the change it should cause.
COMMAND_CONFIRM_TIMEOUT: Final[float] = 3.0
# A track skip flushes too, so a flush only counts as a pause when nothing follows it.
FLUSH_PAUSE_DELAY: Final[float] = 1.0
# shairport-sync moves the AirPlay volume (-30..0 dB) by this much per remote volume step.
VOLUME_STEP_DB: Final[float] = 1.875
AIRPLAY_VOLUME_MIN: Final[float] = -30.0
# The first shairport-sync release that passes remote commands on to AirPlay 2 senders;
# older releases send every command over DACP, which only Classic senders follow.
AIRPLAY2_REMOTE_MIN_VERSION: Final[tuple[int, int]] = (5, 6)
STREAM_TYPE_CLASSIC: Final[str] = "Classic"
_READ_ONLY_MESSAGE: Final[str] = "This AirPlay session is controlled from the sending device"
_CONFIRMED_BY_PLAY: Final = frozenset({"resumed", "play_begin"})
_CONFIRMED_BY_PAUSE: Final = frozenset({"paused"})
_CONFIRMED_BY_STOP: Final = frozenset({"paused", "play_end", "session_end"})
_CONFIRMED_BY_TRACK_CHANGE: Final = frozenset({"track_changed"})
_CONFIRMED_BY_VOLUME: Final = frozenset({"volume"})
# What a sender reports about its track and volume, kept until its session is presented.
_HELD_KEYS: Final = frozenset(
    {
        "title",
        "artist",
        "album",
        "duration",
        "track_id",
        "artwork",
        "elapsed_time",
        "volume",
        "airplay_volume",
    }
)
# The fields a metadata block describes afresh; progress and artwork may come before it.
_BLOCK_KEYS: Final = ("title", "artist", "album", "track_id")


@dataclass
class SenderIdentity:
    """What the receiver reported about the sender that last connected to it."""

    dacp_id: int | None = None
    name: str | None = None
    stream_type: str | None = None


@dataclass
class ExternalSession:
    """Playback another sender runs on the receiver."""

    flag_seen: bool = False
    control_failed: bool = False
    track_id: int | None = None
    new_track: bool = False
    volume: int | None = None
    airplay_volume: float | None = None
    artwork: bytes | None = None
    artwork_id: str | None = None


class ShairportSyncPlayer(GenericAirPlayPlayer):
    """
    AirPlay protocol endpoint for a shairport-sync receiver.

    Next to streaming from Music Assistant, it presents playback that other senders (an
    iPhone, a Mac) run on the receiver as the external ``airplay`` source, and controls
    that playback over MQTT when the receiver publishes there.
    """

    _attr_external_pause_idle_timeout = EXTERNAL_PAUSE_IDLE_TIMEOUT

    def __init__(
        self,
        provider: AirPlayProvider,
        player_id: str,
        raop_discovery_info: AsyncServiceInfo | None,
        airplay_discovery_info: AsyncServiceInfo | None,
        address: str,
        display_name: str,
        manufacturer: str,
        model: str,
        initial_volume: int = FALLBACK_VOLUME,
        is_local_shairport: bool = False,
    ) -> None:
        """Initialize a shairport-sync player."""
        # the base class already calculates the player state while it initializes
        self._session: ExternalSession | None = None
        self._last_session_flag: bool | None = None
        self._sender = SenderIdentity()
        # what the sender reported before its session was presented, shown once it is
        self._held_metadata: dict[str, Any] = {}
        self._pending_confirmations: list[tuple[frozenset[str], asyncio.Future[None]]] = []
        super().__init__(
            provider=provider,
            player_id=player_id,
            raop_discovery_info=raop_discovery_info,
            airplay_discovery_info=airplay_discovery_info,
            address=address,
            display_name=display_name,
            manufacturer=manufacturer,
            model=model,
            initial_volume=initial_volume,
            is_local_shairport=is_local_shairport,
        )

    @property
    def mqtt_topic_setting(self) -> str:
        """Return the MQTT topic the user configured for this receiver, or an empty string."""
        return str(self.config.get_value(CONF_SHAIRPORT_MQTT_TOPIC) or "").strip()

    @property
    def supported_features(self) -> set[PlayerFeature]:
        """Return the supported features of this player."""
        features = super().supported_features
        if (session := self._external_session) and self._control_offered(session)[0]:
            features.add(PlayerFeature.NEXT_PREVIOUS)
        return features

    @property
    def volume_level(self) -> int | None:
        """Return the volume of another sender's session, or the level Music Assistant uses."""
        if (session := self._external_session) and session.volume is not None:
            return session.volume
        return self._attr_volume_level

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        entries = await super().get_config_entries()
        entries.append(
            ConfigEntry(
                key=CONF_SHAIRPORT_MQTT_TOPIC,
                type=ConfigEntryType.STRING,
                default_value="",
                required=False,
                category="protocol_generic",
            )
        )
        return entries

    async def stop(self) -> None:
        """Stop Music Assistant playback, or the session of another sender."""
        if session := self._external_session:
            already_stopped = self._attr_playback_state == PlaybackState.PAUSED
            await self._send_session_command(
                session, ["stop"], None if already_stopped else _CONFIRMED_BY_STOP
            )
            return
        await super().stop()

    async def play(self) -> None:
        """Resume Music Assistant playback, or the session of another sender."""
        if session := self._external_session:
            playing = self._attr_playback_state == PlaybackState.PLAYING
            await self._send_session_command(
                session, ["play"], None if playing else _CONFIRMED_BY_PLAY
            )
            return
        await super().play()

    async def pause(self) -> None:
        """Pause Music Assistant playback, or the session of another sender."""
        if session := self._external_session:
            paused = self._attr_playback_state == PlaybackState.PAUSED
            await self._send_session_command(
                session, ["pause"], None if paused else _CONFIRMED_BY_PAUSE
            )
            return
        await super().pause()

    async def next_track(self) -> None:
        """Skip to the next track of another sender's session."""
        if session := self._external_session:
            await self._send_session_command(session, ["nextitem"], _CONFIRMED_BY_TRACK_CHANGE)
            return
        await super().next_track()

    async def previous_track(self) -> None:
        """Return to the previous track of another sender's session."""
        if session := self._external_session:
            await self._send_session_command(session, ["previtem"], _CONFIRMED_BY_TRACK_CHANGE)
            return
        await super().previous_track()

    async def volume_set(self, volume_level: int) -> None:
        """Set the volume Music Assistant streams at, or step the volume of another sender."""
        session = self._external_session
        if session is None:
            await super().volume_set(volume_level)
            return
        if not self._control_offered(session)[1]:
            raise PlayerCommandFailed(_READ_ONLY_MESSAGE)
        if session.airplay_volume is None:
            raise PlayerCommandFailed(
                f"The volume of the AirPlay session on {self.display_name} is not known yet"
            )
        # shairport-sync raises a muted volume to -30 dB before it applies a step
        current = max(session.airplay_volume, AIRPLAY_VOLUME_MIN)
        if not (steps := volume_steps(current, volume_level)):
            return
        # the receiver reports every step back, which corrects this as it goes
        session.airplay_volume = current + steps * VOLUME_STEP_DB
        session.volume = convert_airplay_volume(session.airplay_volume)
        self._publish()
        command = "volumeup" if steps > 0 else "volumedown"
        await self._send_session_command(session, [command] * abs(steps), _CONFIRMED_BY_VOLUME)

    async def volume_mute(self, muted: bool) -> None:
        """Mute Music Assistant's stream; another sender's session cannot be muted."""
        if self._external_session is not None:
            raise PlayerCommandFailed("An AirPlay session from another device cannot be muted")
        await super().volume_mute(muted)

    def set_discovery_info(self, discovery_info: AsyncServiceInfo, display_name: str) -> None:
        """Update the discovery info and follow the receiver's session flag."""
        super().set_discovery_info(discovery_info, display_name)
        self.evaluate_session_flag()

    def set_state_from_stream(
        self,
        state: PlaybackState | None = None,
        elapsed_time: float | None = None,
        stream: AirPlayStream | None = None,
    ) -> None:
        """
        Set the playback state from Music Assistant's own stream.

        :param state: New playback state (or None to keep current).
        :param elapsed_time: New elapsed time (or None to keep current).
        :param stream: The stream instance sending this update (for validation).
        """
        if self._session is not None and (stream is None or stream is self.stream):
            # Music Assistant's stream has taken the receiver over
            self._drop_session()
        super().set_state_from_stream(state, elapsed_time, stream)

    def record_stream_end(self) -> None:
        """Record that a stream of Music Assistant to this device just ended."""
        super().record_stream_end()
        if self._last_session_flag:
            # the flag was set while we streamed, so a sender that took the receiver over
            # from us is only noticed once the grace period has passed
            self._schedule_flag_recheck()

    def evaluate_session_flag(self) -> None:
        """Begin or end the external session when the receiver's session flag changes."""
        # Act on changes only: once metadata or the pause timeout ended a session, an
        # unrelated TXT update would otherwise revive it from a flag that lags behind.
        flag = self._receiver_session_flag()
        previous, self._last_session_flag = self._last_session_flag, flag
        if flag == previous:
            return
        self._drop_ended_session()
        if not flag:
            if previous:
                self._sender = SenderIdentity()
                self._held_metadata.clear()
                if self._session is not None:
                    self._end_external_session()
            return
        if self.external_state_blocked:
            # most likely our own stream; look again once the grace period is over
            self._schedule_flag_recheck()
            return
        self._begin_from_flag()

    def on_shairport_metadata(self, update: dict[str, Any]) -> None:
        """
        Apply a metadata update the receiver published about its current sender.

        :param update: Decoded metadata, as produced by ShairportMetadataDecoder.
        """
        self._drop_ended_session()
        self._update_sender(update)
        if not self._accepts_session_metadata():
            if self._sender_is_foreign():
                # a sender that took the receiver over from our stream, which still runs
                self._hold_metadata(update)
            return
        session = self._session
        if update.get("play_begin") or update.get("resumed"):
            session = session or self._begin_external_session()
            self.mass.cancel_timer(self._flush_timer_id)
            self._attr_playback_state = PlaybackState.PLAYING
        if session is None:
            # a Classic sender sends its metadata before play begins or the flag comes up
            self._hold_metadata(update)
            return
        if update.get("paused"):
            self._attr_playback_state = PlaybackState.PAUSED
        if update.get("flush"):
            self.mass.call_later(
                FLUSH_PAUSE_DELAY, self._pause_after_flush, task_id=self._flush_timer_id
            )
        if not session.flag_seen and (update.get("play_end") or update.get("active_end")):
            # without a session flag this is the only end of the session the receiver reports
            self._end_external_session()
            return
        if update.get("play_end"):
            # the sender may still be connected: the flag or the pause timeout ends the session
            self._attr_playback_state = PlaybackState.PAUSED
        events = set(update)
        if self._apply_media(session, update):
            events.add("track_changed")
            # a flush that a new track follows was a skip, not a pause
            self.mass.cancel_timer(self._flush_timer_id)
        if "elapsed_time" in update:
            self.mass.cancel_timer(self._flush_timer_id)
            self._attr_elapsed_time = update["elapsed_time"]
            self._attr_elapsed_time_last_updated = time.time()
        if "volume" in update and not self.ignore_volume_reports:
            session.volume = update["volume"]
            session.airplay_volume = update.get("airplay_volume")
        self._publish()
        self._resolve_confirmations(events)

    async def async_get_external_artwork(self, artwork_id: str) -> bytes:
        """
        Return the artwork bytes of the track another sender plays.

        :param artwork_id: Identifier of the artwork requested by the image proxy.
        :raises MediaNotFoundError: If the artwork is no longer current.
        """
        session = self._session
        if session is None or session.artwork is None or session.artwork_id != artwork_id:
            raise MediaNotFoundError("External AirPlay artwork is no longer current")
        return session.artwork

    async def on_config_updated(self) -> None:
        """Handle logic when the player config is updated."""
        await super().on_config_updated()
        if mqtt := self._mqtt:
            mqtt.refresh()

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        await super().on_unload()
        self.mass.cancel_timer(self._flag_recheck_timer_id)
        self.mass.cancel_timer(self._flush_timer_id)
        for _, future in self._pending_confirmations:
            if not future.done():
                future.set_exception(PlayerCommandFailed(f"{self.display_name} was removed"))
        if mqtt := self._mqtt:
            mqtt.refresh()

    @property
    def _mqtt(self) -> ShairportMqttClient | None:
        """Return the MQTT link of the provider, when a broker is configured."""
        return cast("AirPlayProvider", self.provider).shairport_mqtt

    @property
    def _external_session(self) -> ExternalSession | None:
        """Return the session another sender runs, while it is presented and we do not stream."""
        if (
            self._session is None
            or self._attr_active_source != EXTERNAL_SOURCE_ID
            or self.ma_stream_active
        ):
            return None
        return self._session

    @property
    def _flag_recheck_timer_id(self) -> str:
        return f"airplay_external_recheck_{self.player_id}"

    @property
    def _flush_timer_id(self) -> str:
        return f"airplay_external_flush_{self.player_id}"

    def _receiver_session_flag(self) -> bool:
        """Return whether the receiver's status flags say a sender is connected."""
        return bool(self._get_flags() & (RECEIVER_SESSION_ACTIVE_BIT | SHAIRPORT_SYNC_SESSION_BIT))

    def _schedule_flag_recheck(self) -> None:
        """Evaluate the session flag again once the grace period after our stream is over."""
        remaining = EXTERNAL_STATE_GRACE_S - (time.monotonic() - self._stream_last_active)
        self.mass.call_later(
            min(max(remaining, 0.0), EXTERNAL_STATE_GRACE_S),
            self._recheck_session_flag,
            task_id=self._flag_recheck_timer_id,
        )

    def _recheck_session_flag(self) -> None:
        """Begin a session for a flag that came up while our own stream held the receiver."""
        if not self._last_session_flag:
            return
        if self.external_state_blocked:
            # the end of a stream that is still running schedules the next check itself
            if not self.ma_stream_active:
                self._schedule_flag_recheck()
            return
        self._drop_ended_session()
        self._begin_from_flag()

    def _begin_from_flag(self) -> None:
        """Begin the external session for the receiver's flag, unless metadata already did."""
        session = self._session
        if session is None:
            session = self._begin_external_session()
            self._publish()
        session.flag_seen = True

    def _begin_external_session(self) -> ExternalSession:
        """Start presenting another sender's session as the active ``airplay`` source."""
        session = self._session = ExternalSession()
        self._external_source_ids.add(EXTERNAL_SOURCE_ID)
        self._attr_active_source = EXTERNAL_SOURCE_ID
        self._attr_playback_state = PlaybackState.PLAYING
        # whatever Music Assistant last played here is not what this sender plays
        self._attr_current_media = None
        self._attr_elapsed_time = None
        self._attr_elapsed_time_last_updated = None
        self._release_parent_output_protocol()
        if held := self._held_metadata:
            self._held_metadata = {}
            self._show_held_metadata(session, held)
        return session

    def _hold_metadata(self, update: dict[str, Any]) -> None:
        """Keep what the sender reports before its session is presented, to show it then."""
        held = self._held_metadata
        if update.get("play_end") or update.get("active_end"):
            held.clear()
            return
        if update.get("metadata_start"):
            for key in _BLOCK_KEYS:
                held.pop(key, None)
        held.update({key: value for key, value in update.items() if key in _HELD_KEYS})
        if "elapsed_time" in update:
            held["elapsed_at"] = time.time()
        if update.get("paused"):
            held["paused"] = True
        elif update.get("resumed") or update.get("play_begin"):
            held["paused"] = False

    def _show_held_metadata(self, session: ExternalSession, held: dict[str, Any]) -> None:
        """Present what the sender reported before its session began."""
        self._apply_media(session, held)
        if (elapsed_time := held.get("elapsed_time")) is not None:
            if not held.get("paused"):
                elapsed_time += time.time() - held["elapsed_at"]
            self._attr_elapsed_time = elapsed_time
            self._attr_elapsed_time_last_updated = time.time()
        if "volume" in held and not self.ignore_volume_reports:
            session.volume = held["volume"]
            session.airplay_volume = held.get("airplay_volume")
        if held.get("paused"):
            self._attr_playback_state = PlaybackState.PAUSED

    def _end_external_session(self) -> None:
        """Stop presenting another sender's session."""
        self.mark_external_source_ended()
        self._drop_session()
        self._publish()

    def _drop_session(self) -> None:
        """Forget the external session, without touching the presented state."""
        self._session = None
        self.mass.cancel_timer(self._flush_timer_id)
        self._resolve_confirmations({"session_end"})

    def _drop_ended_session(self) -> None:
        """Forget a session the base class ended on its own (the pause timeout)."""
        if self._session is not None and self._attr_active_source != EXTERNAL_SOURCE_ID:
            self._drop_session()

    def _release_parent_output_protocol(self) -> None:
        """
        Clear the parent's output protocol when it still points at this player.

        A queue that ended by itself leaves it set, which keeps the parent from showing
        the external session; a stop command would have cleared it.
        """
        if not (parent_id := self.protocol_parent_id):
            return
        parent = self.mass.players.get_player(parent_id)
        if isinstance(parent, ProtocolBackedPlayer) and parent.active_output_protocol == (
            self.player_id
        ):
            parent.set_active_output_protocol(None)

    def _publish(self) -> None:
        """Publish the player state, and refresh the parent that presents it."""
        self._update_external_source()
        self.update_state()
        # the parent does not see a change of our media alone, so refresh it ourselves
        if parent_id := self.protocol_parent_id:
            self.mass.players.trigger_player_update(parent_id)

    def _update_external_source(self) -> None:
        """Add or update the passive ``airplay`` source with the sender and its controls."""
        session = self._session
        can_control = session is not None and self._control_offered(session)[0]
        name = self._sender.name or "AirPlay"
        for source in self._attr_source_list:
            if source.id == EXTERNAL_SOURCE_ID:
                source.name = name
                source.can_play_pause = can_control
                source.can_next_previous = can_control
                return
        if session is None:
            return
        self._attr_source_list.append(
            PlayerSource(
                id=EXTERNAL_SOURCE_ID,
                name=name,
                passive=True,
                can_play_pause=can_control,
                can_seek=False,
                can_next_previous=can_control,
            )
        )

    def _control_offered(self, session: ExternalSession) -> tuple[bool, bool]:
        """Return whether transport commands and volume steps are offered for a session."""
        mqtt = self._mqtt
        if session.control_failed or mqtt is None or mqtt.topic_for(self.player_id) is None:
            return False, False
        version = mqtt.version_for(self.player_id)
        stream_type = self._sender.stream_type
        if version is None or stream_type is None:
            # the command confirmation finds out whether the sender follows
            return True, True
        classic = stream_type == STREAM_TYPE_CLASSIC
        if version >= AIRPLAY2_REMOTE_MIN_VERSION:
            # a Classic sender gets its commands over DACP, which drops the volume steps
            return True, not classic
        return classic, classic

    def _update_sender(self, update: dict[str, Any]) -> None:
        """Keep track of the connected sender, whoever it is."""
        sender = self._sender
        if "dacp_id" in update or "active_remote" in update:
            # a sender connects, so what was held came from an earlier connection
            self._held_metadata.clear()
        if (dacp_id := update.get("dacp_id")) is not None and dacp_id != sender.dacp_id:
            # a new sender connected, and the stream type was the previous one's
            sender.dacp_id = dacp_id
            sender.stream_type = None
        if sender_name := update.get("sender_name"):
            sender.name = sender_name
        if stream_type := update.get("stream_type"):
            sender.stream_type = stream_type

    def _sender_is_foreign(self) -> bool:
        """Return whether the connected sender is known and is not our own stream."""
        dacp_id = self._sender.dacp_id
        return dacp_id is not None and dacp_id != int(
            cast("AirPlayProvider", self.provider).dacp_id, 16
        )

    def _accepts_session_metadata(self) -> bool:
        """Return whether metadata describes another sender rather than our own stream."""
        if self.ma_stream_active:
            return False
        if self._sender.dacp_id is not None:
            # a known foreign sender is followed even inside the grace period
            return self._sender_is_foreign()
        return not self.external_state_blocked

    def _apply_media(self, session: ExternalSession, update: dict[str, Any]) -> bool:
        """
        Apply the track metadata of an update to the current media.

        :param session: The external session the update belongs to.
        :param update: Decoded metadata.
        :return: Whether the update moved the session to another track.
        """
        track_changed = False
        if update.get("metadata_start"):
            session.new_track = True
        if (track_id := update.get("track_id")) is not None and track_id != session.track_id:
            track_changed = session.track_id is not None
            session.track_id = track_id
            session.new_track = True
        if (artwork := update.get("artwork")) is not None:
            session.artwork = artwork
            session.artwork_id = hashlib.md5(artwork, usedforsecurity=False).hexdigest()[:16]
        fields = {k: v for k in ("title", "artist", "album", "duration") if (v := update.get(k))}
        if not fields and track_id is None and artwork is None:
            return track_changed
        shown = self._attr_current_media
        # a Classic sender opens every metadata block afresh, so compare with what is shown
        if shown and shown.title and (title := fields.get("title")) and title != shown.title:
            track_changed = True
        previous = None if session.new_track else shown
        if track_changed:
            self._attr_elapsed_time = 0
            self._attr_elapsed_time_last_updated = time.time()
        title = fields.get("title") or (previous.title if previous else None)
        artist = fields.get("artist") or (previous.artist if previous else None)
        album = fields.get("album") or (previous.album if previous else None)
        if session.track_id is not None:
            media_key = str(session.track_id)
        else:
            media_key = hashlib.md5(
                f"{title}\n{artist}\n{album}".encode(), usedforsecurity=False
            ).hexdigest()[:16]
        self.set_current_media(
            uri=f"airplay://{self.player_id}/{media_key}",
            media_type=MediaType.TRACK,
            title=fields.get("title"),
            artist=fields.get("artist"),
            album=fields.get("album"),
            image_url=(
                self._get_external_artwork_url(session.artwork_id) if session.artwork_id else None
            ),
            duration=fields.get("duration"),
            source_id=EXTERNAL_SOURCE_ID,
            clear_all=session.new_track,
        )
        session.new_track = False
        return track_changed

    def _pause_after_flush(self) -> None:
        """Show a flushed session as paused once no track change followed the flush."""
        if self._session is None:
            return
        self._attr_playback_state = PlaybackState.PAUSED
        self._publish()
        # a Classic sender pauses with a flush alone, it never reports paus
        self._resolve_confirmations({"paused"})

    async def _send_session_command(
        self,
        session: ExternalSession,
        commands: list[str],
        confirmed_by: frozenset[str] | None,
    ) -> None:
        """
        Send remote commands to the sender of a session over MQTT.

        :param session: The external session the commands are for.
        :param commands: The shairport-sync remote commands, sent in order.
        :param confirmed_by: Metadata events that show the sender acted on the commands,
            or None for commands that are not expected to change anything.
        :raises PlayerCommandFailed: If the session cannot be controlled, or the sender
            did not act on the commands.
        """
        mqtt = self._mqtt
        if mqtt is None or not self._control_offered(session)[0]:
            raise PlayerCommandFailed(_READ_ONLY_MESSAGE)
        # the confirmation is registered first: the receiver may answer before we resume
        confirmation = asyncio.get_running_loop().create_future() if confirmed_by else None
        if confirmed_by and confirmation:
            self._pending_confirmations.append((confirmed_by, confirmation))
        try:
            for command in commands:
                await mqtt.publish_remote(self.player_id, command)
            if confirmation is None:
                return
            try:
                async with asyncio.timeout(COMMAND_CONFIRM_TIMEOUT):
                    await confirmation
            except TimeoutError:
                if self._session is session and not session.control_failed:
                    self.logger.debug(
                        "The sender on %s did not act on the %s command, "
                        "showing its session as read-only",
                        self.display_name,
                        commands[0],
                    )
                    session.control_failed = True
                    self._publish()
                msg = f"The sender of the AirPlay session on {self.display_name} did not respond"
                raise PlayerCommandFailed(msg) from None
        finally:
            if confirmed_by and confirmation:
                self._pending_confirmations.remove((confirmed_by, confirmation))

    def _resolve_confirmations(self, events: set[str]) -> None:
        """Complete the pending commands whose effect one of the events shows."""
        for confirmed_by, confirmation in self._pending_confirmations:
            if not confirmation.done() and confirmed_by & events:
                confirmation.set_result(None)


def volume_steps(current: float, volume_level: int) -> int:
    """
    Return the remote volume steps that move the AirPlay volume to a volume level.

    Positive values step up, negative values step down. The count is rounded away from
    zero, so the steps never stop short of the level, and never goes below -30 dB, where
    shairport-sync turns a step into mute.

    :param current: The current AirPlay volume in dB (-30..0).
    :param volume_level: The volume level (0..100) to move to.
    """
    target = AIRPLAY_VOLUME_MIN - AIRPLAY_VOLUME_MIN * volume_level / 100
    exact = round((target - current) / VOLUME_STEP_DB, 6)
    steps = math.ceil(exact) if exact > 0 else math.floor(exact)
    lowest = -math.floor(round((current - AIRPLAY_VOLUME_MIN) / VOLUME_STEP_DB, 6))
    return max(steps, lowest)
