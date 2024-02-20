"""Airplay Player provider for Music Assistant."""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import socket
import time
from collections.abc import AsyncGenerator
from random import randint, randrange
from typing import TYPE_CHECKING, cast

from pyatv import connect, interface, scan
from pyatv.conf import AppleTV as ATVConf
from pyatv.const import DeviceModel, DeviceState, PowerState, Protocol
from pyatv.convert import model_str
from pyatv.interface import AppleTV as AppleTVInterface
from pyatv.interface import DeviceListener
from pyatv.protocols.raop import RaopStream
from zeroconf.asyncio import AsyncServiceInfo

from music_assistant.common.helpers.datetime import utc
from music_assistant.common.helpers.util import get_ip_pton, select_free_port
from music_assistant.common.models.config_entries import (
    CONF_ENTRY_CROSSFADE,
    CONF_ENTRY_CROSSFADE_DURATION,
    ConfigEntry,
    ConfigValueType,
)
from music_assistant.common.models.enums import (
    ConfigEntryType,
    ContentType,
    PlayerFeature,
    PlayerState,
    PlayerType,
    ProviderFeature,
)
from music_assistant.common.models.media_items import AudioFormat
from music_assistant.common.models.player import DeviceInfo, Player
from music_assistant.common.models.player_queue import PlayerQueue
from music_assistant.server.helpers.process import AsyncProcess, check_output
from music_assistant.server.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.common.models.queue_item import QueueItem
    from music_assistant.server import MusicAssistant
    from music_assistant.server.controllers.streams import MultiClientStreamJob
    from music_assistant.server.models import ProviderInstanceType

DOMAIN = "airplay"

CONF_LATENCY = "latency"
CONF_ENCRYPTION = "encryption"
CONF_ALAC_ENCODE = "alac_encode"
CONF_VOLUME_START = "volume_start"
CONF_SYNC_ADJUST = "sync_adjust"
CONF_PASSWORD = "password"
PLAYER_CONFIG_ENTRIES = (
    CONF_ENTRY_CROSSFADE,
    CONF_ENTRY_CROSSFADE_DURATION,
    ConfigEntry(
        key=CONF_LATENCY,
        type=ConfigEntryType.INTEGER,
        range=(200, 3000),
        default_value=1200,
        label="Latency",
        description="Sets the number of milliseconds of audio buffer in the player. "
        "This is important to absorb network throughput jitter. "
        "Note that the resume after pause will be skipping that amount of time "
        "and volume changes will be delayed by the same amount, when using digital volume.",
        advanced=True,
    ),
    ConfigEntry(
        key=CONF_ENCRYPTION,
        type=ConfigEntryType.BOOLEAN,
        default_value=False,
        label="Enable encryption",
        description="Enable encrypted communication with the player, "
        "some (3rd party) players require this.",
        advanced=True,
    ),
    ConfigEntry(
        key=CONF_ALAC_ENCODE,
        type=ConfigEntryType.BOOLEAN,
        default_value=True,
        label="Enable compression",
        description="Save some network bandwidth by sending the audio as "
        "(lossless) ALAC at the cost of a bit CPU.",
        advanced=True,
    ),
    ConfigEntry(
        key=CONF_VOLUME_START,
        type=ConfigEntryType.BOOLEAN,
        default_value=False,
        label="Send volume at playback start",
        description="Some players require to send/confirm the volume when playback starts. \n"
        "Enable this setting if the playback volume does not match the MA interface.",
        advanced=True,
    ),
    ConfigEntry(
        key=CONF_SYNC_ADJUST,
        type=ConfigEntryType.INTEGER,
        range=(-500, 500),
        default_value=0,
        label="Audio synchronization delay correction",
        description="If this player is playing audio synced with other players "
        "and you always hear the audio too early or late on this player, "
        "you can shift the audio a bit.",
        advanced=True,
    ),
    ConfigEntry(
        key=CONF_PASSWORD,
        type=ConfigEntryType.STRING,
        default_value=None,
        required=False,
        label="Device password",
        description="Some devices require a password to connect/play.",
        advanced=True,
    ),
)
BACKOFF_TIME_LOWER_LIMIT = 15  # seconds
BACKOFF_TIME_UPPER_LIMIT = 300  # Five minutes

