"""SnapCastProvider."""

import asyncio
import logging
import re
import shutil
import socket
from pathlib import Path
from typing import cast

from bidict import bidict
from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import SetupFailedError
from snapcast.control import create_server
from snapcast.control.client import Snapclient
from snapcast.control.group import Snapgroup
from snapcast.control.server import Snapserver
from zeroconf import NonUniqueNameException
from zeroconf.asyncio import AsyncServiceInfo

from music_assistant.helpers.process import AsyncProcess
from music_assistant.helpers.util import get_ip_pton
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.snapcast.constants import (
    CONF_SERVER_BUFFER_SIZE,
    CONF_SERVER_CHUNK_MS,
    CONF_SERVER_CONTROL_PORT,
    CONF_SERVER_HOST,
    CONF_SERVER_INITIAL_VOLUME,
    CONF_SERVER_SEND_AUDIO_TO_MUTED,
    CONF_SERVER_TRANSPORT_CODEC,
    CONF_STREAM_IDLE_THRESHOLD,
    CONF_USE_EXTERNAL_SERVER,
    CONTROL_SCRIPT,
    CONTROL_SOCKET_PATH_TEMPLATE,
    DEFAULT_SNAPSERVER_PORT,
    SNAPWEB_DIR,
)
from music_assistant.providers.snapcast.player import SnapCastPlayer
from music_assistant.providers.snapcast.socket_server import SnapcastSocketServer


