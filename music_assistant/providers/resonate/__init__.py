"""Resonate Audio Player Provider."""

from __future__ import annotations

import asyncio
import struct
from collections.abc import Callable
from contextlib import suppress
from dataclasses import field
from typing import TYPE_CHECKING, cast

from aiohttp import WSMsgType, web
from music_assistant_models.enums import (
    ContentType,
    PlayerFeature,
    PlayerState,
    PlayerType,
    ProviderFeature,
)
from music_assistant_models.errors import PlayerUnavailableError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.player import DeviceInfo, Player

from music_assistant.constants import (
    CONF_ENTRY_FLOW_MODE_ENFORCED,
)
from music_assistant.mass import MusicAssistant
from music_assistant.models import ProviderInstanceType
from music_assistant.models.player_provider import PlayerProvider

from . import resonate_models

if TYPE_CHECKING:
    from music_assistant_models.config_entries import (
        ConfigEntry,
        ConfigValueType,
        ProviderConfig,
    )
    from music_assistant_models.player import PlayerMedia
    from music_assistant_models.provider import ProviderManifest

MAX_PENDING_MSG = 512
TARGET_CHUNK_DURATION_MS = 80

# TODO: make dynamic
STREAM_CODEC = "pcm"
STREAM_SAMPLE_RATE = 48000
STREAM_CHANNELS = 2
STREAM_BIT_DEPTH = 16
STREAM_CONTENT_TYPE = ContentType.PCM_S16LE
STREAM_SAMPLE_SIZE = STREAM_CHANNELS * (STREAM_BIT_DEPTH // 8)
TARGET_CHUNK_BYTES = STREAM_SAMPLE_SIZE * STREAM_SAMPLE_RATE // 1000 * TARGET_CHUNK_DURATION_MS
TARGET_CHUNK_SAMPLES = STREAM_SAMPLE_RATE // 1000 * TARGET_CHUNK_DURATION_MS


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return ResonatePlayerProvider(mass, manifest, config)


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
    return ()


class PlayerInstance:
    """A connected resonate audio player."""

    prov: ResonatePlayerProvider
    request: web.Request
    wsock: web.WebSocketResponse = field(init=False)
    player_id: str | None = None
    player_info: resonate_models.PlayerInfo | None = None
    # Task responsible for handling messages from the player
    _handle_task: asyncio.Task[str] | None = None
    # Task responsible for sending audio and other data
    _writer_task: asyncio.Task[None] | None = None
    # Task responsible for processing the audio stream
    stream_task: asyncio.Task[None] | None = None
    _to_write: asyncio.Queue[str | bytes]
    session_info: resonate_models.SessionInfo | None = None

    def __init__(self, prov: ResonatePlayerProvider, request: web.Request):
        """Init a new player instance."""
        self.prov = prov
        self.request = request
        self.wsock = web.WebSocketResponse(heartbeat=55)
        self._to_write = asyncio.Queue(maxsize=MAX_PENDING_MSG)

    async def disconnect(self) -> None:
        """Disconnect client and cancel tasks."""
        self.prov.logger.debug("Disconnecting client %s", self.player_id or self.request.remote)

        # Cancel running tasks
        if self.stream_task and not self.stream_task.done():
            _ = self.stream_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.stream_task
        if self._writer_task and not self._writer_task.done():
            _ = self._writer_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._writer_task
        # Handle task is cancelled implicitly when wsock closes or externally

        # Close websocket
        if not self.wsock.closed:
            _ = await self.wsock.close()

        # Update player state in MA if player was registered
        if self.player_id and (player := self.prov.mass.players.get(self.player_id)):
            if player.available:
                player.available = False
                player.state = PlayerState.IDLE
                self.prov.mass.players.update(self.player_id)

        self.prov.logger.info("Client %s disconnected", self.player_id or self.request.remote)

    async def handle_client(self) -> web.WebSocketResponse:
        """Handle the websocket connection."""
        remote_addr = self.request.remote or "Unknown"
        wsock = self.wsock
        logger = self.prov.logger
        try:
            async with asyncio.timeout(10):
                _ = await self.wsock.prepare(self.request)
        except TimeoutError:
            self.prov.logger.warning("Timeout preparing request from %s", remote_addr)
            return self.wsock

        self.prov.logger.info("Connection established from %s", remote_addr)
        self._handle_task = asyncio.current_task()
        self._writer_task = self.prov.mass.create_task(self._writer())

        # 1. Send Source Hello
        self.send_message(
            resonate_models.SourceHelloMessage(
                payload=resonate_models.SourceInfo(
                    name="Music Assistant",
                    # TODO: will this make problems with multiple MA instances?
                    source_id=self.prov.instance_id,
                )
            )
        )

        try:
            while not wsock.closed:
                msg = await wsock.receive()

                if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                    break

                if msg.type != WSMsgType.TEXT:
                    continue

                try:
                    await self._handle_message(resonate_models.Message.from_json(msg.data))
                except Exception as e:
                    logger.error(
                        "Failed to process message for %s: %s",
                        self.player_id or remote_addr,
                        e,
                    )
                    break

        except asyncio.CancelledError:
            logger.debug("Connection closed by client")
        except Exception:
            logger.exception("Unexpected error inside websocket API")
        finally:
            # TODO: run disconnect here?
            try:
                self._to_write.put_nowait("")
                # Make sure all error messages are written before closing
                await self._writer_task
                _ = await wsock.close()
            except asyncio.QueueFull:  # can be raised by put_nowait
                _ = self._writer_task.cancel()

            if self.player_id and (player := self.prov.mass.players.get(self.player_id)):
                player.available = False
                player.state = PlayerState.IDLE
                self.prov.mass.players.update(self.player_id)
            else:
                logger.debug("WebSocket disconnected before player/hello or already cleaned up.")

        return self.wsock

    async def _handle_message(self, message: resonate_models.Message) -> None:
        """Handle incoming commands from the client."""
        msg_type = message.type
        payload = message.payload
        logger = self.prov.logger
        if msg_type == "player/hello":
            # TODO: reject if player_id is already connected
            player_info = resonate_models.PlayerInfo.from_dict(payload)
            logger.info(f"Received player/hello from {player_info.player_id} ({player_info.name})")
            self.player_info = player_info
            self.player_id = player_info.player_id

            player = Player(
                player_id=self.player_id,
                provider=self.prov.instance_id,
                type=PlayerType.PLAYER,
                name=player_info.name,
                available=True,
                # TODO: powered should probably indicate if a session is active or not
                powered=True,
                device_info=DeviceInfo(),
                supported_features={PlayerFeature.SET_MEMBERS},
                state=PlayerState.IDLE,
            )
            await self.prov.mass.players.register_or_update(player)

        elif msg_type == "player/state":
            if not self.player_id:
                logger.warning("Received player/state before player/hello")
                return
            state_info = resonate_models.PlayerState.from_dict(payload)
            # Update MA player state
            player = cast("Player", self.prov.mass.players.get(self.player_id, True))
            if state_info.state == resonate_models.PlayerStateType.PLAYING:
                player.state = PlayerState.PLAYING
            elif state_info.state == resonate_models.PlayerStateType.PAUSED:
                player.state = PlayerState.PAUSED
            else:
                player.state = PlayerState.IDLE
            self.prov.mass.players.update(self.player_id)

        elif msg_type == "player/time":
            payload["source_received"] = int(self.prov.time() * 1_000_000)
            payload["source_transmitted"] = int(self.prov.time() * 1_000_000)
            logger.info("player/time received from %s, payload is %s", self.player_id, payload)
            # time_reply = resonate_models.SourceTimeInfo.from_dict(payload)
            self.send_message(
                resonate_models.SourceTimeMessage(
                    payload=resonate_models.SourceTimeInfo.from_dict(payload)
                )
            )

        else:
            logger.debug("%s received unhandled command type: %", self.player_id, msg_type)

    async def _writer(self) -> None:
        """Write outgoing messages from the queue."""
        # Exceptions if Socket disconnected or cancelled by connection handler
        with suppress(
            RuntimeError,
            ConnectionResetError,
            asyncio.CancelledError,
        ):
            while not self.wsock.closed:
                item = await self._to_write.get()

                if isinstance(item, bytes):
                    await self.wsock.send_bytes(item)
                else:
                    await self.wsock.send_str(item)

    def send_message(self, message: resonate_models.ServerMessages) -> None:
        """Enqueue a JSON message to be sent to the client."""
        # TODO: handle full queue
        self._to_write.put_nowait(message.to_json())

    def send_binary(self, data: bytes) -> None:
        """Enqueue a binary message to be sent to the client."""
        # TODO: handle full queue
        self._to_write.put_nowait(data)

    def pack_audio_chunk(self, timestamp_us: int, sample_count: int, audio_data: bytes) -> bytes:
        """Pack audio data the audio header."""
        header = struct.pack(
            resonate_models.BINARY_HEADER_FORMAT,
            resonate_models.BinaryMessageType.PlayAudioChunk.value,
            timestamp_us,
            sample_count,
        )
        return header + audio_data


class ResonatePlayerProvider(PlayerProvider):
    """Implementation of the WIP resonate audio protocol."""

    instances: set[PlayerInstance] = set()
    _unregister_cbs: list[Callable[[], None]] = []
    time: Callable[[], float]

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize the provider."""
        super().__init__(mass, manifest, config)
        self._unregister_cbs = [
            # For web clients
            mass.webserver.register_dynamic_route(
                "/resonate",
                self._handle_player_ws_connect,
            ),
            # And local devices
            mass.streams.register_dynamic_route(
                "/resonate",
                self._handle_player_ws_connect,
            ),
        ]
        self.time = mass.loop.time

    async def _handle_player_ws_connect(self, request: web.Request) -> web.WebSocketResponse:
        """Handle incoming WebSocket connection request."""
        instance = PlayerInstance(self, request)
        try:
            self.instances.add(instance)
            return await instance.handle_client()
        finally:
            self.instances.remove(instance)

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {ProviderFeature.REMOVE_PLAYER}

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        is_removed will be set to True when the provider is removed from the configuration.
        """
        # Unregister web routes
        for unregister_cb in self._unregister_cbs:
            unregister_cb()
        self._unregister_cbs.clear()

        # Disconnect all active instances
        _ = await asyncio.gather(
            *(instance.disconnect() for instance in list(self.instances)),
            return_exceptions=True,  # Don't let one failed disconnect stop others
        )
        self.instances.clear()

    async def get_player_config_entries(self, player_id: str) -> tuple[ConfigEntry, ...]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        # TODO: should be more like builtin_player
        return (
            *await super().get_player_config_entries(player_id),
            CONF_ENTRY_FLOW_MODE_ENFORCED,
        )

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player."""
        instance = self._get_player_instance(player_id)
        if instance is None or instance.session_info is None:
            self.logger.warning("Stop command called but player %s is not connected.", player_id)
            # Still update MA state if the player exists
            if player := self.mass.players.get(player_id):
                player.state = PlayerState.IDLE
                self.mass.players.update(player_id)
            return

        self.logger.debug("Sending STOP command to player %s", player_id)

        # Cancel any active stream task for this player
        if instance.stream_task and not instance.stream_task.done():
            instance.stream_task.cancel()
            with suppress(asyncio.CancelledError):
                await instance.stream_task
            instance.stream_task = None

        # Send session stop message
        instance.send_message(
            resonate_models.SessionEndMessage(
                payload=resonate_models.SessionEndPayload(instance.session_info.session_id)
            )
        )

        player = self.mass.players.get(player_id, True)
        assert player
        player.state = PlayerState.IDLE
        self.mass.players.update(player_id)

    async def play_media(
        self,
        player_id: str,
        media: PlayerMedia,
    ) -> None:
        """Handle PLAY MEDIA on given player."""
        instance = self._get_player_instance(player_id)
        if instance is None:
            raise PlayerUnavailableError(f"Player {player_id} is not available")

        # Stop any prior stream
        await self.cmd_stop(player_id)

        # TODO: dynamic session info
        session_info = resonate_models.SessionInfo(
            session_id=f"mass-session-{int(self.time())}",
            codec=STREAM_CODEC,
            sample_rate=STREAM_SAMPLE_RATE,
            channels=STREAM_CHANNELS,
            bit_depth=STREAM_BIT_DEPTH,
            now=int(self.time() * 1_000_000),
            codec_header=None,
        )
        instance.session_info = session_info

        player = self.mass.players.get(player_id, True)
        assert player
        player.elapsed_time = 0
        player.elapsed_time_last_updated = self.time() - 1
        player.state = PlayerState.PLAYING
        self.mass.players.update(player.player_id)

        # Send Session Start to all connected clients
        instance.send_message(resonate_models.SessionStartMessage(payload=session_info))

        instance.stream_task = self.mass.create_task(
            self._stream_audio(player_id, session_info.now, media)
        )

    async def cmd_group(self, player_id: str, target_player: str) -> None:
        """Handle GROUP command for given player.

        Join/add the given player(id) to the given (master) player/sync group.

            - player_id: player_id of the player to handle the command.
            - target_player: player_id of the sync leader.
        """
        raise NotImplementedError

    async def cmd_ungroup(self, player_id: str) -> None:
        """Handle UNGROUP command for given player.

        Remove the given player from any (sync)groups it currently is grouped to.

            - player_id: player_id of the player to handle the command.
        """
        raise NotImplementedError

    async def _stream_audio(self, player_id: str, start_time_us: int, media: PlayerMedia) -> None:
        # TODO: move to PlayerInstance
        queue = self.mass.player_queues.get(player_id)
        instance = self._get_player_instance(player_id)

        pcm_format = AudioFormat(
            content_type=STREAM_CONTENT_TYPE,
            sample_rate=STREAM_SAMPLE_RATE,
            bit_depth=STREAM_BIT_DEPTH,
            channels=STREAM_CHANNELS,
        )

        # TODO: fix this error handling mess
        if queue is None:
            raise Exception

        if media.queue_id is None or instance is None:
            raise Exception

        queue_item = self.mass.player_queues.get_item(player_id, media.queue_item_id)
        if not queue_item:
            self.logger.error("Queue item %s not found in queue %s", media.queue_item_id, player_id)
            return

        samples_sent = 0
        preload = 2000  # TODO: client tells us this

        # TODO: explicit chunk size, better error handling, better player state,
        # support dynamic chunk sizes, samples_sent should be duration instead
        # verify that the players buffer will not be overrun
        # TODO: rename vars
        async for chunk in self.mass.streams.get_queue_flow_stream(
            queue=queue,
            start_queue_item=queue_item,
            pcm_format=pcm_format,
        ):
            # TODO: check off by one errors
            chunk_pos = 0
            split_chunks_count = 0
            while True:
                if chunk_pos + TARGET_CHUNK_BYTES >= len(chunk):
                    break
                c = chunk[
                    chunk_pos : chunk_pos + TARGET_CHUNK_BYTES
                ]  # TODO: maybe that's not so efficient?

                chunk_timestamp_us = start_time_us + int(
                    (samples_sent / (STREAM_SAMPLE_RATE * STREAM_SAMPLE_SIZE)) * 1_000_000
                )

                binary_chunk = instance.pack_audio_chunk(
                    chunk_timestamp_us, TARGET_CHUNK_SAMPLES, c
                )
                instance.send_binary(binary_chunk)

                samples_sent += TARGET_CHUNK_BYTES

                chunk_pos += TARGET_CHUNK_BYTES
                split_chunks_count += 1

            # self.logger.info(
            #     "Split into %s audio chunks. We sent %s bytes from %s bytes, we lost %s bytes",
            #     split_chunks_count,
            #     chunk_pos + TARGET_CHUNK_BYTES - 1,
            #     len(chunk),
            #     len(chunk) - (chunk_pos + TARGET_CHUNK_BYTES - 1),
            # )

            if preload > 0:
                preload -= split_chunks_count * TARGET_CHUNK_DURATION_MS
            else:
                # self.logger.info(
                #     "sleeping for %s", split_chunks_count * TARGET_CHUNK_DURATION_MS
                # )
                await asyncio.sleep(split_chunks_count * TARGET_CHUNK_DURATION_MS / 1000)

        self.logger.info(
            "Finished streaming queue %s (total samples sent: %s)",
            player_id,
            samples_sent,
        )

    async def remove_player(self, player_id: str) -> None:
        """Remove a player."""
        instance = self._get_player_instance(player_id)

        # Disconnect if currently connected
        if instance:
            await instance.disconnect()
            self.instances.remove(instance)

        # Remove from Music Assistant
        self.mass.players.remove(player_id)

    def _get_player_instance(self, player_id: str) -> PlayerInstance | None:
        for i in self.instances:
            if i.player_id == player_id:
                return i
        return None
