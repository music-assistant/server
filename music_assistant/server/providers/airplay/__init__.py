"""Airplay Player provider for Music Assistant."""
from __future__ import annotations

import asyncio
import os
import platform
import socket
import time
from random import randint, randrange
from typing import TYPE_CHECKING, cast

import aiofiles
from pyatv import connect, exceptions, interface, scan
from pyatv.conf import AppleTV as ATVConf
from pyatv.const import DeviceModel, DeviceState, PowerState, Protocol
from pyatv.convert import model_str
from pyatv.interface import AppleTV as AppleTVInterface
from pyatv.interface import Audio, DeviceListener, Metadata, PushUpdater, RemoteControl
from pyatv.protocols.airplay.auth import extract_credentials
from pyatv.protocols.raop import RaopStream
from zeroconf import ServiceInfo

from music_assistant.common.helpers.datetime import utc
from music_assistant.common.helpers.util import get_ip_pton
from music_assistant.common.models.config_entries import (
    CONF_ENTRY_CROSSFADE,
    CONF_ENTRY_CROSSFADE_DURATION,
    ConfigEntry,
    ConfigValueType,
)
from music_assistant.common.models.enums import ContentType, PlayerFeature, PlayerState, PlayerType
from music_assistant.common.models.media_items import AudioFormat, Track
from music_assistant.common.models.player import DeviceInfo, Player
from music_assistant.server.helpers.process import AsyncProcess, check_output
from music_assistant.server.helpers.util import create_tempfile
from music_assistant.server.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.common.models.queue_item import QueueItem
    from music_assistant.server import MusicAssistant
    from music_assistant.server.controllers.streams import MultiClientStreamJob
    from music_assistant.server.models import ProviderInstanceType


PLAYER_CONFIG_ENTRIES = (CONF_ENTRY_CROSSFADE, CONF_ENTRY_CROSSFADE_DURATION)
BACKOFF_TIME_LOWER_LIMIT = 15  # seconds
BACKOFF_TIME_UPPER_LIMIT = 300  # Five minutes

CONF_CREDENTIALS = "credentials"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = AirplayProvider(mass, manifest, config)
    await prov.handle_setup()
    return prov


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # ruff: noqa: ARG001
    return ()  # we do not have any config entries (yet)


def convert_airplay_volume(value: float) -> int:
    """Remap Airplay Volume to 0..100 scale."""
    airplay_min = -30
    airplay_max = 0
    normal_min = 0
    normal_max = 100
    portion = (value - airplay_min) * (normal_max - normal_min) / (airplay_max - airplay_min)
    return int(portion + normal_min)


