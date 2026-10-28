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
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, cast
from urllib.parse import urljoin, urlparse

import aiohttp
import hassfeld
from async_upnp_client.exceptions import UpnpError
from hassfeld import upnp as hassfeld_upnp
from hassfeld.constants import SERVICE_CONTENT_DIRECTORY

from music_assistant.constants import (
    CONF_ENABLE_ICY_METADATA,
    CONF_IP_ADDRESS,
    CONF_PORT,
)
from music_assistant.models.player_provider import PlayerProvider

from .constants import (
    BUTTON_ITEM_CACHE_TTL,
    CACHE_CATEGORY_BUTTON_ITEMS,
    DEFAULT_PORT,
    DEVICE_INFO_TIMEOUT,
    GROUP_SETTLE_TIME,
    HOST_CHECK_FAILURES,
    HOST_ERRORS,
    INITIAL_UPDATE_TIMEOUT,
    LIBRARY_REFRESH_INTERVAL,
    LINE_IN_OBJECT_ID,
    RECONNECT_INTERVAL,
    STATION_BUTTONS,
    UNLOAD_TIMEOUT,
    ZONE_WATCH_INTERVAL,
)
from .helpers import (
    host_is_valid,
    parse_control_url,
    parse_library_items,
    parse_line_in,
    parse_model_name,
    parse_serial_number,
    room_udn_to_player_id,
    serial_to_player_id,
)
from .player import RaumfeldPlayer

if TYPE_CHECKING:
    from collections.abc import Iterator

    from music_assistant_models.config_entries import ConfigEntry


