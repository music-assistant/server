"""AirPlay Player provider for Music Assistant."""

from __future__ import annotations

import asyncio
import socket
import time
from random import randrange
from typing import cast

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    MediaType,
    PlayerFeature,
    PlayerState,
    PlayerType,
    ProviderFeature,
)
from music_assistant_models.errors import PlayerUnavailableError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.player import DeviceInfo, Player, PlayerMedia
from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceInfo

from music_assistant.constants import (
    CONF_ENTRY_DEPRECATED_EQ_BASS,
    CONF_ENTRY_DEPRECATED_EQ_MID,
    CONF_ENTRY_DEPRECATED_EQ_TREBLE,
    CONF_ENTRY_FLOW_MODE_ENFORCED,
    CONF_ENTRY_OUTPUT_CODEC_HIDDEN,
    CONF_ENTRY_SYNC_ADJUST,
    create_sample_rates_config_entry,
)
from music_assistant.helpers.datetime import utc
from music_assistant.helpers.ffmpeg import get_ffmpeg_stream
from music_assistant.helpers.util import get_ip_pton, lock, select_free_port
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.airplay.raop import RaopStreamSession
from music_assistant.providers.player_group import PlayerGroupProvider

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
from .helpers import (
    convert_airplay_volume,
    get_cliraop_binary,
    get_model_info,
    get_primary_ip_address,
    is_broken_raop_model,
)
from .player import AirPlayPlayer

CONF_IGNORE_VOLUME = "ignore_volume"

PLAYER_CONFIG_ENTRIES = (
    CONF_ENTRY_FLOW_MODE_ENFORCED,
    CONF_ENTRY_DEPRECATED_EQ_BASS,
    CONF_ENTRY_DEPRECATED_EQ_MID,
    CONF_ENTRY_DEPRECATED_EQ_TREBLE,
    CONF_ENTRY_OUTPUT_CODEC_HIDDEN,
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
    create_sample_rates_config_entry(
        supported_sample_rates=[44100], supported_bit_depths=[16], hidden=True
    ),
    ConfigEntry(
        key=CONF_IGNORE_VOLUME,
        type=ConfigEntryType.BOOLEAN,
        default_value=False,
        label="Ignore volume reports sent by the device itself",
        description="The AirPlay protocol allows devices to report their own volume level. \n"
        "For some devices this is not reliable and can cause unexpected volume changes. \n"
        "Enable this option to ignore these reports.",
        category="airplay",
    ),
)