class AppleTVManager(DeviceListener):
    """Connection and power manager for an Apple TV.

    An instance is used per device to share the same power state between
    several platforms. It also manages scanning and connection establishment
    in case of problems.
    """

    def __init__(
        self, mass: MusicAssistant, player_id: str, discovery_info: interface.BaseConfig
    ) -> None:
        """Initialize power manager."""
        self.player_id = player_id
        self.discovery_info = discovery_info
        self.mass = mass
        self.atv: AppleTVInterface | None = None
        self.is_on = False
        self.connected = False
        self._connection_attempts = 0
        self._connection_was_lost = False
        self._task = None
        self._playing: interface.Playing | None = None
        self.logger = self.mass.players.logger.getChild("airplay").getChild(self.player_id)
        self.raop_play_proc: AsyncProcess | None = None
        self.active_remote_id = str(randint(1000, 8000))

    def connection_lost(self, _):
        """Device was unexpectedly disconnected.

        This is a callback function from pyatv.interface.DeviceListener.
        """
        self.logger.warning('Connection lost to Apple TV "%s"', self.discovery_info.name)
        self._connection_was_lost = True
        self._handle_disconnect()

    def connection_closed(self):
        """Device connection was (intentionally) closed.

        This is a callback function from pyatv.interface.DeviceListener.
        """
        self.connected = False
        self._handle_disconnect()

    def _handle_disconnect(self):
        """Handle that the device disconnected and restart connect loop."""
        self.connected = False
        if self.atv:
            self.atv.close()
            self.atv = None
        self._start_connect_loop()

    async def connect(self):
        """Connect to device."""
        if self.connected:
            return
        self.is_on = True
        self._start_connect_loop()
        await asyncio.sleep(2)

    async def disconnect(self):
        """Disconnect from device."""
        self.logger.debug("Disconnecting from device")
        self.is_on = False
        self.connected = False
        try:
            if self.atv:
                self.atv.close()
                self.atv = None
            if self._task:
                self._task.cancel()
                self._task = None
        except Exception:  # pylint: disable=broad-except
            self.logger.exception("An error occurred while disconnecting")

    def _start_connect_loop(self):
        """Start background connect loop to device."""
        if not self._task and self.atv is None and self.is_on:
            self._task = asyncio.create_task(self._connect_loop())
        else:
            self.logger.debug("Not starting connect loop (%s, %s)", self.atv is None, self.is_on)

    async def connect_once(self) -> None:
        """Try to connect once."""
        try:
            if conf := await self._scan():
                await self._connect(conf)
        except exceptions.AuthenticationError:
            await self.disconnect()
            self.logger.exception(
                "Authentication failed for %s, try reconfiguring device",
                self.discovery_info.name,
            )
            return
        except asyncio.CancelledError:
            pass
        except Exception:  # pylint: disable=broad-except
            self.logger.exception("Failed to connect")
            self.atv = None

    async def _connect_loop(self):
        """Connect loop background task function."""
        self.logger.debug("Starting connect loop")

        # Try to find device and connect as long as the user has said that
        # we are allowed to connect and we are not already connected.
        while self.is_on and self.atv is None:
            await self.connect_once()
            if self.atv is not None:
                break
            self._connection_attempts += 1
            backoff = min(
                max(
                    BACKOFF_TIME_LOWER_LIMIT,
                    randrange(2**self._connection_attempts),
                ),
                BACKOFF_TIME_UPPER_LIMIT,
            )

            self.logger.debug("Reconnecting in %d seconds", backoff)
            await asyncio.sleep(backoff)

        self.logger.debug("Connect loop ended")
        self._task = None

    async def _scan(self) -> ATVConf | None:
        """Try to find device by scanning for it."""
        address: str = self.discovery_info.address

        self.logger.debug("Discovering device %s", self.discovery_info.name)
        atvs = await scan(
            self.mass.loop,
            identifier=self.discovery_info.identifier,
            hosts=[address],
        )
        if atvs:
            return cast(ATVConf, atvs[0])

        self.logger.debug(
            "Failed to find device %s with address %s",
            self.discovery_info.name,
            address,
        )
        # We no longer multicast scan for the device since as soon as async_step_zeroconf runs,
        # it will update the address and reload the config entry when the device is found.
        return None

    async def _connect(self, conf: ATVConf) -> None:
        """Connect to device."""
        credentials: dict[int, str | None] = self.mass.config.get_raw_player_config_value(
            self.player_id, CONF_CREDENTIALS, {}
        )
        name: str = self.discovery_info.name
        missing_protocols = []
        for protocol_int, creds in credentials.items():
            protocol = Protocol(int(protocol_int))
            if conf.get_service(protocol) is not None:
                conf.set_credentials(protocol, creds)  # type: ignore[arg-type]
            else:
                missing_protocols.append(protocol.name)

        if missing_protocols:
            missing_protocols_str = ", ".join(missing_protocols)
            self.logger.info(
                "Protocol(s) %s not yet found for %s, trying later",
                missing_protocols_str,
                name,
            )
            return

        self.logger.debug("Connecting to device %s", name)
        session = self.mass.http_session

        self.atv = await connect(conf, self.mass.loop, session=session)
        self.connected = True
        self.atv.power.listener = self
        self.atv.listener = self
        self.atv.audio.listener = self
        if self.atv.features.in_state(
            interface.FeatureState.Available, interface.FeatureName.PushUpdates
        ):
            self.atv.push_updater.listener = self
            self.atv.push_updater.start()

        self._address_updated(str(conf.address))

        self._setup_device()
        self._update_attributes()

        self._connection_attempts = 0
        if self._connection_was_lost:
            self.logger.info(
                'Connection was re-established to device "%s"',
                name,
            )
            self._connection_was_lost = False

    def _setup_device(self):
        if not (mass_player := self.mass.players.get(self.player_id)):
            mass_player = Player(
                player_id=self.player_id,
                provider="airplay",
                type=PlayerType.PLAYER,
                name=self.discovery_info.name,
                available=True,
                powered=False,
                device_info=DeviceInfo(
                    model=self.discovery_info.device_info.raw_model,
                    manufacturer="Apple",
                    address=str(self.discovery_info.address),
                ),
                supported_features=(
                    PlayerFeature.PAUSE,
                    PlayerFeature.SYNC,
                    PlayerFeature.VOLUME_SET,
                    PlayerFeature.POWER,
                ),
                max_sample_rate=44100,
                supports_24bit=False,
            )
        if self.atv:
            dev_info = self.atv.device_info
            mass_player.device_info = DeviceInfo(
                model=(
                    dev_info.raw_model
                    if dev_info.model == DeviceModel.Unknown and dev_info.raw_model
                    else model_str(dev_info.model)
                ),
                manufacturer="Apple",
                address=str(self.discovery_info.address),
            )
        self.mass.players.register_or_update(mass_player)

    def playstatus_update(self, _, playstatus: interface.Playing) -> None:
        """Inform about changes to what is currently playing."""
        self.logger.debug("Playstatus received: %s", playstatus)
        self._playing = playstatus
        self._update_attributes()

    def playstatus_error(self, updater, exception: Exception) -> None:
        """Inform about an error when updating play status."""
        self.logger.debug("Playstatus error received", exc_info=exception)
        self._playing = None
        self._update_attributes()

    def powerstate_update(self, old_state: PowerState, new_state: PowerState) -> None:
        """Update power state when it changes."""
        self._update_attributes()

    def volume_update(self, old_level: float, new_level: float) -> None:
        """Update volume when it changes."""
        self._update_attributes()

    def _update_attributes(self) -> None:
        """Update the player attributes."""
        mass_player = self.mass.players.get(self.player_id)
        mass_player.volume_level = int(self.atv.audio.volume)
        if self.atv is None or not self.connected:
            mass_player.powered = False
            mass_player.state = PlayerState.IDLE
        if self._playing:
            state = self._playing.device_state
            if state in (DeviceState.Idle, DeviceState.Loading):
                mass_player.state = PlayerState.IDLE
            elif state == DeviceState.Playing:
                mass_player.state = PlayerState.PLAYING
            elif state in (DeviceState.Paused, DeviceState.Seeking, DeviceState.Stopped):
                mass_player.state = PlayerState.PAUSED
            else:
                mass_player.state = PlayerState.IDLE
            mass_player.elapsed_time = self._playing.position or 0
            mass_player.elapsed_time_last_updated = time.time()
            mass_player.current_item_id = self._playing.content_identifier
        else:
            mass_player.state = PlayerState.IDLE
        self.mass.players.update(self.player_id)

    @property
    def is_connecting(self):
        """Return true if connection is in progress."""
        return self._task is not None

    def _address_updated(self, address):
        """Update cached address in config entry."""
        self.logger.debug("Changing address to %s", address)
        self._setup_device()


