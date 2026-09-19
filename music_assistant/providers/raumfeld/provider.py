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

from music_assistant.constants import CONF_IP_ADDRESS, CONF_PORT
from music_assistant.models.player_provider import PlayerProvider

from .constants import (
    DEFAULT_PORT,
    HOST_ERRORS,
    INITIAL_UPDATE_TIMEOUT,
    LINE_IN_OBJECT_ID,
    RECONNECT_INTERVAL,
)
from .helpers import parse_line_in, room_udn_to_player_id
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
    # renderer UUID -> (Line-In stream url, title) for rooms that expose an analog input
    _line_in: dict[str, tuple[str, str]]

    @property
    def host_address(self) -> str:
        """Return the configured Raumfeld host IP address."""
        return self._host_address

    def line_in(self, renderer_uuid: str | None) -> tuple[str, str] | None:
        """Return the ``(stream url, title)`` of a room's Line-In input, or ``None``."""
        if not renderer_uuid:
            return None
        return self._line_in.get(renderer_uuid.lower())

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider (setup input is in setup_flow)."""
        return ()

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._host_address = str(self.get_setup_value(CONF_IP_ADDRESS) or "")
        self._host_port = cast("int", self.get_setup_value(CONF_PORT) or DEFAULT_PORT)
        self._line_in = {}
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
                elif (
                    self._update_task is None
                    or self._update_task.done()
                    or not await self.host.async_host_is_valid()
                ):
                    # the host is gone, or hassfeld's update loop exited (it swallows a
                    # disconnect and returns, freezing state) - drop so the next cycle
                    # reconnects and recreates the update task
                    await self._disconnect("host connection lost")
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
        # load Line-In inputs before registering players so each room can expose its own
        await self._load_line_in()
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
        """Periodic re-sync: register rooms that (re)appeared and mark vanished ones gone."""
        await self._sync_rooms()

    async def _load_line_in(self) -> None:
        """Fetch the host's Line-In inputs, keyed by renderer UUID."""
        try:
            didl = await self.host.async_browse_media_server(
                LINE_IN_OBJECT_ID, "BrowseDirectChildren"
            )
        except HOST_ERRORS as err:
            self.logger.debug("Failed to browse Raumfeld Line-In inputs: %r", err)
            return
        self._line_in = parse_line_in(didl)

    async def _sync_rooms(self) -> None:
        """Register (or re-activate) a Music Assistant player for every Raumfeld room."""
        # ``get_rooms`` returns the list of room names currently known to the host.
        present: set[str] = set()
        for room in self.host.get_rooms():
            if (player_id := self._room_player_id(room)) is None:
                continue
            present.add(player_id)
            if (existing := self.mass.players.get_player(player_id)) is not None:
                # already registered (e.g. after a reconnect): mark it available and, since
                # the player_id is the immutable room UDN, follow a room rename by refreshing
                # the stored room name (used for host calls) and the display name
                if isinstance(existing, RaumfeldPlayer):
                    existing.set_room(room)
                    existing.set_available(True)
                continue
            player = RaumfeldPlayer(provider=self, player_id=player_id, room=room)
            await self.mass.players.register(player)
            self.logger.debug("Registered Raumfeld room '%s' as player %s", room, player_id)
        # a room whose UDN the host no longer lists (e.g. a speaker that dropped to deep
        # standby or left the network) is gone until it returns; show it as unavailable.
        # Match on the stable player_id, never the room name, so a rename does not orphan it.
        for known in self.players:
            if isinstance(known, RaumfeldPlayer) and known.player_id not in present:
                known.set_available(False)

    def _sync_groups(self) -> None:
        """Mirror the current Raumfeld zones into the players' sync-group state."""
        leaders: set[str] = set()
        for zone_rooms in self.host.get_zones():
            # order the rooms with the zone coordinator first so the group leader matches
            # the room the group was originally created from (get_zones sorts the rooms)
            member_ids = [
                pid
                for room in self._zone_rooms_leader_first(zone_rooms)
                if (pid := self._room_player_id(room))
                and self.mass.players.get_player(pid) is not None
            ]
            if len(member_ids) < 2:
                continue
            leader = self.mass.players.get_player(member_ids[0])
            if isinstance(leader, RaumfeldPlayer):
                leader.set_group_members(member_ids)
                leaders.add(member_ids[0])
        # only a current leader may carry a member list; clearing every other player (both
        # solo rooms and followers) avoids a former leader that became a follower keeping a
        # stale list and forming a circular group with the new leader
        for player in self.players:
            if isinstance(player, RaumfeldPlayer) and player.player_id not in leaders:
                player.set_group_members([])

    def _room_player_id(self, room: str) -> str | None:
        """Return the stable player_id for a room (from its immutable UDN), or ``None``."""
        try:
            room_udn = self.host.resolve["room_to_udn"].get(room)
        except KeyError, AttributeError:
            return None
        return room_udn_to_player_id(room_udn) if room_udn else None

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
