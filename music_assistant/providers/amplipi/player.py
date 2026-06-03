"""AmpliPi zone player for Music Assistant."""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.player import DeviceInfo, PlayerMedia
from pyamplipi.models import MultiZoneUpdate, PlayMedia, ZoneUpdate

from music_assistant.models.player import Player

from .constants import FREE_SOURCE_INPUTS, SOURCE_DISCONNECTED, ZONE_OFF

if TYPE_CHECKING:
    from pyamplipi.models import Source, Status

    from .provider import AmpliPiPlayerProvider


PLAYER_FEATURES = {
    PlayerFeature.PLAY_MEDIA,
    PlayerFeature.PAUSE,
    PlayerFeature.VOLUME_SET,
    PlayerFeature.VOLUME_MUTE,
    PlayerFeature.POWER,
    PlayerFeature.SET_MEMBERS,
}

STATE_MAP = {
    "playing": PlaybackState.PLAYING,
    "paused": PlaybackState.PAUSED,
    "stopped": PlaybackState.IDLE,
}


class AmpliPiZonePlayer(Player):
    """Representation of a single AmpliPi zone as a Music Assistant player."""

    def __init__(self, provider: AmpliPiPlayerProvider, zone_id: int) -> None:
        """Initialize the AmpliPi zone player."""
        super().__init__(provider, f"{provider.instance_id}_zone_{zone_id}")
        self._zone_id = zone_id
        self._source_id: int | None = None
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
        await self._prov.api.set_zone(self._zone_id, ZoneUpdate(vol_f=volume_level / 100))
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
            await self._prov.api.set_zone(self._zone_id, ZoneUpdate(source_id=SOURCE_DISCONNECTED))
            self._attr_powered = True
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
        if (stream_id := await self._active_stream_id()) is not None:
            await self._prov.api.play_stream(stream_id)
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def pause(self) -> None:
        """Handle PAUSE command on the player."""
        if (stream_id := await self._active_stream_id()) is not None:
            await self._prov.api.pause_stream(stream_id)
        self._attr_playback_state = PlaybackState.PAUSED
        self.update_state()

    async def stop(self) -> None:
        """Handle STOP command on the player."""
        self.mark_stop_called()
        if (stream_id := await self._active_stream_id()) is not None:
            await self._prov.api.stop_stream(stream_id)
        self._attr_playback_state = PlaybackState.IDLE
        self.update_state()

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA command on the player."""
        url = await self.mass.streams.resolve_stream_url(self.player_id, media)
        source = await self._acquire_source()
        if source is None or source.id is None:
            raise PlayerCommandFailed("All AmpliPi sources are currently in use.")
        self._source_id = source.id
        # connect this zone (and any grouped members) to the acquired source
        await self._prov.api.set_zones(
            MultiZoneUpdate(zones=self._member_zone_ids(), update=ZoneUpdate(source_id=source.id))
        )
        await self._prov.api.play_media(PlayMedia(source_id=source.id, media=url))
        self._attr_active_source = self.player_id
        self._attr_current_media = media
        self._attr_powered = True
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
                await self._prov.api.set_zone(self._zone_id, ZoneUpdate(source_id=self._source_id))
            add_zone_ids = self._zone_ids_for(player_ids_to_add)
            await self._prov.api.set_zones(
                MultiZoneUpdate(zones=add_zone_ids, update=ZoneUpdate(source_id=self._source_id))
            )
            members = self._attr_group_members or [self.player_id]
            for player_id in player_ids_to_add:
                if player_id not in members:
                    members.append(player_id)
                affected.add(player_id)
            self._attr_group_members = members
        if player_ids_to_remove:
            remove_zone_ids = self._zone_ids_for(player_ids_to_remove)
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
        self._attr_volume_level = round((zone.vol_f or 0) * 100)
        self._attr_volume_muted = zone.mute
        self._attr_powered = zone.source_id != ZONE_OFF
        self._source_id = zone.source_id if zone.source_id >= 0 else None
        if self._source_id is None:
            self._attr_playback_state = PlaybackState.IDLE
            self._attr_active_source = None
        else:
            source = next((s for s in status.sources if s.id == self._source_id), None)
            self._attr_playback_state = self._map_state(source)
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
        with suppress(Exception):
            source = await self._prov.api.get_source(self._source_id)
            if source.input and source.input.startswith("stream="):
                with suppress(ValueError):
                    return int(source.input.split("=", 1)[1])
        return None

    @staticmethod
    def _map_state(source: Source | None) -> PlaybackState:
        """Map an AmpliPi source's playback state to a Music Assistant PlaybackState."""
        if source is None or source.info is None or source.info.state is None:
            return PlaybackState.IDLE
        return STATE_MAP.get(source.info.state, PlaybackState.IDLE)
