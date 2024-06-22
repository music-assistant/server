"""Snapcast Player provider for Music Assistant."""

from __future__ import annotations

import asyncio
import logging
import pathlib
import random
import re
import socket
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Final, cast

from bidict import bidict
from snapcast.control import create_server
from snapcast.control.client import Snapclient
from zeroconf import NonUniqueNameException
from zeroconf.asyncio import AsyncServiceInfo

from music_assistant.common.helpers.util import get_ip_pton
from music_assistant.common.models.config_entries import (
    CONF_ENTRY_CROSSFADE,
    CONF_ENTRY_CROSSFADE_DURATION,
    CONF_ENTRY_FLOW_MODE_ENFORCED,
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
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
from music_assistant.common.models.errors import SetupFailedError
from music_assistant.common.models.media_items import AudioFormat
from music_assistant.common.models.player import DeviceInfo, Player, PlayerMedia
from music_assistant.server.helpers.audio import FFMpeg, get_ffmpeg_stream, get_player_filter_params
from music_assistant.server.helpers.process import AsyncProcess, check_output
from music_assistant.server.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from snapcast.control.group import Snapgroup
    from snapcast.control.server import Snapserver
    from snapcast.control.stream import Snapstream

    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType
    from music_assistant.server.providers.ugp import UniversalGroupProvider

CONF_SERVER_HOST = "snapcast_server_host"
CONF_SERVER_CONTROL_PORT = "snapcast_server_control_port"
CONF_USE_EXTERNAL_SERVER = "snapcast_use_external_server"
CONF_SERVER_BUFFER_SIZE = "snapcast_server_built_in_buffer_size"
CONF_SERVER_INITIAL_VOLUME = "snapcast_server_built_in_initial_volume"
CONF_SERVER_TRANSPORT_CODEC = "snapcast_server_built_in_codec"
CONF_SERVER_SEND_AUDIO_TO_MUTED = "snapcast_server_built_in_send_muted"


# airplay has fixed sample rate/bit depth so make this config entry static and hidden
CONF_ENTRY_SAMPLE_RATES_SNAPCAST = create_sample_rates_config_entry(48000, 16, 48000, 16, True)

SNAP_STREAM_STATUS_MAP = {
    "idle": PlayerState.IDLE,
    "playing": PlayerState.PLAYING,
    "unknown": PlayerState.IDLE,
}
DEFAULT_SNAPSERVER_PORT = 1705

SNAPWEB_DIR: Final[pathlib.Path] = pathlib.Path(__file__).parent.resolve().joinpath("snapweb")


DEFAULT_SNAPCAST_FORMAT = AudioFormat(
    content_type=ContentType.PCM_S16LE,
    sample_rate=48000,
    # TODO: can we handle 24 bits bit depth ?
    bit_depth=16,
    channels=2,
)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = SnapCastProvider(mass, manifest, config)
    await prov.handle_async_init()
    return prov


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    returncode, output = await check_output("snapserver -v")
    snapserver_present = returncode == 0 and "snapserver v0.27.0" in output.decode()
    return (
        ConfigEntry(
            key=CONF_SERVER_BUFFER_SIZE,
            type=ConfigEntryType.INTEGER,
            range=(500, 6000),
            default_value=1000,
            label="Snapserver buffer size",
            description="Buffer[ms].The end-to-end latency,"
            "from capturing a sample on the snapserver until"
            "the sample is played-out on the client",
            required=False,
            category="Built-in Snapserver Settings",
            hidden=not snapserver_present,
            help_link="https://raw.githubusercontent.com/badaix/snapcast/86cd4b2b63e750a72e0dfe6a46d47caf01426c8d/server/etc/snapserver.conf",
        ),
        ConfigEntry(
            key=CONF_SERVER_INITIAL_VOLUME,
            type=ConfigEntryType.INTEGER,
            range=(0, 100),
            default_value=25,
            label="Snapserver initial volume",
            description="Volume assigned to new snapclients [percent]",
            required=False,
            category="Built-in Snapserver Settings",
            hidden=not snapserver_present,
            help_link="https://raw.githubusercontent.com/badaix/snapcast/86cd4b2b63e750a72e0dfe6a46d47caf01426c8d/server/etc/snapserver.conf",
        ),
        ConfigEntry(
            key=CONF_SERVER_SEND_AUDIO_TO_MUTED,
            type=ConfigEntryType.BOOLEAN,
            default_value=False,
            label="Send audio to muted clients",
            required=False,
            category="Built-in Snapserver Settings",
            hidden=not snapserver_present,
            help_link="https://raw.githubusercontent.com/badaix/snapcast/86cd4b2b63e750a72e0dfe6a46d47caf01426c8d/server/etc/snapserver.conf",
        ),
        ConfigEntry(
            key=CONF_SERVER_TRANSPORT_CODEC,
            type=ConfigEntryType.STRING,
            options=(
                ConfigValueOption(
                    title="FLAC",
                    value="flac",
                ),
                ConfigValueOption(
                    title="OGG",
                    value="ogg",
                ),
                ConfigValueOption(
                    title="OPUS",
                    value="opus",
                ),
                ConfigValueOption(
                    title="PCM",
                    value="pcm",
                ),
            ),
            default_value="flac",
            label="Snapserver default transport codec",
            description="This is the codec used by snapserver to send audio to clients",
            required=False,
            category="Built-in Snapserver Settings",
            hidden=not snapserver_present,
            help_link="https://raw.githubusercontent.com/badaix/snapcast/86cd4b2b63e750a72e0dfe6a46d47caf01426c8d/server/etc/snapserver.conf",
        ),
        ConfigEntry(
            key=CONF_USE_EXTERNAL_SERVER,
            type=ConfigEntryType.BOOLEAN,
            default_value=not snapserver_present,
            label="Use existing Snapserver",
            required=False,
            description="Music Assistant by default already includes a Snapserver. \n\n"
            "Checking this option allows you to connect to your own/external existing Snapserver "
            "and not use the builtin one provided by Music Assistant.",
            category="advanced" if snapserver_present else "generic",
        ),
        ConfigEntry(
            key=CONF_SERVER_HOST,
            type=ConfigEntryType.STRING,
            default_value="127.0.0.1",
            label="Snapcast server ip",
            required=False,
            depends_on=CONF_USE_EXTERNAL_SERVER,
            category="advanced" if snapserver_present else "generic",
        ),
        ConfigEntry(
            key=CONF_SERVER_CONTROL_PORT,
            type=ConfigEntryType.INTEGER,
            default_value=DEFAULT_SNAPSERVER_PORT,
            label="Snapcast control port",
            required=False,
            depends_on=CONF_USE_EXTERNAL_SERVER,
            category="advanced" if snapserver_present else "generic",
        ),
    )


class SnapCastProvider(PlayerProvider):
    """Player provider for Snapcast based players."""

    _snapserver: Snapserver
    _snapcast_server_host: str
    _snapcast_server_control_port: int
    _stream_tasks: dict[str, asyncio.Task]
    _use_builtin_server: bool
    _snapserver_runner: asyncio.Task | None
    _snapserver_started: asyncio.Event | None
    _ids_map: bidict  # ma_id / snapclient_id

    def _get_snapclient_id(self, player_id: str) -> str:
        search_dict = self._ids_map
        return search_dict.get(player_id)

    def _get_ma_id(self, snap_client_id: str) -> str:
        search_dict = self._ids_map.inverse
        return search_dict.get(snap_client_id)

    def _generate_and_register_id(self, snap_client_id) -> str:
        search_dict = self._ids_map.inverse
        if snap_client_id not in search_dict:
            new_id = "ma_" + str(re.sub(r"\W+", "", snap_client_id))
            self._ids_map[new_id] = snap_client_id
            return new_id
        else:
            return self._get_ma_id(snap_client_id)

    def _can_sync_with(self, snap_client: Snapclient) -> dict:
        return tuple(
            self._get_ma_id(x.identifier)
            for x in self._snapserver.clients
            if x.identifier != snap_client.identifier and x.connected
        )

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return (ProviderFeature.SYNC_PLAYERS, ProviderFeature.PLAYER_GROUP_CREATE)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # set snapcast logging
        logging.getLogger("snapcast").setLevel(self.logger.level)
        self._use_builtin_server = not self.config.get_value(CONF_USE_EXTERNAL_SERVER)
        if self._use_builtin_server:
            self._snapcast_server_host = "127.0.0.1"
            self._snapcast_server_control_port = DEFAULT_SNAPSERVER_PORT
            self._snapcast_server_buffer_size = self.config.get_value(CONF_SERVER_BUFFER_SIZE)
            self._snapcast_server_initial_volume = self.config.get_value(CONF_SERVER_INITIAL_VOLUME)
            self._snapcast_server_send_to_muted = self.config.get_value(
                CONF_SERVER_SEND_AUDIO_TO_MUTED
            )
            self._snapcast_server_transport_codec = self.config.get_value(
                CONF_SERVER_TRANSPORT_CODEC
            )

        else:
            self._snapcast_server_host = self.config.get_value(CONF_SERVER_HOST)
            self._snapcast_server_control_port = self.config.get_value(CONF_SERVER_CONTROL_PORT)

        self._stream_tasks = {}
        self._ids_map = bidict({})

        if self._use_builtin_server:
            # start our own builtin snapserver
            self._snapserver_started = asyncio.Event()
            self._snapserver_runner = asyncio.create_task(self._builtin_server_runner())
            await asyncio.wait_for(self._snapserver_started.wait(), 10)
        else:
            self._snapserver_runner = None
            self._snapserver_started = None
        try:
            self._snapserver = await create_server(
                self.mass.loop,
                self._snapcast_server_host,
                port=self._snapcast_server_control_port,
                reconnect=True,
            )
            self._snapserver.set_on_update_callback(self._handle_update)
            self.logger.info(
                "Started connection to Snapserver %s",
                f"{self._snapcast_server_host}:{self._snapcast_server_control_port}",
            )
        except OSError as err:
            msg = "Unable to start the Snapserver connection ?"
            raise SetupFailedError(msg) from err

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        # initial load of players
        self._handle_update()

    async def unload(self) -> None:
        """Handle close/cleanup of the provider."""
        for client in self._snapserver.clients:
            await self.cmd_stop(client.identifier)
        self._snapserver.stop()
        if self._snapserver_runner and not self._snapserver_runner.done():
            self._snapserver_runner.cancel()
        await asyncio.sleep(10)  # prevent race conditions when reloading
        self._snapserver_started.clear()

    def on_player_config_removed(self, player_id: str) -> None:
        """Call (by config manager) when the configuration of a player is removed."""
        super().on_player_config_removed(player_id)
        if self._use_builtin_server:
            self.mass.create_task(
                self._snapserver.delete_client(self._get_snapclient_id(player_id))
            )

    def _handle_update(self) -> None:
        """Process Snapcast init Player/Group and set callback ."""
        for snap_client in self._snapserver.clients:
            self._handle_player_init(snap_client)
            snap_client.set_callback(self._handle_player_update)
        for snap_client in self._snapserver.clients:
            self._handle_player_update(snap_client)
        for snap_group in self._snapserver.groups:
            snap_group.set_callback(self._handle_group_update)

    def _handle_group_update(self, snap_group: Snapgroup) -> None:
        """Process Snapcast group callback."""
        for snap_client in self._snapserver.clients:
            self._handle_player_update(snap_client)

    def _handle_player_init(self, snap_client: Snapclient) -> None:
        """Process Snapcast add to Player controller."""
        player_id = self._generate_and_register_id(snap_client.identifier)
        player = self.mass.players.get(player_id, raise_unavailable=False)
        if not player:
            snap_client = cast(
                Snapclient, self._snapserver.client(self._get_snapclient_id(player_id))
            )
            player = Player(
                player_id=player_id,
                provider=self.instance_id,
                type=PlayerType.PLAYER,
                name=snap_client.friendly_name,
                available=True,
                powered=snap_client.connected,
                device_info=DeviceInfo(
                    model=snap_client._client.get("host").get("os"),
                    address=snap_client._client.get("host").get("ip"),
                    manufacturer=snap_client._client.get("host").get("arch"),
                ),
                supported_features=(
                    PlayerFeature.SYNC,
                    PlayerFeature.VOLUME_SET,
                    PlayerFeature.VOLUME_MUTE,
                ),
            )
        self.mass.players.register_or_update(player)

    def _handle_player_update(self, snap_client: Snapclient) -> None:
        """Process Snapcast update to Player controller."""
        player_id = self._get_ma_id(snap_client.identifier)
        player = self.mass.players.get(player_id)
        player.name = snap_client.friendly_name
        player.volume_level = snap_client.volume
        player.volume_muted = snap_client.muted
        player.available = snap_client.connected
        player.can_sync_with = self._can_sync_with(snap_client)
        player.synced_to = self._synced_to(player_id)
        player.group_childs = self._group_childs(player_id)
        if player.active_group is None:
            if stream := self._get_snapstream(player_id):
                if stream.name.startswith(("MusicAssistant", "default")):
                    player.active_source = player_id
                else:
                    player.active_source = stream.name
            else:
                player.active_source = player_id
        self.mass.players.register_or_update(player)

    async def get_player_config_entries(self, player_id: str) -> tuple[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        base_entries = await super().get_player_config_entries(player_id)
        return (
            *base_entries,
            CONF_ENTRY_FLOW_MODE_ENFORCED,
            CONF_ENTRY_CROSSFADE,
            CONF_ENTRY_CROSSFADE_DURATION,
            CONF_ENTRY_SAMPLE_RATES_SNAPCAST,
        )

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        snap_client_id = self._get_snapclient_id(player_id)
        await self._snapserver.client(snap_client_id).set_volume(volume_level)
        self.mass.players.update(snap_client_id)

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player."""
        player = self.mass.players.get(player_id, raise_unavailable=False)
        if stream_task := self._stream_tasks.pop(player_id, None):
            if not stream_task.done():
                stream_task.cancel()
        player.state = PlayerState.IDLE
        self._set_childs_state(player_id, PlayerState.IDLE)
        self.mass.players.register_or_update(player)
        # assign default/empty stream to the player
        await self._get_snapgroup(player_id).set_stream("default")

    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send MUTE command to given player."""
        snap_client_id = self._get_snapclient_id(player_id)
        await self._snapserver.client(snap_client_id).set_muted(muted)

    async def cmd_sync(self, player_id: str, target_player: str) -> None:
        """Sync Snapcast player."""
        group = self._get_snapgroup(target_player)
        if self._get_snapclient_id(player_id) not in group.clients:
            await group.add_client(self._get_snapclient_id(player_id))
            player = self.mass.players.get(player_id)
            player.synced_to = self._synced_to(player_id)
            self.mass.players.update(target_player)
            # update all players
            self._handle_update()

    async def cmd_unsync(self, player_id: str) -> None:
        """Unsync Snapcast player."""
        snap_client_id = self._get_snapclient_id(player_id)
        group = self._get_snapgroup(player_id)
        await group.remove_client(snap_client_id)
        # assign default/empty stream to the player
        await self._get_snapgroup(player_id).set_stream("default")
        await self.cmd_stop(player_id=player_id)
        # update all players
        self._handle_update()

    async def play_media(self, player_id: str, media: PlayerMedia) -> None:  # noqa: PLR0915
        """Handle PLAY MEDIA on given player."""
        player = self.mass.players.get(player_id)
        if player.synced_to:
            msg = "A synced player cannot receive play commands directly"
            raise RuntimeError(msg)
        # stop any existing streams first
        await self.cmd_stop(player_id)
        stream, port = await self._create_stream()
        snap_group = self._get_snapgroup(player_id)
        await snap_group.set_stream(stream.identifier)

        # select audio source
        if media.media_type == MediaType.ANNOUNCEMENT:
            # special case: stream announcement
            input_format = DEFAULT_SNAPCAST_FORMAT
            audio_source = self.mass.streams.get_announcement_stream(
                media.custom_data["url"],
                output_format=DEFAULT_SNAPCAST_FORMAT,
                use_pre_announce=media.custom_data["use_pre_announce"],
            )
        elif media.queue_id.startswith("ugp_"):
            # special case: UGP stream
            ugp_provider: UniversalGroupProvider = self.mass.get_provider("ugp")
            ugp_stream = ugp_provider.streams[media.queue_id]
            input_format = ugp_stream.audio_format
            audio_source = ugp_stream.subscribe_raw()
        elif media.media_type == MediaType.RADIO and media.queue_id and media.queue_item_id:
            # radio stream - consume media stream directly
            input_format = DEFAULT_SNAPCAST_FORMAT
            queue_item = self.mass.player_queues.get_item(media.queue_id, media.queue_item_id)
            audio_source = self.mass.streams.get_media_stream(
                streamdetails=queue_item.streamdetails,
                pcm_format=DEFAULT_SNAPCAST_FORMAT,
            )
        elif media.queue_id and media.queue_item_id:
            # regular queue (flow) stream request
            input_format = DEFAULT_SNAPCAST_FORMAT
            audio_source = self.mass.streams.get_flow_stream(
                queue=self.mass.player_queues.get(media.queue_id),
                start_queue_item=self.mass.player_queues.get_item(
                    media.queue_id, media.queue_item_id
                ),
                pcm_format=DEFAULT_SNAPCAST_FORMAT,
            )
        else:
            # assume url or some other direct path
            # NOTE: this will fail if its an uri not playable by ffmpeg
            input_format = DEFAULT_SNAPCAST_FORMAT
            audio_source = get_ffmpeg_stream(
                audio_input=media.uri,
                input_format=AudioFormat(ContentType.try_parse(media.uri)),
                output_format=DEFAULT_SNAPCAST_FORMAT,
            )

        async def _streamer() -> None:
            host = self._snapcast_server_host
            self.mass.players.update(player_id)

            def stream_callback(_stream) -> None:
                player.state = PlayerState(_stream.status)
                if player.state == PlayerState.PLAYING:
                    player.current_media = media
                    player.elapsed_time = 0
                    player.elapsed_time_last_updated = time.time()
                self.mass.players.update(player_id)
                self._set_childs_state(player_id, player.state)

            stream.set_callback(stream_callback)
            stream_path = f"tcp://{host}:{port}"
            self.logger.debug("Start streaming to %s", stream_path)
            try:
                async with FFMpeg(
                    audio_input=audio_source,
                    input_format=input_format,
                    output_format=DEFAULT_SNAPCAST_FORMAT,
                    filter_params=get_player_filter_params(self.mass, player_id),
                    audio_output=f"tcp://{host}:{port}",
                    logger=self.logger.getChild("ffmpeg"),
                ) as ffmpeg_proc:
                    await ffmpeg_proc.wait()
                    # we need to wait a bit for the stream status to become idle
                    # to ensure that all snapclients have consumed the audio
                    await self.mass.players.wait_for_state(player, PlayerState.IDLE)
                    await asyncio.sleep(5)
            finally:
                self.logger.debug("Finished streaming to %s", stream_path)
                # there is no way to unsub the callback to we do this nasty
                stream._callback_func = None
                with suppress(TypeError, KeyError, AttributeError):
                    await self._snapserver.stream_remove_stream(stream.identifier)

        # start streaming the queue (pcm) audio in a background task
        self._stream_tasks[player_id] = asyncio.create_task(_streamer())

    def _get_snapgroup(self, player_id: str) -> Snapgroup:
        """Get snapcast group for given player_id."""
        snap_client_id = self._get_snapclient_id(player_id)
        client: Snapclient = self._snapserver.client(snap_client_id)
        return client.group

    def _get_snapstream(self, player_id: str) -> Snapstream | None:
        """Get snapcast stream for given player_id."""
        if group := self._get_snapgroup(player_id):
            with suppress(KeyError):
                return self._snapserver.stream(group.stream)
        return None

    def _synced_to(self, player_id: str) -> str | None:
        """Return player_id of the player this player is synced to."""
        snap_group: Snapgroup = self._get_snapgroup(player_id)
        master_id: str = self._get_ma_id(player_id)

        if len(snap_group.clients) < 2 or player_id == master_id:
            return None
        return master_id

    def _group_childs(self, player_id: str) -> set[str]:
        """Return player_ids of the players synced to this player."""
        snap_group = self._get_snapgroup(player_id)
        return {
            self._get_ma_id(snap_client_id)
            for snap_client_id in snap_group.clients
            if len(snap_group.clients) > 1
        }

    async def _create_stream(self) -> tuple[Snapstream, int]:
        """Create new stream on snapcast server."""
        attempts = 50
        while attempts:
            attempts -= 1
            # pick a random port
            port = random.randint(4953, 4953 + 200)
            name = f"MusicAssistant--{port}"
            result = await self._snapserver.stream_add_stream(
                # NOTE: setting the sampleformat to something else
                # (like 24 bits bit depth) does not seem to work at all!
                f"tcp://0.0.0.0:{port}?name={name}&sampleformat=48000:16:2",
            )
            if "id" not in result:
                # if the port is already taken, the result will be an error
                self.logger.warning(result)
                continue
            stream = self._snapserver.stream(result["id"])
            return (stream, port)
        msg = "Unable to create stream - No free port found?"
        raise RuntimeError(msg)

    def _get_player_state(self, player_id: str) -> PlayerState:
        """Return the state of the player."""
        snap_group = self._get_snapgroup(player_id)
        return SNAP_STREAM_STATUS_MAP.get(snap_group.stream_status)

    def _set_childs_state(self, player_id: str, state: PlayerState) -> None:
        """Set the state of the child`s of the player."""
        for child_player_id in self._group_childs(player_id):
            player = self.mass.players.get(child_player_id)
            player.state = state
            self.mass.players.update(child_player_id)

    async def _builtin_server_runner(self) -> None:
        """Start running the builtin snapserver."""
        if self._snapserver_started.is_set():
            raise RuntimeError("Snapserver is already started!")
        logger = self.logger.getChild("snapserver")
        logger.info("Starting builtin Snapserver...")
        # register the snapcast mdns services
        for name, port in (
            ("-http", 1780),
            ("-jsonrpc", 1705),
            ("-stream", 1704),
            ("-tcp", 1705),
            ("", 1704),
        ):
            zeroconf_type = f"_snapcast{name}._tcp.local."
            try:
                info = AsyncServiceInfo(
                    zeroconf_type,
                    name=f"Snapcast.{zeroconf_type}",
                    properties={"is_mass": "true"},
                    addresses=[await get_ip_pton(self.mass.streams.publish_ip)],
                    port=port,
                    server=f"{socket.gethostname()}.local",
                )
                attr_name = f"zc_service_set{name}"
                if getattr(self, attr_name, None):
                    await self.mass.aiozc.async_update_service(info)
                else:
                    await self.mass.aiozc.async_register_service(info, strict=False)
                setattr(self, attr_name, True)
            except NonUniqueNameException:
                self.logger.debug(
                    "Could not register mdns record for %s as its already in use", zeroconf_type
                )
            except Exception as err:
                self.logger.exception(
                    "Could not register mdns record for %s: %s", zeroconf_type, str(err)
                )
        args = [
            "snapserver",
            # config settings taken from
            # https://raw.githubusercontent.com/badaix/snapcast/86cd4b2b63e750a72e0dfe6a46d47caf01426c8d/server/etc/snapserver.conf
            f"--server.datadir={self.mass.storage_path}",
            "--http.enabled=true",
            "--http.port=1780",
            f"--http.doc_root={SNAPWEB_DIR}",
            "--tcp.enabled=true",
            "--tcp.port=1705",
            f"--stream.buffer={self._snapcast_server_control_port}",
            f"--stream.codec={self._snapcast_server_transport_codec}",
            f"--stream.send_to_muted={str(self._snapcast_server_send_to_muted).lower()}",
            f"--streaming_client.initial_volume={self._snapcast_server_initial_volume}",
        ]
        async with AsyncProcess(args, stdout=True, name="snapserver") as snapserver_proc:
            # keep reading from stdout until exit
            async for data in snapserver_proc.iter_any():
                data = data.decode().strip()  # noqa: PLW2901
                for line in data.split("\n"):
                    logger.debug(line)
                    if "(Snapserver) Version 0.27.0" in line:
                        # delay init a small bit to prevent race conditions
                        # where we try to connect too soon
                        self.mass.loop.call_later(2, self._snapserver_started.set)
