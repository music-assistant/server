"""Airplay Player provider for Music Assistant."""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import socket
import time
from contextlib import suppress
from random import randint, randrange
from typing import TYPE_CHECKING

from zeroconf import IPVersion, ServiceStateChange
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
    ConfigValueType,
    ProviderConfig,
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
from music_assistant.common.models.player_queue import PlayerQueue
from music_assistant.common.models.provider import ProviderManifest
from music_assistant.constants import CONF_SYNC_ADJUST, VERBOSE_LOG_LEVEL
from music_assistant.server import MusicAssistant
from music_assistant.server.helpers.audio import FFMpeg, get_ffmpeg_stream, get_player_filter_params
from music_assistant.server.helpers.process import AsyncProcess, check_output
from music_assistant.server.helpers.util import TaskManager
from music_assistant.server.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType
    from music_assistant.server.providers.ugp import UniversalGroupProvider

DOMAIN = "airplay"

CONF_ENCRYPTION = "encryption"
CONF_ALAC_ENCODE = "alac_encode"
CONF_VOLUME_START = "volume_start"
CONF_PASSWORD = "password"
CONF_BIND_INTERFACE = "bind_interface"

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
)
BACKOFF_TIME_LOWER_LIMIT = 15  # seconds
BACKOFF_TIME_UPPER_LIMIT = 300  # Five minutes

CONF_CREDENTIALS = "credentials"
CACHE_KEY_PREV_VOLUME = "airplay_prev_volume"
FALLBACK_VOLUME = 20

AIRPLAY_PCM_FORMAT = AudioFormat(
    content_type=ContentType.from_bit_depth(16), sample_rate=44100, bit_depth=16
)

# airplay has fixed sample rate/bit depth so make this config entry static and hidden
CONF_ENTRY_SAMPLE_RATES_AIRPLAY = create_sample_rates_config_entry(44100, 16, 44100, 16, True)


# TODO: Airplay provider
# - split up and cleanup the code into more digestable parts
# - Implement authentication for Apple TV
# - Implement volume control for Apple devices using pyatv
# - Implement metadata for Apple Apple devices using pyatv
# - Use pyatv for communicating with original Apple devices
# and use cliraop for actual streaming


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return AirplayProvider(mass, manifest, config)


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
    return (
        ConfigEntry(
            key=CONF_BIND_INTERFACE,
            type=ConfigEntryType.STRING,
            default_value=mass.streams.publish_ip,
            label="Bind interface",
            description="Interface to bind to for Airplay streaming.",
            category="advanced",
        ),
    )


def convert_airplay_volume(value: float) -> int:
    """Remap Airplay Volume to 0..100 scale."""
    airplay_min = -30
    airplay_max = 0
    normal_min = 0
    normal_max = 100
    portion = (value - airplay_min) * (normal_max - normal_min) / (airplay_max - airplay_min)
    return int(portion + normal_min)


def get_model_from_am(am_property: str | None) -> tuple[str, str]:
    """Return Manufacturer and Model name from mdns AM property."""
    manufacturer = "Unknown"
    model = "Generic Airplay device"
    if not am_property:
        return (manufacturer, model)
    if isinstance(am_property, bytes):
        am_property = am_property.decode("utf-8")
    if am_property == "AudioAccessory5,1":
        model = "HomePod"
        manufacturer = "Apple"
    elif "AppleTV" in am_property:
        model = "Apple TV"
        manufacturer = "Apple"
    else:
        model = am_property
    return (manufacturer, model)


def get_primary_ip_address(discovery_info: AsyncServiceInfo) -> str | None:
    """Get primary IP address from zeroconf discovery info."""
    for address in discovery_info.parsed_addresses(IPVersion.V4Only):
        if address.startswith("127"):
            # filter out loopback address
            continue
        if address.startswith("169.254"):
            # filter out APIPA address
            continue
        return address
    return None


