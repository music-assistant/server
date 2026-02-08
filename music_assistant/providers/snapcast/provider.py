"""SnapCastProvider."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shutil
import socket
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, cast

from bidict import bidict
from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.errors import SetupFailedError
from snapcast.control.server import CONTROL_PORT, Snapserver
from zeroconf import NonUniqueNameException
from zeroconf.asyncio import AsyncServiceInfo

from music_assistant.helpers.compare import create_safe_string
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
    DEFAULT_SNAPSERVER_PORT,
    MASS_ANNOUNCEMENT_POSTFIX,
    MASS_STREAM_PREFIX,
    SNAPWEB_DIR,
)
from music_assistant.providers.snapcast.ma_stream import SnapcastMAStream
from music_assistant.providers.snapcast.player import SnapCastPlayer
from music_assistant.providers.universal_group.constants import UGP_PREFIX

if TYPE_CHECKING:
    from music_assistant_models.player import PlayerMedia

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

    @property
    def use_queue_control(self) -> bool:
        """Return whether queue-based control scripts are available.

        Indicates if the Snapcast control script has been successfully initialized
        and can be used to control playback via a queue-specific control channel.
        """
        return self._controlscript_available

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # set snapcast logging
        logging.getLogger("snapcast").setLevel(self.logger.level)
        self._use_builtin_server = not self.config.get_value(CONF_USE_EXTERNAL_SERVER)
        self._stop_called = False
        self._controlscript_available = False
        if self._use_builtin_server:
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
        self._snapcast_stream_idle_threshold = self.config.get_value(CONF_STREAM_IDLE_THRESHOLD)
        self._ids_map = bidict({})

        self._snapcast_ma_streams = {}
        self._snapcast_ma_streams_lock = asyncio.Lock()

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
        # initial load of players
        self._handle_update()

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
        self._stop_called = True

        for snap_client in self._snapserver.clients:
            player_id = self._get_ma_id(snap_client.identifier)
            if not (player := self.mass.players.get(player_id, raise_unavailable=False)):
                continue
            if player.playback_state != PlaybackState.PLAYING:
                continue
            await player.stop()

        for stream_name in list(self._snapcast_ma_streams):
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
            try:
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
            except asyncio.CancelledError:
                # Currently, MA doesn't guarantee a defined shutdown order;
                # Make sure to close socket servers before
                # shutting down the snapcast server.
                #
                # The snapserver doesn't always cleanup the control script processes
                # properly. We do it explicitly when closing a socket server.
                # Should be fixed on the server side, though.
                for stream_name in list(self._snapcast_ma_streams):
                    await self.delete_ma_stream(stream_name)
                self._snapcast_ma_streams.clear()
                raise

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
        return self._get_ma_id(snap_client_id)

    def _handle_player_init(self, snap_client: SnapclientProto) -> SnapCastPlayer:
        """Process Snapcast add to Player controller."""
        player_id = self._generate_and_register_id(snap_client.identifier)
        player = self.mass.players.get(player_id, raise_unavailable=False)
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
        others_stream_id: str | None = "default",
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
                if ma_player := cast("SnapCastPlayer", self.mass.players.get(ma_player_id)):
                    client = self._snapserver.client(client_id)
                    if client is not None:
                        if client.group is not None:
                            await client.group.set_name(ma_player_id)
                            if others_stream_id:
                                await client.group.set_stream(others_stream_id)
                        client.set_callback(ma_player._handle_player_update)

        if target_stream_id is not None:
            await target_group.set_stream(target_stream_id)

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
        async with self._snapcast_ma_streams_lock:
            if not (stream := self._snapcast_ma_streams.get(stream_name)):
                if existing_only:
                    return None

                stream = SnapcastMAStream(
                    provider=self,
                    media=media,
                    stream_name=stream_name,
                    filter_settings_owner=filter_settings_owner,
                    source_id=source_id,
                    use_cntrl_script=bool(queue_id) and self.use_queue_control,
                    destroy_on_stop=destroy_on_stop,
                )
                self._snapcast_ma_streams[stream_name] = stream
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
        return self._snapcast_ma_streams.get(stream_name)

    async def delete_ma_stream(self, stream_name: str) -> None:
        """Remove and destroy a Music Assistant Snapcast stream.

        The stream is removed from internal tracking and its resources are
        destroyed asynchronously. Errors during destruction are logged but
        otherwise ignored.

        Args:
            stream_name: Snapcast stream name to delete.
        """
        async with self._snapcast_ma_streams_lock:
            stream = self._snapcast_ma_streams.pop(stream_name, None)

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
        unused_streams = set(self._snapcast_ma_streams.keys())
        for grp in self._snapserver.groups:
            stream_id = grp.stream
            if stream_id in self._snapcast_ma_streams:
                ma_stream = self._snapcast_ma_streams[stream_id]
                ma_stream.set_in_use(True)
                unused_streams.discard(stream_id)

            if not unused_streams:
                break

        for stream_id in unused_streams:
            self._snapcast_ma_streams[stream_id].set_in_use(False)

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

        if ma_player := self.mass.players.get(player_id):
            assert isinstance(ma_player, SnapCastPlayer)  # for type checking
            return ma_player

        return None
