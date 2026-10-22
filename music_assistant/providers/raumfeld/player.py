"""Teufel Raumfeld Player (one per Raumfeld room)."""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import re
import time
from typing import TYPE_CHECKING, cast

from async_upnp_client.exceptions import UpnpActionError, UpnpError
from hassfeld import upnp as hassfeld_upnp
from hassfeld import webservice as hassfeld_ws
from hassfeld.constants import SERVICE_AV_TRANSPORT
from music_assistant_models.enums import IdentifierType, PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.player import DeviceInfo, PlayerMedia, PlayerSource

from music_assistant.constants import ATTR_ANNOUNCEMENT_IN_PROGRESS, EXTERNAL_PAUSE_IDLE_TIMEOUT
from music_assistant.helpers.upnp import create_didl_metadata
from music_assistant.models.player import Player

from .constants import (
    HOST_ERRORS,
    PLAYER_CONFIG_ENTRIES,
    SOURCE_LINE_IN,
)
from .helpers import (
    container_object_id,
    external_source,
    library_uri,
    parse_didl_metadata,
    parse_duration,
    query_last_change,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Any, Concatenate

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
# handover), how many times to (re)create one, and how long (seconds) to wait each time for
# the host to publish a zone change before looking again.
# The host lists a new zone after about a second and publishes its renderer, which playback
# is addressed to, up to three seconds after that (measured: 1.1 - 4.3 s in total).
ZONE_CREATE_ATTEMPTS = 3
ZONE_WAIT_TIMEOUT = 6.0
ZONE_WAIT_INTERVAL = 0.1

# After leaving standby, how long (seconds) to wait for a room to report awake before
# playing, so the first play command is not dropped while the renderer powers on.
WAKE_TIMEOUT = 5.0
WAKE_POLL_INTERVAL = 0.3

# The power state the host reports for a room that was switched off. A room in
# AUTOMATIC_STANDBY merely went idle and wakes by itself for playback, so it counts as on:
# every idle room ends up there, and reporting those as off would have MA ungroup them.
POWER_STATE_OFF = "MANUAL_STANDBY"
# How long (seconds) after a power command the host's reported power state is ignored, as
# it follows the command with a delay and would briefly report the old state back.
POWER_SETTLE_TIME = 5.0


# Features that only apply while something other than Music Assistant is playing, and then
# only as far as the host lists the matching transport action for that source.
# The scheme of the uri reported for an external source's current media. It names the item
# the host has loaded, never the address the host streams it from.
EXTERNAL_URI_SCHEME = "raumfeld://"

# How long (seconds) a handover of this room's queue to the rooms that play on may take. A
# stop that arrives in that time is MA ending the queue here, not a request to stop the
# Spotify Connect session that took the room.
QUEUE_HANDOVER_TIMEOUT = 30.0

# The AVTransport error for a transition the transport cannot make from its current state,
# which is what a Stop on a zone that already stopped answers.
TRANSITION_NOT_AVAILABLE = 701

EXTERNAL_SOURCE_FEATURES = {
    PlayerFeature.PAUSE,
    PlayerFeature.NEXT_PREVIOUS,
    PlayerFeature.SEEK,
}


def _changes_playback[**P, R](
    func: Callable[Concatenate[RaumfeldPlayer, P], Coroutine[Any, Any, R]],
) -> Callable[Concatenate[RaumfeldPlayer, P], Coroutine[Any, Any, R]]:
    """Mark a command that changes what plays, so polls do not apply readings from before it."""

    @functools.wraps(func)
    async def wrapper(player: RaumfeldPlayer, /, *args: P.args, **kwargs: P.kwargs) -> R:
        player._playback_changes += 1
        player._playback_generation += 1
        try:
            return await func(player, *args, **kwargs)
        finally:
            player._playback_changes -= 1
            # a poll that overlapped the command read the state from before it ended
            player._playback_generation += 1

    return wrapper


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
    # the host reports what is playing accurately, so a source Music Assistant has no name
    # of its own for counts as a takeover too
    _attr_trusts_reported_source = True
    # a source the host keeps loaded after a stop is reported as paused (see
    # _update_zone_source); give up on it when nobody resumes it
    _attr_external_pause_idle_timeout = EXTERNAL_PAUSE_IDLE_TIMEOUT
    # set before the player is fully constructed, as the base class reads the features
    _external_source: PlayerSource | None = None
    # bumped when a command that changes what plays starts and when it ends, and the
    # number of such commands running, so a poll can tell its readings no longer apply
    _playback_generation = 0
    _playback_changes = 0
    # until when a stop is taken as MA ending this room's queue during a handover
    _handover_until = 0.0
    # what a zone source was playing when it was stopped, to start it again later:
    # (current media uri, transport uri, its metadata, track number, position)
    _resume_context: tuple[str, str, str, int, str | None] | None = None
    # the source that was active when the resume context was read
    _resume_source: str | None = None

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
        # The source of playback Music Assistant did not start (Spotify Connect, the
        # Raumfeld app), and whether the room's own renderer plays it rather than its zone:
        # Spotify Connect in single-room mode takes the room out of every zone.
        self._external_source: PlayerSource | None = None
        self._static_sources: list[PlayerSource] = []
        # source id -> (object id, container) of the station buttons offered as sources
        self._library_sources: dict[str, tuple[str, bool]] = {}
        self._external_via_room = False
        # timestamp of the last power command, see POWER_SETTLE_TIME
        self._power_changed_at = 0.0
        self._advance_task_id = f"raumfeld_advance_{player_id}"
        self._attr_device_info = DeviceInfo(
            model=provider.room_model(room) or "Raumfeld", manufacturer="Teufel"
        )
        # Expose the room's media-renderer UUID (and IP) so Music Assistant links this
        # native player to the same renderer's DLNA/Chromecast/Sendspin representation
        # instead of showing them as duplicate players.
        renderer_uuid, renderer_ip = provider.resolve_room_renderer(room)
        # (stream url, title) of this room's analog Line-In input, if it has one
        self._line_in: tuple[str, str] | None = provider.line_in(renderer_uuid)
        if renderer_uuid:
            self._attr_device_info.add_identifier(IdentifierType.UUID, renderer_uuid)
        # The IP is also needed for the room whose renderer runs on the host: its Chromecast
        # and Sendspin outputs share no UUID with the renderer, so without it they end up
        # in a universal player of their own, which this player can then not take over.
        # The host's other renderers on that IP are its virtual zone renderers, which the
        # DLNA provider ignores, so they can no longer be linked to this room.
        if renderer_ip:
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
        self._update_power()
        # Raumfeld rooms can be (sync-)grouped with any other room of this provider.
        self._attr_can_group_with = {provider.instance_id}
        # expose the room's analog input as a selectable source when it has one
        if self._line_in is not None:
            self._attr_supported_features.add(PlayerFeature.SELECT_SOURCE)
            self._static_sources = [
                PlayerSource(
                    id=SOURCE_LINE_IN,
                    name="Line-in",
                    passive=False,
                    can_play_pause=False,
                    can_next_previous=False,
                    can_seek=False,
                )
            ]
            self._attr_source_list = list(self._static_sources)

    @property
    def raumfeld(self) -> RaumfeldPlayerProvider:
        """Return the owning provider (typed)."""
        return cast("RaumfeldPlayerProvider", self.provider)

    @property
    def supported_features(self) -> set[PlayerFeature]:
        """Return the supported features of the player."""
        if self._presented_external() is None:
            # the controls of an external source go with it, also when MA stopped presenting
            # it on its own (its idle timeout)
            return self._attr_supported_features - EXTERNAL_SOURCE_FEATURES
        return self._attr_supported_features

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return the player-specific config entries (sample-rate / bit-depth options)."""
        return [*PLAYER_CONFIG_ENTRIES]

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        await super().on_unload()
        # the timer is held by mass, not by this player, so it would otherwise fire after
        # it is gone, against a dropped player
        self._clear_next()

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

    def set_station_buttons(self, buttons: list[tuple[int, str, str, bool]]) -> None:
        """
        Offer this speaker's station buttons as sources.

        :param buttons: The station buttons: ``(button, object id, title, container)``.
        """
        library: dict[str, tuple[str, bool]] = {}
        sources = [source for source in self._static_sources if source.id == SOURCE_LINE_IN]
        for button, object_id, title, container in buttons:
            source_id = f"station_button_{button}"
            library[source_id] = (object_id, container)
            sources.append(PlayerSource(id=source_id, name=f"Button {button}: {title}"))
        if library == self._library_sources and [(s.id, s.name) for s in sources] == [
            (s.id, s.name) for s in self._static_sources
        ]:
            return
        self._library_sources = library
        self._static_sources = sources
        if sources:
            self._attr_supported_features.add(PlayerFeature.SELECT_SOURCE)
        # a button that is playing stays listed once, with the controls it plays with
        if (external := self._external_source) is None:
            self._attr_source_list = sources
        elif any(s.id == external.id for s in sources):
            self._attr_source_list = [external if s.id == external.id else s for s in sources]
        else:
            # its button is gone, so it can no longer be selected
            external = self._external_source = dataclasses.replace(external, passive=True)
            self._attr_source_list = [*sources, external]
        self.update_state()

    def set_group_members(self, member_ids: list[str]) -> None:
        """
        Set this player's sync-group members (leader first), or ``[]`` when solo.

        :param member_ids: Player ids in the group, with this player (the leader) first.
        """
        # keep the order MA already has for the same rooms, and stay quiet when nothing changed
        if set(member_ids) == set(self._attr_group_members) and (
            not member_ids or member_ids[0] == self.player_id
        ):
            return
        self._attr_group_members = member_ids
        self.update_state()

    @property
    def leads_group(self) -> bool:
        """Return whether this player currently leads a sync group in Music Assistant."""
        return len(self._attr_group_members) > 1 and self._attr_group_members[0] == self.player_id

    @property
    def plays_own_queue(self) -> bool:
        """Return whether this player is playing a queue Music Assistant started on it."""
        return (
            self._attr_active_source == self.player_id
            and self._attr_playback_state == PlaybackState.PLAYING
        )

    async def power(self, powered: bool) -> None:
        """Send POWER command: wake the room, or switch it off (manual standby)."""
        self._power_changed_at = time.time()
        if powered:
            await self._wake_rooms([self.room], strict=True)
        else:
            try:
                await self.raumfeld.host.async_enter_manual_standby(self.room)
            except HOST_ERRORS as err:
                raise PlayerCommandFailed(f"Failed to power off {self.room}: {err!r}") from err
        self._attr_powered = powered
        self.update_state()

    @_changes_playback
    async def play(self) -> None:
        """Send PLAY/resume command."""
        self._drop_ended_external()
        if self._external_source is not None:
            # resume what the host or the speaker itself was playing, where it paused
            await self._external_command("Play", Speed="1")
            # the position was frozen at the pause; it runs on from now, not from then
            self._attr_elapsed_time_last_updated = time.time()
            self._attr_playback_state = PlaybackState.PLAYING
            self.update_state()
            return
        self._mark_play_started()
        try:
            zone = await self._ensure_playable_zone()
            await self._wake_rooms(zone)
            await self._zone_command(zone, "Play", Speed="1")
        except HOST_ERRORS as err:
            raise PlayerCommandFailed(f"Failed to resume {self.room}: {err!r}") from err

    @_changes_playback
    async def stop(self) -> None:
        """Send STOP command."""
        # a queue handed over takes its next item along: nothing may start here after it
        self._clear_next()
        if time.time() < self._handover_until:
            # MA ends this room's queue while handing it over; the room itself left the
            # group's zone (to Spotify Connect, say), and what it plays now must go on
            self._handover_until = 0.0
            return
        # we treat pause as stop; freeze the extrapolated position into the anchor so the
        # paused progress bar stays where playback was, instead of snapping back to the
        # last (behind) poll anchor
        self._freeze_elapsed()
        self._drop_ended_external()
        # a Raumfeld line-in keeps playing through a normal transport stop, so when we are
        # leaving the line-in hard-stop the room(s) by putting them into (manual) standby
        hard_stop = self._attr_active_source == SOURCE_LINE_IN
        # only a room that is part of an active zone has something to stop; a
        # standby/unassigned room has no zone the host can address
        if self._external_via_room:
            # Spotify Connect plays on the room's own renderer, outside any zone
            await self._external_command("Stop")
        elif (zone := self._active_zone()) is not None:
            if self._presented_external() is not None:
                # MA may replay this source later (after an announcement, say), when the
                # zone has something else loaded
                self._resume_context = await self._read_resume_context(zone)
                self._resume_source = self._attr_active_source
            try:
                await self._zone_command(zone, "Stop")
                if hard_stop:
                    for room in zone:
                        await self.raumfeld.host.async_enter_manual_standby(room)
            except HOST_ERRORS as err:
                # do not report IDLE while the renderer may still be playing
                raise PlayerCommandFailed(f"Failed to stop {self.room}: {err!r}") from err
        # Spotify Connect ends its session on a stop
        if self._external_via_room:
            self._clear_external()
        if self._external_source is None:
            self._attr_active_source = None
        else:
            # poll again shortly, so the loaded source shows as resumable without delay
            self._attr_poll_interval = FAST_POLL_INTERVAL
        # Idle for now, also for a source the zone keeps loaded: MA waits for an idle room
        # before it switches sources or announces. The next poll reports a loaded source
        # as paused again, so it can be resumed.
        self._attr_playback_state = PlaybackState.IDLE
        self.update_state()

    @_changes_playback
    async def pause(self) -> None:
        """Send PAUSE command (only offered while an external source is playing)."""
        await self._external_command("Pause")
        self._freeze_elapsed()
        self._attr_playback_state = PlaybackState.PAUSED
        self.update_state()

    async def next_track(self) -> None:
        """Send NEXT TRACK command (only offered while an external source is playing)."""
        await self._external_command("Next")

    async def previous_track(self) -> None:
        """Send PREVIOUS TRACK command (only offered while an external source is playing)."""
        await self._external_command("Previous")

    @_changes_playback
    async def seek(self, position: int) -> None:
        """Send SEEK command (only offered while an external source is playing)."""
        position = max(position, 0)
        hours, remainder = divmod(position, 3600)
        target = f"{hours:02d}:{remainder // 60:02d}:{remainder % 60:02d}"
        await self._external_command("Seek", Unit="ABS_TIME", Target=target)
        self._attr_elapsed_time = float(position)
        self._attr_elapsed_time_last_updated = time.time()
        self.update_state()

    @_changes_playback
    async def play_media(self, media: PlayerMedia) -> None:
        """
        Handle PLAY MEDIA: point the room's zone at the MA stream URL.

        In flow mode (the default) this is the whole queue as one continuous stream and MA
        advances the queue inside it, so this runs on the first play and again whenever MA
        restarts the flow (a seek, or a sample-rate change). With flow mode off it is one
        queue item, and the provider starts the next one itself when this one finishes.
        """
        self._drop_ended_external()
        if media.uri.startswith(EXTERNAL_URI_SCHEME):
            # MA resumes "the current media", which here is an item of the host's own
            if self._external_source is not None:
                # still loaded on the zone
                await self.play()
                return
            context = self._resume_context
            if context is not None and context[0] == media.uri:
                # the zone has something else loaded by now (an announcement)
                await self._restore_zone_source(context)
                return
            # Nothing left to start again. Spotify Connect is the case: any other playback
            # on the room, an announcement included, takes it out of single-room mode and
            # ends the session, which only the Spotify app can start again.
            self.logger.debug("Nothing to resume on %s for %s", self.room, media.uri)
            self._attr_playback_state = PlaybackState.IDLE
            self.update_state()
            return
        self._mark_play_started()
        # a new item supersedes the queued next one; MA enqueues the next after it again
        self._clear_next()
        self._clear_external()
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
                await self._stop_before_switching(zone)
            await self._zone_command(
                zone, "SetAVTransportURI", CurrentURI=url, CurrentURIMetaData=didl_metadata
            )
            await self._zone_command(zone, "Play", Speed="1")
        except HOST_ERRORS as err:
            raise PlayerCommandFailed(f"Failed to start playback on {self.room}: {err!r}") from err
        # optimistic state update; poll() will reconcile with the device
        self.set_current_media(uri=url, clear_all=True)
        self._attr_active_source = self.player_id
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    @_changes_playback
    async def select_source(self, source: str) -> None:
        """Handle SELECT_SOURCE: play the Line-In or one of the station buttons."""
        if (library_item := self._library_sources.get(source)) is not None:
            context = self._resume_context
            if context is not None and self._resume_source == source and self._announcing():
                # MA selects the button again after an announcement: go on where it was
                await self._restore_zone_source(context)
                return
            await self._play_library_item(source, *library_item)
            return
        if source != SOURCE_LINE_IN or self._line_in is None:
            raise PlayerCommandFailed(f"Unknown source '{source}' for {self.room}")
        url, _title = self._line_in
        self._mark_play_started()
        self._clear_next()  # a live input never ends, so never auto-advance
        didl_metadata = create_didl_metadata(PlayerMedia(uri=url, title="Line-in"), url)
        try:
            zone = await self._ensure_playable_zone()
            await self._wake_rooms(zone)
            await self._stop_before_switching(zone)
            await self._zone_command(
                zone, "SetAVTransportURI", CurrentURI=url, CurrentURIMetaData=didl_metadata
            )
            await self._zone_command(zone, "Play", Speed="1")
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
        """Send VOLUME_MUTE command to this room's renderer."""
        # Mute the room's own renderer rather than its zone: the zone mutes every room in
        # the group at once, and a room that plays nothing is in no zone at all.
        if (location := self.raumfeld.room_renderer_location(self.room)) is None:
            raise PlayerCommandFailed(f"Failed to mute {self.room}: its renderer is unknown")
        try:
            await hassfeld_upnp.async_set_mute(self.mass.http_session, location, muted)
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
        with self.raumfeld.changing_group():
            # this player is the group leader and owns the zone: add/drop each member's room
            try:
                if player_ids_to_add:
                    add_rooms = [
                        room
                        for pid in player_ids_to_add
                        if (room := self._player_id_to_room(pid)) is not None
                    ]
                    await self._add_rooms_to_zone(add_rooms)
                    # the host pulls an added room into the running stream by itself, but
                    # not one that is in standby
                    if self._attr_playback_state == PlaybackState.PLAYING:
                        await self._wake_rooms(add_rooms)
                for pid in player_ids_to_remove or []:
                    if (room := self._player_id_to_room(pid)) is not None:
                        await host.async_drop_room_from_zone(
                            room, self._active_zone() or [self.room]
                        )
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
            self._attr_group_members = (
                [self.player_id, *other_member_ids] if other_member_ids else []
            )
            self.update_state()
        for pid in [*(player_ids_to_add or []), *(player_ids_to_remove or [])]:
            if (member := self.mass.players.get_player(pid)) is not None:
                member.update_state()

    async def leave_group_if_dropped(self) -> bool | None:
        """
        Leave the group this room leads, if the host took the room out of the group's zone.

        Return whether this room's queue is being handed over to the rooms that play on, or
        ``None`` while it cannot be told yet whether this room was taken out.
        """
        if self._attr_playback_state != PlaybackState.PLAYING:
            return False
        if not (member_zones := self._zones_without_leader()):
            return False
        # The host shows this room taken out of the group's zone the same way as the other
        # rooms taken out of it: each part gets a zone of its own. The part that was taken
        # out stops, so only playback tells them apart.
        host = self.raumfeld.host
        generation = self._playback_generation
        own_zone = self.raumfeld.get_zone_for_room(self.room)
        members_play = [await self._zone_plays_queue(host, zone) for zone in member_zones]
        # this room may play something else by now, which does not keep the queue here
        own_plays = False if own_zone is None else await self._zone_plays_queue(host, own_zone)
        if None in members_play or own_plays is None:
            return None
        # the reads took a while: act only on the playback and layout they were taken for
        if (
            not self._reading_is_current(generation)
            or self.raumfeld.group_change_settling
            or self._zones_without_leader() != member_zones
            or self.raumfeld.get_zone_for_room(self.room) != own_zone
        ):
            return None
        playing_on = [zone for zone, plays in zip(member_zones, members_play, strict=True) if plays]
        if own_plays or len(playing_on) != 1:
            return False
        self.logger.debug("%s was taken out of the zone its group plays in", self.room)
        # the queue goes to the rooms that play on, not to ones that left the zone before
        self.set_group_members(
            [
                self.player_id,
                *(
                    pid
                    for pid in self._attr_group_members[1:]
                    if self._player_id_to_room(pid) in playing_on[0]
                ),
            ]
        )
        return self._leave_group()

    @property
    def playback_generation(self) -> int:
        """Return a count that moves on with every playback command on this room."""
        return self._playback_generation

    def playback_unchanged_since(self, generation: int) -> bool:
        """
        Return whether no playback command ran, or runs, since a generation was read.

        :param generation: The value ``playback_generation`` had then.
        """
        return self._reading_is_current(generation)

    def drop_next_item(self) -> None:
        """Drop this room's queued next item and its end watch, as a room joining a group."""
        if self._advance_armed or self._next_media is not None:
            self._clear_next()

    async def plays_queue_in(self, zone: list[str]) -> bool | None:
        """
        Return whether a zone plays this room's queue, or ``None`` while that cannot be told.

        :param zone: The rooms of the zone to read.
        """
        return await self._zone_plays_queue(self.raumfeld.host, zone)

    async def poll(self) -> None:
        """Poll the Raumfeld host for this room's current state."""
        host = self.raumfeld.host
        was_powered = self._attr_powered
        self._update_power()
        if (
            was_powered
            and self._attr_powered is False
            and self.leads_group
            and self._attr_playback_state == PlaybackState.PLAYING
        ):
            # Switched off outside MA (the Raumfeld app does that to a room it takes out of
            # a group) while leading a group that plays: the other rooms play on in the zone.
            # MA hands the queue to one of them once it sees the room off; leaving the zone
            # to them first saves taking each out of it and making them a new one.
            await self._leave_zone()
        if self._plays_spotify_connect() and self._leave_group():
            # the handover reads this room's queue as it is now; Spotify Connect is taken
            # over on the next poll
            self.update_state()
            return
        if self._held_for_watcher():
            # This room or the others were taken out of the group's zone. The zone watcher
            # tells which, and hands the queue over if it was this room; reading this room's
            # zone meanwhile would end the queue it plays.
            self.update_state()
            return
        # a grouped follower shares the zone's transport/position with its leader, so it
        # polls only its own (per-room) volume and mirrors the leader's state: without that a
        # stopped group would leave the follower reading as playing until it is ungrouped
        if self.synced_to and self.synced_to != self.player_id:
            await self._update_volume(host)
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
            # ... unless Spotify Connect plays on the room itself, outside any zone
            if self._plays_spotify_connect():
                await self._poll_room_playback(host)
                return
            await self._update_volume(host)
            self._clear_external()
            self._attr_playback_state = PlaybackState.IDLE
            self._attr_poll_interval = IDLE_POLL_INTERVAL
            self.update_state()
            return

        await self._poll_zone(host, zone)

    async def _poll_zone(self, host: hassfeld.RaumfeldHost, zone: list[str]) -> None:
        """Read this room's volume and its zone's transport and position, and apply them."""
        # read volume (per room), transport and position (per zone) concurrently so a poll
        # cycle costs one round-trip of wall time instead of three, keeping state snappy.
        # State always comes from the zone: the room's own renderer reports no TrackURI and
        # a meaningless RelTime, so it can only be asked *whether* it plays, not what.
        generation = self._playback_generation
        _, transport, position = await asyncio.gather(
            self._update_volume(host),
            self._read_transport(host, zone),
            self._read_position(host, zone),
        )
        if not self._reading_is_current(generation):
            # a play or stop command ran meanwhile; the next poll reads what it brought
            self.update_state()
            return
        if self._follows_or_held():
            # the room joined another room's group during the reads, or its group's zone
            # split: the readings are not this room's queue's to act on
            self.update_state()
            return
        if self._active_zone() != zone:
            # the room is in another zone by now, so the readings describe one it left
            self.update_state()
            return

        playing = False
        ended = False
        transitioning = False
        if transport is not None:
            raw_state = (transport.get("CurrentTransportState") or "").upper()
            transitioning = raw_state == "TRANSITIONING"
            # the zone passes through TRANSITIONING for about half a second when it starts
            # and when it stops (measured), so that says nothing about where it is heading
            if raw_state != "TRANSITIONING":
                self._attr_playback_state = _map_transport_state(raw_state)
            playing = self._attr_playback_state == PlaybackState.PLAYING
            ended = raw_state in ("STOPPED", "NO_MEDIA_PRESENT")

        # while switching items the zone briefly reports no item at all (measured), which
        # must not end the source that is playing and show MA's own queue in between
        if position is not None and not transitioning:
            await self._update_zone_source(position, zone, generation)
            if not self._reading_is_current(generation):
                # a play or stop command ran while the source was being read
                self.update_state()
                return
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
        raw_elapsed = parse_duration(pos.get("RelTime"))
        if raw_elapsed is not None and not playing and self._external_source is not None:
            # a source paused from its own app: show where it stopped
            self._attr_elapsed_time = float(raw_elapsed)
            self._attr_elapsed_time_last_updated = time.time()
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

    def _update_power(self) -> None:
        """Follow the power state the host reports, once a power command has settled."""
        if time.time() - self._power_changed_at < POWER_SETTLE_TIME:
            return
        try:
            state = self.raumfeld.host.get_room_power_state(self.room)
        except HOST_ERRORS as err:
            self.logger.debug("Failed to read power state for %s: %r", self.room, err)
            return
        # a room without a power state (Gen 1 speakers with only a physical switch) cannot
        # be switched on or off remotely, so it gets no power control
        if not state:
            self._attr_supported_features.discard(PlayerFeature.POWER)
            return
        self._attr_supported_features.add(PlayerFeature.POWER)
        self._attr_powered = state != POWER_STATE_OFF

    async def _update_volume(self, host: hassfeld.RaumfeldHost) -> None:
        """Read this room's volume and mute state, keeping the last value of one not read."""
        volume, muted = await asyncio.gather(self._read_volume(host), self._read_mute())
        if volume is not None:
            self._attr_volume_level = volume
        if muted is not None:
            self._attr_volume_muted = muted

    def _plays_spotify_connect(self) -> bool:
        """Return whether Spotify Connect is playing on this room's own renderer."""
        # Spotify Connect takes the room out of every zone, so a room in a zone does not play
        # it. hassfeld keeps a room flagged once the host reported Spotify Connect on it, also
        # after the session ended, so the flag alone would keep pulling the room out of any
        # group it joins later.
        if self._active_zone() is not None:
            return False
        try:
            return bool(self.raumfeld.host.room_is_spotify_single_room(self.room))
        except HOST_ERRORS:
            return False

    async def _poll_room_playback(self, host: hassfeld.RaumfeldHost) -> None:
        """Read what the room's own renderer plays (Spotify Connect), outside any zone."""
        location = self.raumfeld.room_renderer_location(self.room)
        control_url = self.raumfeld.room_transport_url(self.room)
        if location is None or control_url is None:
            await self._update_volume(host)
            self.update_state()
            return
        generation = self._playback_generation
        _, state, position = await asyncio.gather(
            self._update_volume(host),
            self._read_room_state(control_url),
            # not through hassfeld: its position read expects the track metadata a zone
            # returns, which a room's own renderer does not have
            self._call_transport(location, "GetPositionInfo"),
        )
        if not self._reading_is_current(generation):
            # a play or stop command ran meanwhile; the next poll reads what it brought
            self.update_state()
            return
        if state:
            self._attr_playback_state = _map_transport_state(state.get("TransportState"))
        active = self._attr_playback_state in (PlaybackState.PLAYING, PlaybackState.PAUSED)
        if state and active:
            meta = parse_didl_metadata(state.get("AVTransportURIMetaData"))
            self._set_external(
                meta,
                state.get("CurrentTransportActions"),
                parse_duration(state.get("CurrentTrackDuration")),
                via_room=True,
            )
        elif not active:
            self._clear_external()
        if position:
            self._apply_position(position, self._attr_playback_state == PlaybackState.PLAYING)
        self._update_poll_interval()
        self.update_state()

    async def _read_room_state(self, control_url: str) -> dict[str, str] | None:
        """Read the room renderer's transport state, or ``None`` if it could not be read."""
        try:
            return await query_last_change(self.mass.http_session, control_url) or None
        except HOST_ERRORS as err:
            self.logger.debug("Failed to read transport state of %s: %r", self.room, err)
            return None

    async def _update_zone_source(
        self, pos: dict[str, str], zone: list[str], generation: int
    ) -> None:
        """
        Follow what the zone plays when Music Assistant did not start it.

        :param pos: The zone's position info.
        :param zone: The rooms of the zone.
        :param generation: The playback generation the position was read in.
        """
        device_uri = pos.get("TrackURI", "") or ""
        active = self._attr_playback_state in (PlaybackState.PLAYING, PlaybackState.PAUSED)
        if self._line_in is not None and device_uri == self._line_in[0]:
            # this room's Line-In, a source of its own, also when the Raumfeld app started it
            self._clear_external()
            if active:
                self._attr_active_source = SOURCE_LINE_IN
                self.set_current_media(uri=device_uri, clear_all=True, title="Line-in")
            return
        if not device_uri or device_uri.startswith(self.mass.streams.base_url):
            # nothing loaded, or our own stream (owned by MA's queue controller, which holds
            # the queue_item_id progress tracking needs)
            self._clear_external()
            return
        # also while stopped: the zone keeps the item loaded and can start it again
        actions = await self._read_zone_actions(zone)
        # the track metadata names the playing track, not the album or playlist a button
        # started, so those are recognised by what the zone has loaded
        loaded_container = (
            await self._read_loaded_container(zone)
            if any(container for _, container in self._library_sources.values())
            else None
        )
        if not self._reading_is_current(generation):
            # a play or stop command ran while reading, which this reading predates
            return
        self._set_external(
            parse_didl_metadata(pos.get("TrackMetaData")),
            actions,
            parse_duration(pos.get("TrackDuration")),
            via_room=False,
            loaded_container=loaded_container,
        )
        source = self._external_source
        if (
            source
            and source.can_play_pause
            and self._attr_playback_state == PlaybackState.IDLE
            and not self._announcing()
        ):
            # MA only offers to resume a source that is paused
            self._attr_playback_state = PlaybackState.PAUSED

    async def _read_loaded_container(self, zone: list[str]) -> str | None:
        """Return the library container the zone has loaded, or ``None``."""
        if (location := self.raumfeld.zone_location(zone)) is None:
            return None
        response = await self._call_transport(location, "GetMediaInfo")
        return container_object_id((response or {}).get("CurrentURI"))

    async def _read_zone_actions(self, zone: list[str]) -> str | None:
        """Read the transport actions the zone allows right now, or ``None``."""
        if (location := self.raumfeld.zone_location(zone)) is None:
            return None
        response = await self._call_transport(location, "GetCurrentTransportActions")
        actions: str | None = (response or {}).get("Actions")
        return actions

    async def _call_transport(self, location: str, action_name: str) -> dict[str, str] | None:
        """
        Read from a renderer's AVTransport service, returning ``None`` if that failed.

        :param location: The device description URL of the renderer to call.
        :param action_name: The AVTransport action to call.
        """
        try:
            return await self._transport_action(location, action_name)
        except (*HOST_ERRORS, UpnpError) as err:
            self.logger.debug("%s failed on %s: %r", action_name, location, err)
            return None

    async def _transport_action(
        self, location: str, action_name: str, **arguments: str
    ) -> dict[str, str]:
        """
        Call an AVTransport action on a renderer and return its response.

        :param location: The device description URL of the renderer to call.
        :param action_name: The AVTransport action to call.
        :param arguments: The action's arguments, besides the instance id.
        """
        # not through hassfeld's own wrappers: those swallow every error, so a command
        # the renderer rejected would be reported as done
        action = await hassfeld_upnp.get_dlna_action(
            location, SERVICE_AV_TRANSPORT, action_name, session=self.mass.http_session
        )
        response: dict[str, str] = await action.async_call(InstanceID=0, **arguments)
        return response

    def _set_external(
        self,
        meta: dict[str, str | None],
        actions: str | None,
        duration: int | None,
        via_room: bool,
        loaded_container: str | None = None,
    ) -> None:
        """
        Report an external source as active, with the controls the host allows for it.

        :param meta: The parsed DIDL metadata of the playing item.
        :param actions: The comma separated transport actions the host allows, if known.
        :param duration: The duration of the playing item in seconds, if it has one.
        :param via_room: Whether the room's own renderer plays it rather than its zone.
        :param loaded_container: The library album or playlist the zone has loaded, if any.
        """
        if self._advance_armed or self._next_media is not None:
            # a queue item MA handed over must not start when this source's track ends
            self._clear_next()
        allowed = {action.strip() for action in (actions or "").split(",")}
        source_id, name = external_source(meta["section"])
        passive = True
        # a station button, pressed here or on the speaker, plays its own library item:
        # report that button rather than the service behind it
        buttons = [
            library_id
            for library_id, (object_id, container) in self._library_sources.items()
            if object_id == (loaded_container if container else meta["item_id"])
        ]
        if buttons:
            # buttons can share an item; the one chosen last stays the one shown
            source_id = (
                self._attr_active_source if self._attr_active_source in buttons else buttons[0]
            )
            name = next(s.name for s in self._static_sources if s.id == source_id)
            passive = False
        source = PlayerSource(
            id=source_id,
            name=name,
            passive=passive,
            can_play_pause=bool(allowed & {"Pause", "Play"}),
            # one capability covers both commands, so the host has to allow both
            can_next_previous={"Next", "Previous"} <= allowed,
            can_seek="Seek" in allowed,
        )
        self._external_source = source
        self._external_via_room = via_room
        self._attr_active_source = source.id
        others = [s for s in self._static_sources if s.id != source.id]
        self._attr_source_list = (
            [*others, source]
            if passive
            else [source if s.id == source.id else s for s in self._static_sources]
        )
        self._attr_supported_features -= EXTERNAL_SOURCE_FEATURES
        if source.can_play_pause:
            self._attr_supported_features.add(PlayerFeature.PAUSE)
        if source.can_next_previous:
            self._attr_supported_features.add(PlayerFeature.NEXT_PREVIOUS)
        if source.can_seek:
            self._attr_supported_features.add(PlayerFeature.SEEK)
        # the uri names the item, never the address the host streams it from
        self.set_current_media(
            uri=f"{EXTERNAL_URI_SCHEME}{meta['item_id'] or source.id}",
            clear_all=True,
            title=meta["title"],
            artist=meta["artist"],
            album=meta["album"],
            image_url=meta["image_url"],
            duration=duration or None,
        )

    def _announcing(self) -> bool:
        """Return whether Music Assistant is playing an announcement on this player."""
        return bool(self.extra_data.get(ATTR_ANNOUNCEMENT_IN_PROGRESS))

    def _reading_is_current(self, generation: int) -> bool:
        """
        Return whether a poll's readings still describe what plays.

        :param generation: The playback generation the poll started its reads in.
        """
        return generation == self._playback_generation and not self._playback_changes

    def _presented_external(self) -> PlayerSource | None:
        """Return the external source, unless Music Assistant no longer presents it."""
        source = self._external_source
        if source is None or self._attr_active_source != source.id:
            return None
        return source

    def _drop_ended_external(self) -> None:
        """Forget an external source Music Assistant gave up on (its idle timeout)."""
        # the core then clears the active source, but not what this player keeps about it,
        # which would otherwise still route play/stop to the renderer
        if self._external_source is not None and self._presented_external() is None:
            self._clear_external()

    def _leave_group(self) -> bool:
        """
        Leave the sync group, after the host took this room out of its zone.

        Return whether this room's queue is being handed over to the rooms that play on.
        """
        # Spotify Connect does that, and the room then no longer shares the group's session,
        # whether it followed or led it
        if not self.synced_to or self.synced_to == self.player_id:
            if not self._attr_group_members:
                return False
            if time.time() < self._handover_until:
                return True
            queue = self.mass.player_queues.get_active_queue(self.player_id)
            # a paused queue is handed over too, as MA does when a leader leaves its group:
            # the rooms that remain keep it, with its position, to resume later
            if (
                queue is not None
                and queue.queue_id == self.player_id
                and queue.state != PlaybackState.IDLE
            ):
                # The other rooms play on in a zone of their own, from this room's queue.
                # Hand that queue to one of them the way MA does when a leader leaves its
                # group, so they show and control what they play.
                self._handover_until = time.time() + QUEUE_HANDOVER_TIMEOUT
                self._clear_next()
                self.mass.create_task(self.mass.players.cmd_ungroup(self.player_id))
                return True
            remaining = [pid for pid in self._attr_group_members if pid != self.player_id]
            self.set_group_members([])
            # the other rooms keep sharing a zone on the host, so they stay a group here,
            # led by one of them
            if len(remaining) > 1:
                new_leader = self.mass.players.get_player(remaining[0])
                if isinstance(new_leader, RaumfeldPlayer):
                    new_leader.set_group_members(remaining)
            return False
        leader = self.mass.players.get_player(self.synced_to)
        if isinstance(leader, RaumfeldPlayer):
            remaining = [pid for pid in leader.group_members if pid != self.player_id]
            leader.set_group_members(remaining if len(remaining) > 1 else [])
        return False

    def _zones_without_leader(self) -> list[list[str]]:
        """Return the zones this leading room's members are in, other than its own."""
        if not self.leads_group:
            return []
        own_zone = self.raumfeld.get_zone_for_room(self.room) or [self.room]
        zones: list[list[str]] = []
        for pid in self._attr_group_members[1:]:
            # a member still in this room's zone does not tell where the queue plays on: a
            # group can split with some members staying, and the others playing on
            if (room := self._player_id_to_room(pid)) is None or room in own_zone:
                continue
            if (zone := self.raumfeld.get_zone_for_room(room)) is not None and zone not in zones:
                zones.append(zone)
        return zones

    async def _leave_zone(self) -> None:
        """Take this room out of its zone on the host, leaving the other rooms in it."""
        try:
            await self.raumfeld.host.async_drop_room_from_zone(self.room)
        except HOST_ERRORS as err:
            self.logger.debug("Failed to take %s out of its zone: %r", self.room, err)
            return
        # MA's handover acts on the rooms' zones right after this, so wait (bounded) until
        # the host's zone list shows the room out
        started = time.time()
        while (zone := self.raumfeld.get_zone_for_room(self.room)) and len(zone) > 1:
            if time.time() - started >= ZONE_WAIT_TIMEOUT:
                self.logger.debug("%s still listed in zone %s", self.room, zone)
                return
            await asyncio.sleep(ZONE_WAIT_INTERVAL)
        self.logger.debug("%s left its zone after %.1fs", self.room, time.time() - started)

    def _follows_or_held(self) -> bool:
        """
        Return whether this room's own next item must not start now.

        A room that joined another room's group follows that room's queue, so the next item
        of its own queue is dropped; a leader whose group left its zone waits for the zone
        watcher.
        """
        if self.synced_to and self.synced_to != self.player_id:
            self._clear_next()
            return True
        return self._held_for_watcher()

    def _held_for_watcher(self) -> bool:
        """Return whether this playing leader's group left its zone, for the watcher to sort out."""
        return self._attr_playback_state == PlaybackState.PLAYING and bool(
            self._zones_without_leader()
        )

    async def _zone_plays_queue(self, host: hassfeld.RaumfeldHost, zone: list[str]) -> bool | None:
        """Return whether the zone plays this room's queue, or ``None`` while that cannot be told."""
        if not (plays := await self._zone_plays(host, zone)):
            return plays
        if (position := await self._read_position(host, zone)) is None:
            return None
        # a queue's stream is served at <base url>/flow|single/<session>/<queue id>/...
        base_url = self.mass.streams.base_url
        uri = position.get("TrackURI") or ""
        parts = uri.removeprefix(f"{base_url}/").split("/") if uri.startswith(base_url) else []
        return len(parts) > 2 and parts[0] in ("flow", "single") and parts[2] == self.player_id

    async def _zone_plays(self, host: hassfeld.RaumfeldHost, zone: list[str]) -> bool | None:
        """Return whether the zone is playing, or ``None`` while that cannot be told."""
        if (transport := await self._read_transport(host, zone)) is None:
            return None
        state = (transport.get("CurrentTransportState") or "").upper()
        # it passes through TRANSITIONING on a start as well as on a stop (see _poll_zone)
        return None if state == "TRANSITIONING" else state == "PLAYING"

    def _clear_external(self) -> None:
        """Drop the external source, and the controls that only applied to it."""
        if self._external_source is None:
            return
        media = self._attr_current_media
        if media is not None and media.uri.startswith(EXTERNAL_URI_SCHEME):
            # it names an item the host had loaded, which MA cannot play itself
            self._attr_current_media = None
        if self._attr_active_source == self._external_source.id:
            self._attr_active_source = None
        self._external_source = None
        self._external_via_room = False
        self._attr_source_list = list(self._static_sources)
        self._attr_supported_features -= EXTERNAL_SOURCE_FEATURES

    async def _play_library_item(self, source_id: str, object_id: str, container: bool) -> None:
        """
        Have the room's zone play an item of the host's own library.

        :param source_id: The source the item is offered as.
        :param object_id: The library object (a station button's item).
        :param container: Whether the object is a container (an album, a playlist).
        """
        self._mark_play_started()
        self._clear_next()
        host = self.raumfeld.host
        uri = library_uri(host.media_server_udn, object_id, container)
        try:
            # the host wants the item's own description along with the reference
            metadata = await host.async_browse_media_server(object_id, "BrowseMetadata") or ""
            zone = await self._ensure_playable_zone()
            await self._wake_rooms(zone)
            await self._stop_before_switching(zone)
            await self._zone_command(
                zone, "SetAVTransportURI", CurrentURI=uri, CurrentURIMetaData=metadata
            )
            await self._zone_command(zone, "Play", Speed="1")
        except HOST_ERRORS as err:
            raise PlayerCommandFailed(
                f"Failed to start {object_id} on {self.room}: {err!r}"
            ) from err
        # show the chosen button right away; the next polls report it with what it plays
        name = next((x.name for x in self._static_sources if x.id == source_id), source_id)
        self._attr_active_source = source_id
        self.set_current_media(
            uri=f"{EXTERNAL_URI_SCHEME}{object_id}", clear_all=True, title=name.partition(": ")[2]
        )
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def _zone_command(self, zone: list[str], action_name: str, **arguments: str) -> None:
        """Send a transport command to the zone's renderer, raising when it is not carried out."""
        # straight to the renderer rather than through hassfeld's zone calls, which swallow
        # every error, so a command the host rejects fails here instead of passing silently
        if (location := self.raumfeld.zone_location(zone)) is None:
            raise PlayerCommandFailed(
                f"{action_name} failed on {self.room}: the host has not published its zone"
            )
        try:
            await self._transport_action(location, action_name, **arguments)
        except UpnpActionError as err:
            if action_name == "Stop" and err.error_code == TRANSITION_NOT_AVAILABLE:
                return  # already stopped
            raise PlayerCommandFailed(f"{action_name} failed on {self.room}: {err!r}") from err
        except (*HOST_ERRORS, UpnpError) as err:
            raise PlayerCommandFailed(f"{action_name} failed on {self.room}: {err!r}") from err

    async def _stop_before_switching(self, zone: list[str]) -> None:
        """Stop the zone before it is pointed at another stream, if that works."""
        # only for a clean switch: the new stream is set and started either way, and that
        # is what reports a failure
        try:
            await self._zone_command(zone, "Stop")
        except PlayerCommandFailed as err:
            self.logger.debug("Could not stop %s before switching streams: %s", self.room, err)

    async def _read_resume_context(
        self, zone: list[str]
    ) -> tuple[str, str, str, int, str | None] | None:
        """
        Read what the zone plays, to start it again later, or ``None``.

        :param zone: The rooms of the zone.
        """
        media = self._attr_current_media
        if media is None or (location := self.raumfeld.zone_location(zone)) is None:
            return None
        media_info = await self._call_transport(location, "GetMediaInfo") or {}
        position = await self._call_transport(location, "GetPositionInfo") or {}
        transport_uri = media_info.get("CurrentURI")
        if not isinstance(transport_uri, str) or not transport_uri:
            return None
        track = position.get("Track")
        rel_time = position.get("RelTime")
        return (
            media.uri,
            transport_uri,
            str(media_info.get("CurrentURIMetaData") or ""),
            int(track) if isinstance(track, (int, str)) and str(track).isdigit() else 1,
            # a live stream (radio) has no length and nothing to seek back to
            rel_time
            if isinstance(rel_time, str) and parse_duration(position.get("TrackDuration"))
            else None,
        )

    async def _restore_zone_source(self, context: tuple[str, str, str, int, str | None]) -> None:
        """
        Start a zone source again from what it played when it was stopped.

        :param context: The resume context read when the source was stopped.
        """
        _media_uri, transport_uri, metadata, track, rel_time = context
        self._resume_context = None
        self._mark_play_started()
        # a container (an album, a playlist) starts at the track it was on
        if transport_uri.startswith("dlna-playcontainer://"):
            if "fii=" in transport_uri:
                transport_uri = re.sub(r"fii=\d+", f"fii={track - 1}", transport_uri)
            else:
                transport_uri += f"&fii={track - 1}"
        try:
            zone = await self._ensure_playable_zone()
            await self._wake_rooms(zone)
            location = self.raumfeld.zone_location(zone)
            if location is None:
                raise PlayerCommandFailed(f"Failed to resume {self.room}: no zone renderer")
            await self._transport_action(
                location, "SetAVTransportURI", CurrentURI=transport_uri, CurrentURIMetaData=metadata
            )
            await self._transport_action(location, "Play", Speed="1")
            if rel_time:
                await self._transport_action(location, "Seek", Unit="ABS_TIME", Target=rel_time)
        except (*HOST_ERRORS, UpnpError) as err:
            raise PlayerCommandFailed(f"Failed to resume {self.room}: {err!r}") from err
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def _external_command(self, action_name: str, **arguments: str) -> None:
        """
        Send a transport command to whichever renderer plays the external source.

        :param action_name: The AVTransport action to call.
        :param arguments: The action's arguments, besides the instance id.
        """
        # Spotify Connect plays on the room's own renderer, everything else on its zone
        if self._external_via_room:
            location = self.raumfeld.room_renderer_location(self.room)
        elif (zone := self._active_zone()) is not None:
            location = self.raumfeld.zone_location(zone)
        else:
            location = None
        if location is None:
            raise PlayerCommandFailed(f"{action_name} failed on {self.room}: nothing is playing")
        try:
            await self._transport_action(location, action_name, **arguments)
        except (*HOST_ERRORS, UpnpError) as err:
            raise PlayerCommandFailed(f"{action_name} failed on {self.room}: {err!r}") from err
        # poll fast for a while so the new state and track show up promptly
        self._play_started_at = time.time()

    def _update_poll_interval(self) -> None:
        """Poll at the playing rate while something plays, and slowly otherwise."""
        if (time.time() - self._play_started_at) < STARTUP_POLL_WINDOW:
            self._attr_poll_interval = FAST_POLL_INTERVAL
        elif self._attr_playback_state == PlaybackState.PLAYING:
            self._attr_poll_interval = PLAYING_POLL_INTERVAL
        else:
            self._attr_poll_interval = IDLE_POLL_INTERVAL

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

    async def _read_mute(self) -> bool | None:
        """Read this room's mute state, or ``None`` if it could not be read."""
        if (location := self.raumfeld.room_renderer_location(self.room)) is None:
            return None
        try:
            # hassfeld swallows a failed read and returns None
            muted = await hassfeld_upnp.async_get_mute(self.mass.http_session, location)
        except HOST_ERRORS as err:
            self.logger.debug("Failed to read mute state for %s: %r", self.room, err)
            return None
        return bool(muted) if muted is not None else None

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
        # a leader whose group left its zone is the zone watcher's to sort out (see poll)
        if self._follows_or_held() or (zone := self._active_zone()) is None:
            return
        info = await self._read_transport(self.raumfeld.host, zone)
        if self._follows_or_held():
            return
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
        # only a playing position has moved on since its anchor; a paused one has not
        if (
            self._attr_playback_state == PlaybackState.PLAYING
            and self._attr_elapsed_time is not None
            and self._attr_elapsed_time_last_updated is not None
        ):
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

    async def _wake_rooms(self, zone: list[str], strict: bool = False) -> None:
        """
        Bring the zone's rooms out of standby and wait until they report awake.

        :param zone: The rooms to wake.
        :param strict: Fail when a room does not wake up. Without it a failure is only
            logged, which suits playback: that goes on to address the zone either way.
        """
        host = self.raumfeld.host
        woke = False
        for room in zone:
            # rooms in MANUAL_STANDBY do not auto-power-on for playback (unlike
            # AUTOMATIC_STANDBY); leaving standby is a no-op for an already-awake room
            try:
                state = host.get_room_power_state(room)
                if state is None and strict:
                    # the host does not know the room (renamed, or gone), so there is
                    # nothing to confirm it against
                    raise PlayerCommandFailed(
                        f"Failed to wake {room}: the host reports no power state for it"
                    )
                if "STANDBY" in (state or ""):
                    await host.async_leave_standby(room)
                    woke = True
            except HOST_ERRORS as err:
                if strict:
                    raise PlayerCommandFailed(f"Failed to wake {room}: {err!r}") from err
                self.logger.debug("Failed to wake room %s: %r", room, err)
        if not woke:
            return
        # a renderer that just left standby drops the first play command while it powers
        # on, so wait (bounded) for the woken rooms to report awake before playing
        deadline = time.time() + WAKE_TIMEOUT
        while time.time() < deadline:
            try:
                states = [host.get_room_power_state(room) for room in zone]
                # a room the host stops reporting counts as awake only when lenient
                if all("STANDBY" not in state if state else not strict for state in states):
                    return
            except HOST_ERRORS as err:
                self.logger.debug("Failed to read power state while waking: %r", err)
            await asyncio.sleep(WAKE_POLL_INTERVAL)
        if strict:
            raise PlayerCommandFailed(f"{', '.join(zone)} did not leave standby")

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
        # a room that is idle/standby (e.g. just left a group) is in no zone, so create a
        # single-room one. During a group handover the room is still detaching, so retry
        # while the state settles.
        for _ in range(ZONE_CREATE_ATTEMPTS):
            if self._active_zone() is None:
                await self._create_zone([self.room])
            if (
                await self._wait_for_zone([self.room], addressable=True)
                and (zone := self._active_zone()) is not None
            ):
                return zone
        raise PlayerCommandFailed(
            f"The Raumfeld host has no zone for {self.room}; check that the room is online"
        )

    async def _add_rooms_to_zone(self, rooms: list[str]) -> None:
        """Add the given rooms to this leader's zone, waiting until they have joined."""
        host = self.raumfeld.host
        wanted = [self.room, *rooms]
        # hassfeld's add-room call does not wait, so during rapid regrouping (a room moving
        # straight from one group to another) it can race and leave the room unassigned;
        # verify the room really landed in the zone and retry while the state settles
        for _ in range(ZONE_CREATE_ATTEMPTS):
            if (zone := self._active_zone()) is None:
                # no zone yet: have the host make it with all rooms at once, rather than
                # one for this room alone that the others then have to be moved into
                await self._create_zone(wanted)
                continue
            missing = [room for room in rooms if room not in zone]
            if not missing:
                return
            for room in missing:
                await host.async_add_room_to_zone(room, zone)
            await self._wait_for_zone(wanted, addressable=False)
        zone = self._active_zone()
        if missing := [room for room in rooms if zone is None or room not in zone]:
            raise PlayerCommandFailed(
                f"The Raumfeld host did not add {', '.join(missing)} to the zone of {self.room}"
            )

    async def _create_zone(self, rooms: list[str]) -> None:
        """
        Have the host put the rooms in a zone of their own, and wait until it lists it.

        :param rooms: The rooms of the new zone.
        """
        host = self.raumfeld.host
        # Not hassfeld's create-and-wait: that looks the new zone up by room name where it
        # needs room UDNs, never finds it and so always waits out its full five seconds.
        await hassfeld_ws.async_connect_rooms_to_zone(
            self.mass.http_session, host.location, room_udns=host.roomlst_to_udnlst(rooms)
        )
        await self._wait_for_zone(rooms, addressable=False)

    async def _wait_for_zone(self, rooms: list[str], addressable: bool) -> bool:
        """
        Wait (bounded) until the host lists a zone holding the rooms.

        :param rooms: The rooms that have to be in this room's zone.
        :param addressable: Whether to wait for the zone's renderer to be published as
            well, which playback needs and a group change does not.
        """
        started = time.time()
        while True:
            zone = self._active_zone()
            if (
                zone is not None
                and all(room in zone for room in rooms)
                and (not addressable or self.raumfeld.zone_location(zone) is not None)
            ):
                self.logger.debug(
                    "Zone %s ready after %.1fs (addressable: %s)",
                    zone,
                    time.time() - started,
                    addressable,
                )
                return True
            if time.time() - started >= ZONE_WAIT_TIMEOUT:
                self.logger.debug("Zone for %s not ready after %.1fs", rooms, ZONE_WAIT_TIMEOUT)
                return False
            await asyncio.sleep(ZONE_WAIT_INTERVAL)

    def _player_id_to_room(self, player_id: str) -> str | None:
        """Resolve a MA player_id of this provider back to its Raumfeld room name."""
        player = self.mass.players.get_player(player_id)
        return player.room if isinstance(player, RaumfeldPlayer) else None