class RaopStream:
    """Object that holds the details of a (RAOP) stream job."""

    def __init__(
        self, prov: AirplayProvider, airplay_player: AirPlayPlayer, input_format: AudioFormat
    ) -> None:
        """Initialize RaopStream."""
        self.prov = prov
        self.mass = prov.mass
        self.airplay_player = airplay_player
        self.input_format = input_format
        # always generate a new active remote id to prevent race conditions
        # with the named pipe used to send audio
        self.active_remote_id: str = str(randint(1000, 8000))
        self.prevent_playback: bool = False
        # audio_source_task will only exist for the main player in a sync group
        self.audio_source_task: asyncio.Task | None = None
        self._log_reader_task: asyncio.Task | None = None
        self._cliraop_proc: AsyncProcess | None = None
        self._ffmpeg_proc: AsyncProcess | None = None
        self._started = asyncio.Event()
        self._stopped = False

    @property
    def running(self) -> bool:
        """Return boolean if this stream is running."""
        return not self._stopped and self._started.is_set()

    async def start(self, start_ntp: int, wait_start: int = 1000) -> None:
        """Initialize CLIRaop process for a player."""
        extra_args = []
        player_id = self.airplay_player.player_id
        mass_player = self.mass.players.get(player_id)
        bind_ip = await self.mass.config.get_provider_config_value(
            self.prov.instance_id, CONF_BIND_INTERFACE
        )
        extra_args += ["-if", bind_ip]
        if self.mass.config.get_raw_player_config_value(player_id, CONF_ENCRYPTION, False):
            extra_args += ["-encrypt"]
        if self.mass.config.get_raw_player_config_value(player_id, CONF_ALAC_ENCODE, True):
            extra_args += ["-alac"]
        for prop in ("et", "md", "am", "pk", "pw"):
            if prop_value := self.airplay_player.discovery_info.decoded_properties.get(prop):
                extra_args += [f"-{prop}", prop_value]
        sync_adjust = self.mass.config.get_raw_player_config_value(player_id, CONF_SYNC_ADJUST, 0)
        if device_password := self.mass.config.get_raw_player_config_value(
            player_id, CONF_PASSWORD, None
        ):
            extra_args += ["-password", device_password]
        if self.prov.logger.isEnabledFor(logging.DEBUG):
            extra_args += ["-debug", "5"]
        elif self.prov.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            extra_args += ["-debug", "10"]

        # create os pipes to pipe ffmpeg to cliraop
        read, write = await asyncio.to_thread(os.pipe)

        # ffmpeg handles the player specific stream + filters and pipes
        # audio to the cliraop process
        self._ffmpeg_proc = FFMpeg(
            audio_input="-",
            input_format=self.input_format,
            output_format=AIRPLAY_PCM_FORMAT,
            filter_params=get_player_filter_params(self.mass, player_id),
            audio_output=write,
            logger=self.airplay_player.logger.getChild("ffmpeg"),
        )
        await self._ffmpeg_proc.start()
        await asyncio.to_thread(os.close, write)

        # cliraop is the binary that handles the actual raop streaming to the player
        cliraop_args = [
            self.prov.cliraop_bin,
            "-ntpstart",
            str(start_ntp),
            "-port",
            str(self.airplay_player.discovery_info.port),
            "-wait",
            str(wait_start - sync_adjust),
            "-volume",
            str(mass_player.volume_level),
            *extra_args,
            "-dacp",
            self.prov.dacp_id,
            "-activeremote",
            self.active_remote_id,
            "-udn",
            self.airplay_player.discovery_info.name,
            self.airplay_player.address,
            "-",
        ]
        self._cliraop_proc = AsyncProcess(cliraop_args, stdin=read, stderr=True, name="cliraop")
        if platform.system() == "Darwin":
            os.environ["DYLD_LIBRARY_PATH"] = "/usr/local/lib"
        await self._cliraop_proc.start()
        await asyncio.to_thread(os.close, read)
        self._started.set()
        self._log_reader_task = self.mass.create_task(self._log_watcher())

    async def stop(self):
        """Stop playback and cleanup."""
        if self._stopped:
            return
        if self.audio_source_task and not self.audio_source_task.done():
            self.audio_source_task.cancel()
        if self._cliraop_proc.proc and not self._cliraop_proc.closed:
            await self.send_cli_command("ACTION=STOP")
        self._stopped = True  # set after send_cli command!
        if self._cliraop_proc.proc and not self._cliraop_proc.closed:
            await self._cliraop_proc.close(True)
        if self._ffmpeg_proc and not self._ffmpeg_proc.closed:
            await self._ffmpeg_proc.close(True)
        self._cliraop_proc = None
        self._ffmpeg_proc = None

    async def write_chunk(self, chunk: bytes) -> None:
        """Write a (pcm) audio chunk."""
        if self._stopped:
            return
        await self._started.wait()
        await self._ffmpeg_proc.write(chunk)

    async def write_eof(self) -> None:
        """Write EOF."""
        if self._stopped:
            return
        await self._started.wait()
        await self._ffmpeg_proc.write_eof()

    async def send_cli_command(self, command: str) -> None:
        """Send an interactive command to the running CLIRaop binary."""
        if self._stopped:
            return
        await self._started.wait()

        if not command.endswith("\n"):
            command += "\n"

        def send_data():
            with suppress(BrokenPipeError), open(named_pipe, "w") as f:
                f.write(command)

        named_pipe = f"/tmp/raop-{self.active_remote_id}"  # noqa: S108
        self.airplay_player.logger.log(VERBOSE_LOG_LEVEL, "sending command %s", command)
        await asyncio.to_thread(send_data)

    async def _log_watcher(self) -> None:
        """Monitor stderr for the running CLIRaop process."""
        airplay_player = self.airplay_player
        mass_player = self.mass.players.get(airplay_player.player_id)
        queue = self.mass.player_queues.get_active_queue(mass_player.active_source)
        logger = airplay_player.logger
        lost_packets = 0
        prev_metadata_checksum: str = ""
        prev_progress_report: float = 0
        async for line in self._cliraop_proc.iter_stderr():
            if "elapsed milliseconds:" in line:
                # this is received more or less every second while playing
                millis = int(line.split("elapsed milliseconds: ")[1])
                mass_player.elapsed_time = millis / 1000
                mass_player.elapsed_time_last_updated = time.time()
                # send metadata to player(s) if needed
                # NOTE: this must all be done in separate tasks to not disturb audio
                now = time.time()
                if (
                    mass_player.elapsed_time > 2
                    and queue
                    and queue.current_item
                    and queue.current_item.streamdetails
                ):
                    metadata_checksum = (
                        queue.current_item.streamdetails.stream_title
                        or queue.current_item.queue_item_id
                    )
                    if prev_metadata_checksum != metadata_checksum:
                        prev_metadata_checksum = metadata_checksum
                        prev_progress_report = now
                        self.mass.create_task(self._send_metadata(queue))
                    # send the progress report every 5 seconds
                    elif now - prev_progress_report >= 5:
                        prev_progress_report = now
                        self.mass.create_task(self._send_progress(queue))
            if "set pause" in line or "Pause at" in line:
                mass_player.state = PlayerState.PAUSED
                self.mass.players.update(airplay_player.player_id)
            if "Restarted at" in line or "restarting w/ pause" in line:
                mass_player.state = PlayerState.PLAYING
                self.mass.players.update(airplay_player.player_id)
            if "restarting w/o pause" in line:
                # streaming has started
                mass_player.state = PlayerState.PLAYING
                mass_player.elapsed_time = 0
                mass_player.elapsed_time_last_updated = time.time()
                self.mass.players.update(airplay_player.player_id)
            if "lost packet out of backlog" in line:
                lost_packets += 1
                if lost_packets == 100:
                    logger.error("High packet loss detected, restarting playback...")
                    self.mass.create_task(self.mass.player_queues.resume(queue.queue_id))
                else:
                    logger.warning("Packet loss detected!")
            if "end of stream reached" in line:
                logger.debug("End of stream reached")
                break

            logger.log(VERBOSE_LOG_LEVEL, line)

        # if we reach this point, the process exited
        if airplay_player.raop_stream == self:
            mass_player.state = PlayerState.IDLE
            self.mass.players.update(airplay_player.player_id)
        # ensure we're cleaned up afterwards (this also logs the returncode)
        await self.stop()

    async def _send_metadata(self, queue: PlayerQueue) -> None:
        """Send metadata to player (and connected sync childs)."""
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
        elif media_item := queue.current_item.media_item:
            title = media_item.name
            if artist_str := getattr(media_item, "artist_str", None):
                artist = artist_str
            if _album := getattr(media_item, "album", None):
                album = _album.name

        cmd = f"TITLE={title or 'Music Assistant'}\nARTIST={artist}\nALBUM={album}\n"
        cmd += f"DURATION={duration}\nPROGRESS=0\nACTION=SENDMETA\n"

        await self.send_cli_command(cmd)

        # get image
        if not queue.current_item.image:
            return

        # the image format needs to be 500x500 jpeg for maximum compatibility with players
        image_url = self.mass.metadata.get_image_url(
            queue.current_item.image, size=500, prefer_proxy=True, image_format="jpeg"
        )
        await self.send_cli_command(f"ARTWORK={image_url}\n")

    async def _send_progress(self, queue: PlayerQueue) -> None:
        """Send progress report to player (and connected sync childs)."""
        if not queue or not queue.current_item:
            return
        progress = int(queue.corrected_elapsed_time)
        await self.send_cli_command(f"PROGRESS={progress}\n")