BROKEN_RAOP_WARN = ConfigEntry(
    key="broken_raop",
    type=ConfigEntryType.ALERT,
    default_value=None,
    required=False,
    label="This player is known to have broken AirPlay 1 (RAOP) support. "
    "Playback may fail or simply be silent. There is no workaround for this issue at the moment.",
)


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
            if mass_player := self.mass.players.get(player_id):
                if not mass_player.available:
                    return
                # the player has become unavailable
                self.logger.debug("Player offline: %s", display_name)
                mass_player.available = False
                self.mass.players.update(player_id)
            return
        # handle update for existing device
        assert info is not None  # type guard
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
                        ip_address=str(cur_address),
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

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        # power off all players (will disconnect and close cliraop)
        for player in self._players.values():
            await player.cmd_stop()
        # shutdown DACP server
        if self._dacp_server:
            self._dacp_server.close()
        # shutdown DACP zeroconf service
        if self._dacp_info:
            await self.mass.aiozc.async_unregister_service(self._dacp_info)

    async def get_player_config_entries(self, player_id: str) -> tuple[ConfigEntry, ...]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        base_entries = await super().get_player_config_entries(player_id)

        if player := self.mass.players.get(player_id):
            if is_broken_raop_model(player.device_info.manufacturer, player.device_info.model):
                return (*base_entries, BROKEN_RAOP_WARN, *PLAYER_CONFIG_ENTRIES)
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
        if not player:
            return
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
        if not (player := self.mass.players.get(player_id)):
            # this should not happen, but guard anyways
            raise PlayerUnavailableError
        if player.synced_to:
            # this should not happen, but guard anyways
            raise RuntimeError("Player is synced")
        if not (airplay_player := self._players.get(player_id)):
            # this should not happen, but guard anyways
            raise PlayerUnavailableError
        # set the active source for the player to the media queue
        # this accounts for syncgroups and linked players (e.g. sonos)
        player.active_source = media.queue_id
        player.current_media = media

        # select audio source
        if media.media_type == MediaType.ANNOUNCEMENT:
            # special case: stream announcement
            assert media.custom_data
            input_format = AIRPLAY_PCM_FORMAT
            audio_source = self.mass.streams.get_announcement_stream(
                media.custom_data["url"],
                output_format=AIRPLAY_PCM_FORMAT,
                use_pre_announce=media.custom_data["use_pre_announce"],
            )
        elif media.media_type == MediaType.PLUGIN_SOURCE:
            # special case: plugin source stream
            input_format = AIRPLAY_PCM_FORMAT
            assert media.custom_data
            audio_source = self.mass.streams.get_plugin_source_stream(
                plugin_source_id=media.custom_data["source_id"],
                output_format=AIRPLAY_PCM_FORMAT,
                # need to pass player_id from the PlayerMedia object
                # because this could have been a group
                player_id=media.custom_data["player_id"],
            )
        elif media.queue_id and media.queue_id.startswith("ugp_"):
            # special case: UGP stream
            ugp_provider = cast("PlayerGroupProvider", self.mass.get_provider("player_group"))
            ugp_stream = ugp_provider.ugp_streams[media.queue_id]
            input_format = ugp_stream.base_pcm_format
            audio_source = ugp_stream.subscribe_raw()
        elif media.queue_id and media.queue_item_id:
            # regular queue (flow) stream request
            input_format = AIRPLAY_FLOW_PCM_FORMAT
            queue = self.mass.player_queues.get(media.queue_id)
            assert queue
            start_queue_item = self.mass.player_queues.get_item(media.queue_id, media.queue_item_id)
            assert start_queue_item
            audio_source = self.mass.streams.get_queue_flow_stream(
                queue=queue,
                start_queue_item=start_queue_item,
                pcm_format=input_format,
            )
        else:
            # assume url or some other direct path
            # NOTE: this will fail if its an uri not playable by ffmpeg
            input_format = AIRPLAY_PCM_FORMAT
            audio_source = get_ffmpeg_stream(
                audio_input=media.uri,
                input_format=AudioFormat(content_type=ContentType.try_parse(media.uri)),
                output_format=AIRPLAY_PCM_FORMAT,
            )

        # if an existing stream session is running, we could replace it with the new stream
        if airplay_player.raop_stream and airplay_player.raop_stream.running:
            # check if we need to replace the stream
            if airplay_player.raop_stream.prevent_playback:
                # player is in prevent playback mode, we need to stop the stream
                await airplay_player.cmd_stop()
            else:
                await airplay_player.raop_stream.session.replace_stream(audio_source)
                return

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
        if not mass_player:
            return
        mass_player.volume_level = volume_level
        mass_player.volume_muted = volume_level == 0
        self.mass.players.update(player_id)
        # store last state in cache
        await self.mass.cache.set(player_id, volume_level, base_key=CACHE_KEY_PREV_VOLUME)

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
        # ensure the child does not have an existing steam session active
        if airplay_player := self._players.get(player_id):
            if airplay_player.raop_stream and airplay_player.raop_stream.running:
                await airplay_player.raop_stream.session.remove_client(airplay_player)
        # always make sure that the parent player is part of the sync group
        parent_player.group_childs.append(parent_player.player_id)
        parent_player.group_childs.append(child_player.player_id)
        child_player.synced_to = parent_player.player_id

        # check if we should (re)start or join a stream session
        active_queue = self.mass.player_queues.get_active_queue(parent_player.player_id)
        if active_queue.state == PlayerState.PLAYING:
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
        airplay_player = self._players.get(player_id)
        if airplay_player:
            await airplay_player.cmd_stop()
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
        address = get_primary_ip_address(info)
        if address is None:
            return
        self.logger.debug("Discovered AirPlay device %s on %s", display_name, address)

        # prefer airplay mdns info as it has more details
        # fallback to raop info if airplay info is not available
        airplay_info = AsyncServiceInfo(
            "_airplay._tcp.local.", info.name.split("@")[-1].replace("_raop", "_airplay")
        )
        if await airplay_info.async_request(self.mass.aiozc.zeroconf, 3000):
            manufacturer, model = get_model_info(airplay_info)
        else:
            manufacturer, model = get_model_info(info)

        if not self.mass.config.get_raw_player_config_value(player_id, "enabled", True):
            self.logger.debug("Ignoring %s in discovery as it is disabled.", display_name)
            return

        if "apple tv" in model.lower():
            # For now, we ignore the Apple TV until we implement the authentication.
            # maybe we can simply use pyatv only for this part?
            # the cliraop application has already been prepared to accept the secret.
            self.logger.info(
                "Ignoring %s in discovery because it is not yet supported.", display_name
            )
            return

        # append airplay to the default display name for generic (non-apple) devices
        # this makes it easier for users to distinguish between airplay and non-airplay devices
        if manufacturer.lower() != "apple" and "airplay" not in display_name.lower():
            display_name += " (AirPlay)"

        self._players[player_id] = AirPlayPlayer(self, player_id, info, address)
        if not (volume := await self.mass.cache.get(player_id, base_key=CACHE_KEY_PREV_VOLUME)):
            volume = FALLBACK_VOLUME
        mass_player = Player(
            player_id=player_id,
            provider=self.instance_id,
            type=PlayerType.PLAYER,
            name=display_name,
            available=True,
            device_info=DeviceInfo(
                model=model,
                manufacturer=manufacturer,
                ip_address=address,
            ),
            supported_features={
                PlayerFeature.PAUSE,
                PlayerFeature.SET_MEMBERS,
                PlayerFeature.MULTI_DEVICE_DSP,
                PlayerFeature.VOLUME_SET,
            },
            volume_level=volume,
            can_group_with={self.instance_id},
            enabled_by_default=not is_broken_raop_model(manufacturer, model),
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
                if not queue:
                    return
                self.mass.player_queues.set_shuffle(
                    active_queue.queue_id, not queue.shuffle_enabled
                )
            elif path in ("/ctrl-int/1/pause", "/ctrl-int/1/discrete-pause"):
                # sometimes this request is sent by a device as confirmation of a play command
                # we ignore this if the player is already playing
                if mass_player.state == PlayerState.PLAYING:
                    self.mass.create_task(self.mass.player_queues.pause(active_queue.queue_id))
            elif "dmcp.device-volume=" in path and not ignore_volume_report:
                # This is a bit annoying as this can be either the device confirming a new volume
                # we've sent or the device requesting a new volume itself.
                # In case of a small rounding difference, we ignore this,
                # to prevent an endless pingpong of volume changes
                raop_volume = float(path.split("dmcp.device-volume=", 1)[-1])
                volume = convert_airplay_volume(raop_volume)
                cur_volume = mass_player.volume_level or 0
                if (
                    abs(cur_volume - volume) > 3
                    or (time.time() - airplay_player.last_command_sent) > 3
                ):
                    self.mass.create_task(self.cmd_volume_set(player_id, volume))
                else:
                    mass_player.volume_level = volume
                    self.mass.players.update(player_id)
            elif "dmcp.volume=" in path:
                # volume change request from device (e.g. volume buttons)
                volume = int(path.split("dmcp.volume=", 1)[-1])
                cur_volume = mass_player.volume_level or 0
                if (
                    abs(cur_volume - volume) > 2
                    or (time.time() - airplay_player.last_command_sent) > 3
                ):
                    self.mass.create_task(self.cmd_volume_set(player_id, volume))
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
