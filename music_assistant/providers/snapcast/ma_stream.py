"""Music Assistant Snapcast source stream.

This module implements a Music Assistant-managed Snapcast stream that is exposed to the
Snapcast server as a TCP source. The stream is produced by running an FFmpeg pipeline
which pulls audio from Music Assistant and pushes it to the Snapcast source URI.

Optionally, a Unix socket server can be started to provide a control channel for a
Snapcast control script (used by the built-in Snapcast server integration).
"""

from __future__ import annotations

import asyncio
import random
import urllib.parse
from contextlib import suppress
from typing import TYPE_CHECKING, cast

from music_assistant.helpers.audio import get_player_filter_params
from music_assistant.helpers.ffmpeg import FFMpeg
from music_assistant.providers.snapcast.socket_server import SnapcastSocketServer

from .constants import (
    CONTROL_SOCKET_PATH_TEMPLATE,
    DEFAULT_SNAPCAST_FORMAT,
)

if TYPE_CHECKING:
    from music_assistant_models.player import PlayerMedia

    from .provider import SnapCastProvider
    from .snap_cntrl_proto import SnapstreamProto


class SnapcastMAStream:
    """A Music Assistant-managed Snapcast stream.

    The stream lifecycle is:
    - setup: ensure required server resources exist (Snapcast source, optional socket server)
    - start_stream: start the FFmpeg streaming task
    - request_stop_stream / wait_for_stopped: stop streaming and await termination
    - destroy: stop streaming, remove Snapcast source, and stop ancillary services

    If `cntrl_queue_id` is provided, a Unix socket server is started to allow a Snapcast
    control script to communicate with Music Assistant.
    """

    def __init__(
        self,
        provider: SnapCastProvider,
        media: PlayerMedia,
        stream_name: str,
        source_id: str | None = None,
        filter_settings_owner: str | None = None,
        use_cntrl_script: bool = False,
        destroy_on_stop: bool = False,
    ) -> None:
        """Initialize the stream.

        Args:
            provider: The Snapcast provider instance.
            media: The media item to stream.
            stream_name: Name used to register the stream on the Snapcast server.
            cntrl_queue_id: If set, enables the control socket server used by the control script.
            filter_settings_owner: Player/entity id used to fetch DSP/filter parameters.
            destroy_on_stop: If true, delete this MA stream once streaming stops.
        """
        self.media = media
        self.stream_name = stream_name
        self.snap_stream: SnapstreamProto | None = None

        self._provider = provider
        self._logger = provider.logger
        self._mass = provider.mass
        self._source_id = source_id
        self._use_cntrl_script = use_cntrl_script
        self._cntrl_queue_id = source_id if use_cntrl_script else None
        self._filter_settings_owner = filter_settings_owner
        self._destroy_on_stop = destroy_on_stop

        self._lifecycle_lock = asyncio.Lock()
        self._destroyed = False
        self._setup_done = False
        self._is_streaming = False
        self._restart_requested: bool = False
        self._stop_requested: bool = False

        self._socket_server: SnapcastSocketServer | None = None
        self._socket_path: str | None = None
        self._streamer_task: asyncio.Task[None] | None = None
        self._stop_streamer_evt = asyncio.Event()
        self._streamer_started_evt = asyncio.Event()
        self._stop_timer: asyncio.Handle | None = None
        self._stop_timer_started_at: float | None = None
        self._filter_settings: list[str] | None = None

    @property
    def source_id(self) -> str | None:
        """Return the source id this stream was created for."""
        return self._source_id

    @property
    def stream_id(self) -> str | None:
        """Return the Snapcast stream identifier, if registered."""
        if self.snap_stream:
            return self.snap_stream.identifier
        return None

    @property
    def is_streaming(self) -> bool:
        """Return True if the FFmpeg streaming task is currently running."""
        return self._is_streaming

    async def setup(self) -> None:
        """Prepare the Snapcast stream resources.

        Ensures a Snapcast source exists on the server. If `cntrl_queue_id` is set,
        also starts the Unix socket server used by the control script.
        """
        async with self._lifecycle_lock:
            if self._destroyed:
                raise RuntimeError("Session is destroyed")
            if self._setup_done:
                return
            if self._provider._snapserver is None:
                raise RuntimeError("Snapserver needs to be setup first")

            if self._cntrl_queue_id:
                await self._start_socket_server()

            await self._register_tcp_server_source()
            self._setup_done = True

    async def destroy(self) -> None:
        """Stop streaming and tear down all resources.

        This stops the streamer task (if running), removes the Snapcast source,
        and stops the optional control socket server.
        """
        async with self._lifecycle_lock:
            if self._destroyed:
                return
            self._destroyed = True

        self.request_stop_stream()
        await self.wait_for_stopped()
        await self._remove_snap_source()
        await self._stop_socket_server()

    async def start_stream(self, allow_restart: bool = False) -> None:
        """Start streaming the configured media to the Snapcast source.

        Raises:
            RuntimeError: If the streamer task is already running.
        """
        await self.setup()
        async with self._lifecycle_lock:
            if self._streamer_task and not self._streamer_task.done():
                if not allow_restart:
                    raise RuntimeError("streamer already running")
                self._restart_if_running()
                return

            self._stop_requested = False
            self._restart_requested = False
            self._stop_streamer_evt.clear()
            self._streamer_started_evt.clear()
            self._streamer_task = self._mass.create_task(self._streamer_task_impl())
            self._streamer_task.add_done_callback(self._on_streamer_done)

    async def wait_for_started(self, timeout_sec: float | None = None) -> None:
        """Wait until the streamer task signals it has started.

        Args:
            timeout_sec: Optional timeout in seconds.
        """
        try:
            await asyncio.wait_for(self._streamer_started_evt.wait(), timeout_sec)
        except TimeoutError:
            self._logger.warning(
                "Timeout waiting for stream %s to start; Canceling...",
                self.stream_name,
            )

    def update_media(self, media: PlayerMedia) -> None:
        """Update the media to play and restart the stream if required."""
        if media != self.media:
            self.media = media
            self._restart_if_running()

    def update_filter_settings(self, from_player: str | None = None) -> None:
        """Update the filter setting."""
        take_from = from_player or self._filter_settings_owner
        if not take_from:
            raise RuntimeError("No player provided to read filter settings from.")
        new_settings = get_player_filter_params(
            self._mass,
            take_from,
            DEFAULT_SNAPCAST_FORMAT,
            DEFAULT_SNAPCAST_FORMAT,
        )
        if from_player:
            self._filter_settings_owner = from_player
        if new_settings != self._filter_settings:
            self._restart_if_running()

    def request_stop_stream(self) -> None:
        """Request the streamer task to stop.

        This is cooperative: the streamer task will stop when it observes the stop event.
        Any pending inactivity stop timer is canceled.
        """
        self._stop_requested = True
        self._restart_requested = False  # explicit stop cancels any pending restart
        self._stop_streamer_evt.set()

        self._stop_timer_started_at = None
        if self._stop_timer:
            self._stop_timer.cancel()

    def set_in_use(self, in_use: bool) -> None:
        """Mark the stream as in-use or idle.

        When marked idle, a delayed stop is scheduled. When marked in-use, any pending
        delayed stop is canceled.
        """
        if in_use:
            self._stop_timer_started_at = None
            if self._stop_timer:
                self._stop_timer.cancel()
        elif self._stop_timer_started_at is None:
            self._stop_timer_started_at = self._mass.loop.time()
            self._stop_timer = self._mass.loop.call_later(60.0, self.request_stop_stream)

    async def wait_for_stopped(self, timeout_sec: float | None = None) -> None:
        """Wait for the streamer task to finish.

        If the task does not finish within the timeout, it is canceled and awaited.

        Args:
            timeout_sec: Optional timeout in seconds.
        """
        curr_task = self._streamer_task
        if not curr_task:
            return
        try:
            await asyncio.wait_for(curr_task, timeout_sec)
        except asyncio.CancelledError:
            self._logger.warning("Streamer task got canceled")
        except TimeoutError:
            self._logger.warning(
                "Timeout waiting for stream %s to finish; Canceling...",
                self.stream_name,
            )
            curr_task.cancel()
            await asyncio.gather(curr_task, return_exceptions=True)

    async def _streamer_task_impl(self) -> None:
        """Streamer task implementation.

        Runs FFmpeg to push audio to the Snapcast TCP source until FFmpeg exits or a stop
        request is received. After exit, waits briefly for the Snapcast stream to report
        an idle state.
        """
        stream_path = self._snap_get_stream_path()
        if stream_path is None:
            raise RuntimeError("The path to stream to is not set")

        self._logger.debug("Start streaming to %s", stream_path)
        self._stop_streamer_evt.clear()
        self._streamer_started_evt.clear()
        if self._filter_settings_owner:
            self._filter_settings = get_player_filter_params(
                self._mass,
                self._filter_settings_owner,
                DEFAULT_SNAPCAST_FORMAT,
                DEFAULT_SNAPCAST_FORMAT,
            )
        audio_source = self._mass.streams.get_stream(self.media, DEFAULT_SNAPCAST_FORMAT)
        try:
            async with FFMpeg(
                audio_input=audio_source,
                input_format=DEFAULT_SNAPCAST_FORMAT,
                output_format=DEFAULT_SNAPCAST_FORMAT,
                filter_params=self._filter_settings or [],
                audio_output=stream_path,
                extra_input_args=["-y", "-re"],
            ) as ffmpeg_proc:
                wait_ffmpeg = self._mass.create_task(ffmpeg_proc.wait())
                wait_stop = self._mass.create_task(self._stop_streamer_evt.wait())
                self._streamer_started_evt.set()
                self._is_streaming = True

                done, pending = await asyncio.wait(
                    {wait_ffmpeg, wait_stop},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if wait_stop in done and wait_ffmpeg not in done:
                    self._logger.debug("Stopping stream %s requested.", self.stream_name)
                    wait_ffmpeg.cancel()
                    await asyncio.gather(wait_ffmpeg, return_exceptions=True)
                    return

                await wait_ffmpeg
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

        finally:
            self._is_streaming = False
            self._logger.debug("Finished streaming to %s", stream_path)
            # Wait a bit for snap stream to become idle
            try:

                async def wait_until_idle() -> None:
                    while True:
                        stream_is_idle = False
                        with suppress(KeyError):
                            snap_stream = self._provider._snapserver.stream(self.stream_name)
                            stream_is_idle = snap_stream.status == "idle"
                        if self._mass.closing or stream_is_idle:
                            break
                        await asyncio.sleep(0.25)

                await asyncio.wait_for(wait_until_idle(), timeout=10.0)

            except TimeoutError:
                self._logger.warning(
                    "Timeout waiting for stream %s to become idle",
                    self.stream_name,
                )

    def _on_streamer_done(self, t: asyncio.Task[None]) -> None:
        """Handle streamer task completion and optional cleanup."""
        restart = False
        try:
            t.result()
        except asyncio.CancelledError:
            self._logger.debug("Streamer task cancelled: %s", self.stream_name)
        except Exception:
            self._logger.exception("Streamer task failed")
        finally:
            restart = self._restart_requested and not self._destroyed

            if self._streamer_task is t:
                self._streamer_task = None

            # reset per-run state
            self._restart_requested = False
            self._stop_requested = False
            self._stop_streamer_evt.clear()
            self._streamer_started_evt.clear()

        if restart:
            self._mass.create_task(self._restart_stream_locked())
        elif self._destroy_on_stop:
            self._mass.create_task(self._provider.delete_ma_stream(self.stream_name))

    def _restart_if_running(self) -> None:
        """Request a running stream to restart."""
        t = self._streamer_task
        if not t or t.done():
            return

        if self._stop_requested or self._stop_streamer_evt.is_set():
            return

        self._restart_requested = True
        self._stop_requested = True
        self._stop_streamer_evt.set()

        self._stop_timer_started_at = None
        if self._stop_timer:
            self._stop_timer.cancel()

    async def _restart_stream_locked(self) -> None:
        """Restart the streamer under the lifecycle lock."""
        async with self._lifecycle_lock:
            if self._destroyed:
                return
            if self._streamer_task and not self._streamer_task.done():
                return

            # reset state and start a fresh run
            self._stop_requested = False
            self._restart_requested = False
            self._stop_streamer_evt.clear()
            self._streamer_started_evt.clear()

            self._streamer_task = self._mass.create_task(self._streamer_task_impl())
            self._streamer_task.add_done_callback(self._on_streamer_done)

    async def _register_tcp_server_source(self) -> None:
        """Create a Snapcast TCP source for this stream (or reuse an existing one)."""
        # prefer to reuse existing stream if possible
        if self.snap_stream:
            return

        # The control script is used only for music streams in the builtin server
        extra_args = ""
        if (cntrl_queue_id := self._cntrl_queue_id) is not None:
            # Create socket server for control script communication
            socket_path = self._socket_path
            if socket_path is None:
                raise RuntimeError("socket_path needs to be set if cntrl_queue_id is set")
            extra_args = (
                f"&controlscript={urllib.parse.quote_plus('control.py')}"
                f"&controlscriptparams=--queueid={urllib.parse.quote_plus(cntrl_queue_id)}%20"
                f"--socket={urllib.parse.quote_plus(socket_path)}%20"
                f"--streamserver-ip={self._mass.streams.publish_ip}%20"
                f"--streamserver-port={self._mass.streams.publish_port}"
            )

        attempts = 50
        while attempts:
            attempts -= 1
            # pick a random port
            port = random.randint(4953, 4953 + 200)
            ## Do we need to add a time out here?
            result = await self._provider._snapserver.stream_add_stream(
                # NOTE: setting the sampleformat to something else
                # (like 24 bits bit depth) does not seem to work at all!
                f"tcp://0.0.0.0:{port}?sampleformat=48000:16:2"
                f"&idle_threshold={self._provider._snapcast_stream_idle_threshold}"
                f"{extra_args}&name={self.stream_name}"
            )
            if result is None or "id" not in result:
                # if the port is already taken, the result will be an error
                self._logger.warning(result)
                continue
            ## Do we need to synchronize the snapserver repr first?
            self.snap_stream = self._provider._snapserver.stream(result["id"])
            self.snap_stream.set_callback(self._snap_on_stream_update)
            return

        if self._socket_server:
            await self._stop_socket_server()

        msg = "Unable to create stream - No free port found?"
        raise RuntimeError(msg)

    async def _remove_snap_source(self) -> None:
        """Remove the Snapcast source created for this stream and detach groups."""
        if self._mass.closing or self.snap_stream is None:
            return

        for snap_group in self._provider._snapserver.groups:
            if snap_group.stream != self.snap_stream.identifier:
                continue
            self._logger.debug(f"Set stream of group {snap_group.name} to default.")
            await snap_group.set_stream("default")

        with suppress(KeyError, AttributeError):
            snap_stream = self._provider._snapserver.stream(self.stream_name)
            await self._provider._snapserver.stream_remove_stream(snap_stream.identifier)

        if self._socket_server:
            await self._stop_socket_server()
        self._snap_on_stream_update()

        return

    def _snap_get_stream_path(self) -> str | None:
        """Return the Snapcast TCP URI to stream to."""
        if self.snap_stream is None:
            return None

        uri = self.snap_stream._stream.get("uri", {})
        uri_host = uri.get("host", "")
        stream_path = self.snap_stream.path or f"tcp://{uri_host}"
        return stream_path.replace("0.0.0.0", self._provider._snapcast_server_host)

    def _snap_on_stream_update(self, stream: SnapstreamProto | None = None) -> None:
        """Handle Snapcast stream updates and trigger group member refresh."""
        if self.snap_stream is None:
            return

        for snap_group in self._provider._snapserver.groups:
            if snap_group.stream != self.snap_stream.identifier:
                continue
            self._provider.poke_group_members(snap_group)

    async def _start_socket_server(self) -> str:
        """Get or create a socket server for the given queue.

        :return: The path to the Unix socket.
        """
        if self._socket_server:
            return self._socket_server.socket_path

        if self._cntrl_queue_id is None:
            raise RuntimeError("Socket server require _cntrl_queue_id to be set")

        socket_path = CONTROL_SOCKET_PATH_TEMPLATE.format(queue_id=self._cntrl_queue_id)
        socket_server = SnapcastSocketServer(
            mass=self._mass,
            queue_id=self._cntrl_queue_id,
            socket_path=socket_path,
            streamserver_ip=str(self._mass.streams.publish_ip),
            streamserver_port=cast("int", self._mass.streams.publish_port),
        )
        await socket_server.start()
        self._socket_server = socket_server
        self._socket_path = socket_path
        self._logger.debug(
            "Created socket server for queue %s at %s", self._cntrl_queue_id, socket_path
        )
        return socket_path

    async def _stop_socket_server(self) -> None:
        """Stop and remove the socket server for the given queue."""
        if not self._socket_server:
            return

        await self._socket_server.stop()
        self._socket_server = None
        self._logger.debug("Stopped socket server for queue %s", self._cntrl_queue_id)
