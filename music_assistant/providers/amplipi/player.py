"""AmpliPi zone player for Music Assistant."""

from __future__ import annotations

import time
from contextlib import suppress
from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.player import DeviceInfo, PlayerMedia, PlayerSource
from pyamplipi.models import MultiZoneUpdate, SourceUpdate, ZoneUpdate

from music_assistant.models.player import Player

from .constants import (
    AMPLIPI_API_ERRORS,
    FREE_SOURCE_INPUTS,
    INPUT_STREAM_TYPES,
    SOURCE_DISCONNECTED,
    SOURCE_ID_STREAM_PREFIX,
    STREAM_TYPE_LABELS,
    VOLUME_DB_FLOOR,
    ZONE_OFF,
)

if TYPE_CHECKING:
    from pyamplipi.models import Source, Status, Zone
    from pyamplipi.models import Stream as AmpliPiStream

    from .provider import AmpliPiPlayerProvider


# NOTE: AmpliPi has no native pause for a stream (the backend keeps reading the source URL),
# so PAUSE is emulated by stopping the stream. Music Assistant captures the queue position
# before pausing and, on resume, re-issues play_media; in flow mode that re-resolves the
# stream URL at the resume offset, so playback continues from where it was paused. This
# relies on the player self-clocking its position (AmpliPi reports none) - see play_media.
PLAYER_FEATURES = {
    PlayerFeature.PLAY_MEDIA,
    PlayerFeature.PAUSE,
    PlayerFeature.VOLUME_SET,
    PlayerFeature.VOLUME_MUTE,
    PlayerFeature.POWER,
    PlayerFeature.SET_MEMBERS,
    PlayerFeature.SELECT_SOURCE,
}


