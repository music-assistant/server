"""
Teufel Raumfeld Player Provider.

Raumfeld exposes a central *RaumfeldHost* webservice that manages the system's
*rooms* and dynamic *zones* (groups of rooms). We model:

* each Raumfeld **room** as a stable Music Assistant :class:`RaumfeldPlayer`
* each Raumfeld **zone** as a Music Assistant sync-group (leader + members)

All device communication goes through the ``hassfeld`` library, which wraps the
Raumfeld host's UPnP/OpenHome services and keeps an in-memory model of the
current rooms/zones that we read from and act on.

A background supervisor owns the host connection: it never hard-fails on a
(temporarily) unreachable host, and it stops the update loop when the host drops
so hassfeld's long-polling can't flood the log while the host is offline.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast
from urllib.parse import urlparse

import hassfeld
from music_assistant_models.enums import IdentifierType

from music_assistant.constants import ATTR_ENABLED, CONF_IP_ADDRESS, CONF_PORT
from music_assistant.models.player_provider import PlayerProvider

from .constants import (
    DEFAULT_PORT,
    DLNA_DOMAIN,
    HOST_ERRORS,
    INITIAL_UPDATE_TIMEOUT,
    RECONNECT_INTERVAL,
)
from .helpers import room_to_player_id
from .player import RaumfeldPlayer

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry


class RaumfeldPlayerProvider(PlayerProvider):
    """Player provider for Teufel Raumfeld multiroom devices."""

    host: hassfeld.RaumfeldHost
    _host_address: str
    _host_port: int
    _supervisor_task: asyncio.Task[None] | None = None
    _update_task: asyncio.Task[None] | None = None
    _connected: bool = False

    @property
    def host_address(self) -> str:
        """Return the configured Raumfeld host IP address."""
        return self._host_address

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider (setup input is in setup_flow)."""
        return ()

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._host_address = str(self.get_setup_value(CONF_IP_ADDRESS) or "")
        self._host_port = cast("int", self.get_setup_value(CONF_PORT) or DEFAULT_PORT)
        # a background supervisor owns the connection so a missing or (temporarily)
        # unreachable host never leaves the provider permanently unavailable
        self._supervisor_task = self.mass.create_task(self._supervise())

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        for task in (self._supervisor_task, self._update_task):
            if task and not task.done():
                task.cancel()
        self._supervisor_task = None
        self._update_task = None

    def get_zone_for_room(self, room: str) -> list[str] | None:
        """
        Return the sorted room list of the zone ``room`` belongs to, or ``None`` if idle.

        :param room: The Raumfeld room name to look up.
        """
        for zone_rooms in self.host.get_zones():
            if room in zone_rooms:
                return list(zone_rooms)
        return None

    def resolve_room_renderer(self, room: str) -> tuple[str | None, str | None]:
        """
        Return the ``(renderer UUID, renderer IP)`` for a room, or ``(None, None)``.

        :param room: The Raumfeld room name to look up.
        """
        try:
            resolve = self.host.resolve
            room_udn = resolve["room_to_udn"].get(room)
            rend_udn = resolve["roomudn_to_rendudn"].get(room_udn) if room_udn else None
            devloc = resolve["udn_to_devloc"].get(rend_udn) if rend_udn else None
        except KeyError, AttributeError:
            return None, None
        uuid = rend_udn.removeprefix("uuid:") if rend_udn else None
        ip_address = urlparse(devloc).hostname if devloc else None
        return uuid, ip_address

    async def _supervise(self) -> None:
        """Supervise the host connection: connect when reachable, pause when it drops."""
        while True:
            try:
                if not self._connected:
                    await self._try_connect()
                elif not await self.host.async_host_is_valid():
                    # stop the update loop so hassfeld's long-polling cannot flood the log
                    await self._disconnect("host became unreachable")
                else:
                    # host still healthy: re-sync so a room that (re)appeared or vanished
                    # (e.g. a speaker returning from deep standby) is registered / marked
                    # available again without needing a full reconnect
                    await self._resync()
            except asyncio.CancelledError:
                raise
            except HOST_ERRORS as err:
                self.logger.debug("Raumfeld supervisor error: %r", err)
            await asyncio.sleep(RECONNECT_INTERVAL)

    async def _try_connect(self) -> None:
        """Try once to connect to the host and register its rooms."""
        if not self._host_address:
            self.logger.debug("No Raumfeld host configured yet; will retry")
            return
        # a fresh host object avoids stale state left by a previous failed attempt
        self.host = hassfeld.RaumfeldHost(
            self._host_address, self._host_port, session=self.mass.http_session
        )
        if not await self.host.async_host_is_valid():
            self.logger.debug("Raumfeld host %s not reachable yet; will retry", self._host_address)
            return
        self._update_task = self.mass.create_task(
            self.host.async_update_all(self.mass.http_session)
        )
        try:
            async with asyncio.timeout(INITIAL_UPDATE_TIMEOUT):
                await self.host.async_wait_initial_update()
        except TimeoutError:
            self.logger.warning(
                "Timed out waiting for initial data from Raumfeld host %s; will retry",
                self._host_address,
            )
            await self._disconnect("initial update timed out")
            return
        await self._resync()
        # restore any existing Raumfeld zones into MA's group state ONCE on connect (e.g. a
        # group that survived a restart). We deliberately do NOT re-mirror zones on the
        # periodic resync: that fights MA's own live group state and can build a circular
        # leader/member relationship when the Raumfeld zone coordinator differs from the
        # leader MA picked, which then recurses infinitely while streaming.
        self._sync_groups()
        self._connected = True
        self.logger.info("Connected to Raumfeld host %s", self._host_address)

    async def _disconnect(self, reason: str) -> None:
        """Stop the update loop and mark players unavailable until the host returns."""
        if self._connected:
            self.logger.warning("Raumfeld host %s %s - pausing", self._host_address, reason)
        self._connected = False
        if self._update_task and not self._update_task.done():
            self._update_task.cancel()
        self._update_task = None
        for player in self.players:
            if isinstance(player, RaumfeldPlayer):
                player.set_available(False)

    async def _resync(self) -> None:
        """Periodic re-sync: register (re)appeared rooms and suppress shadow renderers."""
        await self._sync_rooms()
        await self._suppress_host_shadow_renderers()

    async def _suppress_host_shadow_renderers(self) -> None:
        """Disable DLNA players that merely shadow this host's own virtual renderers."""
        host_ip = self._host_address
        if not host_ip:
            return
        # The physical room renderers are real speakers we keep. Everything else the host
        # exposes on its own IP is a virtual room/zone renderer that duplicates a native
        # player; the host reassigns those UUIDs over time, so match on the stable trait
        # (a DLNA renderer on the host IP that is not a physical room renderer) instead.
        physical = {
            uuid.lower()
            for room in self.host.get_rooms()
            if (uuid := self.resolve_room_renderer(room)[0])
        }
        for player in self.mass.players.iter_players(
            return_disabled=False, return_protocol_players=True
        ):
            if player.provider.domain != DLNA_DOMAIN:
                continue
            identifiers = player.device_info.identifiers
            uuid = (identifiers.get(IdentifierType.UUID) or "").lower()
            if (
                identifiers.get(IdentifierType.IP_ADDRESS) == host_ip
                and uuid
                and uuid not in physical
            ):
                self.logger.info(
                    "Disabling redundant Raumfeld host DLNA renderer '%s' (%s)",
                    player.display_name,
                    player.player_id,
                )
                await self.mass.config.save_player_config(player.player_id, {ATTR_ENABLED: False})

    async def _sync_rooms(self) -> None:
        """Register (or re-activate) a Music Assistant player for every Raumfeld room."""
        # ``get_rooms`` returns the list of room names currently known to the host.
        rooms = self.host.get_rooms()
        for room in rooms:
            player_id = room_to_player_id(room)
            if (existing := self.mass.players.get_player(player_id)) is not None:
                # already registered (e.g. after a reconnect) - just mark it available
                if isinstance(existing, RaumfeldPlayer):
                    existing.set_available(True)
                continue
            player = RaumfeldPlayer(provider=self, player_id=player_id, room=room)
            await self.mass.players.register(player)
            self.logger.debug("Registered Raumfeld room '%s' as player %s", room, player_id)
        # a room the host no longer lists (e.g. a speaker that dropped to deep standby or
        # left the network) is gone until it returns; show it as unavailable meanwhile
        present = set(rooms)
        for known in self.players:
            if isinstance(known, RaumfeldPlayer) and known.room not in present:
                known.set_available(False)

    def _sync_groups(self) -> None:
        """Mirror the current Raumfeld zones into the players' sync-group state."""
        grouped: set[str] = set()
        for zone_rooms in self.host.get_zones():
            # order the rooms with the zone coordinator first so the group leader matches
            # the room the group was originally created from (get_zones sorts the rooms)
            member_ids = [
                pid
                for room in self._zone_rooms_leader_first(zone_rooms)
                if self.mass.players.get_player(pid := room_to_player_id(room)) is not None
            ]
            if len(member_ids) < 2:
                continue
            leader = self.mass.players.get_player(member_ids[0])
            if isinstance(leader, RaumfeldPlayer):
                leader.set_group_members(member_ids)
            grouped.update(member_ids)
        # any player no longer part of a multi-room zone must be marked solo
        for player in self.players:
            if isinstance(player, RaumfeldPlayer) and player.player_id not in grouped:
                player.set_group_members([])

    def _zone_rooms_leader_first(self, zone_rooms: list[str]) -> list[str]:
        """Return the zone's rooms ordered with the coordinator (leader) first."""
        try:
            resolve = self.host.resolve
            zone_udn = self.host.roomlst_to_zoneudn(zone_rooms)
            udn_order = resolve["zoneudn_to_roomudnlst"].get(zone_udn) or []
            udn_to_room = {udn: room for room, udn in resolve["room_to_udn"].items()}
        except KeyError, AttributeError:
            return list(zone_rooms)
        ordered = [udn_to_room[udn] for udn in udn_order if udn in udn_to_room]
        # keep any room the coordinator list didn't cover
        ordered += [room for room in zone_rooms if room not in ordered]
        return ordered or list(zone_rooms)