class AirPlayPlayer:
    """Holds the details of the (discovered) Airplay (RAOP) player."""

    def __init__(
        self, prov: AirplayProvider, player_id: str, discovery_info: AsyncServiceInfo, address: str
    ) -> None:
        """Initialize AirPlayPlayer."""
        self.prov = prov
        self.mass = prov.mass
        self.player_id = player_id
        self.discovery_info = discovery_info
        self.address = address
        self.logger = prov.logger.getChild(player_id)
        self.raop_stream: RaopStream | None = None

    async def cmd_stop(self, update_state: bool = True) -> None:
        """Send STOP command to player."""
        if self.raop_stream:
            await self.raop_stream.stop()
        if update_state and (mass_player := self.mass.players.get(self.player_id)):
            mass_player.state = PlayerState.IDLE
            self.mass.players.update(mass_player.player_id)

    async def cmd_play(self) -> None:
        """Send PLAY (unpause) command to player."""
        if self.raop_stream and self.raop_stream.running:
            await self.raop_stream.send_cli_command("ACTION=PLAY")

    async def cmd_pause(self) -> None:
        """Send PAUSE command to player."""
        if not self.raop_stream or not self.raop_stream.running:
            return
        await self.raop_stream.send_cli_command("ACTION=PAUSE")


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
        if player_id not in self._players:
            # most probably a syncgroup
            return (
                *base_entries,
                CONF_ENTRY_CROSSFADE,
                CONF_ENTRY_CROSSFADE_DURATION,
                CONF_ENTRY_SAMPLE_RATES_AIRPLAY,
                CONF_ENTRY_FLOW_MODE_ENFORCED,
            )
        return (*base_entries, *PLAYER_CONFIG_ENTRIES, CONF_ENTRY_SAMPLE_RATES_AIRPLAY)

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player.

        - player_id: player_id of the player to handle the command.
        """
        # forward command to player and any connected sync members
        async with TaskManager(self.mass) as tg:
            for airplay_player in self._get_sync_clients(player_id):
                tg.create_task(airplay_player.cmd_stop())

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY (unpause) command to given player.

        - player_id: player_id of the player to handle the command.
        """
        # forward command to player and any connected sync members
        async with TaskManager(self.mass) as tg:
            for airplay_player in self._get_sync_clients(player_id):
                tg.create_task(airplay_player.cmd_play())

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player.

        - player_id: player_id of the player to handle the command.
        """
        player = self.mass.players.get(player_id)
        if player.synced_to:
            # should not happen, but just in case
            raise RuntimeError("Player is synced")
        if player.group_childs:
            # pause is not supported while synced, use stop instead
            await self.cmd_stop(player_id)
            return
        airplay_player = self._players[player_id]
        await airplay_player.cmd_pause()

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
                ugp_provider: UniversalGroupProvider = self.mass.get_provider("ugp")
                ugp_stream = ugp_provider.streams[media.queue_id]
                input_format = ugp_stream.output_format
                audio_source = ugp_stream.subscribe()
            elif media.media_type == MediaType.RADIO and media.queue_id and media.queue_item_id:
                # radio stream - consume media stream directly
                input_format = AIRPLAY_PCM_FORMAT
                queue_item = self.mass.player_queues.get_item(media.queue_id, media.queue_item_id)
                audio_source = self.mass.streams.get_media_stream(
                    streamdetails=queue_item.streamdetails,
                    pcm_format=AIRPLAY_PCM_FORMAT,
                )
            elif media.queue_id and media.queue_item_id:
                # regular queue (flow) stream request
                input_format = AIRPLAY_PCM_FORMAT
                audio_source = self.mass.streams.get_flow_stream(
                    queue=self.mass.player_queues.get(media.queue_id),
                    start_queue_item=self.mass.player_queues.get_item(
                        media.queue_id, media.queue_item_id
                    ),
                    pcm_format=AIRPLAY_PCM_FORMAT,
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

            # Python is not suitable for realtime audio streaming so we do the actual streaming
            # of (RAOP) audio using a small executable written in C based on libraop to do
            # the actual timestamped playback, which reads pcm audio from stdin
            # and we can send some interactive commands using a named pipe.

            # setup RaopStream for player and its sync childs
            sync_clients = self._get_sync_clients(player_id)
            for airplay_player in sync_clients:
                airplay_player.raop_stream = RaopStream(
                    self, airplay_player, input_format=input_format
                )

            async def audio_streamer() -> None:
                async for chunk in audio_source:
                    await asyncio.gather(
                        *[x.raop_stream.write_chunk(chunk) for x in sync_clients],
                        return_exceptions=True,
                    )
                # entire stream consumed: send EOF
                await asyncio.gather(
                    *[x.raop_stream.write_eof() for x in sync_clients],
                    return_exceptions=True,
                )

            # get current ntp and start cliraop
            _, stdout = await check_output(self.cliraop_bin, "-ntp")
            start_ntp = int(stdout.strip())
            wait_start = 1250 + (250 * len(sync_clients))
            await asyncio.gather(
                *[x.raop_stream.start(start_ntp, wait_start) for x in sync_clients],
                return_exceptions=True,
            )
            self._players[player_id].raop_stream.audio_source_task = asyncio.create_task(
                audio_streamer()
            )

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
        self.mass.players.update(player_id)
        # store last state in cache
        await self.mass.cache.set(player_id, volume_level, base_key=CACHE_KEY_PREV_VOLUME)

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
        # make sure that the player manager gets an update
        self.mass.players.update(player.player_id, skip_forward=True)
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
        self._players[player_id] = AirPlayPlayer(self, player_id, info, address)
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
            can_sync_with=tuple(x for x in self._players if x != player_id),
            volume_level=volume,
        )
        self.mass.players.register_or_update(mass_player)
        # update can_sync_with field of all other players
        # this ensure that the field always contains all player ids,
        # even when a player joins later on
        for player in self.players:
            if player.player_id == player_id:
                continue
            player.can_sync_with = tuple(x for x in self._players if x != player.player_id)
            self.mass.players.update(player.player_id)

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
                self.mass.create_task(self.mass.player_queues.pause(active_queue.queue_id))
            elif "dmcp.device-volume=" in path:
                if mass_player.device_info.manufacturer.lower() == "apple":
                    # Apple devices only report their (new) volume level, they dont request it
                    return
                raop_volume = float(path.split("dmcp.device-volume=", 1)[-1])
                volume = convert_airplay_volume(raop_volume)
                if volume != mass_player.volume_level:
                    self.mass.create_task(self.cmd_volume_set(player_id, volume))
                    # optimistically set the new volume to prevent bouncing around
                    mass_player.volume_level = volume
            elif "dmcp.volume=" in path:
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
