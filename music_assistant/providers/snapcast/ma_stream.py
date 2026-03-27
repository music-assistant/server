"""Music Assistant Snapcast source stream.

This module implements a Music Assistant-managed Snapcast stream that is exposed to the
Snapcast server as a TCP source. The stream is produced by running an FFmpeg pipeline
which pulls audio from Music Assistant and pushes it to the Snapcast source URI.

Optionally, a Unix socket server can be started to provide a control channel for a
Snapcast control script (used by the built-in Snapcast server integration).
"""

from __future__ import annotations

import asyncio
import os
import random
import time
import urllib.parse
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Literal, cast

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

    StreamLifecycleState = Literal["created", "attached", "unresolved", "destroyed"]

    def __init__(
        self,
        provider: SnapCastProvider,
        media: PlayerMedia,
        stream_name: str,
        stream_display_name: str | None = None,
        source_id: str | None = None,
        queue_id: str | None = None,
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
        self.stream_display_name = stream_display_name or stream_name
        self.snap_stream: SnapstreamProto | None = None

        self._provider = provider
        self._logger = provider.logger
        self._mass = provider.mass
        self._source_id = source_id
        self._queue_id = queue_id
        self._use_cntrl_script = use_cntrl_script
        self._cntrl_queue_id = queue_id if use_cntrl_script else None
        self._filter_settings_owner = filter_settings_owner
        self._destroy_on_stop = destroy_on_stop

        self._lifecycle_lock = asyncio.Lock()
        self._destroyed = False
        self._setup_done = False
        self._is_streaming = False
        self._is_paused = False
        self._restart_requested: bool = False
        self._stop_requested: bool = False
        self._streaming_started_at: float | None = None

        self._socket_server: SnapcastSocketServer | None = None
        self._socket_path: str | None = None
        self._streamer_task: asyncio.Task[None] | None = None
        self._stop_streamer_evt = asyncio.Event()
        self._streamer_started_evt = asyncio.Event()
        self._stop_timer: asyncio.Handle | None = None
        self._stop_timer_started_at: float | None = None
        self._filter_settings: list[str] | None = None
        self._lifecycle_state: SnapcastMAStream.StreamLifecycleState | None = None

    @property
    def source_id(self) -> str | None:
        """Return the source id this stream was created for."""
        return self._source_id

    @property
    def queue_id(self) -> str | None:
        """Return the queue id this stream was created for, if queue-backed."""
        return self._queue_id

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

    @property
    def is_paused(self) -> bool:
        """Return True if the stream is intentionally paused."""
        return self._is_paused

    @property
    def lifecycle_state(self) -> SnapcastMAStream.StreamLifecycleState | None:
        """Return the latest high-level Snapcast stream lifecycle state."""
        return self._lifecycle_state

    @property
    def playback_started_at(self) -> float | None:
        """Return when the playback started at the clients.

        return The (UTC) timestamp when the playback was started on the client
        or None if not started yet or not streaming.
        """
        if self._streaming_started_at is None:
            return None
        if self._provider._use_builtin_server:
            buffer_ms = self._provider._snapcast_server_buffer_size
            if time.time() - self._streaming_started_at < buffer_ms / 1000.0:
                return None
            return self._streaming_started_at + buffer_ms / 1000.0
        return self._streaming_started_at

    async def setup(self) -> None:
        """Prepare the Snapcast stream resources.

        Ensures a Snapcast source exists on the server. If `cntrl_queue_id` is set,
        also starts the Unix socket server used by the control script.
        """
        async with self._lifecycle_lock:
            if self._destroyed:
                raise RuntimeError("Session is destroyed")
            if self._setup_done and not await self._reset_incompatible_registration_if_needed():
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
        self._set_lifecycle_state("destroyed")

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
            self._is_paused = False
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
        self._is_paused = False
        self._request_stream_end()

    def request_pause_stream(self) -> None:
        """Request the streamer task to pause while preserving resume state."""
        self._is_paused = True
        self._request_stream_end()

    def _request_stream_end(self) -> None:
        """Request the current streamer run to end."""
        self._stop_requested = True
        self._restart_requested = False  # explicit stop cancels any pending restart
        self._stop_streamer_evt.set()
        self._cancel_stop_timer()

    def _cancel_stop_timer(self) -> None:
        """Cancel any pending inactivity stop timer."""
        self._stop_timer_started_at = None
        if self._stop_timer:
            self._stop_timer.cancel()
            self._stop_timer = None

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
        try:
            audio_source = self._mass.streams.get_stream(
                self.media, DEFAULT_SNAPCAST_FORMAT, self._filter_settings_owner
            )
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
                self._streaming_started_at = time.time()
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
        except asyncio.CancelledError:
            self._logger.debug("Snapcast stream %s cancelled", self.stream_name)
            raise
        except Exception as err:
            self._logger.error("Snapcast stream %s error: %s", self.stream_name, err, exc_info=err)
            raise
        finally:
            self._is_streaming = False
            self._logger.debug("Finished streaming to %s", stream_path)
            await self._wait_stream_idle()

    async def _wait_stream_idle(self) -> None:
        """Wait for the Snapcast stream to become idle after streaming ends."""
        try:

            async def wait_until_idle() -> None:
                while True:
                    stream_is_idle = False
                    with suppress(KeyError):
                        if self.snap_stream is None:
                            break
                        snap_stream = self._provider._snapserver.stream(self.snap_stream.identifier)
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
        finally:
            self._streaming_started_at = None

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
            self._set_lifecycle_state("attached", detail="reusing registered stream reference")
            return

        if existing_stream := self._find_existing_snapstream(require_idle=True):
            if self._snapstream_matches_expected_registration(existing_stream):
                self._attach_existing_snapstream(
                    existing_stream,
                    detail="reused idle Snapserver stream",
                )
                return
            await self._remove_conflicting_snapstream(existing_stream)

        extra_args = self._build_control_script_query_args()

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
                f"{extra_args}&name={urllib.parse.quote_plus(self.stream_display_name)}"
            )
            if result is None or "id" not in result:
                error_msg = self._extract_stream_add_error(result)
                if self._is_duplicate_stream_name_error(error_msg):
                    if existing_stream := self._find_existing_snapstream():
                        if self._snapstream_matches_expected_registration(existing_stream):
                            self._attach_existing_snapstream(
                                existing_stream,
                                detail="attached after duplicate stream-name response",
                            )
                            return
                    self._set_lifecycle_state("unresolved", detail=error_msg)
                    raise RuntimeError(error_msg)
                if self._is_retryable_stream_add_error(error_msg):
                    self._logger.warning(
                        "Retryable Snapcast stream create failure for %s (%s): %s",
                        self.stream_name,
                        self.stream_display_name,
                        error_msg,
                    )
                    continue
                self._set_lifecycle_state("unresolved", detail=error_msg)
                raise RuntimeError(error_msg)
            self.snap_stream = None
            if hasattr(self._provider._snapserver, "stream"):
                self.snap_stream = self._provider._snapserver.stream(result["id"])
            if self.snap_stream is None:
                self.snap_stream = self._find_existing_snapstream(stream_ref=result["id"])
            if self.snap_stream is None:
                error_msg = f"Unable to attach created Snapcast stream {result['id']}"
                self._set_lifecycle_state("unresolved", detail=error_msg)
                raise RuntimeError(error_msg)
            self.snap_stream.set_callback(self._snap_on_stream_update)
            self._set_lifecycle_state("created", snap_stream=self.snap_stream)
            return

        if self._socket_server:
            await self._stop_socket_server()

        msg = "Unable to create stream - No free port found?"
        self._set_lifecycle_state("unresolved", detail=msg)
        raise RuntimeError(msg)

    def _build_control_script_query_args(self) -> str:
        """Build optional Snapserver control script query parameters for this stream."""
        if (cntrl_queue_id := self._cntrl_queue_id) is not None:
            socket_path = self._socket_path
            if socket_path is None:
                raise RuntimeError("socket_path needs to be set if cntrl_queue_id is set")
            return (
                f"&controlscript={urllib.parse.quote_plus('control.py')}"
                f"&controlscriptparams=--queueid={urllib.parse.quote_plus(cntrl_queue_id)}%20"
                f"--socket={urllib.parse.quote_plus(socket_path)}%20"
                f"--streamserver-ip={self._mass.streams.publish_ip}%20"
                f"--streamserver-port={self._mass.streams.publish_port}"
            )

        if self._queue_id is not None and not self._provider._use_builtin_server:
            return (
                f"&controlscript={urllib.parse.quote_plus('mass_bridge.py')}"
                f"&controlscriptparams=--stream={urllib.parse.quote_plus(self.stream_display_name)}"
            )

        return ""

    async def _reset_incompatible_registration_if_needed(self) -> bool:
        """Remove an existing Snapserver registration if it no longer matches this stream."""
        if self.snap_stream is None:
            return False
        if self._snapstream_matches_expected_registration(self.snap_stream):
            return False
        await self._remove_conflicting_snapstream(self.snap_stream)
        self.snap_stream = None
        self._setup_done = False
        return True

    async def _remove_snap_source(self) -> None:
        """Remove the Snapcast source created for this stream and detach groups."""
        if self._mass.closing or self.snap_stream is None:
            return

        if self._provider._use_builtin_server:
            for snap_group in self._provider._snapserver.groups:
                if snap_group.stream != self.snap_stream.identifier:
                    continue
                self._logger.debug(f"Set stream of group {snap_group.name} to default.")
                await snap_group.set_stream("default")

        with suppress(KeyError, AttributeError):
            await self._provider._snapserver.stream_remove_stream(self.snap_stream.identifier)

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

    def _find_existing_snapstream(
        self,
        stream_ref: str | None = None,
        require_idle: bool = False,
    ) -> SnapstreamProto | None:
        """Find an existing Snapserver stream by id or visible stream name."""
        candidate_refs = {
            ref
            for ref in (stream_ref, self.stream_name, self.stream_display_name)
            if ref is not None
        }
        for snap_stream in getattr(self._provider._snapserver, "streams", []):
            if require_idle and getattr(snap_stream, "status", None) != "idle":
                continue
            visible_name = self._get_snapstream_visible_name(snap_stream)
            if (
                getattr(snap_stream, "identifier", None) in candidate_refs
                or getattr(snap_stream, "friendly_name", None) in candidate_refs
                or visible_name in candidate_refs
            ):
                return cast("SnapstreamProto", snap_stream)
        return None

    async def _remove_conflicting_snapstream(self, snap_stream: SnapstreamProto) -> None:
        """Remove an incompatible existing Snapserver stream registration."""
        if self._provider._use_builtin_server:
            for snap_group in self._provider._snapserver.groups:
                if snap_group.stream != getattr(snap_stream, "identifier", None):
                    continue
                await snap_group.set_stream("default")

        with suppress(KeyError, AttributeError):
            await self._provider._snapserver.stream_remove_stream(snap_stream.identifier)

    def _snapstream_matches_expected_registration(self, snap_stream: SnapstreamProto) -> bool:
        """Return True if an existing Snapserver stream matches this stream's control config."""
        return (
            self._get_snapstream_control_script_name(snap_stream)
            == self._expected_control_script_name()
        )

    def _expected_control_script_name(self) -> str | None:
        """Return the expected control script basename for this stream, if any."""
        if self._cntrl_queue_id is not None:
            return "control.py"
        if self._queue_id is not None and not self._provider._use_builtin_server:
            return "mass_bridge.py"
        return None

    def _get_snapstream_control_script_name(self, snap_stream: SnapstreamProto) -> str | None:
        """Extract the configured control script basename from a Snapserver stream."""
        raw_uri = getattr(snap_stream, "_stream", {}).get("uri", {}).get("raw")
        if not raw_uri:
            return None
        parsed = urllib.parse.urlparse(raw_uri)
        controlscript = urllib.parse.parse_qs(parsed.query).get("controlscript")
        if not controlscript:
            return None
        return os.path.basename(urllib.parse.unquote_plus(controlscript[0]))

    def _attach_existing_snapstream(
        self,
        snap_stream: SnapstreamProto,
        *,
        detail: str | None = None,
    ) -> None:
        """Attach this MA stream to an already existing Snapserver stream."""
        self.snap_stream = snap_stream
        self.snap_stream.set_callback(self._snap_on_stream_update)
        self._set_lifecycle_state("attached", snap_stream=snap_stream, detail=detail)

    def _set_lifecycle_state(
        self,
        state: StreamLifecycleState,
        *,
        snap_stream: SnapstreamProto | None = None,
        detail: str | None = None,
    ) -> None:
        """Persist and log a human-readable lifecycle transition."""
        self._lifecycle_state = state
        stream_id = getattr(snap_stream or self.snap_stream, "identifier", None)
        detail_suffix = f" ({detail})" if detail else ""
        self._logger.info(
            "Snapcast stream lifecycle=%s stream_name=%s display_name=%s stream_id=%s%s",
            state,
            self.stream_name,
            self.stream_display_name,
            stream_id,
            detail_suffix,
        )

    def _extract_stream_add_error(self, result: Any) -> str:
        """Normalize an add-stream error payload into a single readable message."""
        if result is None:
            return "Empty response from Snapserver while creating stream"
        if isinstance(result, dict):
            parts = [
                str(result.get("message") or "").strip(),
                str(result.get("data") or "").strip(),
            ]
            error_msg = " ".join(part for part in parts if part)
            return error_msg or str(result)
        return str(result)

    def _is_duplicate_stream_name_error(self, error_msg: str) -> bool:
        """Return True if the add-stream error indicates a duplicate visible stream name."""
        error_msg = error_msg.lower()
        return "already exists" in error_msg and "stream" in error_msg

    def _is_retryable_stream_add_error(self, error_msg: str) -> bool:
        """Return True if the add-stream failure is retryable on another random port."""
        error_msg = error_msg.lower()
        retryable_markers = (
            "address already in use",
            "bind failed",
            "eaddrinuse",
            "port is already in use",
            "failed to bind",
        )
        return any(marker in error_msg for marker in retryable_markers)

    def _get_snapstream_visible_name(self, snap_stream: SnapstreamProto) -> str | None:
        """Extract the configured visible name from a Snapserver stream object."""
        raw_uri = getattr(snap_stream, "_stream", {}).get("uri", {}).get("raw")
        if not raw_uri:
            return None
        parsed = urllib.parse.urlparse(raw_uri)
        name = urllib.parse.parse_qs(parsed.query).get("name")
        if not name:
            return None
        return urllib.parse.unquote_plus(name[0])

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