class AmpliPiZonePlayer(Player):
    """Representation of a single AmpliPi zone as a Music Assistant player."""

    def __init__(self, provider: AmpliPiPlayerProvider, zone_id: int) -> None:
        """Initialize the AmpliPi zone player."""
        super().__init__(provider, f"{provider.instance_id}_zone_{zone_id}")
        self._zone_id = zone_id
        self._source_id: int | None = None
        # default the player name to the zone's AmpliPi name (e.g. "Living Room"); this is
        # only a default - a name set on the player in Music Assistant always takes priority
        zone = next((z for z in provider.status.zones if z.id == zone_id), None)
        self._attr_name = self._zone_display_name(zone, zone_id)
        self._attr_type = PlayerType.PLAYER
        self._attr_supported_features = PLAYER_FEATURES
        # all zones on the same AmpliPi controller can be grouped with each other
        self._attr_can_group_with = {provider.instance_id}
        self._attr_device_info = DeviceInfo(manufacturer="MicroNova", model="AmpliPi Zone")

    @property
    def zone_id(self) -> int:
        """Return the AmpliPi zone id of this player."""
        return self._zone_id

    @property
    def requires_flow_mode(self) -> bool:
        """Return if the player requires flow mode (AmpliPi plays a single stream URL)."""
        return True

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command on the player."""
        await self._prov.api.set_zone(
            self._zone_id, ZoneUpdate(vol=self._volume_to_db(volume_level))
        )
        self._attr_volume_level = volume_level
        self.update_state()

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME MUTE command on the player."""
        await self._prov.api.set_zone(self._zone_id, ZoneUpdate(mute=muted))
        self._attr_volume_muted = muted
        self.update_state()

    async def power(self, powered: bool) -> None:
        """Handle POWER command on the player."""
        if powered:
            # AmpliPi mutes a zone while it is disconnected (source -1); unmute it so
            # that MA's mute state stays consistent and playback is audible.
            await self._prov.api.set_zone(
                self._zone_id, ZoneUpdate(source_id=SOURCE_DISCONNECTED, mute=False)
            )
            self._attr_powered = True
            self._attr_volume_muted = False
        else:
            # turn off this zone and any zones grouped to it
            await self._prov.api.set_zones(
                MultiZoneUpdate(
                    zones=self._member_zone_ids(), update=ZoneUpdate(source_id=ZONE_OFF)
                )
            )
            self._attr_powered = False
            self._attr_playback_state = PlaybackState.IDLE
            self._attr_active_source = None
            self._source_id = None
            self._dissolve_group()
        self.update_state()

    async def play(self) -> None:
        """Handle PLAY command on the player."""
        # AmpliPi has no native unpause: pause() stopped the stream, so a play/unpause of a
        # Music Assistant queue must re-resolve playback from the saved resume position
        # rather than replay the (stale) stream, which would restart from the stream's start.
        queue = self.mass.player_queues.get(self.player_id)
        if self._attr_playback_state == PlaybackState.PAUSED and queue is not None and queue.active:
            await self.mass.player_queues.resume(self.player_id)
            return
        if (stream_id := await self._active_stream_id()) is not None:
            await self._prov.api.play_stream(stream_id)
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def stop(self) -> None:
        """Handle STOP command on the player."""
        self.mark_stop_called()
        if (stream_id := await self._active_stream_id()) is not None:
            await self._prov.api.stop_stream(stream_id)
        self._attr_playback_state = PlaybackState.IDLE
        self.update_state()

    async def pause(self) -> None:
        """Handle PAUSE command on the player."""
        # AmpliPi cannot truly pause a stream, so we stop it and report PAUSED; Music
        # Assistant captures the resume position before this runs and, on play, resumes
        # from it (see the PLAYER_FEATURES note and the play/resume delegation above).
        if (stream_id := await self._active_stream_id()) is not None:
            await self._prov.api.stop_stream(stream_id)
        self._attr_playback_state = PlaybackState.PAUSED
        self.update_state()

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA command on the player."""
        url = await self.mass.streams.resolve_stream_url(self.player_id, media)
        source = await self._acquire_source()
        if source is None or source.id is None:
            raise PlayerCommandFailed("All AmpliPi sources are currently in use.")
        self._source_id = source.id
        # Use AmpliPi's internetradio stream type (not the announcement fileplayer):
        # it is built for continuous HTTP streams and supports reliable play/stop,
        # where the fileplayer does not. (Pause is emulated via stop - see the
        # PLAYER_FEATURES note above.)
        stream_id = await self._prov.ensure_stream(source.id, url)
        zone_ids = self._member_zone_ids()
        self.logger.debug(
            "play_media: stream %s on source %s -> zones %s (%s)",
            stream_id,
            source.id,
            zone_ids,
            url,
        )
        # point the source at our stream, then make its zones EXACTLY this group
        # (attach our members unmuted; detach any stray zones left on the source),
        # so playback follows the Music Assistant group and nothing else
        await self._prov.api.set_source(
            source.id, SourceUpdate(input=f"{SOURCE_ID_STREAM_PREFIX}{stream_id}")
        )
        await self._attach_only_zones(source.id, zone_ids)
        await self._prov.api.play_stream(stream_id)
        self._attr_active_source = self.player_id
        self._attr_current_media = media
        self._attr_powered = True
        self._attr_volume_muted = False
        self._attr_playback_state = PlaybackState.PLAYING
        # AmpliPi reports no playback position, so we self-clock: start at 0 for this (flow)
        # stream and let corrected_elapsed_time advance with wall time while PLAYING. Music
        # Assistant maps this cumulative stream-time back to the queue position - without it
        # the queue's elapsed time stays pinned at the stream start, so pause/seek-resume
        # would always jump back to the start of the stream.
        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time.time()
        self.update_state()

    async def select_source(self, source: str) -> None:
        """
        Handle SELECT_SOURCE command on the player.

        Routes an AmpliPi-side source (a native stream such as its own Spotify Connect /
        AirPlay, or a physical RCA/line input) to this zone and any grouped members. The
        source id encodes the AmpliPi stream id as "stream=<id>", which doubles as the value
        written to the AmpliPi source input.

        :param source: The source id to select, as defined in source_list.
        """
        # Setting source.input="stream=<id>" both selects and starts the source: native
        # streams (e.g. internetradio) auto-play on connect, and RCA/line inputs are
        # passthrough, so no explicit play_stream is needed.
        if not source.startswith(SOURCE_ID_STREAM_PREFIX):
            raise PlayerCommandFailed(f"Unknown source '{source}' for {self.display_name}")
        amplipi_source = await self._acquire_source()
        if amplipi_source is None or amplipi_source.id is None:
            raise PlayerCommandFailed("All AmpliPi sources are currently in use.")
        self._source_id = amplipi_source.id
        zone_ids = self._member_zone_ids()
        # the source id ("stream=<id>") is exactly the AmpliPi source.input value
        await self._prov.api.set_source(amplipi_source.id, SourceUpdate(input=source))
        await self._attach_only_zones(amplipi_source.id, zone_ids)
        self._attr_active_source = source
        self._attr_current_media = None
        self._attr_powered = True
        self._attr_volume_muted = False
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command on the player."""
        affected: set[str] = set()
        if player_ids_to_add:
            # the group leader needs a source to share with its members
            if self._source_id is None:
                source = await self._acquire_source()
                if source is None or source.id is None:
                    raise PlayerCommandFailed("All AmpliPi sources are currently in use.")
                self._source_id = source.id
                await self._prov.api.set_zone(
                    self._zone_id, ZoneUpdate(source_id=self._source_id, mute=False)
                )
            # connect and UNMUTE the added zones: AmpliPi keeps disconnected zones muted,
            # so without mute=False a newly grouped zone would join silently
            add_zone_ids = self._zone_ids_for(player_ids_to_add)
            if add_zone_ids:
                await self._prov.api.set_zones(
                    MultiZoneUpdate(
                        zones=add_zone_ids,
                        update=ZoneUpdate(source_id=self._source_id, mute=False),
                    )
                )
            members = self._attr_group_members or [self.player_id]
            for player_id in player_ids_to_add:
                if player_id not in members:
                    members.append(player_id)
                affected.add(player_id)
            self._attr_group_members = members
        if player_ids_to_remove:
            remove_zone_ids = self._zone_ids_for(player_ids_to_remove)
            if remove_zone_ids:
                await self._prov.api.set_zones(
                    MultiZoneUpdate(
                        zones=remove_zone_ids, update=ZoneUpdate(source_id=SOURCE_DISCONNECTED)
                    )
                )
            members = [m for m in self._attr_group_members if m not in player_ids_to_remove]
            self._attr_group_members = [] if members == [self.player_id] else members
            affected.update(player_ids_to_remove)
        self.update_state()
        # refresh the state of the affected member players so their sync state updates
        for player_id in affected:
            self.mass.players.trigger_player_update(player_id)

    def set_unavailable(self) -> None:
        """Mark the player as (temporarily) unavailable."""
        if not self._attr_available:
            return
        self._attr_available = False
        self.update_state()

    def update_from_status(self, status: Status) -> None:
        """Update the player state from a polled AmpliPi status object."""
        zone = next((z for z in status.zones if z.id == self._zone_id), None)
        if zone is None:
            self.set_unavailable()
            return
        self._attr_available = True
        # keep the default name in sync with the zone's (possibly renamed) AmpliPi name
        self._attr_name = self._zone_display_name(zone, self._zone_id)
        self._build_source_list()
        self._attr_volume_level = self._db_to_volume(zone.vol)
        self._attr_volume_muted = zone.mute
        self._attr_powered = zone.source_id != ZONE_OFF
        self._source_id = zone.source_id if zone.source_id >= 0 else None
        if self._source_id is None:
            # zone is disconnected or powered off: nothing is playing on it
            self._attr_playback_state = PlaybackState.IDLE
            self._attr_active_source = None
        else:
            # connected to a source: default to MA playback (active_source == player_id),
            # then, if a user-selectable AmpliPi source (native stream / RCA input) is the
            # connected input, reflect that instead. Resetting first avoids leaving a stale
            # external source selected after the input changes back to our own MA stream.
            self._attr_active_source = self.player_id
            self._reflect_external_active_source(status)
        # NOTE: while the zone is connected to a source we deliberately keep the
        # playback_state set by our play/stop commands and do NOT derive it from the
        # AmpliPi source state. The internetradio stream play_media uses reports an
        # unreliable info.state (e.g. "stopped" while audio is actually playing), which
        # would otherwise continuously desync the player state in the UI.
        if self._attr_group_members:
            self._prune_group_members(status)
        self.update_state()

    # private helpers

    @property
    def _prov(self) -> AmpliPiPlayerProvider:
        """Return the (typed) AmpliPi provider for this player."""
        return cast("AmpliPiPlayerProvider", self.provider)

    def _zone_ids_for(self, player_ids: list[str]) -> list[int]:
        """Return the AmpliPi zone ids for the given Music Assistant player_ids."""
        zone_ids: list[int] = []
        for player_id in player_ids:
            if (zone_id := self._prov.zone_id_for(player_id)) is not None:
                zone_ids.append(zone_id)
        return zone_ids

    def _member_zone_ids(self) -> list[int]:
        """Return this zone's id plus the zone ids of any grouped members."""
        zone_ids = [self._zone_id]
        for player_id in self._attr_group_members:
            if player_id == self.player_id:
                continue
            if (zone_id := self._prov.zone_id_for(player_id)) is not None:
                zone_ids.append(zone_id)
        return zone_ids

    async def _attach_only_zones(self, source_id: int, zone_ids: list[int]) -> None:
        """
        Attach exactly the given zones to the source, detaching any others.

        Connects the requested zones to the source (unmuted) and disconnects any stray
        zones still bound to it (e.g. left over from a previous group), so the source's
        zones match the Music Assistant group exactly.

        :param source_id: The AmpliPi source id to attach the zones to.
        :param zone_ids: The zone ids that should be playing on the source.
        """
        wanted = set(zone_ids)
        strays = [
            z.id
            for z in self._prov.status.zones
            if z.id is not None
            and not z.disabled
            and z.source_id == source_id
            and z.id not in wanted
        ]
        if strays:
            await self._prov.api.set_zones(
                MultiZoneUpdate(zones=strays, update=ZoneUpdate(source_id=SOURCE_DISCONNECTED))
            )
        await self._prov.api.set_zones(
            MultiZoneUpdate(zones=zone_ids, update=ZoneUpdate(source_id=source_id, mute=False))
        )

    def _dissolve_group(self) -> None:
        """Clear this player's group and refresh any former members."""
        former_members = [m for m in self._attr_group_members if m != self.player_id]
        self._attr_group_members = []
        for player_id in former_members:
            self.mass.players.trigger_player_update(player_id)

    def _prune_group_members(self, status: Status) -> None:
        """Drop group members that are no longer connected to this zone's source."""
        if self._source_id is None:
            self._dissolve_group()
            return
        valid = [self.player_id]
        for player_id in self._attr_group_members:
            if player_id == self.player_id:
                continue
            zone_id = self._prov.zone_id_for(player_id)
            zone = next((z for z in status.zones if z.id == zone_id), None)
            if zone is not None and zone.source_id == self._source_id:
                valid.append(player_id)
        self._attr_group_members = [] if valid == [self.player_id] else valid

    async def _acquire_source(self) -> Source | None:
        """
        Acquire an AmpliPi source for this zone to play on.

        Reuses the currently connected source if any, otherwise claims a free source.
        Returns None if all sources are in use (AmpliPi has 4 sources for up to 6+ zones).
        """
        status = self._prov.status
        if self._source_id is not None:
            if source := next((s for s in status.sources if s.id == self._source_id), None):
                return source
        used_source_ids = {z.source_id for z in status.zones if not z.disabled and z.source_id >= 0}
        for source in status.sources:
            if source.id is None or source.id in used_source_ids:
                continue
            if source.input in FREE_SOURCE_INPUTS:
                return source
        # fall back to any source not currently bound to a zone
        for source in status.sources:
            if source.id is not None and source.id not in used_source_ids:
                return source
        return None

    async def _active_stream_id(self) -> int | None:
        """Return the id of the stream currently connected to this zone's source, if any."""
        if self._source_id is None:
            return None
        with suppress(*AMPLIPI_API_ERRORS):
            source = await self._prov.api.get_source(self._source_id)
            if source.input and source.input.startswith(SOURCE_ID_STREAM_PREFIX):
                with suppress(ValueError):
                    return int(source.input.removeprefix(SOURCE_ID_STREAM_PREFIX))
        return None

    def _build_source_list(self) -> None:
        """
        Rebuild the selectable source list from the AmpliPi streams.

        Exposes the AmpliPi-side sources the user can route to this zone: physical line
        inputs (RCA/aux) and native streams (its own Spotify Connect, AirPlay, internet
        radio, ...). These are routing-only in Music Assistant - the wired input or the
        owning app drives the audio - so no transport capabilities are advertised.
        """
        self._attr_source_list = [
            PlayerSource(
                id=f"{SOURCE_ID_STREAM_PREFIX}{stream.id}",
                name=self._source_label(stream),
                passive=False,
            )
            for stream in self._prov.selectable_streams()
        ]

    def _reflect_external_active_source(self, status: Status) -> None:
        """Set active_source to a user-selectable AmpliPi source if one is connected."""
        source = next((s for s in status.sources if s.id == self._source_id), None)
        if source is None or not (source.input or "").startswith(SOURCE_ID_STREAM_PREFIX):
            return
        if any(source.input == src.id for src in self._attr_source_list):
            self._attr_active_source = source.input

    @staticmethod
    def _zone_display_name(zone: Zone | None, zone_id: int) -> str:
        """
        Return a sensible default player name for a zone.

        Prefers the zone's configured AmpliPi name; when that is unset (a common case,
        as users often leave zones unnamed) it falls back to a generic, 1-based label.
        This is only the default - a name set on the player in Music Assistant wins.

        :param zone: The AmpliPi zone, or None if it is not present in the status.
        :param zone_id: The AmpliPi zone id, used for the fallback label.
        """
        name = (getattr(zone, "name", None) or "").strip()
        return name or f"AmpliPi Zone {zone_id + 1}"

    @staticmethod
    def _source_label(stream: AmpliPiStream) -> str:
        """
        Return a friendly, disambiguated label for a selectable AmpliPi stream.

        Physical inputs keep their configured name ("Input 1", "Aux"); native streams get a
        type suffix so same-named endpoints (e.g. Spotify vs AirPlay "AmpliPro 1") are distinct.
        """
        if stream.type in INPUT_STREAM_TYPES:
            return str(stream.name)
        return f"{stream.name} ({STREAM_TYPE_LABELS.get(stream.type, stream.type)})"

    @staticmethod
    def _volume_to_db(volume_level: int) -> int:
        """Map a Music Assistant volume (0-100) to an AmpliPi volume in dB."""
        return round(VOLUME_DB_FLOOR * (1 - volume_level / 100))

    @staticmethod
    def _db_to_volume(vol_db: int) -> int:
        """Map an AmpliPi volume in dB back to a Music Assistant volume (0-100)."""
        volume = round(100 * (1 - vol_db / VOLUME_DB_FLOOR))
        return max(0, min(100, volume))
