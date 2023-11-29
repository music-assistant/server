"""Snapcast Player provider for Music Assistant."""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import TYPE_CHECKING

from ffmpeg import FFmpegError
from ffmpeg.asyncio import FFmpeg
from snapcast.control import create_server
from snapcast.control.client import Snapclient as SnapClient
from snapcast.control.group import Snapgroup as SnapGroup
from snapcast.control.stream import Snapstream as SnapStream

from music_assistant.common.models.config_entries import ConfigEntry, ConfigValueType
from music_assistant.common.models.enums import (
    ConfigEntryType,
    PlayerFeature,
    PlayerState,
    PlayerType,
)
from music_assistant.common.models.errors import SetupFailedError
from music_assistant.common.models.player import DeviceInfo, Player
from music_assistant.common.models.queue_item import QueueItem
from music_assistant.server.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType
CONF_SNAPCAST_SERVER_HOST = "snapcast_server_host"
CONF_SNAPCAST_SERVER_CONTROL_PORT = "snapcast_server_control_port"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = SnapCastProvider(mass, manifest, config)
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
    return (
        ConfigEntry(
            key=CONF_SNAPCAST_SERVER_HOST,
            type=ConfigEntryType.STRING,
            default_value="127.0.0.1",
            label="Snapcast server ip",
            required=True,
        ),
        ConfigEntry(
            key=CONF_SNAPCAST_SERVER_CONTROL_PORT,
            type=ConfigEntryType.INTEGER,
            default_value="1705",
            label="Snapcast control port",
            required=True,
        ),
    )


