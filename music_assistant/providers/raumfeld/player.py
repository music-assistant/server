"""Teufel Raumfeld Player (one per Raumfeld room)."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import IdentifierType, PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.player import DeviceInfo, PlayerMedia, PlayerSource

from music_assistant.helpers.upnp import create_didl_metadata
from music_assistant.models.player import Player

from .constants import (
    HOST_ERRORS,
    PLAYER_CONFIG_ENTRIES,
    SOURCE_LINE_IN,
    SUPPORTED_SAMPLE_RATES,
)
from .helpers import parse_didl_metadata, parse_duration

if TYPE_CHECKING:
    import hassfeld
    from music_assistant_models.config_entries import ConfigEntry

    from .provider import RaumfeldPlayerProvider

# Poll interval (seconds) for the fast window right after a play/resume command, so MA sees
# the device actually start playing (Raumfeld renderers buffer before they start), and - with
# flow mode off - in the final seconds of a track, so its end is caught promptly.
FAST_POLL_INTERVAL = 1
# Relaxed interval while playing steadily, and the slow interval while idle, to keep the
# request rate on the Raumfeld host low.
PLAYING_POLL_INTERVAL = 5
IDLE_POLL_INTERVAL = 15
# How long (seconds) after a play/resume command to keep polling fast, so MA catches
# the device actually starting playback (Raumfeld renderers buffer before they start).
STARTUP_POLL_WINDOW = 20
# Only re-anchor MA's elapsed-time clock when the reported flow position diverges from
# the expected (extrapolated) position by more than this many seconds. Keeps the
# progress bar smooth despite the device's 1-second position granularity while a real
# divergence (a seek, a stall) is still corrected promptly.
POSITION_DRIFT_THRESHOLD = 1.0
# The settings below only matter with flow mode switched off, where each queue item is
# played as its own stream and the provider starts the next one itself.
# How close (seconds) to the track's end counts as "near the end": both switches to the
# fast poll and gates auto-advance, so a mid-track buffer stall can't pass for a finish.
NEAR_END_WINDOW = 10
# How far (seconds) the zone's reported position sits behind the audio actually playing,
# measured as the zone clock at the moment the room's renderer flips PLAYING -> STOPPED at
# the real end of a track (+1.00 / +0.74 / +1.00 over three tracks). Used to time the end
# watch below: a track's audio ends this much before the zone's clock says it does.
POSITION_REPORT_LAG = 1.0
# When a stream ends the renderer holds on to its audio hardware for one second (its own log:
# "Renderer abandons AudioHardware (delay: 1 seconds)"); a next stream arriving within that
# second is played straight away, one arriving later finds the output muted, and that mute
# is only lifted once the track is already playing - swallowing its first moments. MA's
# 1-second poll alone misses that window, so from the moment a track is due to end, read
# the transport every ADVANCE_WATCH_INTERVAL seconds (~40ms a read) and start the next track
# as soon as it reports stopped. Reading it rather than acting on the predicted time alone
# cannot cut a track short when its metadata duration is off.
ADVANCE_WATCH_INTERVAL = 0.25
ADVANCE_WATCH_ATTEMPTS = 12
# When a room has no addressable zone (e.g. it just detached during a group leadership
# handover), how many times to (re)create a single-room zone and how long to wait between
# attempts for the host to actually publish it before giving up.
ZONE_CREATE_ATTEMPTS = 4
ZONE_CREATE_RETRY_DELAY = 0.75
# After leaving standby, how long (seconds) to wait for a room to report awake before
# playing, so the first play command is not dropped while the renderer powers on.
WAKE_TIMEOUT = 5.0
WAKE_POLL_INTERVAL = 0.3


def _map_transport_state(state: str | None) -> PlaybackState:
    """Map a UPnP ``CurrentTransportState`` value to a Music Assistant PlaybackState."""
    value = (state or "").upper()
    if value in ("PLAYING", "TRANSITIONING"):
        return PlaybackState.PLAYING
    if value in ("PAUSED_PLAYBACK", "PAUSED_RECORDING"):
        return PlaybackState.PAUSED
    return PlaybackState.IDLE


def _near_track_end(pos: dict[str, str]) -> bool:
    """Return whether the playing track is within NEAR_END_WINDOW of its end."""
    elapsed = parse_duration(pos.get("RelTime"))
    if elapsed is None:
        return False
    duration = parse_duration(pos.get("TrackDuration"))
    # an unknown duration cannot rule the end out, so it must not block the advance
    return not duration or elapsed >= duration - NEAR_END_WINDOW


class RaumfeldPlayer(Player):
    """A single Raumfeld room, exposed to Music Assistant as a player."""

    _attr_type = PlayerType.PLAYER

    def __init__(
        self,
        provider: RaumfeldPlayerProvider,
        player_id: str,
        room: str,
    ) -> None:
        """Init the player for the given Raumfeld room."""
        super().__init__(provider, player_id)
        self.room = room
        self._attr_name = room
        self._attr_available = True
        self._attr_needs_poll = True
        self._attr_poll_interval = FAST_POLL_INTERVAL
        # timestamp of the last play/resume command, used to poll fast during startup
        self._play_started_at = 0.0
        # With flow mode off: the next queue item MA handed over (enqueue_next_media), whether
        # the playing track should advance to it when it finishes, and the playing state and
        # near-end flag seen on the previous poll (to recognise a track that has finished).
        self._next_media: PlayerMedia | None = None
        self._advance_armed = False
        self._prev_playing = False
        self._near_end = False
        self._advance_task_id = f"raumfeld_advance_{player_id}"
        self._attr_device_info = DeviceInfo(model="Raumfeld", manufacturer="Teufel")
        # Raumfeld renderers are hi-res capable; declaring the rates lets MA output each
        # source at its native quality (up to 24-bit/192kHz) without manual configuration
        self._attr_supported_sample_rates = SUPPORTED_SAMPLE_RATES
        # Expose the room's media-renderer UUID (and IP) so Music Assistant links this
        # native player to the same renderer's DLNA/Chromecast/Sendspin representation
        # instead of showing them as duplicate players.
        renderer_uuid, renderer_ip = provider.resolve_room_renderer(room)
        # (stream url, title) of this room's analog Line-In input, if it has one
        self._line_in: tuple[str, str] | None = provider.line_in(renderer_uuid)
        if renderer_uuid:
            self._attr_device_info.add_identifier(IdentifierType.UUID, renderer_uuid)
        # Only add the IP identifier when the renderer lives on its own device. The
        # Raumfeld host hosts several virtual UPnP renderers on a single IP, so adding
        # that shared IP would make MA's IP-fallback matching link all of them to the one
        # room whose renderer runs on the host (the TV room here) - cluttering it with
        # unrelated DLNA outputs. There the unique renderer UUID is the only safe link.
        if renderer_ip and renderer_ip != provider.host_address:
            self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, renderer_ip)
        self._attr_supported_features = {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.SET_MEMBERS,
            # ENQUEUE lets the flow mode setting be switched off: MA then hands over each
            # queue item on its own and the provider starts it when the current one ends.
            # With flow mode on (the default) MA never enqueues, so this changes nothing there.
            PlayerFeature.ENQUEUE,
            # NOTE: we deliberately do NOT advertise PlayerFeature.PAUSE. Raumfeld
            # renderers drop the HTTP connection on a UPnP Pause and re-fetch the stream
            # from the start on Play, so an in-place pause/resume restarts the track.
            # Without PAUSE, MA treats pause as stop and, on resume, re-streams the track
            # from the stored position (resume_pos) - which resumes where the user paused.
        }
        # Raumfeld rooms can be (sync-)grouped with any other room of this provider.
        self._attr_can_group_with = {provider.instance_id}
        # expose the room's analog input as a selectable source when it has one
        if self._line_in is not None:
            self._attr_supported_features.add(PlayerFeature.SELECT_SOURCE)
            self._attr_source_list = [
                PlayerSource(
                    id=SOURCE_LINE_IN,
                    name="Line-in",
                    passive=False,
                    can_play_pause=False,
                    can_next_previous=False,
                    can_seek=False,
                )
            ]

    @property
    def raumfeld(self) -> RaumfeldPlayerProvider:
        """Return the owning provider (typed)."""
        return cast("RaumfeldPlayerProvider", self.provider)

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return the player-specific config entries (sample-rate / bit-depth options)."""
        return [*PLAYER_CONFIG_ENTRIES]

    def set_available(self, available: bool) -> None:
        """
        Set the availability of this player.

        :param available: Whether the player is currently reachable/usable.
        """
        self._attr_available = available
        self.update_state()

    def set_room(self, room: str) -> None:
        """
        Follow a Raumfeld room rename: update the name used for host calls and display.

        :param room: The room's current name as reported by the host.
        """
        # the player_id is tied to the hardware, but hassfeld is keyed by room name, so
        # the stored name must track a rename or play/stop/volume would target a stale name
        if room == self.room:
            return
        self.room = room
        self._attr_name = room
        self.update_state()

    def set_group_members(self, member_ids: list[str]) -> None:
        """
        Set this player's sync-group members (leader first), or ``[]`` when solo.

        :param member_ids: Player ids in the group, with this player (the leader) first.
        """
        self._attr_group_members = member_ids
        self.update_state()

    async def play(self) -> None:
        """Send PLAY/resume command."""
        self._mark_play_started()
        try:
            zone = await self._ensure_playable_zone()
            await self._wake_rooms(zone)
            await self.raumfeld.host.async_zone_play(zone)
        except HOST_ERRORS as err:
            raise PlayerCommandFailed(f"Failed to resume {self.room}: {err!r}") from err

    async def stop(self) -> None:
        """Send STOP command."""
        self._clear_next()
        # we treat pause as stop; freeze the extrapolated position into the anchor so the
        # paused progress bar stays where playback was, instead of snapping back to the
        # last (behind) poll anchor
        self._freeze_elapsed()
        # a Raumfeld line-in keeps playing through a normal transport stop, so when we are
        # leaving the line-in hard-stop the room(s) by putting them into (manual) standby
        hard_stop = self._attr_active_source == SOURCE_LINE_IN
        # only a room that is part of an active zone has something to stop; a
        # standby/unassigned room has no zone the host can address
        if (zone := self._active_zone()) is not None:
            try:
                await self.raumfeld.host.async_zone_stop(zone)
                if hard_stop:
                    for room in zone:
                        await self.raumfeld.host.async_enter_manual_standby(room)
            except HOST_ERRORS as err:
                # do not report IDLE while the renderer may still be playing
                raise PlayerCommandFailed(f"Failed to stop {self.room}: {err!r}") from err
        self._attr_active_source = None
        self._attr_playback_state = PlaybackState.IDLE
        self.update_state()

    async def play_media(self, media: PlayerMedia) -> None:
        """
        Handle PLAY MEDIA: point the room's zone at the MA stream URL.

        In flow mode (the default) this is the whole queue as one continuous stream and MA
        advances the queue inside it, so this runs on the first play and again whenever MA
        restarts the flow (a seek, or a sample-rate change). With flow mode off it is one
        queue item, and the provider starts the next one itself when this one finishes.
        """
        self._mark_play_started()
        # a new item supersedes the queued next one; MA enqueues the next after it again
        self._clear_next()
        self._advance_armed = not self.flow_mode
        url = await self.mass.streams.resolve_stream_url(self.player_id, media)
        didl_metadata = create_didl_metadata(media, url)
        try:
            zone = await self._ensure_playable_zone()
            # wake any room in (manual) standby first: a manually-standby renderer does not
            # auto-power-on for playback and the host answers "Please turn on a device"
            await self._wake_rooms(zone)
            # re-pointing a zone that is still playing (a seek, a track picked by the user, or
            # MA restarting the flow) needs a clean stop first; a stopped zone takes the new
            # stream directly.
            if self._attr_playback_state == PlaybackState.PLAYING:
                await self.raumfeld.host.async_zone_stop(zone)
            await self.raumfeld.host.async_set_av_transport_uri(zone, url, didl_metadata)
            await self.raumfeld.host.async_zone_play(zone)
        except HOST_ERRORS as err:
            raise PlayerCommandFailed(f"Failed to start playback on {self.room}: {err!r}") from err
        # optimistic state update; poll() will reconcile with the device
        self.set_current_media(uri=url, clear_all=True)
        self._attr_active_source = self.player_id
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def select_source(self, source: str) -> None:
        """Handle SELECT_SOURCE: play the room's analog Line-In input."""
        if source != SOURCE_LINE_IN or self._line_in is None:
            raise PlayerCommandFailed(f"Unknown source '{source}' for {self.room}")
        url, _title = self._line_in
        self._mark_play_started()
        self._clear_next()  # a live input never ends, so never auto-advance
        didl_metadata = create_didl_metadata(PlayerMedia(uri=url, title="Line-in"), url)
        try:
            zone = await self._ensure_playable_zone()
            await self._wake_rooms(zone)
            await self.raumfeld.host.async_zone_stop(zone)
            await self.raumfeld.host.async_set_av_transport_uri(zone, url, didl_metadata)
            await self.raumfeld.host.async_zone_play(zone)
        except HOST_ERRORS as err:
            raise PlayerCommandFailed(f"Failed to select Line-In on {self.room}: {err!r}") from err
        self._attr_active_source = SOURCE_LINE_IN
        self.set_current_media(uri=url, clear_all=True, title="Line-in")
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """Handle enqueuing of the next queue item (flow mode off only)."""
        # the zone renderer cannot take a next item itself, so keep it and start it from
        # poll() / the end watch when the current track finishes
        self._next_media = media

    async def volume_set(self, volume_level: int) -> None:
        """Send VOLUME_SET command (Raumfeld volume is 0-100, same as MA)."""
        try:
            await self.raumfeld.host.async_set_room_volume(self.room, volume_level)
        except HOST_ERRORS as err:
            raise PlayerCommandFailed(f"Failed to set volume on {self.room}: {err!r}") from err
        self._attr_volume_level = volume_level
        self.update_state()

    async def volume_mute(self, muted: bool) -> None:
        """Send VOLUME_MUTE command for this room's zone."""
        # muting is a zone operation; skip it while the room is not in an active zone
        if (zone := self._active_zone()) is not None:
            try:
                await self.raumfeld.host.async_set_zone_mute(zone, muted)
            except HOST_ERRORS as err:
                raise PlayerCommandFailed(f"Failed to mute {self.room}: {err!r}") from err
        self._attr_volume_muted = muted
        self.update_state()

    # grouping -----------------------------------------------------------------

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS: translate MA group changes into Raumfeld zone changes."""
        host = self.raumfeld.host

        # this player is the group leader and owns the zone: add/drop each member's room
        try:
            if player_ids_to_add:
                add_rooms = [
                    room
                    for pid in player_ids_to_add
                    if (room := self._player_id_to_room(pid)) is not None
                ]
                await self._add_rooms_to_zone(add_rooms)
            for pid in player_ids_to_remove or []:
                if (room := self._player_id_to_room(pid)) is not None:
                    await host.async_drop_room_from_zone(room, self._active_zone() or [self.room])
        except HOST_ERRORS as err:
            raise PlayerCommandFailed(
                f"Failed to change Raumfeld group for {self.room}: {err!r}"
            ) from err

        # MA side: maintain the leader's member list (source of truth for MA).
        members = dict.fromkeys(self._attr_group_members)
        for pid in player_ids_to_add or []:
            members[pid] = None
        for pid in player_ids_to_remove or []:
            members.pop(pid, None)
        other_member_ids = [pid for pid in members if pid != self.player_id]
        self._attr_group_members = [self.player_id, *other_member_ids] if other_member_ids else []
        self.update_state()
        for pid in [*(player_ids_to_add or []), *(player_ids_to_remove or [])]:
            if (member := self.mass.players.get_player(pid)) is not None:
                member.update_state()

        # a room added to an already-playing Raumfeld zone does not get pulled into the
        # running stream, so re-push playback to bring the new member into sync. Resume
        # (rather than a fresh play) keeps the position, and running it deferred avoids
        # re-entering the player lock held by this command; debounced per leader so
        # adding several rooms at once only re-syncs once.
        if player_ids_to_add:
            queue = self.mass.player_queues.get_active_queue(self.player_id)
            if queue is not None and queue.state == PlaybackState.PLAYING:
                self.mass.call_later(
                    1,
                    self.mass.player_queues.resume,
                    queue.queue_id,
                    task_id=f"raumfeld_resync_{self.player_id}",
                )

    async def poll(self) -> None:
        """Poll the Raumfeld host for this room's current state."""
        host = self.raumfeld.host
        # a grouped follower shares the zone's transport/position with its leader, so it
        # polls only its own (per-room) volume and mirrors the leader's state: without that a
        # stopped group would leave the follower reading as playing until it is ungrouped
        if self.synced_to and self.synced_to != self.player_id:
            volume = await self._read_volume(host)
            if volume is not None:
                self._attr_volume_level = volume
            leader = self.mass.players.get_player(self.synced_to)
            self._attr_playback_state = leader.playback_state if leader else PlaybackState.IDLE
            if self._attr_playback_state == PlaybackState.PLAYING:
                self._attr_poll_interval = PLAYING_POLL_INTERVAL
            else:
                self._attr_poll_interval = IDLE_POLL_INTERVAL
            self.update_state()
            return

        # A room that is playing nothing belongs to no zone, and transport/position can
        # only be read from a zone - asking anyway costs two round-trips per poll and
        # leaves the host resolving a missing zone. Read just the volume instead.
        if (zone := self._active_zone()) is None:
            volume = await self._read_volume(host)
            if volume is not None:
                self._attr_volume_level = volume
            self._attr_playback_state = PlaybackState.IDLE
            self._attr_poll_interval = IDLE_POLL_INTERVAL
            self.update_state()
            return

        # read volume (per room), transport and position (per zone) concurrently so a poll
        # cycle costs one round-trip of wall time instead of three, keeping state snappy.
        # State always comes from the zone: the room's own renderer reports no TrackURI and
        # a meaningless RelTime, so it can only be asked *whether* it plays, not what.
        volume, transport, position = await asyncio.gather(
            self._read_volume(host),
            self._read_transport(host, zone),
            self._read_position(host, zone),
        )

        if volume is not None:
            self._attr_volume_level = volume

        playing = False
        ended = False
        if transport is not None:
            raw_state = (transport.get("CurrentTransportState") or "").upper()
            self._attr_playback_state = _map_transport_state(raw_state)
            playing = self._attr_playback_state == PlaybackState.PLAYING
            ended = raw_state in ("STOPPED", "NO_MEDIA_PRESENT")

        if position is not None:
            self._apply_position(position, playing)
            if playing:
                self._near_end = _near_track_end(position)

        # With flow mode off each item is its own stream and nothing advances the queue on
        # the device, so start the next item here - from the end watch, which catches the
        # stop within a fraction of a second, or this poll as the fallback. (With flow mode
        # on, MA advances the queue inside the flow and none of this is armed.)
        if self._advance_armed:
            if playing:
                self._schedule_end_watch(position)
            if transport is not None:
                self._maybe_advance(playing, ended)

        # poll fast right after a play command so MA sees playback actually start, and in
        # the final seconds of a track when this room advances the queue itself; a relaxed
        # rate while it plays on, the slow rate when idle
        recently_started = (time.time() - self._play_started_at) < STARTUP_POLL_WINDOW
        if recently_started or (self._advance_armed and playing and self._near_end):
            self._attr_poll_interval = FAST_POLL_INTERVAL
        elif playing:
            self._attr_poll_interval = PLAYING_POLL_INTERVAL
        else:
            self._attr_poll_interval = IDLE_POLL_INTERVAL

        self.update_state()

    def _apply_position(self, pos: dict[str, str], playing: bool) -> None:
        """Report the device position to MA (in flow mode: the position within the flow)."""
        device_uri = pos.get("TrackURI", "") or ""
        our_stream = device_uri.startswith(self.mass.streams.base_url)
        # Line-In is an external source with no queue item behind it, so reflect what the
        # device reports. Our own flow stream is owned by MA's queue controller (it holds
        # the queue_item_id progress tracking needs), so never overwrite current_media there.
        if playing and device_uri and not our_stream:
            meta = parse_didl_metadata(pos.get("TrackMetaData"))
            self.set_current_media(
                uri=device_uri,
                clear_all=True,
                title=meta["title"],
                artist=meta["artist"],
                album=meta["album"],
                image_url=meta["image_url"],
                duration=parse_duration(pos.get("TrackDuration")),
            )
        raw_elapsed = parse_duration(pos.get("RelTime"))
        if raw_elapsed is None or not playing:
            return
        # In flow mode the zone clock runs on across the whole queue (ICY changes only the
        # title, measured), which already is the cumulative stream-time MA maps back to the
        # queue item that is playing; a new play command restarts both sides together.
        elapsed = float(raw_elapsed)
        # The device reports whole seconds, so re-anchor MA's smooth clock only when it
        # really diverges (a seek, a stall) and otherwise let it extrapolate.
        now = time.time()
        if self._attr_elapsed_time is not None and self._attr_elapsed_time_last_updated is not None:
            expected = self._attr_elapsed_time + (now - self._attr_elapsed_time_last_updated)
            if abs(elapsed - expected) <= POSITION_DRIFT_THRESHOLD:
                return
        self._attr_elapsed_time = float(elapsed)
        self._attr_elapsed_time_last_updated = now

    async def _read_volume(self, host: hassfeld.RaumfeldHost) -> int | None:
        """Read this room's volume (0-100), or ``None`` on a host error."""
        try:
            volume = await host.async_get_room_volume(self.room)
        except HOST_ERRORS as err:
            self.logger.debug("Failed to read volume for %s: %r", self.room, err)
            return None
        if isinstance(volume, dict):
            volume = volume.get("CurrentVolume")
        return int(volume) if volume is not None else None

    async def _read_transport(
        self, host: hassfeld.RaumfeldHost, zone: list[str]
    ) -> dict[str, str] | None:
        """Read the zone's GetTransportInfo response, or ``None`` if it could not be read."""
        try:
            # hassfeld swallows a UPnP timeout and returns None, so an unanswered read has
            # to stay distinguishable from a real one: as an empty dict it would report a
            # playing zone as idle for a poll.
            return await host.async_get_transport_info(zone) or None
        except HOST_ERRORS as err:
            self.logger.debug("Failed to read transport info for zone %s: %r", zone, err)
            return None

    async def _read_position(
        self, host: hassfeld.RaumfeldHost, zone: list[str]
    ) -> dict[str, str] | None:
        """Read the zone's GetPositionInfo response, or ``None`` if it could not be read."""
        try:
            return await host.async_get_position_info(zone) or None
        except HOST_ERRORS as err:
            self.logger.debug("Failed to read position info for zone %s: %r", zone, err)
            return None

    def _maybe_advance(self, playing: bool, ended: bool) -> None:
        """Play the next queue item when the current track has actually finished."""
        # advance only on a real stop (STOPPED/NO_MEDIA, not a pause) near the track's end,
        # so pausing from the Raumfeld app or a mid-track buffer stall never skips.
        # _mark_play_started resets _prev_playing/_near_end, so the not-yet-playing startup
        # gap can't look like an ended track.
        if self._prev_playing and ended and self._near_end:
            if self._next_media is not None:
                self._advance_to_next()
            else:
                self._advance_armed = False
        self._prev_playing = playing

    def _advance_to_next(self) -> None:
        """Start the queued next item, from whichever path spotted the track ending."""
        next_media, self._next_media = self._next_media, None
        if next_media is None:
            return
        # Claim it before the play command is even created. The end watch and poll() both
        # reach this, and a poll landing in between would otherwise see a finished track
        # with nothing queued and disarm the advance the new track still needs.
        self._prev_playing = False
        self.mass.create_task(self.play_media(next_media))

    def _schedule_end_watch(self, pos: dict[str, str] | None) -> None:
        """Arrange to look for the end of the current track at the moment it is due."""
        if self._next_media is None:
            return
        raw_elapsed = parse_duration((pos or {}).get("RelTime"))
        duration = parse_duration((pos or {}).get("TrackDuration"))
        if raw_elapsed is None or not duration:
            return
        remaining = duration - (raw_elapsed + POSITION_REPORT_LAG)
        # Only arm it ahead of the end. Past that the watch is already running its bounded
        # round of checks, and re-arming it from here would reset that count on every poll
        # and leave a track that outlives its metadata duration being checked forever.
        if remaining <= 0:
            return
        # re-scheduled on every poll, so it follows the device's clock rather than drifting
        self.mass.call_later(remaining, self._watch_for_track_end, 0, task_id=self._advance_task_id)

    async def _watch_for_track_end(self, attempt: int) -> None:
        """Advance as soon as the device reports the track stopped, or give up trying."""
        if not self._advance_armed or self._next_media is None:
            return
        if (zone := self._active_zone()) is None:
            return
        info = await self._read_transport(self.raumfeld.host, zone)
        if (info or {}).get("CurrentTransportState", "").upper() in ("STOPPED", "NO_MEDIA_PRESENT"):
            self._advance_to_next()
            return
        # still playing: the track runs longer than its metadata says, so keep looking for
        # a while and otherwise leave it to poll(), which catches the stop either way
        if attempt + 1 < ADVANCE_WATCH_ATTEMPTS:
            self.mass.call_later(
                ADVANCE_WATCH_INTERVAL,
                self._watch_for_track_end,
                attempt + 1,
                task_id=self._advance_task_id,
            )

    def _clear_next(self) -> None:
        """Disarm the auto-advance and drop the queued next item and its end watch."""
        self._advance_armed = False
        self._next_media = None
        self.mass.cancel_timer(self._advance_task_id)

    def _freeze_elapsed(self) -> None:
        """Advance the elapsed-time anchor to now so a pause keeps the shown position."""
        if self._attr_elapsed_time is not None and self._attr_elapsed_time_last_updated is not None:
            now = time.time()
            self._attr_elapsed_time += now - self._attr_elapsed_time_last_updated
            self._attr_elapsed_time_last_updated = now

    def _mark_play_started(self) -> None:
        """Record a play/resume command and switch to fast polling."""
        self._play_started_at = time.time()
        self._attr_poll_interval = FAST_POLL_INTERVAL
        # a fresh play/resume: the not-yet-playing startup gap must not look like the
        # previous track ending, so restart end detection from "was not playing"
        self._prev_playing = False
        self._near_end = False
        # reset the position: on resume MA adds a seek offset, so a stale pre-pause
        # position here would be double-counted until the next poll re-anchors to the real
        # position
        self._attr_elapsed_time = 0.0
        self._attr_elapsed_time_last_updated = time.time()

    async def _wake_rooms(self, zone: list[str]) -> None:
        """Bring the zone's rooms out of standby and wait until they report awake."""
        host = self.raumfeld.host
        woke = False
        for room in zone:
            # rooms in MANUAL_STANDBY do not auto-power-on for playback (unlike
            # AUTOMATIC_STANDBY); leaving standby is a no-op for an already-awake room
            try:
                if "STANDBY" in (host.get_room_power_state(room) or ""):
                    await host.async_leave_standby(room)
                    woke = True
            except HOST_ERRORS as err:
                self.logger.debug("Failed to wake room %s: %r", room, err)
        if not woke:
            return
        # a renderer that just left standby drops the first play command while it powers
        # on, so wait (bounded) for the woken rooms to report awake before playing
        deadline = time.time() + WAKE_TIMEOUT
        while time.time() < deadline:
            try:
                if all("STANDBY" not in (host.get_room_power_state(r) or "") for r in zone):
                    return
            except HOST_ERRORS as err:
                self.logger.debug("Failed to read power state while waking: %r", err)
            await asyncio.sleep(WAKE_POLL_INTERVAL)

    def _active_zone(self) -> list[str] | None:
        """Return this room's zone if the host can address it, else ``None``."""
        # get_zone_for_room only lists active zones; double-check the host can still
        # resolve it to a zone UDN, so callers never hand hassfeld an unknown zone
        zone = self.raumfeld.get_zone_for_room(self.room)
        if zone and self.raumfeld.host.roomlst_to_zoneudn(zone) is not None:
            return zone
        return None

    async def _ensure_playable_zone(self) -> list[str]:
        """Return an addressable Raumfeld zone for this room, creating one if needed."""
        if (zone := self._active_zone()) is not None:
            return zone
        host = self.raumfeld.host
        # a room that is idle/standby (e.g. just left a group) is in no active zone, so
        # create a single-room zone. During a group handover the room is still detaching
        # and hassfeld's create-and-wait gives up silently on timeout, so confirm the host
        # can resolve the new zone before returning and retry while the state settles.
        for attempt in range(ZONE_CREATE_ATTEMPTS):
            await host.async_create_zone([self.room])
            if (zone := self._active_zone()) is not None:
                return zone
            if host.roomlst_to_zoneudn([self.room]) is not None:
                return [self.room]
            if attempt < ZONE_CREATE_ATTEMPTS - 1:
                await asyncio.sleep(ZONE_CREATE_RETRY_DELAY)
        # let the caller surface a clear failure rather than silently doing nothing
        return [self.room]

    async def _add_rooms_to_zone(self, rooms: list[str]) -> None:
        """Add the given rooms to this leader's zone, waiting until they have joined."""
        host = self.raumfeld.host
        # hassfeld's add-room call does not wait, so during rapid regrouping (a room moving
        # straight from one group to another) it can race and leave the room unassigned;
        # verify the room really landed in the zone and retry while the state settles
        for attempt in range(ZONE_CREATE_ATTEMPTS):
            zone = await self._ensure_playable_zone()
            missing = [room for room in rooms if room not in zone]
            if not missing:
                return
            for room in missing:
                await host.async_add_room_to_zone(room, zone)
            if attempt < ZONE_CREATE_ATTEMPTS - 1:
                await asyncio.sleep(ZONE_CREATE_RETRY_DELAY)

    def _player_id_to_room(self, player_id: str) -> str | None:
        """Resolve a MA player_id of this provider back to its Raumfeld room name."""
        player = self.mass.players.get_player(player_id)
        return player.room if isinstance(player, RaumfeldPlayer) else None
