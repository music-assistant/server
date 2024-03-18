"""Snapcast Player provider for Music Assistant."""

from __future__ import annotations

import asyncio
import random
import time
from contextlib import suppress
from typing import TYPE_CHECKING, cast

from snapcast.control import create_server
from snapcast.control.client import Snapclient

from music_assistant.common.models.config_entries import (
    CONF_ENTRY_CROSSFADE,
    CONF_ENTRY_CROSSFADE_DURATION,
    ConfigEntry,
    ConfigValueType,
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
from music_assistant.common.models.player import DeviceInfo, Player
from music_assistant.server.helpers.audio import get_media_stream
from music_assistant.server.helpers.process import AsyncProcess, check_output
from music_assistant.server.models.player_provider import PlayerProvider
from music_assistant.server.providers.ugp import UGP_PREFIX

if TYPE_CHECKING:
    from snapcast.control.group import Snapgroup
    from snapcast.control.server import Snapserver
    from snapcast.control.stream import Snapstream

    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.common.models.queue_item import QueueItem
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType

CONF_SERVER_HOST = "snapcast_server_host"
CONF_SERVER_CONTROL_PORT = "snapcast_server_control_port"
CONF_USE_EXTERNAL_SERVER = "snapcast_use_external_server"

SNAP_STREAM_STATUS_MAP = {
    "idle": PlayerState.IDLE,
    "playing": PlayerState.PLAYING,
    "unknown": PlayerState.IDLE,
}
DEFAULT_SNAPSERVER_PORT = 1705


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
            key=CONF_USE_EXTERNAL_SERVER,
            type=ConfigEntryType.BOOLEAN,
            default_value=not snapserver_present,
            label="Use existing Snapserver",
            required=False,
            description="Music Assistant by default already includes a Snapserver. \n\n"
            "Checking this option allows you to connect to your own/external existing Snapserver "
            "and not use the builtin one provided by Music Assistant.",
            advanced=snapserver_present,
        ),
        ConfigEntry(
            key=CONF_SERVER_HOST,
            type=ConfigEntryType.STRING,
            default_value="127.0.0.1",
            label="Snapcast server ip",
            required=False,
            depends_on=CONF_USE_EXTERNAL_SERVER,
            advanced=snapserver_present,
        ),
        ConfigEntry(
            key=CONF_SERVER_CONTROL_PORT,
            type=ConfigEntryType.INTEGER,
            default_value=DEFAULT_SNAPSERVER_PORT,
            label="Snapcast control port",
            required=False,
            depends_on=CONF_USE_EXTERNAL_SERVER,
            advanced=snapserver_present,
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
    _snapserver_started = asyncio.Event | None

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return (ProviderFeature.SYNC_PLAYERS,)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._snapcast_server_host = self.config.get_value(CONF_SERVER_HOST)
        self._snapcast_server_control_port = self.config.get_value(CONF_SERVER_CONTROL_PORT)
        self._use_builtin_server = not self.config.get_value(CONF_USE_EXTERNAL_SERVER)
        self._stream_tasks = {}
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
        await self._snapserver.stop()
        self._snapserver_started.clear()
        if self._snapserver_runner and not self._snapserver_runner.done():
            self._snapserver_runner.cancel()
        await asyncio.sleep(2)  # prevent race conditions when reloading

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
        player_id = snap_client.identifier
        player = self.mass.players.get(player_id, raise_unavailable=False)
        if not player:
            snap_client = cast(Snapclient, self._snapserver.client(player_id))
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
        player_id = snap_client.identifier
        player = self.mass.players.get(player_id)
        player.name = snap_client.friendly_name
        player.volume_level = snap_client.volume
        player.volume_muted = snap_client.muted
        player.available = snap_client.connected
        player.can_sync_with = tuple(
            x.identifier
            for x in self._snapserver.clients
            if x.identifier != player_id and x.connected
        )
        player.synced_to = self._synced_to(player_id)
        player.group_childs = self._group_childs(player_id)
        if player.current_item_id and player_id in player.current_item_id:
            player.active_source = player_id
        elif stream := self._get_snapstream(player_id):
            player.active_source = stream.name
        self.mass.players.register_or_update(player)

    async def get_player_config_entries(self, player_id: str) -> tuple[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        base_entries = await super().get_player_config_entries(player_id)
        return (*base_entries, CONF_ENTRY_CROSSFADE, CONF_ENTRY_CROSSFADE_DURATION)

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        await self._snapserver.client_volume(
            player_id, {"percent": volume_level, "muted": volume_level == 0}
        )

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
        await self._snapserver.client(player_id).set_muted(muted)

    async def cmd_sync(self, player_id: str, target_player: str) -> None:
        """Sync Snapcast player."""
        group = self._get_snapgroup(target_player)
        await group.add_client(player_id)

    async def cmd_unsync(self, player_id: str) -> None:
        """Unsync Snapcast player."""
        group = self._get_snapgroup(player_id)
        await group.remove_client(player_id)
        # assign default/empty stream to the player
        await self._get_snapgroup(player_id).set_stream("default")
        self._handle_update()

    async def play_media(self, player_id: str, queue_item: QueueItem) -> None:
        """Handle PLAY MEDIA on given player."""
        player = self.mass.players.get(player_id)
        if player.synced_to:
            msg = "A synced player cannot receive play commands directly"
            raise RuntimeError(msg)
        # stop any existing streams first
        await self.cmd_stop(player_id)
        queue = self.mass.player_queues.get(queue_item.queue_id)
        stream, port = await self._create_stream()
        snap_group = self._get_snapgroup(player_id)
        await snap_group.set_stream(stream.identifier)

        # TODO: can we handle 24 bits bit depth ?
        pcm_format = AudioFormat(
            content_type=ContentType.PCM_S16LE,
            sample_rate=48000,
            bit_depth=16,
            channels=2,
        )
        if queue_item.media_type == MediaType.ANNOUNCEMENT:
            # stream announcement url directly
            audio_iterator = get_media_stream(
                self.mass, queue_item.streamdetails, pcm_format=pcm_format
            )
        elif (
            queue_item.queue_id.startswith(UGP_PREFIX)
            and (stream_job := self.mass.streams.multi_client_jobs.get(queue_item.queue_id))
            and stream_job.pending
        ):
            # handle special case for UGP multi client stream
            stream_job = self.mass.streams.multi_client_jobs.get(queue_item.queue_id)
            stream_job.expected_players.add(player_id)
            audio_iterator = stream_job.subscribe(
                player_id=player_id,
                output_format=pcm_format,
            )
        else:
            audio_iterator = self.mass.streams.get_flow_stream(
                queue,
                start_queue_item=queue_item,
                pcm_format=pcm_format,
            )

        async def _streamer() -> None:
            host = self._snapcast_server_host
            _, writer = await asyncio.open_connection(host, port)
            self.logger.debug("Opened connection to %s:%s", host, port)
            player.current_item_id = f"{queue_item.queue_id}.{queue_item.queue_item_id}"
            player.elapsed_time = 0
            player.elapsed_time_last_updated = time.time()
            player.state = PlayerState.PLAYING
            self._set_childs_state(player_id, PlayerState.PLAYING)
            self.mass.players.register_or_update(player)
            try:
                async for pcm_chunk in audio_iterator:
                    writer.write(pcm_chunk)
                    await writer.drain()
                # end of the stream reached
                if writer.can_write_eof():
                    writer.write_eof()
                    await writer.drain()
                # we need to wait a bit before removing the stream to ensure
                # that all snapclients have consumed the audio
                # https://github.com/music-assistant/hass-music-assistant/issues/1962
                await asyncio.sleep(30)
            finally:
                if not writer.is_closing():
                    writer.close()
                await self._snapserver.stream_remove_stream(stream.identifier)
                self.logger.debug("Closed connection to %s:%s", host, port)

        # start streaming the queue (pcm) audio in a background task
        self._stream_tasks[player_id] = asyncio.create_task(_streamer())

    def _get_snapgroup(self, player_id: str) -> Snapgroup:
        """Get snapcast group for given player_id."""
        client: Snapclient = self._snapserver.client(player_id)
        return client.group

    def _get_snapstream(self, player_id: str) -> Snapstream | None:
        """Get snapcast stream for given player_id."""
        if group := self._get_snapgroup(player_id):
            with suppress(KeyError):
                return self._snapserver.stream(group.stream)
        return None

    def _synced_to(self, player_id: str) -> str | None:
        """Return player_id of the player this player is synced to."""
        snap_group = self._get_snapgroup(player_id)
        if player_id != snap_group.clients[0]:
            return snap_group.clients[0]
        return None

    def _group_childs(self, player_id: str) -> set[str]:
        """Return player_ids of the players synced to this player."""
        snap_group = self._get_snapgroup(player_id)
        return {snap_client for snap_client in snap_group.clients if snap_client != player_id}

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
        async with AsyncProcess(
            ["snapserver"], enable_stdin=False, enable_stdout=True, enable_stderr=False
        ) as snapserver_proc:
            # keep reading from stderr until exit
            async for data in snapserver_proc.iter_any():
                data = data.decode().strip()  # noqa: PLW2901
                for line in data.split("\n"):
                    logger.debug(line)
                    if "(Snapserver) Version 0.27.0" in line:
                        # delay init a small bit to prevent race conditions
                        # where we try to connect too soon
                        self.mass.loop.call_later(2, self._snapserver_started.set)
