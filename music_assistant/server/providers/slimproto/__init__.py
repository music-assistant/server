"""Base/builtin provider with support for players using slimproto."""
from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable, Generator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aioslimproto.client import PlayerState as SlimPlayerState
from aioslimproto.client import SlimClient
from aioslimproto.client import TransitionType as SlimTransition
from aioslimproto.const import EventType as SlimEventType
from aioslimproto.discovery import start_discovery

from music_assistant.common.models.config_entries import ConfigEntry
from music_assistant.common.models.enums import (
    ConfigEntryType,
    ContentType,
    PlayerFeature,
    PlayerState,
    PlayerType,
)
from music_assistant.common.models.errors import PlayerUnavailableError, QueueEmpty
from music_assistant.common.models.player import DeviceInfo, Player
from music_assistant.common.models.queue_item import QueueItem
from music_assistant.constants import CONF_PLAYERS
from music_assistant.server.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType

# sync constants
MIN_DEVIATION_ADJUST = 10  # 10 milliseconds
MAX_DEVIATION_ADJUST = 20000  # 10 seconds
MIN_REQ_PLAYPOINTS = 8  # we need at least 8 measurements

# TODO: Implement display support

STATE_MAP = {
    SlimPlayerState.BUFFERING: PlayerState.PLAYING,
    SlimPlayerState.PAUSED: PlayerState.PAUSED,
    SlimPlayerState.PLAYING: PlayerState.PLAYING,
    SlimPlayerState.STOPPED: PlayerState.IDLE,
}


@dataclass
class SyncPlayPoint:
    """Simple structure to describe a Sync Playpoint."""

    timestamp: float
    item_id: str
    diff: int


CONF_SYNC_ADJUST = "sync_adjust"
CONF_PLAYER_VOLUME = "player_volume"
DEFAULT_PLAYER_VOLUME = 20

SLIM_PLAYER_CONFIG_ENTRIES = (
    ConfigEntry(
        key=CONF_SYNC_ADJUST,
        type=ConfigEntryType.INTEGER,
        range=(0, 1500),
        default_value=0,
        label="Correct synchronization delay",
        description="If this player is playing audio synced with other players "
        "and you always hear the audio too late on this player, you can shift the audio a bit.",
        advanced=True,
    ),
    ConfigEntry(
        key=CONF_PLAYER_VOLUME,
        type=ConfigEntryType.INTEGER,
        default_value=DEFAULT_PLAYER_VOLUME,
        label="Default startup volume",
        description="Default volume level to set/use when initializing the player.",
        advanced=True,
    ),
)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = SlimprotoProvider(mass, manifest, config)
    await prov.handle_setup()
    return prov


