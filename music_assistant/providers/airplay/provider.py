"""AirPlay Player provider for Music Assistant."""

from __future__ import annotations

import asyncio
import socket
from random import randrange
from typing import cast

from music_assistant_models.enums import PlaybackState
from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceInfo

from music_assistant.helpers.datetime import utc
from music_assistant.helpers.util import (
    get_ip_pton,
    get_primary_ip_address_from_zeroconf,
    select_free_port,
)
from music_assistant.models.player_provider import PlayerProvider

from .constants import (
    AIRPLAY_DISCOVERY_TYPE,
    CACHE_CATEGORY_PREV_VOLUME,
    CONF_IGNORE_VOLUME,
    DACP_DISCOVERY_TYPE,
    FALLBACK_VOLUME,
    RAOP_DISCOVERY_TYPE,
)
from .helpers import convert_airplay_volume, get_model_info
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

    _dacp_server: asyncio.Server
    _dacp_info: AsyncServiceInfo

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # register DACP zeroconf service
        dacp_port = await select_free_port(39831, 49831)
        self.dacp_id = dacp_id = f"{randrange(2**64):X}"
        self.logger.debug("Starting DACP ActiveRemote %s on port %s", dacp_id, dacp_port)
        self._dacp_server = await asyncio.start_server(
            self._handle_dacp_request, "0.0.0.0", dacp_port
        )
        server_id = f"iTunes_Ctrl_{dacp_id}.{DACP_DISCOVERY_TYPE}"
        self._dacp_info = AsyncServiceInfo(
            DACP_DISCOVERY_TYPE,
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
            if _player := self.mass.players.get(player_id):
                # the player has become unavailable
                self.logger.debug("Player offline: %s", _player.display_name)
                await self.mass.players.unregister(player_id)
            return
        # handle update for existing device
        assert info is not None  # type guard
        player: AirPlayPlayer | None
        if player := cast("AirPlayPlayer | None", self.mass.players.get(player_id)):
            # update the latest discovery info for existing player
            player.set_discovery_info(info, display_name)
            return
        await self._setup_player(player_id, display_name, info)

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        # shutdown DACP server
        if self._dacp_server:
            self._dacp_server.close()
        # shutdown DACP zeroconf service
        if self._dacp_info:
            await self.mass.aiozc.async_unregister_service(self._dacp_info)

    async def _setup_player(
        self, player_id: str, display_name: str, discovery_info: AsyncServiceInfo
    ) -> None:
        """Handle setup of a new player that is discovered using mdns."""
        raop_discovery_info: AsyncServiceInfo | None = None
        airplay_discovery_info: AsyncServiceInfo | None = None
        if discovery_info.type == RAOP_DISCOVERY_TYPE:
            # RAOP service discovered
            raop_discovery_info = discovery_info
            self.logger.debug("Discovered RAOP service for %s", display_name)
            # always prefer airplay mdns info as it has more details
            # fallback to raop info if airplay info is not available,
            # (old device only announcing raop)
            airplay_discovery_info = AsyncServiceInfo(
                AIRPLAY_DISCOVERY_TYPE,
                discovery_info.name.split("@")[-1].replace("_raop", "_airplay"),
            )
            await airplay_discovery_info.async_request(self.mass.aiozc.zeroconf, 3000)
        else:
            # AirPlay service discovered
            self.logger.debug("Discovered AirPlay service for %s", display_name)
            airplay_discovery_info = discovery_info

        if airplay_discovery_info:
            manufacturer, model = get_model_info(airplay_discovery_info)
        elif raop_discovery_info:
            manufacturer, model = get_model_info(raop_discovery_info)
        else:
            manufacturer, model = "Unknown", "Unknown"

        address = get_primary_ip_address_from_zeroconf(discovery_info)
        if not address:
            return  # should not happen, but guard just in case

        # Filter out shairport-sync instances running on THIS Music Assistant server
        # These are managed by the AirPlay Receiver provider, not the AirPlay provider
        # We check both model name AND that it's a local address to avoid filtering
        # shairport-sync instances running on other machines
        if model == "ShairportSync":
            # Check if this is a local address (127.x.x.x or matches our server's IP)
            if address.startswith("127.") or address == self.mass.streams.publish_ip:
                return

        if not self.mass.config.get_raw_player_config_value(player_id, "enabled", True):
            self.logger.debug("Ignoring %s in discovery as it is disabled.", display_name)
            return
        if not discovery_info:
            return  # should not happen, but guard just in case

        # if we reach this point, all preflights are ok and we can create the player
        self.logger.debug("Discovered AirPlay device %s on %s", display_name, address)

        # Get volume from cache
        if not (
            volume := await self.mass.cache.get(
                key=player_id, provider=self.instance_id, category=CACHE_CATEGORY_PREV_VOLUME
            )
        ):
            volume = FALLBACK_VOLUME

        # Append airplay to the default name for non-apple devices
        # to make it easier for users to distinguish
        is_apple = manufacturer.lower() == "apple"
        if not is_apple and "airplay" not in display_name.lower():
            display_name += " (AirPlay)"

        # Final check before registration to handle race conditions
        # (multiple MDNS events processed in parallel for same device)
        if self.mass.players.get(player_id):
            self.logger.debug(
                "Player %s already registered during setup, skipping registration", player_id
            )
            return

        self.logger.debug(
            "Setting up player %s: manufacturer=%s, model=%s",
            display_name,
            manufacturer,
            model,
        )

        # Create single AirPlayPlayer for all devices
        # Pairing config entries will be shown conditionally based on device type
        player = AirPlayPlayer(
            provider=self,
            player_id=player_id,
            raop_discovery_info=raop_discovery_info,
            airplay_discovery_info=airplay_discovery_info,
            address=address,
            display_name=display_name,
            manufacturer=manufacturer,
            model=model,
            initial_volume=volume,
        )
        await self.mass.players.register(player)

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
            # lookup airplay player by active remote id
            player: AirPlayPlayer | None = next(
                (
                    x
                    for x in self.get_players()
                    if x.stream and x.stream.active_remote_id == active_remote
                ),
                None,
            )
            self.logger.debug(
                "DACP request for %s (%s): %s -- %s",
                player.name if player else "UNKNOWN PLAYER",
                active_remote,
                path,
                body,
            )
            if not player:
                return

            player_id = player.player_id
            ignore_volume_report = (
                self.mass.config.get_raw_player_config_value(player_id, CONF_IGNORE_VOLUME, False)
                or player.device_info.manufacturer.lower() == "apple"
            )
            active_queue = self.mass.player_queues.get_active_queue(player_id)
            if not active_queue:
                self.logger.warning(
                    "DACP request for %s (%s) but no active queue found, ignoring request",
                    player.display_name,
                    player_id,
                )
                return
            if path == "/ctrl-int/1/nextitem":
                self.mass.create_task(self.mass.player_queues.next(active_queue.queue_id))
            elif path == "/ctrl-int/1/previtem":
                self.mass.create_task(self.mass.player_queues.previous(active_queue.queue_id))
            elif path == "/ctrl-int/1/play":
                # sometimes this request is sent by a device as confirmation of a play command
                # we ignore this if the player is already playing
                if player.playback_state != PlaybackState.PLAYING:
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
                await self.mass.player_queues.set_shuffle(
                    active_queue.queue_id, not queue.shuffle_enabled
                )
            elif path in ("/ctrl-int/1/pause", "/ctrl-int/1/discrete-pause"):
                # sometimes this request is sent by a device as confirmation of a play command
                # we ignore this if the player is already playing
                if player.playback_state == PlaybackState.PLAYING:
                    self.mass.create_task(self.mass.player_queues.pause(active_queue.queue_id))
            elif "dmcp.device-volume=" in path and not ignore_volume_report:
                # This is a bit annoying as this can be either the device confirming a new volume
                # we've sent or the device requesting a new volume itself.
                # In case of a small rounding difference, we ignore this,
                # to prevent an endless pingpong of volume changes
                airplay_volume = float(path.split("dmcp.device-volume=", 1)[-1])
                volume = convert_airplay_volume(airplay_volume)
                player.update_volume_from_device(volume)
            elif "dmcp.volume=" in path:
                # volume change request from device (e.g. volume buttons)
                volume = int(path.split("dmcp.volume=", 1)[-1])
                player.update_volume_from_device(volume)
            elif "device-prevent-playback=1" in path:
                # device switched to another source (or is powered off)
                if stream := player.stream:
                    stream.prevent_playback = True
                    if stream.session:
                        self.mass.create_task(stream.session.remove_client(player))
            elif "device-prevent-playback=0" in path:
                # device reports that its ready for playback again
                if stream := player.stream:
                    stream.prevent_playback = False

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

    def get_players(self) -> list[AirPlayPlayer]:
        """Return all airplay players belonging to this instance."""
        return cast("list[AirPlayPlayer]", self.players)

    def get_player(self, player_id: str) -> AirPlayPlayer | None:
        """Return AirplayPlayer by id."""
        return cast("AirPlayPlayer | None", self.mass.players.get(player_id))