CONF_CREDENTIALS = "credentials"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = AirplayProvider(mass, manifest, config)
    await prov.handle_async_init()
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


class AirPlayPlayer(DeviceListener):
    """Holds the connection to the apyatv instance and the cliraop."""

    def __init__(
        self, mass: MusicAssistant, player_id: str, discovery_info: interface.BaseConfig
    ) -> None:
        """Initialize power manager."""
        self.player_id = player_id
        self.discovery_info = discovery_info
        self.mass = mass
        self.atv: AppleTVInterface | None = None
        self.connected = False
        self._connection_attempts = 0
        self._connection_was_lost = False
        self._playing: interface.Playing | None = None
        self.logger = self.mass.players.logger.getChild("airplay").getChild(self.player_id)
        self.cliraop_proc: AsyncProcess | None = None
        self.active_remote_id = str(randint(1000, 8000))
        self.optimistic_state: PlayerState = PlayerState.IDLE

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

    async def connect(self):
        """Connect to device."""
        if self.connected:
            return
        try:
            await self._connect(self.discovery_info)
        except Exception:
            # retry with scanning for the device
            if conf := await self._scan():
                await self._connect(conf)
            raise

    async def disconnect(self):
        """Disconnect from device."""
        self.logger.debug("Disconnecting from device")
        self.is_on = False
        self.connected = False
        try:
            if self.atv:
                self.atv.close()
                self.atv = None
        except Exception:  # pylint: disable=broad-except
            self.logger.exception("An error occurred while disconnecting")

    async def stop(self):
        """Stop playback and cleanup any running CLIRaop Process."""
        if self.cliraop_proc and not self.cliraop_proc.closed:
            # prefer interactive command to our streamer
            await self.send_cli_command("ACTION=STOP")
            await self.cliraop_proc.wait()
            self.optimistic_state = PlayerState.IDLE
            self.update_attributes()
        elif atv := self.atv:
            await atv.remote_control.stop()

    async def send_cli_command(self, command: str) -> None:
        """Send an interactive command to the running CLIRaop binary."""
        if not self.cliraop_proc or self.cliraop_proc.closed:
            return

        named_pipe = f"/tmp/fifo-{self.active_remote_id}"  # noqa: S108
        if not command.endswith("\n"):
            command += "\n"

        def send_data():
            with open(named_pipe, "w") as f:
                f.write(command)

        self.logger.debug("sending command %s", command)
        await self.mass.create_task(send_data)

    async def _scan(self) -> ATVConf | None:
        """Try to find device by scanning for it."""
        address: str = self.discovery_info.address

        self.logger.debug("Discovering device %s", self.discovery_info.name)
        atvs = await scan(
            self.mass.loop,
            identifier=self.discovery_info.identifier,
            aiozc=self.mass.aiozc,
            hosts=[address],
        )
        if atvs:
            return cast(ATVConf, atvs[0])

        self.logger.debug(
            "Failed to find device %s with address %s",
            self.discovery_info.name,
            address,
        )
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

        self.address_updated(str(conf.address))

        self._setup_device()
        self.update_attributes()

        self._connection_attempts = 0
        if self._connection_was_lost:
            self.logger.info(
                'Connection was (re)established to device "%s"',
                name,
            )
            self._connection_was_lost = False

    def _setup_device(self):
        if not (mass_player := self.mass.players.get(self.player_id)):
            mass_player = Player(
                player_id=self.player_id,
                provider=DOMAIN,
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
        self.update_attributes()

    def playstatus_error(self, updater, exception: Exception) -> None:
        """Inform about an error when updating play status."""
        self.logger.debug("Playstatus error received", exc_info=exception)
        self._playing = None
        self.update_attributes()

    def powerstate_update(self, old_state: PowerState, new_state: PowerState) -> None:
        """Update power state when it changes."""
        self.update_attributes()

    def volume_update(self, old_level: float, new_level: float) -> None:
        """Update volume when it changes."""
        self.update_attributes()

    def update_attributes(self) -> None:
        """Update the player attributes."""
        mass_player = self.mass.players.get(self.player_id)
        mass_player.volume_level = int(self.atv.audio.volume)
        mass_player.powered = self.connected or self.cliraop_proc and not self.cliraop_proc.closed
        if self.cliraop_proc and not self.cliraop_proc.closed:
            mass_player.state = self.optimistic_state
            # NOTE: alapsed time is pushed from cliraop
        elif self.atv is None or not self.connected:
            mass_player.powered = False
            mass_player.state = PlayerState.IDLE
        elif self._playing:
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

    def address_updated(self, address):
        """Update cached address in config entry."""
        self.logger.debug("Changing address to %s", address)
        self._setup_device()


class AirplayProvider(PlayerProvider):
    """Player provider for Airplay based players."""

    _atv_players: dict[str, AirPlayPlayer]
    _discovery_running: bool = False
    _cliraop_bin: str | None = None
    _stream_tasks: dict[str, asyncio.Task]
    _dacp_server: asyncio.Server = None
    _dacp_info: AsyncServiceInfo = None

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return (ProviderFeature.SYNC_PLAYERS,)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._atv_players = {}
        self._stream_tasks = {}
        self._cliraop_bin = await self.get_cliraop_binary()
        dacp_port = await select_free_port(39831, 49831)
        # the pyatv logger is way to noisy, silence it a bit
        logging.getLogger("pyatv").setLevel(self.logger.level + 10)
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

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await self._run_discovery()

    async def unload(self) -> None:
        """Handle close/cleanup of the provider."""
        # power off all players (will disconnct and close cliraop)
        for player_id in self._atv_players:
            await self.cmd_power(player_id, False)
        # shutdown DACP server
        if self._dacp_server:
            self._dacp_server.close()
        # shutdown DACP zeroconf service
        if self._dacp_info:
            await self.mass.aiozc.async_unregister_service(self._dacp_info)

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
                self.logger.debug(
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
        if stream_task := self._stream_tasks.pop(player_id, None):
            if not stream_task.done():
                stream_task.cancel()

        # forward command to player and any connected sync members
        async with asyncio.TaskGroup() as tg:
            for atv_player in self._get_sync_clients(player_id):
                tg.create_task(atv_player.stop())

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY (unpause) command to given player.

        - player_id: player_id of the player to handle the command.
        """
        # forward command to player and any connected sync members
        async with asyncio.TaskGroup() as tg:
            for atv_player in self._get_sync_clients(player_id):
                if atv_player.cliraop_proc and not atv_player.cliraop_proc.closed:
                    # prefer interactive command to our streamer
                    tg.create_task(atv_player.send_cli_command("ACTION=PLAY"))
                    atv_player.optimistic_state = PlayerState.PLAYING
                    atv_player.update_attributes()
                elif atv := atv_player.atv:
                    tg.create_task(atv.remote_control.play())

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player.

        - player_id: player_id of the player to handle the command.
        """
        # forward command to player and any connected sync members
        async with asyncio.TaskGroup() as tg:
            for atv_player in self._get_sync_clients(player_id):
                if atv_player.cliraop_proc and not atv_player.cliraop_proc.closed:
                    # prefer interactive command to our streamer
                    tg.create_task(atv_player.send_cli_command("ACTION=PAUSE"))
                    atv_player.optimistic_state = PlayerState.PAUSED
                    atv_player.update_attributes()
                elif atv := atv_player.atv:
                    tg.create_task(atv.remote_control.pause())

    async def play_media(
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
        # stop existing streams first
        await self.cmd_stop(player_id)
        # power on player if needed
        await self.cmd_power(player_id, True)
        # start streaming the queue (pcm) audio in a background task
        queue = self.mass.player_queues.get_active_queue(player_id)
        self._stream_tasks[player_id] = asyncio.create_task(
            self._stream_audio(
                player_id,
                queue=queue,
                audio_iterator=self.mass.streams.get_flow_stream(
                    queue,
                    start_queue_item=queue_item,
                    pcm_format=AudioFormat(
                        content_type=ContentType.PCM_S16LE,
                        sample_rate=44100,
                        bit_depth=16,
                        channels=2,
                    ),
                    seek_position=seek_position,
                    fade_in=fade_in,
                ),
            )
        )

    async def play_stream(self, player_id: str, stream_job: MultiClientStreamJob) -> None:
        """Handle PLAY STREAM on given player.

        This is a special feature from the Universal Group provider.
        """
        # stop existing streams first
        await self.cmd_stop(player_id)
        # power on player if needed
        await self.cmd_power(player_id, True)
        if stream_job.pcm_format.bit_depth != 16 or stream_job.pcm_format.sample_rate != 44100:
            # TODO: resample on the fly here ?
            raise RuntimeError("Unsupported PCM format")
        # start streaming the queue (pcm) audio in a background task
        queue = self.mass.player_queues.get_active_queue(player_id)
        self._stream_tasks[player_id] = asyncio.create_task(
            self._stream_audio(
                player_id,
                queue=queue,
                audio_iterator=stream_job.subscribe(player_id),
            )
        )

    async def _stream_audio(
        self, player_id: str, queue: PlayerQueue, audio_iterator: AsyncGenerator[bytes, None]
    ) -> None:
        """Handle the actual streaming of audio to Airplay."""
        player = self.mass.players.get(player_id)
        if player.synced_to:
            # should not happen, but just in case
            raise RuntimeError("Player is synced")
        player.elapsed_time = 0
        player.elapsed_time_last_updated = time.time()
        player.state = PlayerState.PLAYING
        self.mass.players.update(player_id)
        # NOTE: Although the pyatv library is perfectly capable of playback
        # to not only raop targets but also airplay 1 + 2, its not suitable
        # for synced playback to multiple clients at once.
        # Also the performance is horrible. Python is not suitable for realtime
        # audio streaming.
        # So, I've decided to a combined route here. I've created a small binary
        # written in C based on libraop to do the actual timestamped playback.
        # the raw pcm audio is fed to the stdin of this cliraop binary and we can
        # send some commands over a named pipe.

        # get current ntp before we start
        _, stdout = await check_output(f"{self._cliraop_bin} -ntp")
        ntp = int(stdout.strip())

        # setup Raop process for player and its sync childs
        async with asyncio.TaskGroup() as tg:
            for atv_player in self._get_sync_clients(player_id):
                if not atv_player.atv:
                    # should not be possible, but just in case...
                    await atv_player.connect()
                tg.create_task(self._init_cliraop(atv_player, ntp))
        prev_metadata_checksum: str = ""
        try:
            async for pcm_chunk in audio_iterator:
                # send metadata to player(s) if needed
                # NOTE: this must all be done in separate tasks to not disturb audio
                if queue and queue.current_item and queue.current_item.streamdetails:
                    metadata_checksum = (
                        queue.current_item.streamdetails.stream_title
                        or queue.current_item.queue_item_id
                    )
                    if prev_metadata_checksum != metadata_checksum:
                        prev_metadata_checksum = metadata_checksum
                        self.mass.create_task(self._send_metadata(player_id))

                async with asyncio.TaskGroup() as tg:
                    # send progress metadata
                    if queue.elapsed_time:
                        for atv_player in self._get_sync_clients(player_id):
                            tg.create_task(
                                atv_player.send_cli_command(f"PROGRESS={int(queue.elapsed_time)}\n")
                            )
                    # send audio chunk to player(s)
                    available_clients = 0
                    for atv_player in self._get_sync_clients(player_id):
                        if not atv_player.cliraop_proc or atv_player.cliraop_proc.closed:
                            # this may not happen, but just in case
                            continue
                        available_clients += 1
                        tg.create_task(atv_player.cliraop_proc.write(pcm_chunk))
                    if not available_clients:
                        return
        finally:
            self.logger.debug("Streaming ended for player %s", player.display_name)
            for atv_player in self._get_sync_clients(player_id):
                if atv_player.cliraop_proc and not atv_player.cliraop_proc.closed:
                    atv_player.cliraop_proc.write_eof()

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
        if atv_player.cliraop_proc:
            # prefer interactive command to our streamer
            await atv_player.send_cli_command(f"VOLUME={volume_level}\n")
        if atv := atv_player.atv:
            await atv.audio.set_volume(volume_level)

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
        if group_leader.powered:
            await self.cmd_power(player_id, True)
        active_queue = self.mass.player_queues.get_active_queue(group_leader.player_id)
        if active_queue.state == PlayerState.PLAYING:
            self.mass.create_task(self.mass.player_queues.resume(active_queue.queue_id))

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
        await self.cmd_stop(player_id)
        self.mass.players.update(player_id)

    async def _run_discovery(self) -> None:
        """Discover Airplay players on the network."""
        if self._discovery_running:
            return
        try:
            self._discovery_running = True
            self.logger.debug("Airplay discovery started...")
            discovered_devices = await scan(self.mass.loop, protocol=Protocol.RAOP, timeout=30)

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
        player_id = f"ap{discovery_info.identifier.lower().replace(':', '')}"
        if player_id in self._atv_players:
            atv_player = self._atv_players[player_id]
            if discovery_info.address != atv_player.discovery_info.address:
                atv_player.address_updated(discovery_info.address)
            return
        if "_raop._tcp.local" not in discovery_info.properties:
            # skip players without raop
            return
        self.logger.debug(
            "Discovered Airplay device %s on %s", discovery_info.name, discovery_info.address
        )
        self._atv_players[player_id] = atv_player = AirPlayPlayer(
            self.mass, player_id, discovery_info
        )
        atv_player._setup_device()
        for player in self.players:
            player.can_sync_with = tuple(x for x in self._atv_players if x != player.player_id)
            self.mass.players.update(player.player_id)

    def _is_feature_available(
        self, atv_player: interface.AppleTV, feature: interface.FeatureName
    ) -> bool:
        """Return if a feature is available."""
        mass_player = self.mass.players.get(atv_player.device_info.output_device_id)
        if atv_player and mass_player.state == PlayerState.PLAYING:
            return atv_player.features.in_state(interface.FeatureState.Available, feature)
        return False

    async def get_cliraop_binary(self):
        """Find the correct raop/airplay binary belonging to the platform."""
        # ruff: noqa: SIM102
        if self._cliraop_bin is not None:
            return self._cliraop_bin

        async def check_binary(cliraop_path: str) -> str | None:
            try:
                cliraop = await asyncio.create_subprocess_exec(
                    *[cliraop_path, "-check"],
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                stdout, _ = await cliraop.communicate()
                stdout = stdout.strip().decode()
                if cliraop.returncode == 0 and stdout == "cliraop check":
                    self._cliraop_bin = cliraop_path
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
            if client := self._atv_players.get(child_id):
                sync_clients.append(client)
        return sync_clients

    async def _init_cliraop(self, atv_player: AirPlayPlayer, ntp: int) -> None:  # noqa: PLR0915
        """Initiatlize CLIRaop process for a player."""
        stream: RaopStream | None = next(
            (x for x in atv_player.atv.stream.instances if isinstance(x, RaopStream)), None
        )
        if stream is None:
            raise RuntimeError("RAOP Not available")

        async def log_watcher(cliraop_proc: AsyncProcess) -> None:
            """Monitor stderr for a running CLIRaop process."""
            mass_player = self.mass.players.get(atv_player.player_id)
            logger = self.logger.getChild(atv_player.player_id)
            async for line in cliraop_proc._proc.stderr:
                line = line.decode().strip()  # noqa: PLW2901
                if not line:
                    continue
                if "set pause" in line:
                    atv_player.optimistic_state = PlayerState.PAUSED
                    atv_player.update_attributes()
                    logger.info(line)
                elif "Restarted at" in line:
                    atv_player.optimistic_state = PlayerState.PLAYING
                    atv_player.update_attributes()
                    logger.info(line)
                elif "after start), played" in line:
                    millis = int(line.split("played ")[1].split(" ")[0])
                    mass_player.elapsed_time = millis / 1000
                    mass_player.elapsed_time_last_updated = time.time()
                else:
                    logger.debug(line)
            # if we reach this point, the process exited
            if cliraop_proc._proc.returncode is not None:
                cliraop_proc.closed = True
            atv_player.optimistic_state = PlayerState.IDLE
            atv_player.update_attributes()
            logger.debug(
                "CLIRaop process stopped with errorcode %s",
                cliraop_proc._proc.returncode,
            )

        extra_args = []
        latency = self.mass.config.get_raw_player_config_value(
            atv_player.player_id, CONF_LATENCY, 1200
        )
        extra_args += ["-l", str(latency)]
        if self.mass.config.get_raw_player_config_value(
            atv_player.player_id, CONF_ENCRYPTION, False
        ):
            extra_args += ["-u"]
        if self.mass.config.get_raw_player_config_value(
            atv_player.player_id, CONF_ALAC_ENCODE, True
        ):
            extra_args += ["-a"]
        if self.mass.config.get_raw_player_config_value(
            atv_player.player_id, CONF_VOLUME_START, False
        ):
            extra_args += ["-v", str(int(atv_player.atv.audio.volume))]
        sync_adjust = self.mass.config.get_raw_player_config_value(
            atv_player.player_id, CONF_SYNC_ADJUST, 0
        )
        if device_password := self.mass.config.get_raw_player_config_value(
            atv_player.player_id, CONF_PASSWORD, None
        ):
            extra_args += ["-P", device_password]
        if self.logger.level == logging.DEBUG:
            extra_args += ["-d", "5"]

        atv_player.optimistic_state = PlayerState.PLAYING
        # always generate a new active remote id to prevent race conditions
        # with the named pipe used to send commands
        atv_player.active_remote_id = str(randint(1000, 8000))
        args = [
            self._cliraop_bin,
            "-n",
            str(ntp),
            "-p",
            str(stream.core.service.port),
            "-w",
            str(2000 + sync_adjust),
            *extra_args,
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
        atv_player.cliraop_proc = AsyncProcess(
            args, enable_stdin=True, enable_stdout=False, enable_stderr=True
        )
        await atv_player.cliraop_proc.start()
        atv_player.cliraop_proc.attach_task(log_watcher(atv_player.cliraop_proc))

    async def _send_metadata(self, player_id: str) -> None:
        """Send metadata to player (and connected sync childs)."""
        queue = self.mass.player_queues.get_active_queue(player_id)
        if not queue or not queue.current_item:
            return
        duration = min(queue.current_item.duration or 0, 3600)
        title = queue.current_item.name
        artist = ""
        album = ""
        if queue.current_item.streamdetails and queue.current_item.streamdetails.stream_title:
            # stream title from radio station
            stream_title = queue.current_item.streamdetails.stream_title
            if " - " in stream_title:
                artist, title = stream_title.split(" - ", 1)
            else:
                title = stream_title
            # set album to radio station name
            album = queue.current_item.name
        if media_item := queue.current_item.media_item:
            if artist_str := getattr(media_item, "artist_str", None):
                artist = artist_str
            if _album := getattr(media_item, "album", None):
                album = _album.name

        cmd = f"TITLE={title or 'Music Assistant'}\nARTIST={artist}\nALBUM={album}\n"
        cmd += f"DURATION={duration}\nACTION=SENDMETA\n"

        for atv_player in self._get_sync_clients(player_id):
            await atv_player.send_cli_command(cmd)

        # get image
        if not queue.current_item.image:
            return

        image_url = self.mass.metadata.get_image_url(
            queue.current_item.image, size=512, prefer_proxy=True
        )
        for atv_player in self._get_sync_clients(player_id):
            await atv_player.send_cli_command(f"ARTWORK={image_url}\n")