async def get_config_entries(
    mass: MusicAssistant, manifest: ProviderManifest  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    return tuple()  # we do not have any config entries (yet)


class SlimprotoProvider(PlayerProvider):
    """Base/builtin provider for players using the SLIM protocol (aka slimproto)."""

    _socket_servers: tuple[asyncio.Server | asyncio.BaseTransport]
    _socket_clients: dict[str, SlimClient]
    _sync_playpoints: dict[str, deque[SyncPlayPoint]]
    _virtual_providers: dict[str, tuple[Callable, Callable]]

    async def handle_setup(self) -> None:
        """Handle async initialization of the provider."""
        self._socket_clients = {}
        self._sync_playpoints = {}
        self._virtual_providers = {}
        # autodiscovery of the slimproto server does not work
        # when the port is not the default (3483) so we hardcode it for now
        slimproto_port = 3483
        cli_port = cli_prov.cli_port if (cli_prov := self.mass.get_provider("lms_cli")) else None
        self.logger.info("Starting SLIMProto server on port %s", slimproto_port)
        self._socket_servers = (
            # start slimproto server
            await asyncio.start_server(self._create_client, "0.0.0.0", slimproto_port),
            # setup discovery
            await start_discovery(slimproto_port, cli_port, self.mass.webserver.port),
        )

    async def unload(self) -> None:
        """Handle close/cleanup of the provider."""
        if hasattr(self, "_socket_clients"):
            for client in list(self._socket_clients.values()):
                client.disconnect()
        self._socket_clients = {}
        if hasattr(self, "_socket_servers"):
            for _server in self._socket_servers:
                _server.close()
        self._socket_servers = None

    async def _create_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Create player from new connection on the socket."""
        if self.mass.closing:
            return
        addr = writer.get_extra_info("peername")
        self.logger.debug("Socket client connected: %s", addr)

        def client_callback(
            event_type: SlimEventType, client: SlimClient, data: Any = None  # noqa: ARG001
        ):
            if event_type == SlimEventType.PLAYER_DISCONNECTED:
                self._handle_disconnected(client)
                return

            if event_type == SlimEventType.PLAYER_CONNECTED:
                self._handle_connected(client)

            if event_type == SlimEventType.PLAYER_DECODER_READY:
                self.mass.create_task(self._handle_decoder_ready(client))
                return

            if event_type == SlimEventType.PLAYER_HEARTBEAT:
                self._handle_player_heartbeat(client)
                return

            # ignore some uninteresting events
            if event_type in (
                SlimEventType.PLAYER_CLI_EVENT,
                SlimEventType.PLAYER_DECODER_ERROR,
            ):
                return

            # forward player update to MA player controller
            self._handle_player_update(client)

        # construct SlimClient from socket client
        SlimClient(reader, writer, client_callback)

    def get_player_config_entries(self, player_id: str) -> tuple[ConfigEntry]:  # noqa: ARG002
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        return SLIM_PLAYER_CONFIG_ENTRIES

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player."""
        # forward command to player and any connected sync child's
        for client in self._get_sync_clients(player_id):
            if client.state == SlimPlayerState.STOPPED:
                continue
            await client.stop()
            # workaround: some players do not send an event when playback stopped
            client._process_stat_stmu(b"")

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY command to given player."""
        # forward command to player and any connected sync child's
        for client in self._get_sync_clients(player_id):
            if client.state not in (
                SlimPlayerState.PAUSED,
                SlimPlayerState.BUFFERING,
            ):
                continue
            await client.play()

    async def cmd_play_media(
        self,
        player_id: str,
        queue_item: QueueItem,
        seek_position: int = 0,
        fade_in: bool = False,
        flow_mode: bool = False,
    ) -> None:
        """Send PLAY MEDIA command to given player.

        This is called when the Queue wants the player to start playing a specific QueueItem.
        The player implementation can decide how to process the request, such as playing
        queue items one-by-one or enqueue all/some items.

            - player_id: player_id of the player to handle the command.
            - queue_item: the QueueItem to start playing on the player.
            - seek_position: start playing from this specific position.
            - fade_in: fade in the music at start (e.g. at resume).
        """
        # send stop first
        await self.cmd_stop(player_id)

        player = self.mass.players.get(player_id)
        # make sure that the (master) player is powered
        # powering any client players must be done in other ways
        if not player.synced_to:
            await self._socket_clients[player_id].power(True)

        # forward command to player and any connected sync child's
        for client in self._get_sync_clients(player_id):
            await self._handle_play_media(
                client,
                queue_item=queue_item,
                seek_position=seek_position,
                fade_in=fade_in,
                send_flush=True,
                flow_mode=flow_mode,
            )

    async def _handle_play_media(
        self,
        client: SlimClient,
        queue_item: QueueItem,
        seek_position: int = 0,
        fade_in: bool = False,
        send_flush: bool = True,
        crossfade: bool = False,
        flow_mode: bool = False,
    ) -> None:
        """Handle PlayMedia on slimproto player(s)."""
        player_id = client.player_id
        # pick codec based on capabilities
        codec_map = (
            ("flc", ContentType.FLAC),
            ("pcm", ContentType.PCM),
            ("mp3", ContentType.MP3),
        )
        for fmt, fmt_type in codec_map:
            if fmt in client.supported_codecs:
                content_type = fmt_type
                break
        else:
            self.logger.debug("Could not auto determine supported codec, fallback to PCM")
            content_type = ContentType.PCM
        url = await self.mass.streams.resolve_stream_url(
            queue_item=queue_item,
            player_id=player_id,
            seek_position=seek_position,
            fade_in=fade_in,
            content_type=content_type,
            flow_mode=flow_mode,
        )
        await client.play_url(
            url=url,
            mime_type=f"audio/{content_type.value}",
            metadata={"item_id": queue_item.queue_item_id},
            send_flush=send_flush,
            transition=SlimTransition.CROSSFADE if crossfade else SlimTransition.NONE,
            transition_duration=10 if crossfade else 0,
        )

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player."""
        # forward command to player and any connected sync child's
        for client in self._get_sync_clients(player_id):
            if client.state not in (
                SlimPlayerState.PLAYING,
                SlimPlayerState.BUFFERING,
            ):
                continue
            await client.pause()

    async def cmd_power(self, player_id: str, powered: bool) -> None:
        """Send POWER command to given player."""
        if client := self._socket_clients.get(player_id):
            await client.power(powered)
        # TODO: unsync client at poweroff if synced

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        if client := self._socket_clients.get(player_id):
            await client.volume_set(volume_level)

    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME MUTE command to given player."""
        if client := self._socket_clients.get(player_id):
            await client.mute(muted)

    async def cmd_sync(self, player_id: str, target_player: str) -> None:
        """Handle SYNC command for given player."""
        child_player = self.mass.players.get(player_id)
        assert child_player
        parent_player = self.mass.players.get(target_player)
        assert parent_player
        parent_player.group_childs.append(child_player.player_id)
        child_player.synced_to = parent_player.player_id
        self.mass.players.update(child_player.player_id)
        self.mass.players.update(parent_player.player_id)
        if parent_player.state == PlayerState.PLAYING:
            # playback needs to be restarted to get all players in sync
            # TODO: If there is any need, we could make this smarter where the new
            # sync child waits for the next track.
            await self.mass.players.queues.resume(parent_player.player_id)

    async def cmd_unsync(self, player_id: str) -> None:
        """Handle UNSYNC command for given player."""
        child_player = self.mass.players.get(player_id)
        parent_player = self.mass.players.get(child_player.synced_to)
        if child_player.state == PlayerState.PLAYING:
            await self.cmd_stop(child_player.player_id)
        child_player.synced_to = None
        parent_player.group_childs.remove(child_player.player_id)
        self.mass.players.update(child_player.player_id)
        self.mass.players.update(parent_player.player_id)

    def register_virtual_provider(
        self,
        player_model: str,
        register_callback: Callable,
        update_callback: Callable,
    ) -> None:
        """Register a virtual provider based on slimproto, such as the airplay bridge."""
        self._virtual_providers[player_model] = (
            register_callback,
            update_callback,
        )

    def _handle_player_update(self, client: SlimClient) -> None:
        """Process SlimClient update/add to Player controller."""
        player_id = client.player_id
        virtual_provider_info = self._virtual_providers.get(client.device_model)
        try:
            player = self.mass.players.get(player_id, raise_unavailable=False)
        except PlayerUnavailableError:
            # player does not yet exist, create it
            player = Player(
                player_id=player_id,
                provider=self.domain,
                type=PlayerType.PLAYER,
                name=client.name,
                available=True,
                powered=client.powered,
                device_info=DeviceInfo(
                    model=client.device_model,
                    address=client.device_address,
                    manufacturer=client.device_type,
                ),
                supported_features=(
                    PlayerFeature.ACCURATE_TIME,
                    PlayerFeature.POWER,
                    PlayerFeature.SYNC,
                    PlayerFeature.VOLUME_MUTE,
                    PlayerFeature.VOLUME_SET,
                ),
                max_sample_rate=int(client.max_sample_rate),
            )
            if virtual_provider_info:
                # if this player is part of a virtual provider run the callback
                virtual_provider_info[0](player)
            self.mass.players.register_or_update(player)

        # update player state on player events
        player.available = True
        player.current_url = client.current_url
        player.current_item_id = (
            client.current_metadata["item_id"] if client.current_metadata else None
        )
        player.name = client.name
        player.powered = client.powered
        player.state = STATE_MAP[client.state]
        player.volume_level = client.volume_level
        player.volume_muted = client.muted
        # set all existing player ids in `can_sync_with` field
        player.can_sync_with = tuple(
            x.player_id for x in self._socket_clients.values() if x.player_id != player_id
        )
        if virtual_provider_info:
            # if this player is part of a virtual provider run the callback
            virtual_provider_info[1](player)
        self.mass.players.update(player_id)

    def _handle_player_heartbeat(self, client: SlimClient) -> None:
        """Process SlimClient elapsed_time update."""
        if client.state != SlimPlayerState.PLAYING:
            # ignore server heartbeats
            return

        player = self.mass.players.get(client.player_id)
        sync_master_id = player.synced_to

        # elapsed time change on the time will be auto picked up
        # by the player manager.
        player.elapsed_time = client.elapsed_seconds
        player.elapsed_time_last_updated = time.time()

        # handle sync
        if not sync_master_id:
            # we only correct sync child's, not the sync master itself
            return
        if sync_master_id not in self._socket_clients:
            return  # just here as a guard as bad things can happen

        sync_master = self._socket_clients[sync_master_id]

        # we collect a few playpoints of the player to determine
        # average lag/drift so we can adjust accordingly
        sync_playpoints = self._sync_playpoints.setdefault(
            client.player_id, deque(maxlen=MIN_REQ_PLAYPOINTS)
        )

        # make sure client has loaded the same track as sync master
        client_item_id = client.current_metadata["item_id"] if client.current_metadata else None
        master_item_id = (
            sync_master.current_metadata["item_id"] if sync_master.current_metadata else None
        )
        if client_item_id != master_item_id:
            sync_playpoints.clear()
            return

        last_playpoint = sync_playpoints[-1] if sync_playpoints else None
        if last_playpoint and (time.time() - last_playpoint.timestamp) > 10:
            # last playpoint is too old, invalidate
            sync_playpoints.clear()
        if last_playpoint and last_playpoint.item_id != client.current_metadata["item_id"]:
            # item has changed, invalidate
            sync_playpoints.clear()

        diff = int(
            self._get_corrected_elapsed_milliseconds(sync_master)
            - self._get_corrected_elapsed_milliseconds(client)
        )

        if abs(diff) > MAX_DEVIATION_ADJUST:
            # safety guard when player is transitioning or something is just plain wrong
            sync_playpoints.clear()
            return

        # we can now append the current playpoint to our list
        sync_playpoints.append(SyncPlayPoint(time.time(), client.current_metadata["item_id"], diff))

        if len(sync_playpoints) < MIN_REQ_PLAYPOINTS:
            return

        # if we have enough playpoints, get the average value
        prev_diffs = [x.diff for x in sync_playpoints]
        avg_diff = sum(prev_diffs) / len(prev_diffs)
        delta = abs(avg_diff)

        if delta < MIN_DEVIATION_ADJUST:
            return

        # handle player lagging behind, fix with skip_ahead
        if avg_diff > 0:
            self.logger.debug("%s resync: skipAhead %sms", player.display_name, delta)
            sync_playpoints.clear()
            asyncio.create_task(self._skip_over(client.player_id, delta))
        else:
            # handle player is drifting too far ahead, use pause_for to adjust
            self.logger.debug("%s resync: pauseFor %sms", player.display_name, delta)
            sync_playpoints.clear()
            asyncio.create_task(self._pause_for(client.player_id, delta))

    async def _handle_decoder_ready(self, client: SlimClient) -> None:
        """Handle decoder ready event, player is ready for the next track."""
        if not client.current_metadata:
            return
        try:
            next_item, crossfade = await self.mass.players.queues.player_ready_for_next_track(
                client.player_id, client.current_metadata["item_id"]
            )
            await self._handle_play_media(client, next_item, send_flush=False, crossfade=crossfade)
        except QueueEmpty:
            pass

    def _handle_connected(self, client: SlimClient) -> None:
        """Handle a client connected event."""
        player_id = client.player_id
        prev = self._socket_clients.pop(player_id, None)
        if prev is not None:
            # player reconnected while we did not yet cleanup the old socket
            prev.disconnect()
        self._socket_clients[player_id] = client
        if prev is None:
            # update existing players so they can update their `can_sync_with` field
            for client in self._socket_clients.values():
                self._handle_player_update(client)
        # handle init/startup volume
        init_volume = self.mass.config.get(
            f"{CONF_PLAYERS}/{player_id}/{CONF_PLAYER_VOLUME}", DEFAULT_PLAYER_VOLUME
        )
        self.mass.create_task(client.volume_set(init_volume))

    def _handle_disconnected(self, client: SlimClient) -> None:
        """Handle a client disconnected event."""
        player_id = client.player_id
        prev = self._socket_clients.pop(player_id, None)
        if prev is None:
            # already cleaned up
            return
        if player := self.mass.players.get(player_id):
            player.available = False
            self.mass.players.update(player_id)

    async def _pause_for(self, client_id: str, millis: int) -> None:
        """Handle pause for x amount of time to help with syncing."""
        client = self._socket_clients[client_id]
        # https://wiki.slimdevices.com/index.php/SlimProto_TCP_protocol.html#u.2C_p.2C_a_.26_t_commands_and_replay_gain_field§
        await client.send_strm(b"p", replay_gain=int(millis))

    async def _skip_over(self, client_id: str, millis: int) -> None:
        """Handle skip for x amount of time to help with syncing."""
        client = self._socket_clients[client_id]
        # https://wiki.slimdevices.com/index.php/SlimProto_TCP_protocol.html#u.2C_p.2C_a_.26_t_commands_and_replay_gain_field
        await client.send_strm(b"a", replay_gain=int(millis))

    def _get_sync_clients(self, player_id: str) -> Generator[SlimClient]:
        """Get all sync clients for a player."""
        player = self.mass.players.get(player_id)
        for child_id in [player.player_id] + player.group_childs:
            if client := self._socket_clients.get(child_id):
                if not player_id and not client.powered:
                    # only powered child's
                    continue
                yield client

    def _get_corrected_elapsed_milliseconds(self, client: SlimClient) -> int:
        """Return corrected elapsed milliseconds."""
        sync_delay = self.mass.config.get(
            f"{CONF_PLAYERS}/{client.player_id}/{CONF_SYNC_ADJUST}", 0
        )
        if sync_delay != 0:
            return client.elapsed_milliseconds - sync_delay
        return client.elapsed_milliseconds
