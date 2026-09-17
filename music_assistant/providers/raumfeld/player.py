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
    from music_assistant_models.config_entries import ConfigEntry

    from .provider import RaumfeldPlayerProvider

# Poll interval (seconds) while playing / just after a play command.
FAST_POLL_INTERVAL = 2
# How long (seconds) after a play/resume command to keep polling fast, so MA catches
# the device actually starting playback (Raumfeld renderers buffer before they start).
STARTUP_POLL_WINDOW = 20
# Only re-anchor MA's elapsed-time clock when the device position diverges from the
# expected (extrapolated) position by more than this many seconds. Keeps the progress
# bar smooth despite the device's 1-second position granularity.
POSITION_DRIFT_THRESHOLD = 3.0
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
        # the next queue item MA handed us; the device has no SetNextAVTransportURI so we
        # play it ourselves when the current track ends (see poll())
        self._next_media: PlayerMedia | None = None
        # whether a track is playing that should auto-advance when it finishes, and the
        # playing-state seen on the previous poll (used to detect a track ending)
        self._advance_armed = False
        self._prev_playing = False
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
            # ENQUEUE disables MA 'flow mode' so each queue item is played as its own
            # stream via SetAVTransportURI (a flow stream cannot be resumed by seeking).
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
        self._advance_armed = False
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
                self.logger.debug("Failed to stop %s: %r", self.room, err)
        self._attr_active_source = None
        self._attr_playback_state = PlaybackState.IDLE
        self.update_state()

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA: point the room's zone at the MA stream URL."""
        self._mark_play_started()
        self._next_media = None
        self._advance_armed = True
        url = await self.mass.streams.resolve_stream_url(self.player_id, media)
        didl_metadata = create_didl_metadata(media, url)
        try:
            zone = await self._ensure_playable_zone()
            # wake any room in (manual) standby first: a manually-standby renderer does not
            # auto-power-on for playback and the host answers "Please turn on a device"
            await self._wake_rooms(zone)
            # stop first so the renderer cleanly loads the new URI (a seek/next re-streams
            # a different URL); without this it may keep playing the previous stream
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
        self._next_media = None
        self._advance_armed = False  # a live input never ends, so never auto-advance
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
        """Handle enqueuing of the next queue item."""
        # Raumfeld zone renderers have no SetNextAVTransportURI, so we can't hand the next
        # item to the device; keep it and play it ourselves when the current track ends.
        self._next_media = media

    async def volume_set(self, volume_level: int) -> None:
        """Send VOLUME_SET command (Raumfeld volume is 0-100, same as MA)."""
        await self.raumfeld.host.async_set_room_volume(self.room, volume_level)
        self._attr_volume_level = volume_level
        self.update_state()

    async def volume_mute(self, muted: bool) -> None:
        """Send VOLUME_MUTE command for this room's zone."""
        # muting is a zone operation; skip it while the room is not in an active zone
        if (zone := self._active_zone()) is not None:
            await self.raumfeld.host.async_set_zone_mute(zone, muted)
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
        # only the async_* getters are used here; hassfeld's sync getters wrap
        # asyncio.run() and can't be called from within MA's event loop
        host = self.raumfeld.host
        zone = self._current_zone()

        # Volume (per room). GetVolume returns the CurrentVolume int (0-100).
        try:
            volume = await host.async_get_room_volume(self.room)
            if isinstance(volume, dict):
                volume = volume.get("CurrentVolume")
            if volume is not None:
                self._attr_volume_level = int(volume)
        except HOST_ERRORS as err:
            self.logger.debug("Failed to read volume for %s: %r", self.room, err)

        # Playback state (per zone). GetTransportInfo -> CurrentTransportState.
        playing = False
        transport_ok = False
        try:
            transport = await host.async_get_transport_info(zone)
            self._attr_playback_state = _map_transport_state(
                (transport or {}).get("CurrentTransportState")
            )
            playing = self._attr_playback_state == PlaybackState.PLAYING
            transport_ok = True
        except HOST_ERRORS as err:
            self.logger.debug("Failed to read transport info for zone %s: %r", zone, err)

        # Current media + position (per zone). GetPositionInfo -> TrackURI, RelTime, DIDL.
        try:
            pos = await host.async_get_position_info(zone) or {}
            device_uri = pos.get("TrackURI", "") or ""
            # Only take over current_media for EXTERNAL sources. When the device is
            # playing one of our own MA streams, the queue controller owns current_media
            # (including the queue_item_id it needs to track progress); overwriting it
            # here would clear that link and break elapsed-time / resume tracking.
            if playing and device_uri and not device_uri.startswith(self.mass.streams.base_url):
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
            # Report the current position so MA can track progress and, since we treat
            # pause as stop, resume the track from where it was paused.
            #
            # Only update the position while actually PLAYING. A stopped/paused Raumfeld
            # renderer reports an unreliable RelTime, so re-anchoring then would make the
            # paused progress bar wobble; leaving it frozen keeps it on the pause point.
            #
            # While playing, the device reports RelTime at 1-second granularity, so
            # overwriting our anchor every poll would make MA's (smooth, extrapolated)
            # clock jump by up to a second each time. Instead re-anchor only when the
            # device position really diverges from what MA expects - a seek, track change
            # or buffer stall - and otherwise let MA's clock run smoothly.
            elapsed = parse_duration(pos.get("RelTime"))
            if elapsed is not None and playing:
                now = time.time()
                if (
                    self._attr_elapsed_time is not None
                    and self._attr_elapsed_time_last_updated is not None
                ):
                    expected = self._attr_elapsed_time + (
                        now - self._attr_elapsed_time_last_updated
                    )
                    diverged = abs(elapsed - expected) > POSITION_DRIFT_THRESHOLD
                else:
                    diverged = True
                if diverged:
                    self._attr_elapsed_time = float(elapsed)
                    self._attr_elapsed_time_last_updated = now
        except HOST_ERRORS as err:
            self.logger.debug("Failed to read position info for zone %s: %r", zone, err)

        # Poll fast while playing - and for a short window right after a play/resume
        # command, so MA locks onto the device's real position quickly instead of
        # running its own clock ahead during the device's buffering/startup delay.
        recently_started = (time.time() - self._play_started_at) < STARTUP_POLL_WINDOW
        self._attr_poll_interval = FAST_POLL_INTERVAL if (playing or recently_started) else 15

        # the renderer can't pre-enqueue the next track, so advance the queue ourselves
        if transport_ok:
            self._maybe_advance(playing, recently_started)

        self.update_state()

    def _maybe_advance(self, playing: bool, recently_started: bool) -> None:
        """Play the next queue item when the current track has finished."""
        # a track that was playing and is now stopped - outside the startup transition and
        # not a user stop (which disarms) - has ended on its own
        if self._advance_armed and self._prev_playing and not playing and not recently_started:
            if self._next_media is not None:
                next_media, self._next_media = self._next_media, None
                self.mass.create_task(self.play_media(next_media))
            else:
                self._advance_armed = False
        self._prev_playing = playing

    def _mark_play_started(self) -> None:
        """Record a play/resume command and switch to fast polling."""
        self._play_started_at = time.time()
        self._attr_poll_interval = FAST_POLL_INTERVAL
        # reset the position: on resume MA adds a seek offset, so a stale pre-pause
        # position here would be double-counted until the device reports the new stream's
        # 0-based time (the next poll re-anchors to the real position)
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

    def _current_zone(self) -> list[str]:
        """Return the room-list of the zone this room currently controls."""
        # a room not in an active multi-room zone is treated as its own single-room zone
        return self.raumfeld.get_zone_for_room(self.room) or [self.room]

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
        return getattr(player, "room", None)