class SnapCastProvider(PlayerProvider):
    """Player provider for Snapcast based players."""

    _snapserver: [asyncio.Server | asyncio.BaseTransport]
    snapcast_server_host: str
    snapcast_server_control_port: int

    async def handle_setup(self) -> None:
        """Handle async initialization of the provider."""
        self.snapcast_server_host = self.config.get_value(CONF_SNAPCAST_SERVER_HOST)
        self.snapcast_server_control_port = self.config.get_value(CONF_SNAPCAST_SERVER_CONTROL_PORT)
        try:
            self._snapserver = await create_server(
                self.mass.loop,
                self.snapcast_server_host,
                port=self.snapcast_server_control_port,
                reconnect=True,
            )
            self._snapserver.set_on_update_callback(self._handle_update)
            self._handle_update()
            self.logger.info(
                f"Started Snapserver connection on:"
                f"{self.snapcast_server_host}:{self.snapcast_server_control_port}"
            )
        except OSError:
            raise SetupFailedError("Unable to start the Snapserver connection ?")

    def _handle_update(self) -> None:
        """Process Snapcast init Player/Group and set callback ."""
        for snap_client in self._snapserver.clients:
            self._handle_player_init(snap_client)
            snap_client.set_callback(self._handle_player_update)
        for snap_client in self._snapserver.clients:
            self._handle_player_update(snap_client)
        for snap_group in self._snapserver.groups:
            snap_group.set_callback(self._handle_group_update)

    def _handle_group_update(self, snap_group: SnapGroup) -> None:  # noqa: ARG002
        """Process Snapcast group callback."""
        for snap_client in self._snapserver.clients:
            self._handle_player_update(snap_client)

    def _handle_player_init(self, snap_client: SnapClient) -> None:
        """Process Snapcast add to Player controller."""
        player_id = snap_client.identifier
        player = self.mass.players.get(player_id, raise_unavailable=False)
        if not player:
            snap_client = self._snapserver.client(player_id)
            player = Player(
                player_id=player_id,
                provider=self.domain,
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

    def _handle_player_update(self, snap_client: SnapClient) -> None:
        """Process Snapcast update to Player controller."""
        player_id = snap_client.identifier
        player = self.mass.players.get(player_id)
        player.name = snap_client.friendly_name
        player.volume_level = snap_client.volume
        player.volume_muted = snap_client.muted
        player.available = snap_client.connected
        player.can_sync_with = tuple(
            x.identifier for x in self._snapserver.clients if x.identifier != player_id
        )
        player.synced_to = self._synced_to(player_id)
        player.group_childs = self._group_childs(player_id)
        self.mass.players.register_or_update(player)

    async def unload(self) -> None:
        """Handle close/cleanup of the provider."""
        for client in self._snapserver.clients:
            await self.cmd_stop(client.identifier)
        self._snapserver.stop()

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        mass_player = self.mass.players.get(player_id)
        if mass_player.volume_level != volume_level:
            await self._snapserver.client_volume(
                player_id, {"percent": volume_level, "muted": mass_player.volume_muted}
            )
            self.cmd_volume_mute(player_id, False)

    async def cmd_play_url(
        self,
        player_id: str,
        url: str,
        queue_item: QueueItem | None,  # noqa: ARG002
    ) -> None:
        """Send PLAY URL command to given player.

        This is called when the Queue wants the player to start playing a specific url.
        If an item from the Queue is being played, the QueueItem will be provided with
        all metadata present.

            - player_id: player_id of the player to handle the command.
            - url: the url that the player should start playing.
            - queue_item: the QueueItem that is related to the URL (None when playing direct url).
        """
        player = self.mass.players.get(player_id)
        stream = self._get_snapstream(player_id)
        if stream.path != "":
            await self._get_snapgroup(player_id).set_stream(await self._get_empty_stream())

        stream_host = stream._stream.get("uri").get("host")
        ffmpeg = (
            FFmpeg()
            .option("y")
            .option("re")
            .input(url=url)
            .output(
                f"tcp://{stream_host}",
                f="s16le",
                acodec="pcm_s16le",
                ac=2,
                ar=48000,
            )
        )
        await self.cmd_stop(player_id)

        ffmpeg_task = self.mass.create_task(ffmpeg.execute())

        @ffmpeg.on("start")
        async def on_start(arguments: list[str]):
            self.logger.debug("Ffmpeg stream is running")
            stream.ffmpeg = ffmpeg
            stream.ffmpeg_task = ffmpeg_task
            player.current_url = url
            player.elapsed_time = 0
            player.elapsed_time_last_updated = time.time()
            player.state = PlayerState.PLAYING
            self.mass.players.register_or_update(player)

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player."""
        player = self.mass.players.get(player_id, raise_unavailable=False)
        if player.state != PlayerState.IDLE:
            stream = self._get_snapstream(player_id)
            if hasattr(stream, "ffmpeg_task") and stream.ffmpeg_task.done() is False:
                try:
                    stream.ffmpeg.terminate()
                    stream.ffmpeg_task.cancel()
                    self.logger.debug("ffmpeg player stopped")
                except FFmpegError:
                    self.logger.debug("Fail to stop ffmpeg player")
                player.state = PlayerState.IDLE
                self.mass.players.register_or_update(player)

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player."""
        await self.cmd_stop(player_id)

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
        group = self._get_snapgroup(player_id)
        stream_id = await self._get_empty_stream()
        await group.set_stream(stream_id)
        self._handle_update()

    def _get_snapgroup(self, player_id: str) -> SnapGroup:
        """Get snapcast group for given player_id."""
        client = self._snapserver.client(player_id)
        return client.group

    def _get_snapstream(self, player_id: str) -> SnapStream:
        """Get snapcast stream for given player_id."""
        group = self._get_snapgroup(player_id)
        return self._snapserver.stream(group.stream)

    def _synced_to(self, player_id: str) -> str | None:
        """Return player_id of the player this player is synced to."""
        snap_group = self._get_snapgroup(player_id)
        if player_id != snap_group.clients[0]:
            return snap_group.clients[0]

    def _group_childs(self, player_id: str) -> set[str]:
        """Return player_ids of the players synced to this player."""
        snap_group = self._get_snapgroup(player_id)
        return {snap_client for snap_client in snap_group.clients if snap_client != player_id}

    async def _get_empty_stream(self) -> str:
        """Find or create empty stream on snapcast server.

        This method ensures that there is a snapstream for each snapclient,
        even if the snapserver only have one stream configured. This is needed
        because the default config of snapserver is one stream on a named pipe.
        """
        used_streams = {group.stream for group in self._snapserver.groups}
        for stream in self._snapserver.streams:
            if stream.path == "" and stream.identifier not in used_streams:
                return stream.identifier
        port = 4953
        name = str(uuid.uuid4())
        while True:
            port += 1
            new_stream = await self._snapserver.stream_add_stream(
                f"tcp://{self.snapcast_server_host}:{port}?name={name}"
            )
            if new_stream["id"] not in used_streams:
                return new_stream["id"]
