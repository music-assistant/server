"""Airplay Player provider for Music Assistant."""

from __future__ import annotations

import asyncio
import os
import platform
import socket
import time
from random import randrange
from typing import TYPE_CHECKING

from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceInfo

from music_assistant.common.helpers.datetime import utc
from music_assistant.common.helpers.util import get_ip_pton, select_free_port
from music_assistant.common.models.config_entries import (
    CONF_ENTRY_CROSSFADE,
    CONF_ENTRY_CROSSFADE_DURATION,
    CONF_ENTRY_EQ_BASS,
    CONF_ENTRY_EQ_MID,
    CONF_ENTRY_EQ_TREBLE,
    CONF_ENTRY_FLOW_MODE_ENFORCED,
    CONF_ENTRY_OUTPUT_CHANNELS,
    CONF_ENTRY_SYNC_ADJUST,
    ConfigEntry,
    create_sample_rates_config_entry,
)
from music_assistant.common.models.enums import (
    ConfigEntryType,
    ContentType,
    MediaType,
    PlayerFeature,
    PlayerState,
    PlayerType,
    ProviderFeature,
)
from music_assistant.common.models.media_items import AudioFormat
from music_assistant.common.models.player import DeviceInfo, Player, PlayerMedia
from music_assistant.server.helpers.audio import get_ffmpeg_stream
from music_assistant.server.helpers.process import check_output
from music_assistant.server.helpers.util import TaskManager, lock
from music_assistant.server.models.player_provider import PlayerProvider
from music_assistant.server.providers.airplay.raop import RaopStreamSession

from .const import (
    AIRPLAY_FLOW_PCM_FORMAT,
    AIRPLAY_PCM_FORMAT,
    CACHE_KEY_PREV_VOLUME,
    CONF_ALAC_ENCODE,
    CONF_ENCRYPTION,
    CONF_PASSWORD,
    CONF_READ_AHEAD_BUFFER,
    FALLBACK_VOLUME,
)
from .helpers import convert_airplay_volume, get_model_from_am, get_primary_ip_address
from .player import AirPlayPlayer

if TYPE_CHECKING:
    from music_assistant.server.providers.player_group import PlayerGroupProvider


PLAYER_CONFIG_ENTRIES = (
    CONF_ENTRY_FLOW_MODE_ENFORCED,
    CONF_ENTRY_CROSSFADE,
    CONF_ENTRY_CROSSFADE_DURATION,
    CONF_ENTRY_EQ_BASS,
    CONF_ENTRY_EQ_MID,
    CONF_ENTRY_EQ_TREBLE,
    CONF_ENTRY_OUTPUT_CHANNELS,
    ConfigEntry(
        key=CONF_ENCRYPTION,
        type=ConfigEntryType.BOOLEAN,
        default_value=False,
        label="Enable encryption",
        description="Enable encrypted communication with the player, "
        "some (3rd party) players require this.",
        category="airplay",
    ),
    ConfigEntry(
        key=CONF_ALAC_ENCODE,
        type=ConfigEntryType.BOOLEAN,
        default_value=True,
        label="Enable compression",
        description="Save some network bandwidth by sending the audio as "
        "(lossless) ALAC at the cost of a bit CPU.",
        category="airplay",
    ),
    CONF_ENTRY_SYNC_ADJUST,
    ConfigEntry(
        key=CONF_PASSWORD,
        type=ConfigEntryType.SECURE_STRING,
        default_value=None,
        required=False,
        label="Device password",
        description="Some devices require a password to connect/play.",
        category="airplay",
    ),
    ConfigEntry(
        key=CONF_READ_AHEAD_BUFFER,
        type=ConfigEntryType.INTEGER,
        default_value=1000,
        required=False,
        label="Audio buffer (ms)",
        description="Amount of buffer (in milliseconds), "
        "the player should keep to absorb network throughput jitter. "
        "If you experience audio dropouts, try increasing this value.",
        category="airplay",
        range=(500, 3000),
    ),
    # airplay has fixed sample rate/bit depth so make this config entry static and hidden
    create_sample_rates_config_entry(44100, 16, 44100, 16, True),
)


