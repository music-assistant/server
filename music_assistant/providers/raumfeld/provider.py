"""
Teufel Raumfeld Player Provider.

Raumfeld exposes a central *RaumfeldHost* webservice that manages the system's
*rooms* and dynamic *zones* (groups of rooms). We model:

* each Raumfeld **room** as a stable Music Assistant :class:`RaumfeldPlayer`
* each Raumfeld **zone** as a Music Assistant sync-group (leader + members)

All device communication goes through the ``hassfeld`` library, which wraps the
Raumfeld host's UPnP/OpenHome services and keeps an in-memory model of the
current rooms/zones that we read from and act on.
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
)
from .helpers import room_to_player_id
from .player import RaumfeldPlayer

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry


class RaumfeldPlayerProvider(PlayerProvider):
    """Player provider for Teufel Raumfeld multiroom devices."""

    host: hassfeld.RaumfeldHost
    _update_task: asyncio.Task[None] | None = None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider (setup input is in setup_flow)."""
        return ()

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        host = cast("str", self.get_setup_value(CONF_HOST))
        port = cast("int", self.get_setup_value(CONF_PORT) or DEFAULT_PORT)

        self.host = hassfeld.RaumfeldHost(host, port, session=self.mass.http_session)

        if not await self.host.async_host_is_valid():
            raise RuntimeError(f"'{host}:{port}' is not a valid Raumfeld host")

        # async_update_all runs the hassfeld background update loops for the whole
        # lifetime of the provider; keep the handle so we can cancel it on unload.
        self._update_task = self.mass.create_task(
            self.host.async_update_all(self.mass.http_session)
        )
        try:
            async with asyncio.timeout(INITIAL_UPDATE_TIMEOUT):
                await self.host.async_wait_initial_update()
        except TimeoutError as err:
            raise RuntimeError(
                f"Timed out waiting for initial data from Raumfeld host '{host}'"
            ) from err

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await self._sync_rooms()

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        if self._update_task and not self._update_task.done():
            self._update_task.cancel()
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

    async def _sync_rooms(self) -> None:
        """Register a Music Assistant player for every Raumfeld room."""
        # ``get_rooms`` returns the list of room names known to the host.
        for room in self.host.get_rooms():
            player_id = room_to_player_id(room)
            if self.mass.players.get_player(player_id) is not None:
                continue
            player = RaumfeldPlayer(provider=self, player_id=player_id, room=room)
            await self.mass.players.register(player)
            self.logger.debug("Registered Raumfeld room '%s' as player %s", room, player_id)
