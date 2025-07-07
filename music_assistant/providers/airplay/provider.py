"""AirPlay Player provider for Music Assistant."""

from __future__ import annotations

import asyncio
import socket
from random import randrange

from music_assistant_models.enums import PlaybackState, ProviderFeature
from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceInfo

from music_assistant.helpers.datetime import utc
from music_assistant.helpers.util import get_ip_pton, lock, select_free_port
from music_assistant.models.player import DeviceInfo
from music_assistant.models.player_provider import PlayerProvider

from .constants import CONF_IGNORE_VOLUME
from .helpers import convert_airplay_volume, get_cliraop_binary, get_primary_ip_address
from .player import AirPlayPlayer

# TODO: AirPlay provider
# - Implement authentication for Apple TV
# - Implement volume control for Apple devices using pyatv
# - Implement metadata for Apple Apple devices using pyatv
# - Use pyatv for communicating with original Apple devices (and use cliraop for actual streaming)
# - Implement AirPlay 2 support
# - Implement late joining to existing stream (instead of restarting it)


class AirPlayProvider(PlayerProvider):
    """Player provider for AirPlay based players."""

    cliraop_bin: str | None
    _players: dict[str, AirPlayPlayer]
    _dacp_server: asyncio.Server
    _dacp_info: AsyncServiceInfo

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {ProviderFeature.SYNC_PLAYERS}

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._players = {}
        self.cliraop_bin: str | None = await get_cliraop_binary()
        dacp_port = await select_free_port(39831, 49831)
        self.dacp_id = dacp_id = f"{randrange(2**64):X}"
        self.logger.debug("Starting DACP ActiveRemote %s on port %s", dacp_id, dacp_port)
        self._dacp_server = await asyncio.start_server(
            self._handle_dacp_request, "0.0.0.0", dacp_port
        )
        zeroconf_type = "_dacp._tcp.local."
        server_id = f"iTunes_Ctrl_{dacp_id}.{zeroconf_type}"
        self._dacp_info = AsyncServiceInfo(
            zeroconf_type,
            name=server_id,
            addresses=[await get_ip_pton(str(self.mass.streams.publish_ip))],
            port=dacp_port,
            properties={
                "txtvers": "1",
                "Ver": "63B5E5C0C201542E",
                "DbId": "63B5E5C0C201542E",
                "OSsi": "0x1F5",
            },
            server=f"{socket.gethostname()}.local",
        )
        await self.mass.aiozc.async_register_service(self._dacp_info)

    async def on_mdns_service_state_change(
        self, name: str, state_change: ServiceStateChange, info: AsyncServiceInfo | None
    ) -> None:
        """Handle MDNS service state callback."""
        if not info:
            # When info are not provided for the service
            if state_change == ServiceStateChange.Removed and "@" in name:
                # Service name is enough to mark the player as unavailable on 'Removed' notification
                raw_id, display_name = name.split(".")[0].split("@", 1)
            else:
                # If we are not in a 'Removed' state, we need info to be filled to update the player
                return
        elif "@" in info.name:
            raw_id, display_name = info.name.split(".")[0].split("@", 1)
        elif deviceid := info.decoded_properties.get("deviceid"):
            raw_id = deviceid.replace(":", "")
            display_name = info.name.split(".")[0]
        else:
            return
        player_id = f"ap{raw_id.lower()}"
        # handle removed player
        if state_change == ServiceStateChange.Removed:
            if airplay_player := self._players.get(player_id):
                if not airplay_player.available:
                    return
                # the player has become unavailable
                self.logger.debug("Player offline: %s", display_name)
                airplay_player._attr_available = False
                airplay_player.update_state()
            return
        # handle update for existing device
        assert info is not None  # type guard
        if airplay_player := self._players.get(player_id):
            cur_address = get_primary_ip_address(info)
            if cur_address and cur_address != airplay_player.address:
                airplay_player.logger.debug(
                    "Address updated from %s to %s", airplay_player.address, cur_address
                )
                airplay_player.address = cur_address
                airplay_player._attr_device_info = DeviceInfo(
                    model=airplay_player.device_info.model,
                    manufacturer=airplay_player.device_info.manufacturer,
                    ip_address=str(cur_address),
                )
            if not airplay_player.available:
                self.logger.debug("Player back online: %s", display_name)
                airplay_player._attr_available = True
            # always update the latest discovery info
            airplay_player.discovery_info = info
            airplay_player.update_state()
            return
        # handle new player
        await self._setup_player(player_id, display_name, info)

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        # power off all players (will disconnect and close cliraop)
        for player in self._players.values():
            await player.stop()
        # shutdown DACP server
        if self._dacp_server:
            self._dacp_server.close()
        # shutdown DACP zeroconf service
        if self._dacp_info:
            await self.mass.aiozc.async_unregister_service(self._dacp_info)

    @lock
    async def cmd_group(self, player_id: str, target_player: str) -> None:
        """Handle GROUP command for given player.

        Join/add the given player(id) to the given (master) player/sync group.

            - player_id: player_id of the player to handle the command.
            - target_player: player_id of the syncgroup master or group player.
        """
        if player_id == target_player:
            return
        child_player = self.mass.players.get(player_id)
        assert child_player  # guard
        parent_player = self.mass.players.get(target_player)
        assert parent_player  # guard
        if parent_player.synced_to:
            raise RuntimeError("Player is already synced")
        if child_player.synced_to and child_player.synced_to != target_player:
            raise RuntimeError("Player is already synced to another player")
        if player_id in parent_player.group_childs:
            # nothing to do: player is already part of the group
            return
        # ensure the child does not have an existing stream session active
        if airplay_player := self._players.get(player_id):
            if airplay_player.raop_stream and airplay_player.raop_stream.running:
                await airplay_player.raop_stream.session.remove_client(airplay_player)
        # always make sure that the parent player is part of the sync group
        parent_player.group_childs.append(parent_player.player_id)
        parent_player.group_childs.append(child_player.player_id)
        child_player.synced_to = parent_player.player_id

        # check if we should (re)start or join a stream session
        active_queue = self.mass.player_queues.get_active_queue(parent_player.player_id)
        if active_queue.state == PlaybackState.PLAYING:
            # playback needs to be restarted to form a new multi client stream session
            # TODO: allow late joining to existing stream
            await self.mass.player_queues.stop(active_queue.queue_id)
            # this could potentially be called by multiple players at the exact same time
            # so we debounce the resync a bit here with a timer
            self.mass.call_later(
                0.5,
                self.mass.player_queues.resume,
                active_queue.queue_id,
                fade_in=False,
                task_id=f"resume_{active_queue.queue_id}",
            )
        else:
            # make sure that the player manager gets an update
            self.mass.players.update(child_player.player_id, skip_forward=True)
            self.mass.players.update(parent_player.player_id, skip_forward=True)

    @lock
    async def cmd_ungroup(self, player_id: str) -> None:
        """Handle UNGROUP command for given player.

        Remove the given player from any (sync)groups it currently is grouped to.

            - player_id: player_id of the player to handle the command.
        """
        mass_player = self.mass.players.get(player_id, raise_unavailable=True)
        if not mass_player or not mass_player.synced_to:
            return
        ap_player = self._players[player_id]
        if ap_player.raop_stream and ap_player.raop_stream.running:
            await ap_player.raop_stream.session.remove_client(ap_player)
        group_leader = self.mass.players.get(mass_player.synced_to, raise_unavailable=True)
        assert group_leader
        if player_id in group_leader.group_childs:
            group_leader.group_childs.remove(player_id)
        mass_player.synced_to = None
        mass_player.active_source = None
        mass_player.state = PlaybackState.IDLE
        airplay_player = self._players.get(player_id)
        if airplay_player:
            await airplay_player.stop()
        # make sure that the player manager gets an update
        self.mass.players.update(mass_player.player_id, skip_forward=True)
        self.mass.players.update(group_leader.player_id, skip_forward=True)

    def _get_sync_clients(self, player_id: str) -> list[AirPlayPlayer]:
        """Get all sync clients for a player."""
        mass_player = self.mass.players.get(player_id, True)
        assert mass_player
        sync_clients: list[AirPlayPlayer] = []
        # we need to return the player itself too
        group_child_ids = {player_id}
        group_child_ids.update(mass_player.group_childs)
        for child_id in group_child_ids:
            if client := self._players.get(child_id):
                sync_clients.append(client)
        return sync_clients

    async def _setup_player(
        self, player_id: str, display_name: str, info: AsyncServiceInfo
    ) -> None:
        """Handle setup of a new player that is discovered using mdns."""
        # Create player using the new pattern
        airplay_player = await AirPlayPlayer.create_from_discovery(
            self, player_id, display_name, info
        )
        if airplay_player is None:
            return

        self._players[player_id] = airplay_player
        await self.mass.players.register_or_update(airplay_player)

    async def poll_player(self, player_id: str) -> None:
        """Poll player for state updates."""
        if self._players.get(player_id):
            # Airplay players don't need regular polling as they send DACP events
            pass

    async def _handle_dacp_request(  # noqa: PLR0915
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle new connection on the socket."""
        try:
            raw_request = b""
            while recv := await reader.read(1024):
                raw_request += recv
                if len(recv) < 1024:
                    break
            if not raw_request:
                # Some device (Phorus PS10) seems to send empty request
                # Maybe as a ack message? we have nothing to do here with empty request
                # so we return early.
                return

            request = raw_request.decode("UTF-8")
            if "\r\n\r\n" in request:
                headers_raw, body = request.split("\r\n\r\n", 1)
            else:
                headers_raw = request
                body = ""
            headers_split = headers_raw.split("\r\n")
            headers = {}
            for line in headers_split[1:]:
                if ":" not in line:
                    continue
                x, y = line.split(":", 1)
                headers[x.strip()] = y.strip()
            active_remote = headers.get("Active-Remote")
            _, path, _ = headers_split[0].split(" ")
            airplay_player = next(
                (
                    x
                    for x in self._players.values()
                    if x.raop_stream and x.raop_stream.active_remote_id == active_remote
                ),
                None,
            )
            self.logger.debug(
                "DACP request for %s (%s): %s -- %s",
                airplay_player.discovery_info.name if airplay_player else "UNKNOWN PLAYER",
                active_remote,
                path,
                body,
            )
            if not airplay_player:
                return

            player_id = airplay_player.player_id
            mass_player = self.mass.players.get(player_id)
            if not mass_player:
                return
            ignore_volume_report = (
                self.mass.config.get_raw_player_config_value(player_id, CONF_IGNORE_VOLUME, False)
                or mass_player.device_info.manufacturer.lower() == "apple"
            )
            active_queue = self.mass.player_queues.get_active_queue(player_id)
            if path == "/ctrl-int/1/nextitem":
                self.mass.create_task(self.mass.player_queues.next(active_queue.queue_id))
            elif path == "/ctrl-int/1/previtem":
                self.mass.create_task(self.mass.player_queues.previous(active_queue.queue_id))
            elif path == "/ctrl-int/1/play":
                # sometimes this request is sent by a device as confirmation of a play command
                # we ignore this if the player is already playing
                if mass_player.state != PlaybackState.PLAYING:
                    self.mass.create_task(self.mass.player_queues.play(active_queue.queue_id))
            elif path == "/ctrl-int/1/playpause":
                self.mass.create_task(self.mass.player_queues.play_pause(active_queue.queue_id))
            elif path == "/ctrl-int/1/stop":
                self.mass.create_task(self.mass.player_queues.stop(active_queue.queue_id))
            elif path == "/ctrl-int/1/volumeup":
                self.mass.create_task(self.mass.players.cmd_volume_up(player_id))
            elif path == "/ctrl-int/1/volumedown":
                self.mass.create_task(self.mass.players.cmd_volume_down(player_id))
            elif path == "/ctrl-int/1/shuffle_songs":
                queue = self.mass.player_queues.get(player_id)
                if not queue:
                    return
                self.mass.player_queues.set_shuffle(
                    active_queue.queue_id, not queue.shuffle_enabled
                )
            elif path in ("/ctrl-int/1/pause", "/ctrl-int/1/discrete-pause"):
                # sometimes this request is sent by a device as confirmation of a play command
                # we ignore this if the player is already playing
                if mass_player.state == PlaybackState.PLAYING:
                    self.mass.create_task(self.mass.player_queues.pause(active_queue.queue_id))
            elif "dmcp.device-volume=" in path and not ignore_volume_report:
                # This is a bit annoying as this can be either the device confirming a new volume
                # we've sent or the device requesting a new volume itself.
                # In case of a small rounding difference, we ignore this,
                # to prevent an endless pingpong of volume changes
                raop_volume = float(path.split("dmcp.device-volume=", 1)[-1])
                volume = convert_airplay_volume(raop_volume)
                airplay_player.update_volume_from_device(volume)
            elif "dmcp.volume=" in path:
                # volume change request from device (e.g. volume buttons)
                volume = int(path.split("dmcp.volume=", 1)[-1])
                airplay_player.update_volume_from_device(volume)
            elif "device-prevent-playback=1" in path:
                # device switched to another source (or is powered off)
                if raop_stream := airplay_player.raop_stream:
                    raop_stream.prevent_playback = True
                    if mass_player.synced_to:
                        self.mass.create_task(self.cmd_ungroup(airplay_player.player_id))
                    else:
                        self.mass.create_task(
                            airplay_player.raop_stream.session.remove_client(airplay_player)
                        )
            elif "device-prevent-playback=0" in path:
                # device reports that its ready for playback again
                if raop_stream := airplay_player.raop_stream:
                    raop_stream.prevent_playback = False

            # send response
            date_str = utc().strftime("%a, %-d %b %Y %H:%M:%S")
            response = (
                f"HTTP/1.0 204 No Content\r\nDate: {date_str} "
                "GMT\r\nDAAP-Server: iTunes/7.6.2 (Windows; N;)\r\nContent-Type: "
                "application/x-dmap-tagged\r\nContent-Length: 0\r\n"
                "Connection: close\r\n\r\n"
            )
            writer.write(response.encode())
            await writer.drain()
        finally:
            writer.close()
