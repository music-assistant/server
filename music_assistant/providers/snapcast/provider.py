"""SnapCastProvider."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import socket
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from bidict import bidict
from music_assistant_models.enums import EventType, MediaType, PlaybackState
from music_assistant_models.errors import SetupFailedError
from music_assistant_models.player import PlayerMedia
from snapcast.control.server import CONTROL_PORT, Snapserver
from zeroconf import NonUniqueNameException
from zeroconf.asyncio import AsyncServiceInfo

from music_assistant.constants import CONF_ENABLED
from music_assistant.helpers.compare import create_safe_string
from music_assistant.helpers.process import AsyncProcess
from music_assistant.helpers.util import get_ip_pton
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.snapcast.constants import (
    CONF_EXTERNAL_DEDICATED_FALLBACK_GROUP,
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
    DEFAULT_SNAPSERVER_CONFIG_FILE,
    DEFAULT_SNAPSERVER_PLUGIN_DIR,
    DEFAULT_SNAPSERVER_PORT,
    MASS_ANNOUNCEMENT_POSTFIX,
    MASS_STREAM_PREFIX,
    SHIPPED_SNAPSERVER_CONFIG_FILE,
    SNAPWEB_DIR,
)
from music_assistant.providers.snapcast.group_materialize import SnapcastGroupMaterializer
from music_assistant.providers.snapcast.group_restore import SnapcastGroupRestorer
from music_assistant.providers.snapcast.ma_stream import SnapcastMAStream
from music_assistant.providers.snapcast.player import SnapCastPlayer
from music_assistant.providers.snapcast.stream_registry import SnapcastStreamRegistry
from music_assistant.providers.sync_group.constants import SGP_PREFIX
from music_assistant.providers.universal_group.constants import UGP_PREFIX

if TYPE_CHECKING:
    from music_assistant_models.event import MassEvent

    from .snap_cntrl_proto import SnapclientProto, SnapgroupProto, SnapserverProto


async def _create_cntrl_server(
    loop: asyncio.AbstractEventLoop,
    host: str,
    port: int = CONTROL_PORT,
    reconnect: bool = False,
) -> SnapserverProto:
    """Server factory."""
    server = Snapserver(loop, host, port, reconnect)
    await server.start()
    return cast("SnapserverProto", server)


class SnapCastProvider(PlayerProvider):
    """SnapCastProvider."""

    _snapserver: SnapserverProto
    _snapserver_runner: asyncio.Task[None] | None
    _snapserver_started: asyncio.Event | None
    _snapcast_server_host: str
    _snapcast_server_control_port: int
    _ids_map: bidict[str, str]  # ma_id / snapclient_id
    _use_builtin_server: bool
    _stop_called: bool
    _controlscript_available: bool
    _snapcast_ma_streams: dict[str, SnapcastMAStream]
    _snapcast_ma_streams_lock: asyncio.Lock
    _stream_registry: SnapcastStreamRegistry
    _unregister_resolve_api_command: Any
    _group_restore_lock: asyncio.Lock
    _external_dedicated_fallback_group: str | None
    _unsub_syncgroup_event_listener: Any

    def _get_stream_registry(self) -> SnapcastStreamRegistry:
        """Return the central stream registry, lazily initializing it when needed."""
        if not hasattr(self, "_stream_registry"):
            existing_streams = getattr(self, "_snapcast_ma_streams", {})
            self._stream_registry = SnapcastStreamRegistry(existing_streams)
            self._snapcast_ma_streams = self._stream_registry.streams_by_name
        return self._stream_registry

    @property
    def queue_control_available(self) -> bool:
        """Return whether queue-based control scripts are available.

        Indicates if the Snapcast control script has been successfully initialized
        and can be used to control playback via a queue-specific control channel.
        """
        return (
            self._use_builtin_server
            and self._controlscript_available
            and self._snapserver_started is not None
            and self._snapserver_started.is_set()
        )

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # set snapcast logging
        logging.getLogger("snapcast").setLevel(self.logger.level)
        self._use_builtin_server = not self.config.get_value(CONF_USE_EXTERNAL_SERVER)
        self._stop_called = False
        self._controlscript_available = False
        self._unregister_resolve_api_command = None
        self._unsub_syncgroup_event_listener = None
        if self._use_builtin_server:
            if Path(DEFAULT_SNAPSERVER_CONFIG_FILE).exists():
                self._snapcast_server_config_file = DEFAULT_SNAPSERVER_CONFIG_FILE
            else:
                # Fallback for dev environments without a Snapserver config file.
                # If the file is missing, Snapserver silently ignores all command-line arguments.
                self._snapcast_server_config_file = str(SHIPPED_SNAPSERVER_CONFIG_FILE)

            self._snapcast_server_host = "127.0.0.1"
            self._snapcast_server_control_port = DEFAULT_SNAPSERVER_PORT
            self._snapcast_server_buffer_size = cast(
                "int", self.config.get_value(CONF_SERVER_BUFFER_SIZE)
            )
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
        fallback_group_name = str(
            self.config.get_value(CONF_EXTERNAL_DEDICATED_FALLBACK_GROUP) or ""
        ).strip()
        self._external_dedicated_fallback_group = (
            fallback_group_name if fallback_group_name and not self._use_builtin_server else None
        )
        self._snapcast_stream_idle_threshold = self.config.get_value(CONF_STREAM_IDLE_THRESHOLD)
        self._ids_map = bidict({})

        self._stream_registry = SnapcastStreamRegistry()
        self._snapcast_ma_streams = self._stream_registry.streams_by_name
        self._snapcast_ma_streams_lock = asyncio.Lock()
        self._group_restore_lock = asyncio.Lock()

        if self._use_builtin_server:
            await self._start_builtin_server()
        else:
            self._snapserver_runner = None
            self._snapserver_started = None
        try:
            self._snapserver = await _create_cntrl_server(
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
        if self._unregister_resolve_api_command is None:
            self._unregister_resolve_api_command = self.mass.register_api_command(
                "snapcast/resolve_control_stream", self._api_resolve_control_stream
            )
        if self._unsub_syncgroup_event_listener is None:
            self._unsub_syncgroup_event_listener = self.mass.subscribe(
                self._on_mass_player_event,
                (EventType.PLAYER_ADDED, EventType.PLAYER_UPDATED, EventType.PLAYER_REMOVED),
            )
        # initial load of players
        self._handle_update()
        self.mass.call_later(
            1,
            self._restore_group_runtime_state,
            task_id=f"snapcast_group_restore_{self.instance_id}",
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
        self._stop_called = True
        if self._unregister_resolve_api_command is not None:
            self._unregister_resolve_api_command()
            self._unregister_resolve_api_command = None
        if self._unsub_syncgroup_event_listener is not None:
            self._unsub_syncgroup_event_listener()
            self._unsub_syncgroup_event_listener = None

        for snap_client in self._snapserver.clients:
            player_id = self._get_ma_id(snap_client.identifier)
            if not (player := self.mass.players.get_player(player_id, raise_unavailable=False)):
                continue
            if player.playback_state != PlaybackState.PLAYING:
                continue
            await player.stop()

        for stream_name in self._get_stream_registry().names():
            await self.delete_ma_stream(stream_name)

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

    def _setup_controlscript(self) -> str | None:
        """Copy control script to plugin directory (blocking I/O).

        :return: plugin dir if successful, None otherwise.
        """
        logger = self.logger.getChild("snapserver")
        if not CONTROL_SCRIPT.exists():
            logger.warning("Control script does not exist: %s", CONTROL_SCRIPT)
            return None

        candidates = (
            Path(DEFAULT_SNAPSERVER_PLUGIN_DIR),
            # fallback directory for dev environments
            Path(self.mass.storage_path) / "snapcast" / "plugins",
        )
        for plugin_dir in candidates:
            control_dest = plugin_dir / "control.py"
            try:
                plugin_dir.mkdir(parents=True, exist_ok=True)
                # Clean up existing file
                control_dest.unlink(missing_ok=True)

                # Copy the control script to the plugin directory
                shutil.copy2(CONTROL_SCRIPT, control_dest)
                # Ensure it's executable
                control_dest.chmod(0o755)
                logger.debug("Copied controlscript to: %s", control_dest)
                return str(plugin_dir)
            except (OSError, PermissionError) as err:
                logger.debug("Could not copy controlscript to %s : %s", plugin_dir, err)
        logger.warning("Could not copy controlscript (metadata/control disabled)")
        return None

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
                    await self.mass.discovery.aiozc.async_update_service(info)
                else:
                    await self.mass.discovery.aiozc.async_register_service(info, strict=False)
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
            f"--config={self._snapcast_server_config_file}",
            f"--server.datadir={self.mass.storage_path}",
            "--http.enabled=true",
            "--http.port=1780",
            f"--http.doc_root={SNAPWEB_DIR}",
            "--tcp-control.enabled=true",
            f"--tcp-control.port={self._snapcast_server_control_port}",
            "--stream.sampleformat=48000:16:2",
            f"--stream.buffer={self._snapcast_server_buffer_size}",
            f"--stream.chunk_ms={self._snapcast_server_chunk_ms}",
            f"--stream.codec={self._snapcast_server_transport_codec}",
            f"--stream.send_to_muted={str(self._snapcast_server_send_to_muted).lower()}",
            f"--streaming_client.initial_volume={self._snapcast_server_initial_volume}",
        ]
        loop = asyncio.get_running_loop()
        plugin_dir = await loop.run_in_executor(None, self._setup_controlscript)
        if plugin_dir is not None:
            args.append(f"--stream.plugin_dir={plugin_dir}")
            self._controlscript_available = True

        started_handle: asyncio.Handle | None = None
        async with AsyncProcess(args, stdout=True, name="snapserver") as snapserver_proc:
            try:
                # keep reading from stdout until exit
                async for raw_data in snapserver_proc.iter_any():
                    text = raw_data.decode().strip()
                    for line in text.split("\n"):
                        logger.debug(line)
                        if "(Snapserver) Version 0." in line:
                            # delay init a small bit to prevent race conditions
                            # where we try to connect too soon
                            if started_handle is None:
                                started_handle = self.mass.loop.call_later(
                                    2, self._snapserver_started.set
                                )

            except asyncio.CancelledError:
                # Currently, MA doesn't guarantee a defined shutdown order;
                # Make sure to close socket servers before
                # shutting down the snapcast server.
                #
                # The snapserver doesn't always cleanup the control script processes
                # properly. We do it explicitly when closing a socket server.
                # Should be fixed on the server side, though.
                for stream_name in self._get_stream_registry().names():
                    await self.delete_ma_stream(stream_name)
                self._get_stream_registry().clear()
                raise

            finally:
                if started_handle is not None:
                    started_handle.cancel()
                if self._snapserver_started is not None:
                    self._snapserver_started.clear()
                self._controlscript_available = False

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
            new_id = snap_client_id
            self._ids_map[new_id] = snap_client_id
            return new_id
        return self._get_ma_id(snap_client_id)

    def _handle_player_init(self, snap_client: SnapclientProto) -> SnapCastPlayer | None:
        """Process Snapcast add to Player controller."""
        player_id = self._generate_and_register_id(snap_client.identifier)
        if not self.mass.config.get_raw_player_config_value(player_id, CONF_ENABLED, True):
            self.logger.debug("Ignoring disabled snapcast player: %s", player_id)
            return None
        player = self.mass.players.get_player(player_id, raise_unavailable=False)
        if not player:
            snap_client = self._snapserver.client(self._get_snapclient_id(player_id))
            player = SnapCastPlayer(
                provider=self,
                player_id=player_id,
                snap_client=snap_client,
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
            self._generate_and_register_id(snap_client.identifier)
        for snap_client in self._snapserver.clients:
            if not snap_client.identifier:
                continue
            if ma_player := self._handle_player_init(snap_client):
                snap_client.set_callback(ma_player._handle_player_update)
        for snap_client in self._snapserver.clients:
            if player := self.get_snap_player(client_id=snap_client.identifier):
                snap_client.set_callback(player._handle_player_update)
        self._update_group_callbacks()

    def poke_group_members(self, snap_group: SnapgroupProto) -> None:
        """Process Snapcast group callback."""
        for snap_client_id in snap_group.clients:
            if ma_player := self.get_snap_player(client_id=snap_client_id):
                ma_player.poke_player_update()

    def _handle_disconnect(self, exc: Exception) -> None:
        """Handle disconnect callback from snapserver."""
        if self._stop_called or self.mass.closing:
            # prevent auto-reconnecting of snapcast controller
            self._snapserver.stop()
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

    def _update_group_callbacks(self, poke: bool = False) -> None:
        for grp in self._snapserver.groups:
            grp.set_callback(self.poke_group_members)
            if poke:
                self.poke_group_members(grp)

    @property
    def dedicated_fallback_group_name(self) -> str | None:
        """Return the configured external Snapserver fallback group name."""
        return self._external_dedicated_fallback_group

    def get_snap_group(
        self, *, group_id: str | None = None, group_name: str | None = None
    ) -> SnapgroupProto | None:
        """Return a Snapcast group by identifier or by visible group name."""
        if group_id is not None:
            with suppress(KeyError):
                return self._snapserver.group(group_id)

        if group_name is None:
            return None

        for snap_group in self._snapserver.groups:
            if getattr(snap_group, "name", None) == group_name:
                return snap_group
        return None

    async def move_player_to_fallback_group(self, target_player_id: str) -> bool:
        """Move a player to the configured dedicated fallback group if available."""
        fallback_group_name = self.dedicated_fallback_group_name
        if not fallback_group_name:
            return False

        fallback_group = self.get_snap_group(group_name=fallback_group_name)
        if fallback_group is None:
            self.logger.warning(
                "Configured Snapcast fallback group '%s' does not exist; "
                "falling back to dedicated isolate flow",
                fallback_group_name,
            )
            return False

        target_client = self.get_snap_client(player_id=target_player_id)
        if target_client is None:
            return False

        current_group = getattr(target_client, "group", None)
        if current_group is not None and current_group.identifier == fallback_group.identifier:
            return True

        await fallback_group.add_client(target_client.identifier)
        return True

    async def ensure_player_owned_group(
        self, ma_player_id: str, set_stream_id: str | None = None
    ) -> SnapgroupProto | None:
        """Ensure a Snapcast group is owned by the given player.

        This method guarantees that the returned Snapcast group is *owned* by the
        specified Music Assistant player, meaning the group name equals the
        player's ID and the player is the group leader.

        Behavior:
        - If the player is already the leader of its current group, that group is
        returned unchanged.
        - If the player is a member of another group (but not the leader), the
        player is removed from that group, which causes Snapcast to create a new
        single-client group for the player.
        - The resulting group is renamed to the player's ID.

        If `set_stream_id` is provided and a new group is created, the group's
        stream is updated accordingly.

        Args:
            ma_player_id: Music Assistant player ID.
            set_stream_id: Optional Snapcast stream ID to assign to the player's group.

        Returns:
            The Snapcast group owned by the player, or ``None`` if the player is not
            currently part of any group.
        """
        player_client = self.get_snap_client(player_id=ma_player_id)
        if player_client is None:
            return None

        curr_group = player_client.group

        if curr_group is None:
            return None

        if curr_group.name == ma_player_id:
            return curr_group

        group_members = list(curr_group.clients)
        if len(group_members) > 1 and curr_group.name:
            # player is member of other player group, remove it, which results in a new group
            group_members.remove(player_client.identifier)
            res = await self._snapserver.group_clients(curr_group.identifier, group_members)
            if not (isinstance(res, dict) and "server" in res):
                raise RuntimeError("Couldn't remove client from group")
            self._snapserver.synchronize(res)
            curr_group = player_client.group
            if curr_group is None:
                return None
            if set_stream_id:
                await curr_group.set_stream(set_stream_id)

        await curr_group.set_name(ma_player_id)
        return curr_group

    async def isolate_player_to_dedicated_group(
        self,
        target_player_id: str,
        target_stream_id: str | None = None,
        others_stream_id: str | None = None,
    ) -> None:
        """Isolate a player into a dedicated Snapcast group.

        Ensures that the target player ends up in a group where it is the sole
        member and group leader.

        Behavior:
        - The target player is first ensured to own its group.
        - All other members of that group are removed.
        - Each removed player is placed into its own dedicated group.
        - Removed players' groups are optionally assigned `others_stream_id`.
        - The target group is optionally assigned `target_stream_id`.

        Callbacks for affected clients and groups are temporarily disabled during
        the operation to avoid intermediate state updates.

        Args:
            target_player_id: Music Assistant player ID to isolate.
            target_stream_id: Optional stream ID to assign to the target player's group.
            others_stream_id: Stream ID assigned to newly created groups for removed players.
        """
        this_client_id = self._get_snapclient_id(target_player_id)
        target_group = await self.ensure_player_owned_group(
            target_player_id, set_stream_id=target_stream_id
        )

        if target_group is None:
            return

        if others_stream_id is None:
            others_stream_id = self._get_stable_stream_reference(target_group.stream)

        target_group.set_callback(None)
        group_members = list(target_group.clients)
        group_members.remove(this_client_id)
        for client_id in group_members:
            client = self._snapserver.client(client_id)
            client.set_callback(None)
        if group_members:
            res = await self._snapserver.group_clients(target_group.identifier, [this_client_id])
            if not (isinstance(res, dict) and "server" in res):
                raise RuntimeError("Couldn't remove client from group")
            self._snapserver.synchronize(res)
            for client_id in group_members:
                ma_player_id = self._get_ma_id(client_id)
                if ma_player := cast("SnapCastPlayer", self.mass.players.get_player(ma_player_id)):
                    client = self._snapserver.client(client_id)
                    if client is not None:
                        if client.group is not None:
                            await client.group.set_name(ma_player_id)
                            if others_stream_id:
                                await client.group.set_stream(others_stream_id)
                        client.set_callback(ma_player._handle_player_update)

        if target_stream_id is not None:
            await target_group.set_stream(target_stream_id)

    async def ensure_sync_group_idle_stream(
        self, sync_group_player: Any
    ) -> SnapcastMAStream | None:
        """Ensure an idle Snapcast stream exists for a dynamic MA sync group."""
        stream_display_name = getattr(getattr(sync_group_player, "config", None), "name", None)
        if not stream_display_name:
            return None

        stream_name = create_safe_string(sync_group_player.player_id, lowercase=False)
        stream_name = f"{MASS_STREAM_PREFIX}idle_{stream_name}"
        idle_media = PlayerMedia(
            uri=f"snapcast-syncgroup://{sync_group_player.player_id}",
            media_type=MediaType.PLUGIN_SOURCE,
            title=stream_display_name,
            custom_data={
                "provider": self.instance_id,
                "source_id": sync_group_player.player_id,
                "player_id": sync_group_player.player_id,
            },
        )
        async with self._snapcast_ma_streams_lock:
            stream = self._get_stream_registry().get_by_stream_name(stream_name)
            if (
                stream is not None
                and stream.stream_display_name != stream_display_name
                and not stream.is_streaming
            ):
                await stream.destroy()
                self._get_stream_registry().unregister(stream_name)
                stream = None
            if stream is None:
                stream = SnapcastMAStream(
                    provider=self,
                    media=idle_media,
                    stream_name=stream_name,
                    stream_display_name=stream_display_name,
                    source_id=sync_group_player.player_id,
                    destroy_on_stop=False,
                )
                self._get_stream_registry().register(stream)
            else:
                stream.update_media(idle_media)
        await stream.setup()
        return stream

    async def get_snapcast_media_stream(
        self,
        media: PlayerMedia,
        filter_settings_owner: str | None = None,
        existing_only: bool = False,
    ) -> SnapcastMAStream | None:
        """Get or create a Snapcast Music Assistant stream for the given media.

        Determines a deterministic Snapcast stream name based on the media type
        and source, and either returns an existing stream or creates a new one.

        Behavior:
        - Announcement and generic media streams use a hashed name.
        - Plugin and queue-backed sources reuse a stable stream name.
        - Queue-backed streams may persist across playback sessions.
        - If `existing_only` is True, no new stream will be created.

        Newly created streams are registered with the Snapcast server and fully
        set up before being returned.

        Args:
            media: Media item to stream.
            filter_settings_owner: Optional player/entity ID used to resolve DSP filters.
            existing_only: If True, only return an existing stream.

        Returns:
            A ``SnapcastMAStream`` instance, or ``None`` if no stream exists and
            `existing_only` is True.
        """
        stream_name: str = ""
        name_suffix: str = ""
        queue_id: str | None = None
        source_id: str | None = None
        destroy_on_stop = True

        if media.media_type == MediaType.ANNOUNCEMENT:
            stream_name += hashlib.md5(media.uri.encode()).hexdigest()[:6]
            name_suffix = MASS_ANNOUNCEMENT_POSTFIX
        elif media.media_type == MediaType.PLUGIN_SOURCE:
            custom_data = media.custom_data or {}
            plugin: str = media.title or custom_data.get("provider") or ""
            player: str = f" {custom_data.get('player_id', '')}"
            stream_name += f"{plugin} {player}"
            source_id = custom_data.get("source_id")
        elif media.source_id and media.source_id.startswith(UGP_PREFIX):
            stream_name += media.source_id
        elif media.source_id and media.queue_item_id:
            stream_name += media.source_id
            queue_id = media.source_id
            source_id = media.source_id
            destroy_on_stop = False
        else:
            stream_name += hashlib.md5(media.uri.encode()).hexdigest()[:6]

        stream_name = create_safe_string(stream_name, lowercase=False)
        stream_name = f"{MASS_STREAM_PREFIX}{stream_name}{name_suffix}"
        stream_display_name = self._get_sync_group_stream_display_name(media) or stream_name
        stream_registry = self._get_stream_registry()
        async with self._snapcast_ma_streams_lock:
            stream = stream_registry.get_by_stream_name(stream_name)
            if (
                stream is not None
                and stream.stream_display_name != stream_display_name
                and not stream.is_streaming
            ):
                await stream.destroy()
                stream_registry.unregister(stream_name)
                stream = None

            if stream is None:
                if existing_only:
                    return None

                stream = SnapcastMAStream(
                    provider=self,
                    media=media,
                    stream_name=stream_name,
                    stream_display_name=stream_display_name,
                    filter_settings_owner=filter_settings_owner,
                    source_id=source_id,
                    queue_id=queue_id,
                    use_cntrl_script=bool(queue_id) and self.queue_control_available,
                    destroy_on_stop=destroy_on_stop,
                )
                stream_registry.register(stream)
            else:
                stream.update_media(media)
        await stream.setup()
        return stream

    def get_snap_ma_stream(self, stream_name: str) -> SnapcastMAStream | None:
        """Return an existing Music Assistant Snapcast stream by name.

        Args:
            stream_name: Snapcast stream name.

        Returns:
            The corresponding ``SnapcastMAStream`` instance, or ``None`` if not found.
        """
        matches = self._get_stream_registry().resolve_all(stream_name)
        if not matches:
            return None

        return max(
            matches,
            key=lambda stream: (
                getattr(stream, "stream_name", None) == stream_name,
                getattr(stream, "queue_id", None) == stream_name,
                getattr(stream, "stream_id", None) == stream_name,
                getattr(stream, "source_id", None) == stream_name,
                getattr(stream, "stream_display_name", None) == stream_name,
                bool(getattr(stream, "is_streaming", False)),
                getattr(stream, "queue_id", None) is not None,
            ),
        )

    async def delete_ma_stream(self, stream_name: str) -> None:
        """Remove and destroy a Music Assistant Snapcast stream.

        The stream is removed from internal tracking and its resources are
        destroyed asynchronously. Errors during destruction are logged but
        otherwise ignored.

        Args:
            stream_name: Snapcast stream name to delete.
        """
        async with self._snapcast_ma_streams_lock:
            stream = self._get_stream_registry().unregister(stream_name)

        if not stream:
            return

        try:
            await stream.destroy()
        except Exception:
            self.logger.exception("Failed to destroy stream session %s", stream_name)

    def update_stream_usage(self) -> None:
        """Update usage state for all tracked Snapcast streams.

        Marks streams as "in use" if they are currently assigned to any Snapcast
        group, and schedules unused streams for delayed shutdown.

        This method should be called whenever group or stream assignments change
        on the Snapcast server.
        """
        stream_registry = self._get_stream_registry()
        unused_streams = set(stream_registry.names())
        for grp in self._snapserver.groups:
            matching_streams = stream_registry.resolve_all(grp.stream)
            for ma_stream in matching_streams:
                ma_stream.set_in_use(True)
                unused_streams.discard(ma_stream.stream_name)

            if not unused_streams:
                break

        for stream_id in unused_streams:
            if unused_stream := stream_registry.get_by_stream_name(stream_id):
                unused_stream.set_in_use(False)

    def get_snap_client(
        self, *, client_id: str | None = None, player_id: str | None = None
    ) -> SnapclientProto | None:
        """Return the snapclient for either given client_id or player_id."""
        if player_id is not None:
            if client_id is not None and client_id != self._get_snapclient_id(client_id):
                raise ValueError("provided client_id and player_id do not match")
            client_id = self._get_snapclient_id(player_id)

        if client_id:
            with suppress(KeyError):
                return self._snapserver.client(client_id)

        return None

    def get_snap_player(
        self, *, client_id: str | None = None, player_id: str | None = None
    ) -> SnapCastPlayer | None:
        """Return the MA SnapCastPlayer for either given client_id or player_id."""
        if client_id is not None:
            if player_id is not None and player_id != self._get_ma_id(client_id):
                raise ValueError("provided client_id and player_id do not match")
            player_id = self._get_ma_id(client_id)

        if player_id is None:
            return None

        if ma_player := self.mass.players.get_player(player_id):
            assert isinstance(ma_player, SnapCastPlayer)  # for type checking
            return ma_player

        return None

    def resolve_sync_group_player(self, ref: str) -> Any | None:
        """Resolve a sync-group player by player id or configured sync-group name."""
        if player := self.mass.players.get_player(ref, raise_unavailable=False):
            if getattr(player, "player_id", "").startswith(SGP_PREFIX):
                return player

        lookup_name = ref.removeprefix(SGP_PREFIX) if ref.startswith(SGP_PREFIX) else ref
        all_players = getattr(self.mass.players, "all_players", None)
        players_iter = iter(all_players()) if callable(all_players) else iter(self.mass.players)

        for player in players_iter:
            if not getattr(player, "player_id", "").startswith(SGP_PREFIX):
                continue
            if getattr(getattr(player, "config", None), "name", None) == lookup_name:
                return player
        return None

    async def _restore_group_runtime_state(self) -> None:
        """Restore MA sync-group runtime state from live Snapcast groups."""
        async with self._group_restore_lock:
            await SnapcastGroupRestorer(self).restore()

    async def _materialize_sync_group_runtime_state(self, sync_group_player_id: str) -> None:
        """Materialize MA sync-group runtime state into native Snapcast state."""
        async with self._group_restore_lock:
            await SnapcastGroupMaterializer(self).materialize(sync_group_player_id)

    def _get_sync_group_stream_display_name(self, media: PlayerMedia) -> str | None:
        """Resolve the visible Snapcast stream name for sync-group backed playback."""
        player_ref: str | None = None
        if media.media_type == MediaType.PLUGIN_SOURCE:
            player_ref = (media.custom_data or {}).get("player_id")
        elif media.source_id:
            player_ref = media.source_id

        if not player_ref or not player_ref.startswith(SGP_PREFIX):
            return None

        if sync_group_player := self.resolve_sync_group_player(player_ref):
            return getattr(getattr(sync_group_player, "config", None), "name", None)
        return None

    def _get_stable_stream_reference(self, stream_ref: str | None) -> str:
        """Return the most stable human-visible reference for a Snapcast stream."""
        if not stream_ref:
            return "default"
        if stream_ref == "default":
            return stream_ref
        if ma_stream := self._get_stream_registry().resolve(stream_ref):
            return ma_stream.stream_display_name or ma_stream.stream_id or ma_stream.stream_name
        return stream_ref

    def resolve_control_stream(self, stream: str) -> dict[str, Any] | None:
        """Resolve a visible Snapcast stream reference to the active MA queue payload."""
        for ma_stream in self._get_stream_registry().resolve_all(stream):
            if not ma_stream.queue_id:
                continue

            if not (queue := self.mass.player_queues.get(ma_stream.queue_id)):
                continue

            return {
                "player_id": queue.queue_id,
                "queue_id": queue.queue_id,
                "queue": queue.to_dict(),
                "stream_id": ma_stream.stream_id,
                "stream_name": ma_stream.stream_name,
                "stream_display_name": ma_stream.stream_display_name,
            }
        return None

    async def _api_resolve_control_stream(self, stream: str) -> dict[str, Any] | None:
        """Async API wrapper for resolving a visible Snapcast stream reference."""
        return self.resolve_control_stream(stream)

    async def _on_mass_player_event(self, event: MassEvent) -> None:
        """Track sync-group membership changes and materialize native Snapcast state."""
        player_id = event.object_id
        if not player_id or not player_id.startswith(SGP_PREFIX):
            return

        if event.event == EventType.PLAYER_REMOVED:
            return

        self.mass.call_later(
            0.25,
            self._materialize_sync_group_runtime_state,
            player_id,
            task_id=f"snapcast_group_materialize_{self.instance_id}_{player_id}",
        )
