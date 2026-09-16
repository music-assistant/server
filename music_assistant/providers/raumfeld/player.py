"""Teufel Raumfeld Player (one per Raumfeld room)."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import IdentifierType, PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.player import DeviceInfo

from music_assistant.helpers.upnp import create_didl_metadata
from music_assistant.models.player import Player

from .constants import HOST_ERRORS, PLAYER_CONFIG_ENTRIES
from .helpers import parse_didl_metadata, parse_duration

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry
    from music_assistant_models.player import PlayerMedia

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
        # Expose the room's media-renderer UUID and IP so Music Assistant links this
        # native player to the same device's DLNA/Chromecast/Sendspin representations
        # instead of showing them as duplicate players.
        renderer_uuid, renderer_ip = provider.resolve_room_renderer(room)
        if renderer_uuid:
            self._attr_device_info.add_identifier(IdentifierType.UUID, renderer_uuid)
        if renderer_ip:
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

    async def play(self) -> None:
        """Send PLAY/resume command."""
        self._mark_play_started()
        await self.raumfeld.host.async_zone_play(self._current_zone())

    async def stop(self) -> None:
        """Send STOP command."""
        self._advance_armed = False
        await self.raumfeld.host.async_zone_stop(self._current_zone())
        self._attr_playback_state = PlaybackState.IDLE
        self.update_state()

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA: point the room's zone at the MA stream URL."""
        zone = self._current_zone()
        self._mark_play_started()
        self._next_media = None
        self._advance_armed = True
        url = await self.mass.streams.resolve_stream_url(self.player_id, media)
        didl_metadata = create_didl_metadata(media, url)
        # stop first so the renderer cleanly loads the new URI (a seek/next re-streams a
        # different URL); without this it may keep playing the previous stream
        await self.raumfeld.host.async_zone_stop(zone)
        await self.raumfeld.host.async_set_av_transport_uri(zone, url, didl_metadata)
        await self.raumfeld.host.async_zone_play(zone)
        # optimistic state update; poll() will reconcile with the device
        self.set_current_media(uri=url, clear_all=True)
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
        await self.raumfeld.host.async_set_zone_mute(self._current_zone(), muted)
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
        for pid in player_ids_to_add or []:
            if (room := self._player_id_to_room(pid)) is not None:
                await host.async_add_room_to_zone(room, self._current_zone())
        for pid in player_ids_to_remove or []:
            if (room := self._player_id_to_room(pid)) is not None:
                await host.async_drop_room_from_zone(room, self._current_zone())

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
            if device_uri and not device_uri.startswith(self.mass.streams.base_url):
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

    def _current_zone(self) -> list[str]:
        """Return the room-list of the zone this room currently controls."""
        # a room not in an active multi-room zone is treated as its own single-room zone
        return self.raumfeld.get_zone_for_room(self.room) or [self.room]

    def _player_id_to_room(self, player_id: str) -> str | None:
        """Resolve a MA player_id of this provider back to its Raumfeld room name."""
        player = self.mass.players.get_player(player_id)
        return getattr(player, "room", None)
