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

from music_assistant.models.player_provider import PlayerProvider

from .constants import (
    CONF_HOST,
    CONF_PORT,
    DEFAULT_PORT,
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

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider (setup input is in setup_flow)."""
        return ()

    async def handle_async_init(self) -> None:
        """
        Handle async initialization of the provider.

        Deliberately does not connect (or fail) here: a background supervisor connects
        once the host is reachable and reconnects if it later drops, so a sleeping
        speaker or a brief network blip never leaves the provider permanently
        unavailable.
        """
        self._host_address = cast("str", self.get_setup_value(CONF_HOST))
        self._host_port = cast("int", self.get_setup_value(CONF_PORT) or DEFAULT_PORT)
        self.host = hassfeld.RaumfeldHost(
            self._host_address, self._host_port, session=self.mass.http_session
        )
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
        Return the current Raumfeld zone (sorted room list) that ``room`` is part of.

        ``get_zones`` returns a list of sorted room-lists, one per active zone. A room
        that is not part of any active zone (idle/standby) returns ``None``.

        :param room: The Raumfeld room name to look up.
        """
        for zone_rooms in self.host.get_zones():
            if room in zone_rooms:
                return list(zone_rooms)
        return None

    def resolve_room_renderer(self, room: str) -> tuple[str | None, str | None]:
        """
        Return the ``(renderer UUID, renderer IP)`` for a room's media renderer.

        These let Music Assistant link this native player to the same device's other
        protocol representations (DLNA/Chromecast/Sendspin) instead of listing them as
        duplicates. The UUID is the renderer UDN without the ``uuid:`` prefix; either
        value may be ``None`` if the host has not resolved it.

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
        """
        Keep the connection to the Raumfeld host healthy, without ever hard-failing.

        Retries until the host is reachable, then connects and registers the room
        players. If the host later becomes unreachable, the (tight-looping) update task
        is stopped - so hassfeld cannot flood the log - and the supervisor waits for the
        host to return before reconnecting.
        """
        while True:
            try:
                if not self._connected:
                    await self._try_connect()
                elif not await self.host.async_host_is_valid():
                    await self._disconnect("host became unreachable")
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self.logger.debug("Raumfeld supervisor error: %r", err)
            await asyncio.sleep(RECONNECT_INTERVAL)

    async def _try_connect(self) -> None:
        """Attempt one connect: validate the host, start the update loop, register rooms."""
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
        await self._sync_rooms()
        self._connected = True
        self.logger.info("Connected to Raumfeld host %s", self._host_address)

    async def _disconnect(self, reason: str) -> None:
        """
        Stop the update loop and mark players unavailable until the host returns.

        :param reason: Short human-readable reason, logged when a live connection drops.
        """
        if self._connected:
            self.logger.warning("Raumfeld host %s %s - pausing", self._host_address, reason)
        self._connected = False
        if self._update_task and not self._update_task.done():
            self._update_task.cancel()
        self._update_task = None
        for player in self.players:
            if isinstance(player, RaumfeldPlayer):
                player.set_available(False)

    async def _sync_rooms(self) -> None:
        """Register (or re-activate) a Music Assistant player for every Raumfeld room."""
        # ``get_rooms`` returns the list of room names known to the host.
        for room in self.host.get_rooms():
            player_id = room_to_player_id(room)
            if (existing := self.mass.players.get_player(player_id)) is not None:
                # already registered (e.g. after a reconnect) - just mark it available
                if isinstance(existing, RaumfeldPlayer):
                    existing.set_available(True)
                continue
            player = RaumfeldPlayer(provider=self, player_id=player_id, room=room)
            await self.mass.players.register(player)
            self.logger.debug("Registered Raumfeld room '%s' as player %s", room, player_id)