class AirplayProvider(PlayerProvider):
    """Player provider for Airplay based players."""

    _atv_players: dict[str, AppleTVManager]
    _discovery_running: bool = False
    _raop_play_bin: str | None = None
    _stream_tasks: dict[str, asyncio.Task]

    async def handle_setup(self) -> None:
        """Handle async initialization of the provider."""
        self._atv_players = {}
        self._stream_tasks = {}
        self._raop_play_bin = await self.get_raop_play_binary()
        self.mass.create_task(self._run_discovery())
        dacp_port = 49831
        self.dacp_id = dacp_id = "1A2B3D4EA1B2C3D5"
        self.logger.info("Starting DACP ActiveRemote %s on port %s", dacp_id, dacp_port)
        await asyncio.start_server(self._handle_dacp_request, "0.0.0.0", dacp_port)
        zeroconf_type = "_dacp._tcp.local."
        server_id = f"iTunes_Ctrl_{dacp_id}.{zeroconf_type}"
        info = ServiceInfo(
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
        await self.mass.zeroconf.async_register_service(info)

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
            headers_raw, body = request.split("\r\n\r\n", 1)
            headers_raw = headers_raw.split("\r\n")
            headers = {}
            for line in headers_raw[1:]:
                x, y = line.split(":", 1)
                headers[x.strip()] = y.strip()
            active_remote = headers.get("Active-Remote")
            _, path, _ = headers_raw[0].split(" ")
            atv_player = next(
                (x for x in self._atv_players.values() if x.active_remote_id == active_remote), None
            )
            self.logger.debug(
                "DACP request for %s (%s): %s -- %s",
                atv_player.discovery_info.name if atv_player else "UNKNOWN PLAYER",
                active_remote,
                path,
                body,
            )
            if not atv_player:
                return

            player_id = atv_player.player_id
            if path == "/ctrl-int/1/nextitem":
                self.mass.create_task(self.mass.player_queues.next(player_id))
            elif path == "/ctrl-int/1/previtem":
                self.mass.create_task(self.mass.player_queues.previous(player_id))
            elif path == "/ctrl-int/1/play":
                self.mass.create_task(self.mass.player_queues.play(player_id))
            elif path == "/ctrl-int/1/playpause":
                self.mass.create_task(self.mass.player_queues.play_pause(player_id))
            elif path == "/ctrl-int/1/stop":
                self.mass.create_task(self.cmd_stop(player_id))
            elif path == "/ctrl-int/1/volumeup":
                self.mass.create_task(self.mass.players.cmd_volume_up(player_id))
            elif path == "/ctrl-int/1/volumedown":
                self.mass.create_task(self.mass.players.cmd_volume_down(player_id))
            elif path == "/ctrl-int/1/shuffle_songs":
                queue = self.mass.player_queues.get(player_id)
                self.mass.create_task(
                    self.mass.player_queues.set_shuffle(player_id, not queue.shuffle_enabled)
                )
            elif path in ("/ctrl-int/1/pause", "/ctrl-int/1/discrete-pause"):
                self.mass.create_task(self.mass.player_queues.pause(player_id))
            elif "dmcp.device-volume=" in path:
                raop_volume = float(path.split("dmcp.device-volume=", 1)[-1])
                volume = convert_airplay_volume(raop_volume)
                if abs(volume - int(atv_player.atv.audio.volume)) > 2:
                    self.mass.create_task(self.cmd_volume_set(player_id, volume))
            elif "dmcp.volume=" in path:
                volume = int(path.split("dmcp.volume=", 1)[-1])
                if abs(volume - int(atv_player.atv.audio.volume)) > 2:
                    self.mass.create_task(self.cmd_volume_set(player_id, volume))
            else:
                self.logger.warning(
                    "Unknown DACP request for %s: %s",
                    atv_player.discovery_info.name,
                    path,
                )

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

    async def get_player_config_entries(self, player_id: str) -> tuple[ConfigEntry, ...]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        entries = await super().get_player_config_entries(player_id)
        return entries + PLAYER_CONFIG_ENTRIES

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player.

        - player_id: player_id of the player to handle the command.
        """
        # forward command to player and any connected sync members
        for atv_player in self._get_sync_clients(player_id):
            if atv_player.raop_play_proc and not atv_player.raop_play_proc.closed:
                # prefer interactive command to our streamer
                await self._send_command(atv_player, "ACTION=STOP")
                mass_player = self.mass.players.get(player_id)
                mass_player.state = PlayerState.IDLE
                self.mass.players.update(player_id)
                await atv_player.raop_play_proc.communicate()
            elif atv := atv_player.atv:
                await atv.remote_control.stop()
            if atv_player.raop_play_proc and not atv_player.raop_play_proc.closed:
                await atv_player.raop_play_proc.close()
        if stream_task := self._stream_tasks.pop(player_id, None):
            if not stream_task.done():
                stream_task.cancel()

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY (unpause) command to given player.

        - player_id: player_id of the player to handle the command.
        """
        # forward command to player and any connected sync members
        for atv_player in self._get_sync_clients(player_id):
            if atv_player.raop_play_proc:
                # prefer interactive command to our streamer
                await self._send_command(atv_player, "ACTION=PLAY")
                mass_player = self.mass.players.get(player_id)
                mass_player.state = PlayerState.PLAYING
                self.mass.players.update(player_id)
            elif atv := atv_player.atv:
                await atv.remote_control.play()

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player.

        - player_id: player_id of the player to handle the command.
        """
        # forward command to player and any connected sync members
        for atv_player in self._get_sync_clients(player_id):
            if atv_player.raop_play_proc:
                # prefer interactive command to our streamer
                await self._send_command(atv_player, "ACTION=PAUSE")
                mass_player = self.mass.players.get(player_id)
                mass_player.state = PlayerState.PAUSED
                self.mass.players.update(player_id)
            elif atv := atv_player.atv:
                await atv.remote_control.pause()

    async def play_media(  # noqa: PLR0915
        self,
        player_id: str,
        queue_item: QueueItem,
        seek_position: int,
        fade_in: bool,
    ) -> None:
        """Handle PLAY MEDIA on given player.

        This is called by the Queue controller to start playing a queue item on the given player.
        The provider's own implementation should work out how to handle this request.

            - player_id: player_id of the player to handle the command.
            - queue_item: The QueueItem that needs to be played on the player.
            - seek_position: Optional seek to this position.
            - fade_in: Optionally fade in the item at playback start.
        """
        # stop any existing streams first
        await self.cmd_stop(player_id)
        await self.cmd_power(player_id, True)
        atv_player = self._atv_players[player_id]
        player = self.mass.players.get(player_id)

        if player.synced_to:
            # should not happen, but just in case
            raise RuntimeError("Player is synced")

        ntp_file = "/tmp/start.ntp"  # noqa: S108
        ntp_cmd = f"{self._raop_play_bin} -ntp {ntp_file}"
        await check_output(ntp_cmd)

        # setup Raop process for player and its sync childs
        for atv_player in self._get_sync_clients(player_id):
            stream: RaopStream | None = next(
                (x for x in atv_player.atv.stream.instances if isinstance(x, RaopStream)), None
            )
            if stream is None:
                raise RuntimeError("RAOP Not available")

            args = [
                self._raop_play_bin,
                "-nf",
                ntp_file,
                "-w",
                "1200",
                "-p",
                str(stream.core.service.port),
                "-v",
                str(atv_player.atv.audio.volume),
                "-a",
                "-dacp",
                self.dacp_id,
                "-ar",
                atv_player.active_remote_id,
                "-md",
                atv_player.discovery_info.properties["_raop._tcp.local"]["md"],
                "-et",
                atv_player.discovery_info.properties["_raop._tcp.local"]["et"],
                str(atv_player.discovery_info.address),
                "-",
            ]
            if platform.system() == "Darwin":
                os.environ["DYLD_LIBRARY_PATH"] = "/usr/local/lib"
            atv_player.raop_play_proc = AsyncProcess(args, enable_stdin=True, enable_stdout=False)
            await atv_player.raop_play_proc.start()

        async def _streamer() -> None:
            queue = self.mass.player_queues.get(queue_item.queue_id)
            player.current_item_id = f"{queue_item.queue_id}.{queue_item.queue_item_id}"
            player.elapsed_time = 0
            player.elapsed_time_last_updated = time.time()
            player.state = PlayerState.PLAYING
            self.mass.players.register_or_update(player)
            prev_item: QueueItem | None = None
            # TODO: can we handle 24 bits bit depth ?
            pcm_format = AudioFormat(
                content_type=ContentType.PCM_S16LE,
                sample_rate=44100,
                bit_depth=16,
                channels=2,
            )
            try:
                async for pcm_chunk in self.mass.streams.get_flow_stream(
                    queue,
                    start_queue_item=queue_item,
                    pcm_format=pcm_format,
                    seek_position=seek_position,
                    fade_in=fade_in,
                ):
                    async with asyncio.TaskGroup() as tg:
                        # send metadata to player(s) if needed
                        if prev_item != queue.current_item:
                            prev_item = queue.current_item
                            if isinstance(queue.current_item.media_item, Track):
                                artist = queue.current_item.media_item.artist_str
                                album = queue.current_item.media_item.album.name
                                title = queue.current_item.media_item.name
                            elif (
                                queue.current_item.streamdetails
                                and queue.current_item.streamdetails.stream_title
                            ):
                                artist = queue.current_item.name
                                album = ""
                                title = queue.current_item.streamdetails.stream_title
                            else:
                                artist = ""
                                album = ""
                                title = queue.current_item.name
                            duration = queue_item.duration or 0 * 1000
                            for atv_player in self._get_sync_clients(player_id):
                                tg.create_task(
                                    self._send_command(
                                        atv_player,
                                        f"TITLE={title}\nARTIST={artist}\nALBUM={album}\n"
                                        f"DURATION={duration * 1000}\nACTION=SENDMETA",
                                    )
                                )
                            if queue_item.image:
                                image_path = create_tempfile()
                                image_data = await self.mass.metadata.get_thumbnail(
                                    queue_item.image.path, 512, queue_item.image.provider
                                )
                                async with aiofiles.open(image_path.name, "wb") as outfile:
                                    await outfile.write(image_data)
                                for atv_player in self._get_sync_clients(player_id):
                                    tg.create_task(
                                        self._send_command(
                                            atv_player, f"ARTWORK={image_path.name}\n"
                                        )
                                    )

                        # send audio chunk to player(s)
                        for atv_player in self._get_sync_clients(player_id):
                            tg.create_task(atv_player.raop_play_proc.write(pcm_chunk))
            finally:
                await self.cmd_stop(player_id)

        # start streaming the queue (pcm) audio in a background task
        self._stream_tasks[player_id] = asyncio.create_task(_streamer())

    async def play_media_org(
        self,
        player_id: str,
        queue_item: QueueItem,
        seek_position: int,
        fade_in: bool,
    ) -> None:
        """Handle PLAY MEDIA using pyatv itself."""
        await self.cmd_power(player_id, True)
        atv_player = self._atv_players[player_id]
        player = self.mass.players.get(player_id)

        if player.synced_to:
            # should not happen, but just in case
            raise RuntimeError("Player is synced")

        # regular, single player playback
        url = await self.mass.streams.resolve_stream_url(
            queue_item=queue_item,
            output_codec=ContentType.FLAC,
            seek_position=seek_position,
            fade_in=fade_in,
            flow_mode=False,
        )

        async def streamer():
            stream: RaopStream | None = next(
                (x for x in atv_player.atv.stream.instances if isinstance(x, RaopStream)), None
            )
            if stream is None:
                raise RuntimeError("RAOP Not available")

            stream.playback_manager.acquire()
            takeover_release = stream.core.takeover(Audio, Metadata, PushUpdater, RemoteControl)
            try:
                client, context = await stream.playback_manager.setup(stream.core.service)
                context.credentials = extract_credentials(stream.core.service)
                context.password = stream.core.service.password

                client.listener = stream.listener
                await client.initialize(stream.core.service.properties)

                # After initialize has been called, all the audio properties will be
                # initialized and can be used in the miniaudio wrapper
                # audio_file = await open_source(
                #     file,
                #     context.sample_rate,
                #     context.channels,
                #     context.bytes_per_channel,
                # )

                # If the user didn't change volume level prior to streaming, try to extract
                # volume level from device (if supported). Otherwise set the default level
                # in pyatv.
                volume = None
                if not stream.audio.has_changed_volume and "initialVolume" in client.info:
                    initial_volume = client.info["initialVolume"]
                    if not isinstance(initial_volume, float):
                        msg = (
                            f"initial volume {initial_volume} has "
                            "incorrect type {type(initial_volume)}"
                        )
                        raise exceptions.ProtocolError(msg)
                    context.volume = initial_volume
                else:
                    # Try to set volume. If it fails, defer to setting it once
                    # streaming has started.
                    try:
                        await stream.audio.set_volume(self.audio.volume)
                    except Exception as ex:
                        self.logger.debug("Failed to set volume (%s), delaying call", ex)
                        volume = stream.audio.volume
                from pyatv.protocols.raop import open_source

                audio_file = await open_source(url, 44100, 2, 2)
                img_data = await self.mass.metadata.get_thumbnail(
                    queue_item.image.path, 256, queue_item.image.provider
                )
                metadata = interface.MediaMetadata(
                    title=queue_item.name, artwork=img_data, duration=300
                )
                await client.send_audio(audio_file, metadata, volume=volume)

            finally:
                takeover_release()
                # if audio_file:
                #     await audio_file.close()
                await stream.playback_manager.teardown()

        if (existing := self._stream_tasks.get(player_id)) and not existing.done():
            existing.cancel()
            await asyncio.sleep(0.5)

        self._stream_tasks[player_id] = asyncio.create_task(streamer())

    async def play_stream(self, player_id: str, stream_job: MultiClientStreamJob) -> None:
        """Handle PLAY STREAM on given player.

        This is a special feature from the Universal Group provider.
        """
        raise NotImplementedError

    async def cmd_power(self, player_id: str, powered: bool) -> None:
        """Send POWER command to given player.

        - player_id: player_id of the player to handle the command.
        - powered: bool if player should be powered on or off.
        """
        atv_player = self._atv_players[player_id]
        mass_player = self.mass.players.get(player_id)
        if powered:
            await atv_player.connect()
        elif not powered:
            await self.cmd_stop(player_id)
            await atv_player.disconnect()
        mass_player.powered = powered
        self.mass.players.update(player_id)

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player.

        - player_id: player_id of the player to handle the command.
        - volume_level: volume level (0..100) to set on the player.
        """
        atv_player = self._atv_players[player_id]
        if atv_player.raop_play_proc:
            # prefer interactive command to our streamer
            await self._send_command(atv_player, f"VOLUME={volume_level}")
        elif atv := atv_player.atv:
            await atv.audio.set_volume(volume_level)

    async def cmd_seek(self, player_id: str, position: int) -> None:
        """Handle SEEK command for given queue.

        - player_id: player_id of the player to handle the command.
        - position: position in seconds to seek to in the current playing item.
        """
        atv_player = self._atv_players[player_id]
        if atv := atv_player.atv:
            await atv.remote_control.set_position(round(position))

    async def cmd_sync(self, player_id: str, target_player: str) -> None:
        """Handle SYNC command for given player.

        Join/add the given player(id) to the given (master) player/sync group.

            - player_id: player_id of the player to handle the command.
            - target_player: player_id of the syncgroup master or group player.
        """
        player = self.mass.players.get(player_id, raise_unavailable=True)
        group_leader = self.mass.players.get(target_player, raise_unavailable=True)
        if group_leader.synced_to:
            raise RuntimeError("Player is already synced")
        player.synced_to = target_player
        group_leader.group_childs.add(player_id)
        self.mass.players.update(target_player)
        if (
            group_leader.state == PlayerState.PLAYING
            and group_leader.active_source == group_leader.player_id
        ):
            await self.mass.player_queues.resume(group_leader.player_id)

    async def cmd_unsync(self, player_id: str) -> None:
        """Handle UNSYNC command for given player.

        Remove the given player from any syncgroups it currently is synced to.

            - player_id: player_id of the player to handle the command.
        """
        player = self.mass.players.get(player_id, raise_unavailable=True)
        if not player.synced_to:
            return
        group_leader = self.mass.players.get(player.synced_to, raise_unavailable=True)
        group_leader.group_childs.remove(player_id)
        player.synced_to = None
        if player.state == PlayerState.PLAYING:
            await self.cmd_stop(player_id)
        self.mass.players.update(player_id)

    async def poll_player(self, player_id: str) -> None:
        """Poll player for state updates.

        This is called by the Player Manager;
        - every 360 seconds if the player if not powered
        - every 30 seconds if the player is powered
        - every 10 seconds if the player is playing

        Use this method to request any info that is not automatically updated and/or
        to detect if the player is still alive.
        If this method raises the PlayerUnavailable exception,
        the player is marked as unavailable until
        the next successful poll or event where it becomes available again.
        If the player does not need any polling, simply do not override this method.
        """

    async def _run_discovery(self) -> None:
        """Discover Airplay players on the network."""
        if self._discovery_running:
            return
        try:
            self._discovery_running = True
            self.logger.debug("Airplay discovery started...")
            discovered_devices = await scan(self.mass.loop, timeout=5)

            if not discovered_devices:
                self.logger.debug("No devices found")
                return

            for dev in discovered_devices:
                self.mass.create_task(self._player_discovered(dev))

        finally:
            self._discovery_running = False

        def reschedule():
            self.mass.create_task(self._run_discovery())

        # reschedule self once finished
        self.mass.loop.call_later(300, reschedule)

    async def _player_discovered(self, discovery_info: interface.BaseConfig) -> None:
        """Handle discovered Airplay player on mdns."""
        player_id = f"ap{discovery_info.identifier.lower().replace(":", "")}"
        if player_id in self._atv_players:
            return  # TODO: handle player updates
        self.logger.info(f"Connecting to {discovery_info.address}")
        self._atv_players[player_id] = atv_player = AppleTVManager(
            self.mass, player_id, discovery_info
        )
        atv_player._setup_device()
        for player in self.players:
            player.can_sync_with = tuple(x for x in self._atv_players if x != player.player_id)
            self.mass.players.update(player.player_id)

    def on_child_power(self, player_id: str, child_player_id: str, new_power: bool) -> None:
        """
        Call when a power command was executed on one of the child player of a Player/Sync group.

        This is used to handle special actions such as (re)syncing.
        """

    def _is_feature_available(
        self, atv_player: interface.AppleTV, feature: interface.FeatureName
    ) -> bool:
        """Return if a feature is available."""
        mass_player = self.mass.players.get(atv_player.device_info.output_device_id)
        if atv_player and mass_player.state == PlayerState.PLAYING:
            return atv_player.features.in_state(interface.FeatureState.Available, feature)
        return False

    async def get_raop_play_binary(self):
        """Find the correct raop/airplay binary belonging to the platform."""
        # ruff: noqa: SIM102
        if self._raop_play_bin is not None:
            return self._raop_play_bin

        async def check_binary(raop_play_path: str) -> str | None:
            try:
                raop_play = await asyncio.create_subprocess_exec(
                    *[raop_play_path], stdout=asyncio.subprocess.PIPE
                )
                stdout, _ = await raop_play.communicate()
                self.logger.debug(stdout.decode("utf-8"))
                if raop_play.returncode == 255:
                    self._raop_play_bin = raop_play_path
                    return raop_play_path
            except OSError:
                return None

        base_path = os.path.join(os.path.dirname(__file__), "bin")
        system = platform.system().lower().replace("darwin", "macos")
        architecture = platform.machine().lower()

        if bridge_binary := await check_binary(
            os.path.join(base_path, f"raop_play-{system}-{architecture}")
        ):
            return bridge_binary

        msg = f"Unable to locate RAOP Play binary for {system}/{architecture}"
        raise RuntimeError(msg)

    def _get_sync_clients(self, player_id: str) -> list[AppleTVManager]:
        """Get all sync clients for a player."""
        mass_player = self.mass.players.get(player_id, True)
        sync_clients: list[AppleTVManager] = []
        # we need to return the player itself too
        group_child_ids = {player_id}
        group_child_ids.update(mass_player.group_childs)
        for child_id in group_child_ids:
            if client := self._atv_players.get(child_id):
                sync_clients.append(client)
        return sync_clients

    async def _send_command(self, atv_player: AppleTVManager, command: str) -> None:
        """Send an interactive command to the running Raop Play binary."""
        if not atv_player.raop_play_proc or atv_player.raop_play_proc.closed:
            return

        named_pipe = f"/tmp/fifo-{atv_player.active_remote_id}"  # noqa: S108
        if not command.endswith("\n"):
            command += "\n"

        def send_data():
            with open(named_pipe, "w") as f:
                f.write(command)
                f.flush()

        await self.mass.create_task(send_data)
