"""Snapcast Player provider for Music Assistant."""
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import snapcast.control
from ffmpeg import FFmpegError, Progress
from ffmpeg.asyncio import FFmpeg

from music_assistant.common.models.config_entries import ConfigEntry, ConfigValueType
from music_assistant.common.models.enums import PlayerFeature, PlayerState, PlayerType
from music_assistant.common.models.errors import SetupFailedError
from music_assistant.common.models.player import DeviceInfo, Player
from music_assistant.common.models.queue_item import QueueItem
from music_assistant.server.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType

SNAPCAST_SERVER_HOST = "127.0.0.1"
SNAPCAST_SERVER_CONTROL_PORT = 1705


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
    return tuple()  # we do not have any config entries (yet)


class SnapCastProvider(PlayerProvider):
    """Player provider for Snapcast based players."""

    _snapserver: [asyncio.Server | asyncio.BaseTransport]

    async def handle_setup(self) -> None:
        """Handle async initialization of the provider."""
        try:
            self._snapserver = await snapcast.control.create_server(
                self.mass.loop,
                SNAPCAST_SERVER_HOST,
                port=SNAPCAST_SERVER_CONTROL_PORT,
                reconnect=True,
            )
            self._snapserver.set_on_update_callback(self._handle_update)
            self._handle_update()
            self.logger.info(
                f"Started Snapserver connection on:"
                f"{SNAPCAST_SERVER_HOST}:{SNAPCAST_SERVER_CONTROL_PORT}"
            )
        except OSError:
            raise SetupFailedError("Unable to start the Snapserver connection ?")

    def _handle_update(self):
        for snap_client in self._snapserver.clients:
            self._handle_player_update(snap_client)
            snap_client.set_callback(self._handle_player_update)

    def _handle_player_update(self, snap_client):
        player_id = snap_client.identifier
        player = self.mass.players.get(player_id, raise_unavailable=False)
        if not player:
            player = Player(
                player_id=player_id,
                provider=self.domain,
                type=PlayerType.PLAYER,
                name=snap_client.friendly_name,
                available=True,
                powered=snap_client.connected,
                device_info=DeviceInfo(),
                supported_features=(
                    PlayerFeature.SYNC,
                    PlayerFeature.VOLUME_SET,
                    PlayerFeature.VOLUME_MUTE,
                ),
            )
        self.mass.players.register_or_update(player)
        player.name = snap_client.friendly_name
        player.volume_level = snap_client.volume
        player.volume_muted = snap_client.muted
        player.available = snap_client.connected
        player.can_sync_with = tuple(
            x.identifier for x in self._snapserver.clients if x.identifier != player_id
        )
        player.synced_to = self._synced_to(player_id)
        self.mass.players.register_or_update(player)

    async def unload(self) -> None:
        """Handle close/cleanup of the provider."""
        for client in self._snapserver.clients:
            await self.cmd_stop(client.identifier)
        self._snapserver.stop()

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        self.mass.create_task(
            self._snapserver.client_volume(player_id, {"percent": volume_level, "muted": False})
        )

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
        await self.cmd_stop(player_id)

        stream = self._get_snapstream(player_id)
        player = self.mass.players.get(player_id, raise_unavailable=False)

        ffmpeg = (
            FFmpeg()
            .option("y")
            .input(url)
            .output(f"{stream.path}", f="u16le", acodec="pcm_s16le", ac=2, ar=48000)
        )
        self.mass.create_task(ffmpeg.execute())

        @ffmpeg.on("start")
        async def on_start(arguments: list[str]):
            self.logger.debug("Ffmpeg stream is running")
            if hasattr(stream, "ffmpeg"):
                await self.cmd_stop(player_id)
            stream.ffmpeg = ffmpeg
            player.state = PlayerState.PLAYING
            player.current_url = url
            player.elapsed_time = 0
            player.elapsed_time_last_updated = time.time()
            self.mass.players.register_or_update(player)

        @ffmpeg.on("progress")
        def on_progress(progress: Progress):
            player.state = PlayerState.PLAYING
            self.mass.players.register_or_update(player)

        @ffmpeg.on("completed")
        async def on_completed():
            player.state = PlayerState.IDLE
            self.mass.players.register_or_update(player)

        @ffmpeg.on("terminated")
        async def on_terminated():
            player.state = PlayerState.IDLE
            self.mass.players.register_or_update(player)

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player."""
        player = self.mass.players.get(player_id, raise_unavailable=False)
        if player.state != PlayerState.IDLE:
            stream = self._get_snapstream(player_id)
            if hasattr(stream, "ffmpeg"):
                try:
                    stream.ffmpeg.terminate()
                    self.logger.debug("ffmpeg player stopped")
                except FFmpegError:
                    self.logger.debug("Fail to stop ffmpeg player")

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player."""
        await self.cmd_stop(player_id)

    async def cmd_volume_mute(self, player_id, muted):
        """Send MUTE command to given player."""
        self.mass.create_task(self._snapserver.client(player_id).set_muted(muted))

    async def _remove_stream(self, stream_id):
        self.mass.create_task(self._server.stream_remove_stream(stream_id))

    def _snapclient_get_group_clients_identifiers(self, player_id):
        group = self._get_snapgroup(player_id)
        return [ele for ele in group.clients if ele != player_id]

    async def cmd_sync(self, player_id: str, target_player: str) -> None:
        """Sync Snapcast player."""
        child_player = self.mass.players.get(player_id)
        assert child_player  # guard
        parent_player = self.mass.players.get(target_player)
        assert parent_player  # guard
        # always make sure that the parent player is part of the sync group
        parent_player.group_childs.add(parent_player.player_id)
        parent_player.group_childs.add(child_player.player_id)
        child_player.synced_to = parent_player.player_id

        group = self._get_snapgroup(target_player)
        self.mass.create_task(group.add_client(player_id))

        self.mass.players.update(child_player.player_id)
        self.mass.players.update(parent_player.player_id)

    async def cmd_unsync(self, player_id: str) -> None:
        """Unsync Snapcast player."""
        group = self._get_snapgroup(player_id)
        await group.remove_client(player_id)
        group = self._get_snapgroup(player_id)
        stream_id = await self._get_empty_stream(player_id)
        await group.set_stream(stream_id)
        self._handle_update()

    def _get_snapgroup(self, player_id):
        client = self._snapserver.client(player_id)
        return client.group

    def _get_snapstream(self, player_id):
        group = self._get_snapgroup(player_id)
        return self._snapserver.stream(group.stream)

    def _synced_to(self, player_id):
        ret = None
        snap_group = self._get_snapgroup(player_id)
        snap_clients = list(filter(lambda x: x != player_id, snap_group.clients))
        if player_id == snap_group.clients[0]:  # Player is a Sync group master
            player = self.mass.players.get(player_id)
            player.group_childs.clear()
            for snap_client in snap_clients:
                player.group_childs.add(snap_client)
        elif len(snap_clients) > 0:
            ret = snap_clients[0]  # Return sync group master id
        return ret

    async def _get_empty_stream(self, player_id):
        snapserver = self._snapserver
        empty_stream = None
        for stream in snapserver.streams:
            stream_in_group = False
            for group in snapserver.groups:
                if stream.identifier == group.stream:
                    stream_in_group = True
            if not stream_in_group:
                empty_stream = stream.identifier

        if empty_stream is None:
            group = self._get_snapgroup(player_id)
            empty_stream = await snapserver.stream_add_stream(
                f"pipe:///tmp/{group.identifier}?name={group.identifier}"
            )
            empty_stream = empty_stream.get("id")
        return empty_stream