class RaumfeldPlayerProvider(PlayerProvider):
    """Player provider for Teufel Raumfeld multiroom devices."""

    host: hassfeld.RaumfeldHost
    _host_address: str
    _host_port: int
    _supervisor_task: asyncio.Task[None] | None = None
    _update_task: asyncio.Task[None] | None = None
    _zone_watch_task: asyncio.Task[None] | None = None
    # the host's zones as seen on the previous look, when they differed from Music
    # Assistant's groups, since when it could not be told whether a leader was taken out
    # of them, how many group changes MA is making right now, and when the last one ended
    _pending_zones: frozenset[frozenset[str]] | None = None
    _undecided_since: float | None = None
    _group_changes: int = 0
    _group_changed_at: float = 0.0
    _connected: bool = False
    # health checks the host has missed in a row while connected
    _host_check_failures: int = 0
    # renderer UUID -> (Line-In stream url, title) for rooms that expose an analog input
    _line_in: dict[str, tuple[str, str]]
    # when the station buttons were last read
    _library_read_at: float = 0.0
    # room name -> renderer hardware serial, the basis for the player_id. Read from the
    # device once and kept, so a resync does not refetch a description per room.
    _room_serials: dict[str, str]
    # room name -> renderer model name, read from the same device description
    _room_models: dict[str, str]
    # room name -> AVTransport control URL of its renderer as the description gives it,
    # usually relative to the renderer's own address
    _room_transport_paths: dict[str, str]
    # room name -> player_id, derived from _room_serials on every sync (see _assign_room_ids)
    _room_ids: dict[str, str]
    # rooms already warned about sharing a serial, so the warning goes out once per room
    _shared_serial_warned: set[str]

    def line_in(self, renderer_uuid: str | None) -> tuple[str, str] | None:
        """Return the ``(stream url, title)`` of a room's Line-In input, or ``None``."""
        if not renderer_uuid:
            return None
        return self._line_in.get(renderer_uuid.lower())

    def room_model(self, room: str) -> str | None:
        """
        Return the model name of a room's renderer, or ``None`` if it is unknown.

        :param room: The Raumfeld room name to look up.
        """
        return self._room_models.get(room)

    def room_transport_url(self, room: str) -> str | None:
        """
        Return the AVTransport control URL of a room's own renderer, or ``None``.

        :param room: The Raumfeld room name to look up.
        """
        # joined to the renderer's current address: it gets a new port when it restarts,
        # while the serial and this path are read only once
        path = self._room_transport_paths.get(room)
        location = self.room_renderer_location(room)
        if path is None or location is None:
            return None
        return urljoin(location, path)

    def zone_location(self, zone: list[str]) -> str | None:
        """
        Return the device description URL of a zone's renderer, or ``None``.

        :param zone: The rooms of the zone.
        """
        try:
            zone_udn = self.host.roomlst_to_zoneudn(zone)
            location: str | None = self.host.resolve["udn_to_devloc"].get(zone_udn)
        except KeyError, AttributeError:
            return None
        return location

    @property
    def group_change_settling(self) -> bool:
        """Return whether a group change Music Assistant made is still running or settling."""
        return bool(self._group_changes) or time.time() - self._group_changed_at < GROUP_SETTLE_TIME

    @contextmanager
    def changing_group(self) -> Iterator[None]:
        """Keep the host's zones from being mirrored while Music Assistant changes a group."""
        self._group_changes += 1
        try:
            yield
        finally:
            self._group_changes -= 1
            # the settle time counts from the end of the change, a failed one included
            self._group_changed_at = time.time()

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider (setup input is in setup_flow)."""
        return ()

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._host_address = str(self.get_setup_value(CONF_IP_ADDRESS) or "")
        self._host_port = cast("int", self.get_setup_value(CONF_PORT) or DEFAULT_PORT)
        self._line_in = {}
        self._room_serials = {}
        self._room_models = {}
        self._room_transport_paths = {}
        self._room_ids = {}
        self._shared_serial_warned = set()
        # a background supervisor owns the connection so a missing or (temporarily)
        # unreachable host never leaves the provider permanently unavailable
        self._supervisor_task = self.mass.create_task(self._supervise())

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        tasks = [
            task
            for task in (self._supervisor_task, self._update_task, self._zone_watch_task)
            if task and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            # let them finish, so hassfeld's long-poll has ended before the provider is gone
            await asyncio.wait(tasks, timeout=UNLOAD_TIMEOUT)
        self._supervisor_task = None
        self._update_task = None
        self._zone_watch_task = None

    def get_zone_for_room(self, room: str) -> list[str] | None:
        """
        Return the sorted room list of the zone ``room`` belongs to, or ``None`` if idle.

        :param room: The Raumfeld room name to look up.
        """
        for zone_rooms in self.host.get_zones():
            if room in zone_rooms:
                return list(zone_rooms)
        return None

    def room_renderer_location(self, room: str) -> str | None:
        """
        Return the device description URL of a room's own (physical) media renderer.

        :param room: The Raumfeld room name to look up.
        """
        try:
            resolve = self.host.resolve
            room_udn = resolve["room_to_udn"].get(room)
            rend_udn = resolve["roomudn_to_rendudn"].get(room_udn) if room_udn else None
            return resolve["udn_to_devloc"].get(rend_udn) if rend_udn else None
        except KeyError, AttributeError:
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

    async def refresh_station_buttons(self, player: RaumfeldPlayer) -> None:
        """
        Read a room's station buttons again, as they can be reassigned at any time.

        A room whose buttons cannot be read keeps the ones it has.

        :param player: The player of the room.
        """
        if (buttons := await self._read_station_buttons(player.room)) is not None:
            player.set_station_buttons(buttons)

    async def _supervise(self) -> None:
        """Supervise the host connection: connect when reachable, pause when it drops."""
        while True:
            try:
                if not self._connected:
                    await self._try_connect()
                elif self._update_task is None or self._update_task.done():
                    # hassfeld's update loop exited (it swallows a disconnect and returns,
                    # freezing state) - drop so the next cycle reconnects and recreates it
                    await self._disconnect("host connection lost")
                elif not await host_is_valid(
                    self.mass.http_session, self._host_address, self._host_port
                ):
                    # one missed answer is a slow host; only a run of them is a lost one
                    self._host_check_failures += 1
                    if self._host_check_failures >= HOST_CHECK_FAILURES:
                        await self._disconnect("host connection lost")
                else:
                    self._host_check_failures = 0
                    # host still healthy: re-sync so a room that (re)appeared or vanished
                    # (e.g. a speaker returning from deep standby) is registered / marked
                    # available again without needing a full reconnect
                    await self._resync()
            except asyncio.CancelledError:
                raise
            except HOST_ERRORS as err:
                self.logger.debug("Raumfeld supervisor error: %r", err)
            except Exception:
                # anything else (say a hassfeld change that raises something new) must not
                # end the supervisor, or the provider would never reconnect this session
                self.logger.exception("Unexpected error in the Raumfeld connection supervisor")
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
        if not await host_is_valid(self.mass.http_session, self._host_address, self._host_port):
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
        self._library_read_at = 0.0
        await self.mass.cache.delete(
            None, category=CACHE_CATEGORY_BUTTON_ITEMS, provider=self.instance_id
        )
        await self._resync()
        # restore the existing Raumfeld zones into MA's group state (e.g. a group that
        # survived a restart), then keep following changes made outside Music Assistant
        self._sync_groups()
        self._pending_zones = None
        self._zone_watch_task = self.mass.create_task(self._watch_zones())
        self._connected = True
        self.logger.info("Connected to Raumfeld host %s", self._host_address)

    async def _disconnect(self, reason: str) -> None:
        """Stop the update loop and mark players unavailable until the host returns."""
        if self._connected:
            self.logger.warning("Raumfeld host %s %s - pausing", self._host_address, reason)
        self._connected = False
        self._host_check_failures = 0
        for task in (self._update_task, self._zone_watch_task):
            if task and not task.done():
                task.cancel()
        self._update_task = None
        self._zone_watch_task = None
        for player in self.players:
            if isinstance(player, RaumfeldPlayer):
                player.set_available(False)

    async def _resync(self) -> None:
        """Periodic re-sync: register rooms that (re)appeared and mark vanished ones gone."""
        await self._sync_rooms()
        if time.time() - self._library_read_at >= LIBRARY_REFRESH_INTERVAL:
            self._library_read_at = time.time()
            await self._load_library_sources()

    async def _load_library_sources(self) -> None:
        """Read each room's station buttons, and offer them as sources."""
        for player in self.players:
            if isinstance(player, RaumfeldPlayer):
                await self.refresh_station_buttons(player)

    async def _read_station_buttons(self, room: str) -> list[tuple[int, str, str, bool]] | None:
        """
        Return the station buttons of a room's speaker: ``(button, object id, title, container)``.

        Return ``None`` when the buttons could not be read.

        :param room: The Raumfeld room name.
        """
        renderer_uuid, _ = self.resolve_room_renderer(room)
        if renderer_uuid is None:
            return None
        try:
            location = self.host.resolve["udn_to_devloc"][self.host.media_server_udn]
            action = await hassfeld_upnp.get_dlna_action(
                location,
                SERVICE_CONTENT_DIRECTORY,
                "GetStationButtonAssignment",
                session=self.mass.http_session,
            )
            assigned = [
                (button, response.get("ObjectID") or "")
                for button in STATION_BUTTONS
                if (
                    response := await action.async_call(
                        Renderer=f"uuid:{renderer_uuid}", Button=button
                    )
                )
            ]
        except (*HOST_ERRORS, UpnpError) as err:
            self.logger.debug("Failed to read the station buttons of %s: %r", room, err)
            return None
        buttons = []
        for button, object_id in assigned:
            if not object_id:
                continue
            if (item := await self._describe_button_item(object_id)) is None:
                # whether it is an album or a single item decides how it plays, so a button
                # whose item could not be read waits for a later read
                continue
            buttons.append((button, object_id, *item))
        return buttons

    async def _describe_button_item(self, object_id: str) -> tuple[str, bool] | None:
        """
        Return the title of a button's item and whether it is a container, or ``None``.

        :param object_id: The library object assigned to the button.
        """
        # a reassigned button gets a new object id, so a cached id needs no new read
        cached = await self.mass.cache.get(
            object_id, provider=self.instance_id, category=CACHE_CATEGORY_BUTTON_ITEMS
        )
        if cached is not None:
            title, container = cached
            return str(title), bool(container)
        items = parse_library_items(await self._browse(object_id, children=False))
        if not items:
            return None
        await self.mass.cache.set(
            object_id,
            [items[0][1], items[0][2]],
            expiration=BUTTON_ITEM_CACHE_TTL,
            provider=self.instance_id,
            category=CACHE_CATEGORY_BUTTON_ITEMS,
        )
        return items[0][1], items[0][2]

    async def _browse(self, object_id: str, children: bool) -> str | None:
        """
        Browse the host's media server, returning its DIDL-Lite, or ``None``.

        :param object_id: The library object to browse.
        :param children: Whether to list its children rather than describe it.
        """
        flag = "BrowseDirectChildren" if children else "BrowseMetadata"
        try:
            didl: str | None = await self.host.async_browse_media_server(object_id, flag)
        except HOST_ERRORS as err:
            self.logger.debug("Failed to browse %s on the Raumfeld host: %r", object_id, err)
            return None
        return didl

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
        rooms = self.host.get_rooms()
        # the id comes from the renderer's hardware serial, which has to be read from the
        # device once; a room whose serial we cannot read yet is skipped for now rather than
        # registered under an id that would have to change once the serial comes in
        for room in rooms:
            await self._ensure_room_serial(room)
        self._assign_room_ids()
        present: set[str] = set()
        for room in rooms:
            if (player_id := self._room_player_id(room)) is None:
                continue
            present.add(player_id)
            if (existing := self.mass.players.get_player(player_id)) is not None:
                # already registered (e.g. after a reconnect): mark it available and, since
                # the player_id is tied to the hardware, follow a room rename by refreshing
                # the stored room name (used for host calls) and the display name
                if isinstance(existing, RaumfeldPlayer):
                    existing.set_room(room)
                    existing.set_available(True)
                self._ensure_icy_default(player_id)
                continue
            player = RaumfeldPlayer(provider=self, player_id=player_id, room=room)
            await self.mass.players.register(player)
            # the config exists now (created in Player.__init__)
            self._ensure_icy_default(player_id)
            self.logger.debug("Registered Raumfeld room '%s' as player %s", room, player_id)
        # a room whose UDN the host no longer lists (e.g. a speaker that dropped to deep
        # standby or left the network) is gone until it returns; show it as unavailable.
        # Match on the stable player_id, never the room name, so a rename does not orphan it.
        for known in self.players:
            if isinstance(known, RaumfeldPlayer) and known.player_id not in present:
                known.set_available(False)

    async def _watch_zones(self) -> None:
        """Follow zone changes made outside Music Assistant (e.g. in the Raumfeld app)."""
        while True:
            await asyncio.sleep(ZONE_WATCH_INTERVAL)
            try:
                await self._follow_zones()
            except asyncio.CancelledError:
                raise
            except HOST_ERRORS as err:
                self.logger.debug("Failed to compare Raumfeld zones: %r", err)
            except Exception:
                # one bad look must not end the watcher, or zone changes made in the
                # Raumfeld app would no longer be followed until the host reconnects
                self.logger.exception("Unexpected error while following the Raumfeld zones")

    async def _follow_zones(self) -> None:
        """Mirror the host's zones once they differ from MA's groups and have settled."""
        zones = self._grouped_zones()
        # compare with the groups MA shows right now, not with what was mirrored last: a
        # group made in MA and undone in the Raumfeld app brings the host back to a layout
        # that was mirrored before, while MA still shows the group
        if zones == self._shown_groups():
            self._pending_zones = None
            self._undecided_since = None
            return
        # Regrouping passes through states in between, on the host as well as while Music
        # Assistant changes a group itself. Mirroring one of those is what once left two
        # players leading each other, so only a layout that held for two looks in a row is
        # taken over, and none at all while a change MA made is still settling.
        if zones != self._pending_zones:
            self._pending_zones = zones
            self._undecided_since = None
            return
        if self.group_change_settling:
            return
        # A leader taken out of the zone its group plays in hands its queue over to the
        # rooms that play on, as MA does when a leader leaves its group; mirroring the zones
        # first would leave those rooms playing a queue that no player shows.
        undecided = False
        for player in list(self.players):
            if not isinstance(player, RaumfeldPlayer):
                continue
            handing_over = await player.leave_group_if_dropped()
            if handing_over:
                self._pending_zones = None
                self._undecided_since = None
                return
            undecided = undecided or handing_over is None
        owners = await self._queue_owners()
        if undecided or owners is None:
            # a zone did not answer, or the layout or playback moved on meanwhile: look
            # again, until mirroring the zones as they are is all that is left
            if self._undecided_since is None:
                self._undecided_since = time.time()
            if time.time() - self._undecided_since < GROUP_SETTLE_TIME:
                return
        # the reads took a while: mirror only the layout they were taken for
        if self.group_change_settling or self._grouped_zones() != zones:
            return
        self.logger.debug(
            "Raumfeld zones differ from the groups shown, following them: %s -> %s",
            sorted(sorted(group) for group in self._shown_groups()),
            sorted(sorted(zone) for zone in zones),
        )
        self._pending_zones = None
        self._undecided_since = None
        self._sync_groups(owners or {})

    def _grouped_zones(self) -> frozenset[frozenset[str]]:
        """Return the host's zones that hold more than one room with a player."""
        zones = (
            frozenset(room for room in zone_rooms if self._room_player(room) is not None)
            for zone_rooms in self.host.get_zones()
        )
        return frozenset(zone for zone in zones if len(zone) > 1)

    def _shown_groups(self) -> frozenset[frozenset[str]]:
        """Return the sync groups Music Assistant shows right now, as sets of rooms."""
        groups = set()
        for player in self.players:
            if not isinstance(player, RaumfeldPlayer) or not player.leads_group:
                continue
            members = (self.mass.players.get_player(pid) for pid in player.group_members)
            groups.add(
                frozenset(member.room for member in members if isinstance(member, RaumfeldPlayer))
            )
        return frozenset(groups)

    def _room_player(self, room: str) -> RaumfeldPlayer | None:
        """Return the registered player of a room, or ``None``."""
        if (player_id := self._room_player_id(room)) is None:
            return None
        player = self.mass.players.get_player(player_id)
        return player if isinstance(player, RaumfeldPlayer) else None

    async def _queue_owners(self) -> dict[frozenset[str], str] | None:
        """
        Return the room whose queue a zone plays, for each zone more than one room claims.

        Return ``None`` while that cannot be told: a zone did not answer, or playback changed
        on one of those rooms during the reads.
        """
        # two groups that both play a queue MA started, merged in the Raumfeld app, play on
        # with only one of those queues: the zone's stream tells which
        owners: dict[frozenset[str], str] = {}
        generations: dict[RaumfeldPlayer, int] = {}
        for zone_rooms in self.host.get_zones():
            claimants = self._queue_claimants(
                [player for room in zone_rooms if (player := self._room_player(room)) is not None]
            )
            if len(claimants) < 2:
                continue
            generations.update((claimant, claimant.playback_generation) for claimant in claimants)
            answers = []
            for claimant in claimants:
                answers.append(plays := await claimant.plays_queue_in(list(zone_rooms)))
                if plays:
                    owners[frozenset(zone_rooms)] = claimant.player_id
                    break
            else:
                if None in answers:
                    return None
        if not all(
            player.playback_unchanged_since(generation)
            for player, generation in generations.items()
        ):
            return None
        return owners

    def _sync_groups(self, owners: dict[frozenset[str], str] | None = None) -> None:
        """
        Mirror the current Raumfeld zones into the players' sync-group state.

        :param owners: For zones more than one room claims, the room whose queue plays there.
        """
        groups: dict[str, list[str]] = {}
        for zone_rooms in self.host.get_zones():
            # coordinator first: the room the zone was created from (get_zones sorts them)
            member_ids = [
                member.player_id
                for room in self._zone_rooms_leader_first(zone_rooms)
                if (member := self._room_player(room)) is not None
            ]
            if len(member_ids) < 2:
                continue
            owner = (owners or {}).get(frozenset(zone_rooms))
            leader_id = owner if owner in member_ids else self._pick_leader(member_ids)
            groups[leader_id] = [leader_id, *(pid for pid in member_ids if pid != leader_id)]
        # Every player gets its list in one pass, leaders and all others (emptied) alike: a
        # former leader that became a follower must not keep a stale list, or it would form
        # a circular group with the new leader. A follower plays its leader's queue, so the
        # next item of its own queue goes, before an end watch could start it later on.
        followers = {member_id for members in groups.values() for member_id in members[1:]}
        for player in self.players:
            if isinstance(player, RaumfeldPlayer):
                player.set_group_members(groups.get(player.player_id, []))
                if player.player_id in followers:
                    player.drop_next_item()

    def _pick_leader(self, member_ids: list[str]) -> str:
        """
        Return which member leads a zone's group in Music Assistant.

        :param member_ids: The player ids of the zone's rooms, the zone coordinator first.
        """
        # The member playing a queue Music Assistant started leads, and failing that one that
        # already leads a group here: the host's coordinator is often another room than the
        # one MA grouped or played from, and making that other room the leader would leave
        # the queue's own player showing nothing. A follower is passed over: it mirrors its
        # leader's playing state, and still names its own queue as its source until it is
        # stopped. Only a zone with neither (made and played from the Raumfeld app) takes
        # the host's coordinator.
        members = [
            member
            for member_id in member_ids
            if isinstance(member := self.mass.players.get_player(member_id), RaumfeldPlayer)
        ]
        if claimants := self._queue_claimants(members):
            return claimants[0].player_id
        for member in members:
            if member.leads_group:
                return member.player_id
        return member_ids[0]

    def _queue_claimants(self, members: list[RaumfeldPlayer]) -> list[RaumfeldPlayer]:
        """Return the members that play a queue Music Assistant started, followers left out."""
        following = {
            member_id
            for player in self.players
            if isinstance(player, RaumfeldPlayer) and player.leads_group
            for member_id in player.group_members[1:]
        }
        return [
            member
            for member in members
            if member.plays_own_queue and member.player_id not in following
        ]

    def _room_player_id(self, room: str) -> str | None:
        """Return the player_id for a room, or ``None`` if its serial is not known yet."""
        return self._room_ids.get(room)

    def _assign_room_ids(self) -> None:
        """Derive each room's player_id from its serial, falling back to its room UDN."""
        # The host runs virtual renderers that inherit its own serial, so two rooms can end
        # up reporting the same one. Only one of them can use it as its id; the other falls
        # back to its room UDN so it still shows up. Taking the rooms in a fixed order (by
        # room UDN) makes the same room keep the serial on every start, so ids - and the
        # player config stored under them - do not swap between restarts.
        ids: dict[str, str] = {}
        claimed: set[str] = set()
        for room in sorted(self._room_serials, key=self._room_sort_key):
            serial = self._room_serials[room]
            if serial not in claimed:
                claimed.add(serial)
                ids[room] = serial_to_player_id(serial)
                continue
            room_udn = self._room_udn(room)
            if room_udn is None:
                continue
            ids[room] = room_udn_to_player_id(room_udn)
            if room not in self._shared_serial_warned:
                self._shared_serial_warned.add(room)
                self.logger.warning(
                    "Room %s reports the same serial (%s) as another room; using its room "
                    "UDN for its player id instead, which a rebuild of the Raumfeld system "
                    "would change",
                    room,
                    serial,
                )
        self._room_ids = ids

    def _room_udn(self, room: str) -> str | None:
        """Return the room UDN the host reports for a room, or ``None``."""
        try:
            udn: str | None = self.host.resolve["room_to_udn"].get(room)
        except KeyError, AttributeError:
            return None
        return udn

    def _room_sort_key(self, room: str) -> str:
        """Return a stable sort key for a room: its UDN, or its name if that is unknown."""
        return self._room_udn(room) or room

    async def _ensure_room_serial(self, room: str) -> str | None:
        """Read (and remember) the hardware serial of a room's renderer."""
        if (cached := self._room_serials.get(room)) is not None:
            return cached
        location = self.room_renderer_location(room)
        if location is None:
            return None
        timeout = aiohttp.ClientTimeout(total=DEVICE_INFO_TIMEOUT)
        try:
            async with self.mass.http_session.get(location, timeout=timeout) as resp:
                description = await resp.text()
        except HOST_ERRORS as err:
            self.logger.debug("Failed to read device description of %s: %r", room, err)
            return None
        if (serial := parse_serial_number(description)) is None:
            self.logger.debug("Renderer of room %s reports no serial number", room)
            return None
        self._room_serials[room] = serial
        if (model := parse_model_name(description)) is not None:
            self._room_models[room] = model
        if (control_url := parse_control_url(description, "AVTransport")) is not None:
            self._room_transport_paths[room] = control_url
        return serial

    def _ensure_icy_default(self, player_id: str) -> None:
        """
        Store the ICY-metadata default for a player that has none stored yet.

        :param player_id: The player whose stored config to check.
        """
        # Flow mode and the chunked profile come from the player and its config entries.
        # The ICY preference, however, is read straight from stored config with the base
        # entry's own default ("disabled"), so the "basic" default this provider's entry
        # declares would only take effect once the user saves the player's settings. Store
        # it once - only while nothing is stored, so a value the user picks later stays.
        if (
            self.mass.config.get_raw_player_config_value(player_id, CONF_ENABLE_ICY_METADATA)
            is None
        ):
            self.mass.config.set_raw_player_config_value(
                player_id, CONF_ENABLE_ICY_METADATA, "basic"
            )

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