class SnapCastProvider(PlayerProvider):
    """SnapCastProvider."""

    _snapserver: Snapserver
    _snapserver_runner: asyncio.Task[None] | None
    _snapserver_started: asyncio.Event | None
    _snapcast_server_host: str
    _snapcast_server_control_port: int
    _ids_map: bidict[str, str]  # ma_id / snapclient_id
    _use_builtin_server: bool
    _stop_called: bool
    _controlscript_available: bool
    _socket_servers: dict[str, SnapcastSocketServer]  # queue_id -> socket server

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # set snapcast logging
        logging.getLogger("snapcast").setLevel(self.logger.level)
        self._use_builtin_server = not self.config.get_value(CONF_USE_EXTERNAL_SERVER)
        self._stop_called = False
        self._controlscript_available = False
        self._socket_servers = {}
        if self._use_builtin_server:
            self._snapcast_server_host = "127.0.0.1"
            self._snapcast_server_control_port = DEFAULT_SNAPSERVER_PORT
            self._snapcast_server_buffer_size = self.config.get_value(CONF_SERVER_BUFFER_SIZE)
            self._snapcast_server_chunk_ms = self.config.get_value(CONF_SERVER_CHUNK_MS)
            self._snapcast_server_initial_volume = self.config.get_value(CONF_SERVER_INITIAL_VOLUME)
            self._snapcast_server_send_to_muted = self.config.get_value(
                CONF_SERVER_SEND_AUDIO_TO_MUTED
            )
            self._snapcast_server_transport_codec = self.config.get_value(
                CONF_SERVER_TRANSPORT_CODEC
            )
        else:
            self._snapcast_server_host = str(self.config.get_value(CONF_SERVER_HOST))
            self._snapcast_server_control_port = int(
                str(self.config.get_value(CONF_SERVER_CONTROL_PORT))
            )
        self._snapcast_stream_idle_threshold = self.config.get_value(CONF_STREAM_IDLE_THRESHOLD)
        self._ids_map = bidict({})

        if self._use_builtin_server:
            await self._start_builtin_server()
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
            # register callback for when the connection gets lost to the snapserver
            self._snapserver.set_on_disconnect_callback(self._handle_disconnect)

        except OSError as err:
            msg = "Unable to start the Snapserver connection ?"
            raise SetupFailedError(msg) from err

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await super().loaded_in_mass()
        # initial load of players
        self._handle_update()

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
        self._stop_called = True
        # Stop all socket servers
        for socket_server in list(self._socket_servers.values()):
            await socket_server.stop()
        self._socket_servers.clear()
        for snap_client in self._snapserver.clients:
            player_id = self._get_ma_id(snap_client.identifier)
            if not (player := self.mass.players.get(player_id, raise_unavailable=False)):
                continue
            if player.playback_state != PlaybackState.PLAYING:
                continue
            await player.stop()
        self._snapserver.stop()
        await self._stop_builtin_server()

    async def _start_builtin_server(self) -> None:
        """Start the built-in Snapserver."""
        if self._use_builtin_server:
            self._snapserver_started = asyncio.Event()
            self._snapserver_runner = self.mass.create_task(self._builtin_server_runner())
            await asyncio.wait_for(self._snapserver_started.wait(), 10)

    async def _stop_builtin_server(self) -> None:
        """Stop the built-in Snapserver."""
        self.logger.info("Stopping, built-in Snapserver")
        if self._snapserver_runner and not self._snapserver_runner.done():
            self._snapserver_runner.cancel()
            if self._snapserver_started is not None:
                self._snapserver_started.clear()

    def _setup_controlscript(self) -> bool:
        """Copy control script to plugin directory (blocking I/O).

        :return: True if successful, False otherwise.
        """
        plugin_dir = Path("/usr/share/snapserver/plug-ins")
        control_dest = plugin_dir / "control.py"
        logger = self.logger.getChild("snapserver")
        try:
            plugin_dir.mkdir(parents=True, exist_ok=True)
            # Clean up existing file
            control_dest.unlink(missing_ok=True)
            if not CONTROL_SCRIPT.exists():
                logger.warning("Control script does not exist: %s", CONTROL_SCRIPT)
                return False
            # Copy the control script to the plugin directory
            shutil.copy2(CONTROL_SCRIPT, control_dest)
            # Ensure it's executable
            control_dest.chmod(0o755)
            logger.debug("Copied controlscript to: %s", control_dest)
            return True
        except (OSError, PermissionError) as err:
            logger.warning(
                "Could not copy controlscript (metadata/control disabled): %s",
                err,
            )
            return False

    async def _builtin_server_runner(self) -> None:
        """Start running the builtin snapserver."""
        assert self._snapserver_started is not None  # for type checking
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
                    addresses=[await get_ip_pton(str(self.mass.streams.publish_ip))],
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
                    "Could not register mdns record for %s as its already in use",
                    zeroconf_type,
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
            f"--tcp.port={self._snapcast_server_control_port}",
            "--stream.sampleformat=48000:16:2",
            f"--stream.buffer={self._snapcast_server_buffer_size}",
            f"--stream.chunk_ms={self._snapcast_server_chunk_ms}",
            f"--stream.codec={self._snapcast_server_transport_codec}",
            f"--stream.send_to_muted={str(self._snapcast_server_send_to_muted).lower()}",
            f"--streaming_client.initial_volume={self._snapcast_server_initial_volume}",
        ]
        async with AsyncProcess(args, stdout=True, name="snapserver") as snapserver_proc:
            # keep reading from stdout until exit
            async for raw_data in snapserver_proc.iter_any():
                text = raw_data.decode().strip()
                for line in text.split("\n"):
                    logger.debug(line)
                    if "(Snapserver) Version 0." in line:
                        # delay init a small bit to prevent race conditions
                        # where we try to connect too soon
                        self.mass.loop.call_later(2, self._snapserver_started.set)
                        # Copy control script after snapserver starts
                        # (run in executor to avoid blocking)
                        loop = asyncio.get_running_loop()
                        self._controlscript_available = await loop.run_in_executor(
                            None, self._setup_controlscript
                        )

    def _get_ma_id(self, snap_client_id: str) -> str:
        search_dict = self._ids_map.inverse
        ma_id = search_dict.get(snap_client_id)
        assert ma_id is not None  # for type checking
        return ma_id

    def _get_snapclient_id(self, player_id: str) -> str:
        search_dict = self._ids_map
        snap_id = search_dict.get(player_id)
        assert snap_id is not None  # for type checking
        return snap_id

    def _generate_and_register_id(self, snap_client_id: str) -> str:
        search_dict = self._ids_map.inverse
        if snap_client_id not in search_dict:
            new_id = "ma_" + str(re.sub(r"\W+", "", snap_client_id))
            self._ids_map[new_id] = snap_client_id
            return new_id
        else:
            return self._get_ma_id(snap_client_id)

    def _handle_player_init(self, snap_client: Snapclient) -> SnapCastPlayer:
        """Process Snapcast add to Player controller."""
        player_id = self._generate_and_register_id(snap_client.identifier)
        player = self.mass.players.get(player_id, raise_unavailable=False)
        if not player:
            snap_client = cast(
                "Snapclient", self._snapserver.client(self._get_snapclient_id(player_id))
            )
            player = SnapCastPlayer(
                provider=self,
                player_id=player_id,
                snap_client=snap_client,
                snap_client_id=self._get_snapclient_id(player_id),
            )
            player.setup()
        else:
            player = cast("SnapCastPlayer", player)  # for type checking
        asyncio.run_coroutine_threadsafe(
            self.mass.players.register_or_update(player), loop=self.mass.loop
        )
        return player

    def _handle_update(self) -> None:
        """Process Snapcast init Player/Group and set callback ."""
        for snap_client in self._snapserver.clients:
            if not snap_client.identifier:
                self.logger.warning(
                    "Detected Snapclient %s without identifier, skipping", snap_client.friendly_name
                )
                continue
            if ma_player := self._handle_player_init(snap_client):
                snap_client.set_callback(ma_player._handle_player_update)
        for snap_client in self._snapserver.clients:
            if player := self.mass.players.get(self._get_ma_id(snap_client.identifier)):
                ma_player = cast("SnapCastPlayer", player)
                snap_client.set_callback(ma_player._handle_player_update)
        for snap_group in self._snapserver.groups:
            snap_group.set_callback(self._handle_group_update)

    def _handle_group_update(self, snap_group: Snapgroup) -> None:
        """Process Snapcast group callback."""
        for snap_client in self._snapserver.clients:
            if ma_player := self.mass.players.get(self._get_ma_id(snap_client.identifier)):
                assert isinstance(ma_player, SnapCastPlayer)  # for type checking
                ma_player._handle_player_update(snap_client)

    def _handle_disconnect(self, exc: Exception) -> None:
        """Handle disconnect callback from snapserver."""
        if self._stop_called or self.mass.closing:
            # we're instructed to stop/exit, so no need to restart the connection
            return
        self.logger.info(
            "Connection to SnapServer lost, reason: %s. Reloading provider in 5 seconds.",
            str(exc),
        )
        # schedule a reload of the provider
        self.mass.call_later(5, self.mass.load_provider, self.instance_id, allow_retry=True)

    async def remove_player(self, player_id: str) -> None:
        """Remove the client from the snapserver when it is deleted."""
        success, error_msg = await self._snapserver.delete_client(
            self._get_snapclient_id(player_id)
        )
        if success:
            self.logger.debug("Snapclient removed %s", player_id)
        else:
            self.logger.warning("Unable to remove snapclient %s: %s", player_id, error_msg)

    async def get_or_create_socket_server(self, queue_id: str) -> str:
        """Get or create a socket server for the given queue.

        :param queue_id: The queue ID to create a socket server for.
        :return: The path to the Unix socket.
        """
        if queue_id in self._socket_servers:
            return self._socket_servers[queue_id].socket_path

        socket_path = CONTROL_SOCKET_PATH_TEMPLATE.format(queue_id=queue_id)
        socket_server = SnapcastSocketServer(
            mass=self.mass,
            queue_id=queue_id,
            socket_path=socket_path,
            streamserver_ip=str(self.mass.streams.publish_ip),
            streamserver_port=cast("int", self.mass.streams.publish_port),
        )
        await socket_server.start()
        self._socket_servers[queue_id] = socket_server
        self.logger.debug("Created socket server for queue %s at %s", queue_id, socket_path)
        return socket_path

    async def stop_socket_server(self, queue_id: str) -> None:
        """Stop and remove the socket server for the given queue.

        :param queue_id: The queue ID to stop the socket server for.
        """
        if queue_id in self._socket_servers:
            await self._socket_servers[queue_id].stop()
            del self._socket_servers[queue_id]
            self.logger.debug("Stopped socket server for queue %s", queue_id)