# TODO: Airplay provider
# - Implement authentication for Apple TV
# - Implement volume control for Apple devices using pyatv
# - Implement metadata for Apple Apple devices using pyatv
# - Use pyatv for communicating with original Apple devices (and use cliraop for actual streaming)
# - Implement Airplay 2 support
# - Implement late joining to existing stream (instead of restarting it)


class AirplayProvider(PlayerProvider):
    """Player provider for Airplay based players."""

    cliraop_bin: str | None = None
    _players: dict[str, AirPlayPlayer]
    _dacp_server: asyncio.Server = None
    _dacp_info: AsyncServiceInfo = None
    _play_media_lock: asyncio.Lock = asyncio.Lock()

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return (ProviderFeature.SYNC_PLAYERS,)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._players = {}
        self.cliraop_bin = await self._getcliraop_binary()
        dacp_port = await select_free_port(39831, 49831)
        self.dacp_id = dacp_id = f"{randrange(2 ** 64):X}"
        self.logger.debug("Starting DACP ActiveRemote %s on port %s", dacp_id, dacp_port)
        self._dacp_server = await asyncio.start_server(
            self._handle_dacp_request, "0.0.0.0", dacp_port
        )
        zeroconf_type = "_dacp._tcp.local."
        server_id = f"iTunes_Ctrl_{dacp_id}.{zeroconf_type}"
        self._dacp_info = AsyncServiceInfo(
            zeroconf_type,
            name=server_id,
            addresses=[await get_ip_pton(self.mass.streams.publish_ip)],
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
        raw_id, display_name = name.split(".")[0].split("@", 1)
        player_id = f"ap{raw_id.lower()}"
        # handle removed player
        if state_change == ServiceStateChange.Removed:
            if mass_player := self.mass.players.get(player_id):
                if not mass_player.available:
                    return
                # the player has become unavailable
                self.logger.debug("Player offline: %s", display_name)
                mass_player.available = False
                self.mass.players.update(player_id)
            return
        # handle update for existing device
        if airplay_player := self._players.get(player_id):
            if mass_player := self.mass.players.get(player_id):
                cur_address = get_primary_ip_address(info)
                if cur_address and cur_address != airplay_player.address:
                    airplay_player.logger.debug(
                        "Address updated from %s to %s", airplay_player.address, cur_address
                    )
                    airplay_player.address = cur_address
                    mass_player.device_info = DeviceInfo(
                        model=mass_player.device_info.model,
                        manufacturer=mass_player.device_info.manufacturer,
                        address=str(cur_address),
                    )
                if not mass_player.available:
                    self.logger.debug("Player back online: %s", display_name)
                    mass_player.available = True
            # always update the latest discovery info
            airplay_player.discovery_info = info
            self.mass.players.update(player_id)
            return
        # handle new player
        await self._setup_player(player_id, display_name, info)

    async def unload(self) -> None:
        """Handle close/cleanup of the provider."""
        # power off all players (will disconnect and close cliraop)
        for player_id in self._players:
            await self.cmd_power(player_id, False)
        # shutdown DACP server
        if self._dacp_server:
            self._dacp_server.close()
        # shutdown DACP zeroconf service
        if self._dacp_info:
            await self.mass.aiozc.async_unregister_service(self._dacp_info)

    async def get_player_config_entries(self, player_id: str) -> tuple[ConfigEntry, ...]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        base_entries = await super().get_player_config_entries(player_id)
        return (*base_entries, *PLAYER_CONFIG_ENTRIES)

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player.

        - player_id: player_id of the player to handle the command.
        """
        if airplay_player := self._players.get(player_id):
            await airplay_player.cmd_stop()

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY (unpause) command to given player.

        - player_id: player_id of the player to handle the command.
        """
        if airplay_player := self._players.get(player_id):
            await airplay_player.cmd_play()

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player.

        - player_id: player_id of the player to handle the command.
        """
        player = self.mass.players.get(player_id)
        if player.group_childs:
            # pause is not supported while synced, use stop instead
            self.logger.debug("Player is synced, using STOP instead of PAUSE")
            await self.cmd_stop(player_id)
            return
        airplay_player = self._players[player_id]
        await airplay_player.cmd_pause()

    @lock
    async def play_media(
        self,
        player_id: str,
        media: PlayerMedia,
    ) -> None:
        """Handle PLAY MEDIA on given player."""
        async with self._play_media_lock:
            player = self.mass.players.get(player_id)
            # set the active source for the player to the media queue
            # this accounts for syncgroups and linked players (e.g. sonos)
            player.active_source = media.queue_id
            if player.synced_to:
                # should not happen, but just in case
                raise RuntimeError("Player is synced")
            # always stop existing stream first
            async with TaskManager(self.mass) as tg:
                for airplay_player in self._get_sync_clients(player_id):
                    tg.create_task(airplay_player.cmd_stop(update_state=False))
            # select audio source
            if media.media_type == MediaType.ANNOUNCEMENT:
                # special case: stream announcement
                input_format = AIRPLAY_PCM_FORMAT
                audio_source = self.mass.streams.get_announcement_stream(
                    media.custom_data["url"],
                    output_format=AIRPLAY_PCM_FORMAT,
                    use_pre_announce=media.custom_data["use_pre_announce"],
                )
            elif media.queue_id.startswith("ugp_"):
                # special case: UGP stream
                ugp_provider: PlayerGroupProvider = self.mass.get_provider("player_group")
                ugp_stream = ugp_provider.ugp_streams[media.queue_id]
                input_format = ugp_stream.output_format
                audio_source = ugp_stream.subscribe()
            elif media.queue_id and media.queue_item_id:
                # regular queue (flow) stream request
                input_format = AIRPLAY_FLOW_PCM_FORMAT
                audio_source = self.mass.streams.get_flow_stream(
                    queue=self.mass.player_queues.get(media.queue_id),
                    start_queue_item=self.mass.player_queues.get_item(
                        media.queue_id, media.queue_item_id
                    ),
                    pcm_format=input_format,
                )
            else:
                # assume url or some other direct path
                # NOTE: this will fail if its an uri not playable by ffmpeg
                input_format = AIRPLAY_PCM_FORMAT
                audio_source = get_ffmpeg_stream(
                    audio_input=media.uri,
                    input_format=AudioFormat(ContentType.try_parse(media.uri)),
                    output_format=AIRPLAY_PCM_FORMAT,
                )
            # setup RaopStreamSession for player (and its sync childs if any)
            sync_clients = self._get_sync_clients(player_id)
            raop_stream_session = RaopStreamSession(self, sync_clients, input_format, audio_source)
            await raop_stream_session.start()

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player.

        - player_id: player_id of the player to handle the command.
        - volume_level: volume level (0..100) to set on the player.
        """
        airplay_player = self._players[player_id]
        if airplay_player.raop_stream and airplay_player.raop_stream.running:
            await airplay_player.raop_stream.send_cli_command(f"VOLUME={volume_level}\n")
        mass_player = self.mass.players.get(player_id)
        mass_player.volume_level = volume_level
        mass_player.volume_muted = volume_level == 0
        self.mass.players.update(player_id)
        # store last state in cache
        await self.mass.cache.set(player_id, volume_level, base_key=CACHE_KEY_PREV_VOLUME)

    @lock
    async def cmd_sync(self, player_id: str, target_player: str) -> None:
        """Handle SYNC command for given player.

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
        # ensure the child does not have an existing steam session active
        if airplay_player := self._players.get(player_id):
            if airplay_player.raop_stream and airplay_player.raop_stream.running:
                await airplay_player.raop_stream.session.remove_client(airplay_player)
        # always make sure that the parent player is part of the sync group
        parent_player.group_childs.add(parent_player.player_id)
        parent_player.group_childs.add(child_player.player_id)
        child_player.synced_to = parent_player.player_id
        # mark players as powered
        parent_player.powered = True
        child_player.powered = True
        # check if we should (re)start or join a stream session
        active_queue = self.mass.player_queues.get_active_queue(parent_player.player_id)
        if active_queue.state == PlayerState.PLAYING:
            # playback needs to be restarted to form a new multi client stream session
            # this could potentially be called by multiple players at the exact same time
            # so we debounce the resync a bit here with a timer
            self.mass.call_later(
                1,
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
    async def cmd_unsync(self, player_id: str) -> None:
        """Handle UNSYNC command for given player.

        Remove the given player from any syncgroups it currently is synced to.

            - player_id: player_id of the player to handle the command.
        """
        mass_player = self.mass.players.get(player_id, raise_unavailable=True)
        if not mass_player.synced_to:
            return
        ap_player = self._players[player_id]
        if ap_player.raop_stream and ap_player.raop_stream.running:
            await ap_player.raop_stream.session.remove_client(ap_player)
        group_leader = self.mass.players.get(mass_player.synced_to, raise_unavailable=True)
        if player_id in group_leader.group_childs:
            group_leader.group_childs.remove(player_id)
        mass_player.synced_to = None
        airplay_player = self._players.get(player_id)
        await airplay_player.cmd_stop()
        # make sure that the player manager gets an update
        self.mass.players.update(mass_player.player_id, skip_forward=True)
        self.mass.players.update(group_leader.player_id, skip_forward=True)

    async def _getcliraop_binary(self):
        """Find the correct raop/airplay binary belonging to the platform."""
        # ruff: noqa: SIM102
        if self.cliraop_bin is not None:
            return self.cliraop_bin

        async def check_binary(cliraop_path: str) -> str | None:
            try:
                returncode, output = await check_output(
                    cliraop_path,
                    "-check",
                )
                if returncode == 0 and output.strip().decode() == "cliraop check":
                    self.cliraop_bin = cliraop_path
                    return cliraop_path
            except OSError:
                return None

        base_path = os.path.join(os.path.dirname(__file__), "bin")
        system = platform.system().lower().replace("darwin", "macos")
        architecture = platform.machine().lower()

        if bridge_binary := await check_binary(
            os.path.join(base_path, f"cliraop-{system}-{architecture}")
        ):
            return bridge_binary

        msg = f"Unable to locate RAOP Play binary for {system}/{architecture}"
        raise RuntimeError(msg)

    def _get_sync_clients(self, player_id: str) -> list[AirPlayPlayer]:
        """Get all sync clients for a player."""
        mass_player = self.mass.players.get(player_id, True)
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
        address = get_primary_ip_address(info)
        if address is None:
            return
        self.logger.debug("Discovered Airplay device %s on %s", display_name, address)
        manufacturer, model = get_model_from_am(info.decoded_properties.get("am"))
        if "apple tv" in model.lower():
            # For now, we ignore the Apple TV until we implement the authentication.
            # maybe we can simply use pyatv only for this part?
            # the cliraop application has already been prepared to accept the secret.
            self.logger.debug(
                "Ignoring %s in discovery due to authentication requirement.", display_name
            )
            return
        if not self.mass.config.get_raw_player_config_value(player_id, "enabled", True):
            self.logger.debug("Ignoring %s in discovery as it is disabled.", display_name)
            return
        self._players[player_id] = AirPlayPlayer(self, player_id, info, address)
        if not (volume := await self.mass.cache.get(player_id, base_key=CACHE_KEY_PREV_VOLUME)):
            volume = FALLBACK_VOLUME
        mass_player = Player(
            player_id=player_id,
            provider=self.instance_id,
            type=PlayerType.PLAYER,
            name=display_name,
            available=True,
            powered=False,
            device_info=DeviceInfo(
                model=model,
                manufacturer=manufacturer,
                address=address,
            ),
            supported_features=(
                PlayerFeature.PAUSE,
                PlayerFeature.SYNC,
                PlayerFeature.VOLUME_SET,
            ),
            volume_level=volume,
        )
        await self.mass.players.register_or_update(mass_player)

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

            request = raw_request.decode("UTF-8")
            if "\r\n\r\n" in request:
                headers_raw, body = request.split("\r\n\r\n", 1)
            else:
                headers_raw = request
                body = ""
            headers_raw = headers_raw.split("\r\n")
            headers = {}
            for line in headers_raw[1:]:
                if ":" not in line:
                    continue
                x, y = line.split(":", 1)
                headers[x.strip()] = y.strip()
            active_remote = headers.get("Active-Remote")
            _, path, _ = headers_raw[0].split(" ")
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
            active_queue = self.mass.player_queues.get_active_queue(player_id)
            if path == "/ctrl-int/1/nextitem":
                self.mass.create_task(self.mass.player_queues.next(active_queue.queue_id))
            elif path == "/ctrl-int/1/previtem":
                self.mass.create_task(self.mass.player_queues.previous(active_queue.queue_id))
            elif path == "/ctrl-int/1/play":
                # sometimes this request is sent by a device as confirmation of a play command
                # we ignore this if the player is already playing
                if mass_player.state != PlayerState.PLAYING:
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
                self.mass.loop.call_soon(
                    self.mass.player_queues.set_shuffle(
                        active_queue.queue_id, not queue.shuffle_enabled
                    )
                )
            elif path in ("/ctrl-int/1/pause", "/ctrl-int/1/discrete-pause"):
                # sometimes this request is sent by a device as confirmation of a play command
                # we ignore this if the player is already playing
                if mass_player.state == PlayerState.PLAYING:
                    self.mass.create_task(self.mass.player_queues.pause(active_queue.queue_id))
            elif "dmcp.device-volume=" in path:
                if mass_player.device_info.manufacturer.lower() == "apple":
                    # Apple devices only report their previous volume level ?!
                    return
                # This is a bit annoying as this can be either the device confirming a new volume
                # we've sent or the device requesting a new volume itself.
                # In case of a small rounding difference, we ignore this,
                # to prevent an endless pingpong of volume changes
                raop_volume = float(path.split("dmcp.device-volume=", 1)[-1])
                volume = convert_airplay_volume(raop_volume)
                if (
                    abs(mass_player.volume_level - volume) > 5
                    or (time.time() - airplay_player.last_command_sent) < 2
                ):
                    self.mass.create_task(self.cmd_volume_set(player_id, volume))
                else:
                    mass_player.volume_level = volume
                    self.mass.players.update(player_id)
            elif "dmcp.volume=" in path:
                # volume change request from device (e.g. volume buttons)
                volume = int(path.split("dmcp.volume=", 1)[-1])
                if volume != mass_player.volume_level:
                    self.mass.create_task(self.cmd_volume_set(player_id, volume))
                    # optimistically set the new volume to prevent bouncing around
                    mass_player.volume_level = volume
            elif "device-prevent-playback=1" in path:
                # device switched to another source (or is powered off)
                if raop_stream := airplay_player.raop_stream:
                    # ignore this if we just started playing to prevent false positives
                    if mass_player.elapsed_time > 10 and mass_player.state == PlayerState.PLAYING:
                        raop_stream.prevent_playback = True
                        self.mass.create_task(self.monitor_prevent_playback(player_id))
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

    async def monitor_prevent_playback(self, player_id: str):
        """Monitor the prevent playback state of an airplay player."""
        count = 0
        if not (airplay_player := self._players.get(player_id)):
            return
        prev_active_remote_id = airplay_player.raop_stream.active_remote_id
        while count < 40:
            count += 1
            if not (airplay_player := self._players.get(player_id)):
                return
            if not (raop_stream := airplay_player.raop_stream):
                return
            if raop_stream.active_remote_id != prev_active_remote_id:
                # checksum
                return
            if not raop_stream.prevent_playback:
                return
            await asyncio.sleep(0.5)

        airplay_player.logger.info(
            "Player has been in prevent playback mode for too long, powering off.",
        )
        await self.mass.players.cmd_power(airplay_player.player_id, False)
