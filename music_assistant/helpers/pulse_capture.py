"""
Private PulseAudio capture server for grabbing PCM audio from client applications.

Runs a minimal, MA-owned classic PulseAudio daemon (no autodetected devices, no
network) whose module-pipe-sink FIFOs let MA capture the audio output of external
client processes that can only play via PulseAudio/PipeWire (e.g. the official
Spotify client). Consumers read a sink's FIFO with
``helpers.named_pipe.read_named_pipe`` or by pointing ffmpeg at it directly.

Also hosts :class:`PAVolumeController`, the shared ctypes libpulse controller for
sink volume and module control, usable against any PA server (the private capture
daemon or the system/host one).
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import math
import os
import shutil
import threading
import uuid
import weakref
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Final

from music_assistant.constants import MASS_LOGGER_NAME
from music_assistant.helpers.process import AsyncProcess, check_output, get_subprocess_env

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.helpers.pulse_capture")

# Fixed capture format for all pipe sinks: 32-bit little-endian PCM, CD sample
# rate, stereo. s32le keeps full precision of any 16/24-bit source even when the
# sink volume applies (reciprocal) gain before MA reads the samples back.
CAPTURE_SAMPLE_FORMAT: Final[str] = "s32le"
CAPTURE_SAMPLE_RATE: Final[int] = 44100
CAPTURE_CHANNELS: Final[int] = 2

PA_VOLUME_NORM: Final = 65536  # 100% (0 dB) on PA's raw volume scale
PA_CHANNELS_MAX: Final = 32

# Ceiling for the raw (linear) volume setter. Reciprocal cubic compensation
# needs up to 10000% (a client stream at 1% compensates with a 100x sink gain);
# the resulting raw value (100 * PA_VOLUME_NORM) stays far below PA_VOLUME_MAX.
MAX_RAW_VOLUME_PCT: Final = 10000.0

# Daemon startup: how long to wait for the native socket to appear, and the
# poll interval while waiting.
_READY_TIMEOUT: Final = 10.0
_READY_POLL_INTERVAL: Final = 0.1

# Supervised-restart backoff: starts small so a one-off crash recovers fast,
# doubles up to the max so a permanently failing daemon doesn't spin.
_RESTART_BACKOFF_INITIAL: Final = 1.0
_RESTART_BACKOFF_MAX: Final = 30.0

# set_sink_volume() is called frequently (on every volume/mute change, and
# at bridge start for every player). A short timeout limits how long a
# stuck/unresponsive PA call can occupy an executor thread — under normal
# conditions PA responds in single-digit milliseconds, so 0.5s is generous
# while bounding the worst case. load_module()/unload_module() (rare,
# one-time during topology setup/teardown) keep a longer 2.0s timeout since
# we'd rather wait than have sink creation/cleanup spuriously fail.
_SET_VOLUME_TIMEOUT: Final = 0.5

PA_CONTEXT_READY: Final = 4
PA_CONTEXT_FAILED: Final = 5
PA_CONTEXT_TERMINATED: Final = 6
PA_CONTEXT_NOAUTOSPAWN: Final = 1

_CONTEXT_NOTIFY_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)
_CONTEXT_SUCCESS_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p)
_CONTEXT_INDEX_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p)

PA_INVALID_INDEX: Final = 0xFFFFFFFF

# --- Audio taper curve (dr-lex exponential, with linear roll-off) -----------
#
# y = a * e^(b*x) gives constant dB-per-slider-step ("audio taper" /
# logarithmic potentiometer behavior), unlike a plain linear-amplitude
# mapping (y = x) where the bottom of the slider is wildly more sensitive
# than the top. See https://www.dr-lex.be/info-stuff/volumecontrols.html
#
# a = 10**(-range_dB/20) sets the amplitude floor; b = ln(1/a) ensures
# y(1.0) = 1.0 (0dB) at full volume. Below _TAPER_ROLLOFF_X, a linear ramp
# to (0, 0) is used so volume_pct=0 is true silence rather than asymptoting
# toward the floor.
#
# Reference values for common dB ranges (pick one _TAPER_A and comment out
# the rest; _TAPER_B recalculates automatically):
#
#   Range   _TAPER_A   _TAPER_B   MA 70% =    Notes
#   40 dB   0.01       ~4.605     -12 dB      receiver / outdoor speakers (current)
#   50 dB   0.003162   ~5.757     -15 dB      medium-range setups
#   60 dB   0.001      ~6.908     -18 dB      consumer headphones / desktop speakers
#   70 dB   0.000316   ~8.059     -21 dB      high-dynamic-range hi-fi systems
#
# Used both for PA hardware volume (PAVolumeController.set_sink_volume, after
# a cube-root step to counteract PA's own cubic volume curve) and for the
# software PCM-scaling fallback path in local_audio, so the same slider
# position sounds the same regardless of which volume-control mode is active.
_TAPER_A: Final = 0.01  # 10**(-40/20) — 40dB range, suits receiver/outdoor setups
# _TAPER_A: Final = 0.003162  # 10**(-50/20) — 50dB range
# _TAPER_A: Final = 0.001     # 10**(-60/20) — 60dB range, suits headphones/desktop
# _TAPER_A: Final = 0.000316  # 10**(-70/20) — 70dB range, hi-fi high dynamic range
_TAPER_B: Final = math.log(1.0 / _TAPER_A)  # recalculates automatically from _TAPER_A
_TAPER_ROLLOFF_X: Final = 0.10  # below 10% slider, linear ramp to true silence


def volume_pct_to_amplitude(volume_pct: int) -> float:
    """
    Map a 0-100 volume percentage to a linear amplitude scale factor.

    Uses the dr-lex exponential audio taper (y = a*e^(b*x)) for
    volume_pct >= 10, giving constant dB change per slider step. Below 10%,
    a linear ramp to (0, 0) ensures volume_pct=0 produces true silence.
    """
    x = max(0, min(volume_pct, 100)) / 100.0
    if x <= 0:
        return 0.0
    if x < _TAPER_ROLLOFF_X:
        y1 = _TAPER_A * math.exp(_TAPER_B * _TAPER_ROLLOFF_X)
        return y1 * (x / _TAPER_ROLLOFF_X)
    return _TAPER_A * math.exp(_TAPER_B * x)


def get_default_pulse_server() -> str:
    """
    Detect the system's default PulseAudio server address.

    Checked fresh on each call — the socket may not exist at import time but
    appear later once the audio host/addon has fully started.

    :returns: Server address (env value or "unix:<socket path>"), or an empty
        string when nothing was found (libpulse then uses its own defaults).
    """
    if server := os.environ.get("PULSE_SERVER"):
        return server
    for path in (
        "/run/audio/pulse.sock",
        "/run/pulse/native",
        "/var/run/pulse/native",
    ):
        if Path(path).exists():
            return f"unix:{path}"
    return ""


def get_pulse_capture_server(mass: MusicAssistant) -> PulseCaptureServer:
    """
    Return the shared PulseCaptureServer instance for this MusicAssistant.

    All consumers must obtain the server through this function so they share a
    single private daemon, and must pair acquire()/release() around their usage.
    """
    if (server := _servers.get(mass)) is None:
        server = PulseCaptureServer(mass)
        _servers[mass] = server
    return server


class PulseCaptureServer:
    """
    Private, MA-owned classic PulseAudio daemon for audio capture.

    Lazily started on the first :meth:`acquire` and stopped again on the last
    :meth:`release` (reference-counted) — use :func:`get_pulse_capture_server`
    to obtain the shared per-process instance. The daemon loads only the native
    protocol on a unix socket inside a private runtime dir under MA's cache dir;
    audio clients are pointed at it via :meth:`child_env` and their audio is
    captured through :class:`PipeSink` FIFOs.

    If the daemon dies it is restarted automatically and :attr:`generation` is
    bumped. All sinks created against the previous daemon are gone at that
    point, so consumers must snapshot ``generation`` when creating a sink and
    recreate their :class:`PipeSink` when it changes.
    """

    def __init__(self, mass: MusicAssistant) -> None:
        """
        Initialize the capture server (the daemon is not started yet).

        :param mass: MusicAssistant instance, used for the private runtime dir
            under its cache path.
        """
        # only the cache path is kept: holding mass itself would pin the
        # WeakKeyDictionary registry entry (value -> key) forever
        self._base_dir = Path(mass.cache_path) / "pulse_capture"
        self._socket_path = self._base_dir / "native"
        self._config_path = self._base_dir / "pulse_capture.pa"
        self._lock = asyncio.Lock()
        self._refcount = 0
        self._generation = 0
        self._proc: AsyncProcess | None = None
        self._controller: PAVolumeController | None = None
        self._supervisor_task: asyncio.Task[None] | None = None

    @property
    def generation(self) -> int:
        """Daemon generation, bumped on every (re)start — snapshot when creating sinks."""
        return self._generation

    @property
    def server_address(self) -> str:
        """PA server address ("unix:<socket path>") of the private daemon."""
        return f"unix:{self._socket_path}"

    async def acquire(self) -> PulseCaptureServer:
        """
        Register a consumer, starting the private daemon on first use.

        Every successful acquire() must be paired with exactly one release().

        :returns: This server instance, for convenient chaining.
        """
        async with self._lock:
            if self._refcount == 0:
                await self._start()
            self._refcount += 1
        return self

    async def release(self) -> None:
        """
        Unregister a consumer, stopping the daemon when none remain.

        Safe to call more often than acquire() (extra calls are ignored).
        """
        async with self._lock:
            if self._refcount == 0:
                return
            if self._refcount > 1:
                self._refcount -= 1
                return
            # last consumer: run teardown to completion even when this call is
            # cancelled, and only commit the zero refcount once it finished —
            # no half-stopped daemon can be left behind or double-started
            teardown = asyncio.ensure_future(self._stop())
            try:
                await asyncio.shield(teardown)
            except asyncio.CancelledError:
                while not teardown.done():
                    with suppress(asyncio.CancelledError):
                        await asyncio.shield(teardown)
                self._refcount = 0
                raise
            self._refcount = 0

    def child_env(self, sink_name: str) -> dict[str, str]:
        """
        Environment for an audio-client subprocess that must play into a sink.

        :param sink_name: Sink the client's audio must go to (PipeSink.sink_name).
        :returns: Full subprocess environment with PULSE_SERVER/PULSE_SINK set.
        """
        return get_subprocess_env(
            {
                "PULSE_SERVER": self.server_address,
                "PULSE_SINK": sink_name,
            }
        )

    async def _start(self) -> None:
        """Start the daemon and its supervisor. Called with the lock held."""
        await self._launch_daemon()
        self._supervisor_task = asyncio.create_task(self._supervise())
        self._supervisor_task.add_done_callback(_log_supervisor_exit)

    async def _stop(self) -> None:
        """Stop supervisor and daemon, clean the private dir. Called with the lock held."""
        if (task := self._supervisor_task) is not None:
            self._supervisor_task = None
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if (controller := self._controller) is not None:
            self._controller = None
            with suppress(Exception):
                await asyncio.to_thread(controller.close)
        if (proc := self._proc) is not None:
            self._proc = None
            await proc.close()

        # the daemon's death drops all loaded modules with it, so nothing needs
        # unloading; only the private runtime dir (socket + leftover FIFOs) remains
        def _cleanup() -> None:
            shutil.rmtree(self._base_dir, ignore_errors=True)

        await asyncio.to_thread(_cleanup)

    async def _launch_daemon(self) -> None:
        """Start the daemon process and wait until it accepts connections."""
        config_text = (
            f"load-module module-native-protocol-unix socket={self._socket_path} auth-anonymous=1\n"
        )

        # PA's .pa config and module argument parsers are space-delimited with no
        # escaping; refuse early with a clear error instead of mis-parsing later
        if " " in str(self._base_dir):
            raise RuntimeError(f"cache path may not contain spaces: {self._base_dir}")

        def _prepare() -> None:
            self._base_dir.mkdir(parents=True, exist_ok=True)
            # the daemon accepts anonymous connections on its socket, so the
            # private dir must never be accessible to other local users
            self._base_dir.chmod(0o700)
            self._socket_path.unlink(missing_ok=True)
            self._config_path.write_text(config_text, encoding="utf-8")

        await asyncio.to_thread(_prepare)
        # runtime/state dirs are redirected into the private dir via the child
        # environment only; os.environ is never mutated
        proc = AsyncProcess(
            [
                "pulseaudio",
                "--daemonize=no",
                "-n",
                "--exit-idle-time=-1",
                f"--file={self._config_path}",
            ],
            stderr=True,
            name="pulse-capture",
            env={
                "XDG_RUNTIME_DIR": str(self._base_dir),
                "PULSE_RUNTIME_PATH": str(self._base_dir),
                "PULSE_STATE_PATH": str(self._base_dir),
            },
        )
        self._proc = proc
        try:
            await proc.start()
            await self._wait_ready(proc)
            # verify the daemon actually accepts connections before declaring
            # ready; the construction cannot be interrupted mid-flight, so on
            # cancellation close whatever the worker thread still produced to
            # avoid leaking a live threaded mainloop
            ctor = asyncio.ensure_future(asyncio.to_thread(PAVolumeController, self.server_address))
            try:
                controller = await asyncio.shield(ctor)
            except asyncio.CancelledError:
                ctor.add_done_callback(_close_controller_result)
                raise
        except BaseException:
            if self._proc is proc:
                self._proc = None
            with suppress(Exception):
                await proc.close()
            raise
        if (old_controller := self._controller) is not None:
            with suppress(Exception):
                await asyncio.to_thread(old_controller.close)
        self._controller = controller
        self._generation += 1
        LOGGER.debug(
            "Private PulseAudio capture daemon ready on %s (generation %d)",
            self.server_address,
            self._generation,
        )

    async def _wait_ready(self, proc: AsyncProcess) -> None:
        """Wait for the daemon's native socket to appear."""
        try:
            async with asyncio.timeout(_READY_TIMEOUT):
                while not await asyncio.to_thread(self._socket_path.exists):
                    if proc.returncode is not None:
                        raise RuntimeError(
                            f"pulseaudio exited during startup (code {proc.returncode})"
                        )
                    await asyncio.sleep(_READY_POLL_INTERVAL)
        except TimeoutError:
            raise RuntimeError("Timeout waiting for the pulseaudio daemon socket") from None

    async def _supervise(self) -> None:
        """Restart the daemon (with bounded backoff) until the supervisor is cancelled."""
        backoff = _RESTART_BACKOFF_INITIAL
        while True:
            if (proc := self._proc) is not None:
                try:
                    async for line in proc.iter_stderr():
                        LOGGER.debug("pulseaudio: %s", line)
                except Exception as err:
                    LOGGER.debug("pulseaudio log reader stopped: %s", err)
                await proc.close()
                if self._proc is proc:
                    self._proc = None
            LOGGER.warning(
                "Private PulseAudio capture daemon exited unexpectedly, restarting in %.1fs",
                backoff,
            )
            await asyncio.sleep(backoff)
            try:
                await self._launch_daemon()
            except Exception as err:
                backoff = min(backoff * 2, _RESTART_BACKOFF_MAX)
                LOGGER.error("Failed to restart the PulseAudio capture daemon: %s", err)
                continue
            backoff = _RESTART_BACKOFF_INITIAL

    async def _load_module(self, module_name: str, argument: str) -> int | None:
        """Load a PA module on the private daemon (blocking libpulse call in a thread)."""
        controller = self._require_controller()
        return await asyncio.to_thread(controller.load_module, module_name, argument)

    async def _unload_module(self, module_index: int) -> bool:
        """Unload a PA module from the private daemon."""
        controller = self._require_controller()
        return await asyncio.to_thread(controller.unload_module, module_index)

    async def _set_sink_volume_raw(self, sink_name: str, volume_pct: float) -> bool:
        """Set raw (linear) volume on a sink of the private daemon."""
        controller = self._require_controller()
        return await asyncio.to_thread(controller.set_sink_volume_raw, sink_name, volume_pct)

    def _require_controller(self) -> PAVolumeController:
        """Return the connected controller or raise if the server is not running."""
        if (controller := self._controller) is None:
            raise RuntimeError("Pulse capture server is not running")
        return controller


class PipeSink:
    """
    One isolated module-pipe-sink capture sink on a :class:`PulseCaptureServer`.

    The PA daemon creates a FIFO at :attr:`fifo_path` that delivers the sink's
    audio as raw PCM in the fixed capture format (CAPTURE_SAMPLE_FORMAT /
    CAPTURE_SAMPLE_RATE / CAPTURE_CHANNELS); read it with
    ``helpers.named_pipe.read_named_pipe`` or by pointing ffmpeg at it.

    Both consumer lifecycles are supported: a single long-lived sink per
    provider instance, or a fresh sink per stream (create -> suspend/resume ->
    unload). A sink does not survive a daemon restart: when the server's
    ``generation`` no longer matches the value snapshotted at creation, drop
    this instance and create a new one.
    """

    def __init__(
        self,
        server: PulseCaptureServer,
        sink_name: str,
        fifo_path: Path,
        module_index: int,
        generation: int,
    ) -> None:
        """Initialize the sink. Use the async :meth:`create` factory instead."""
        self._server = server
        self._sink_name = sink_name
        self._fifo_path = fifo_path
        self._module_index: int | None = module_index
        self._generation = generation

    @classmethod
    async def create(cls, server: PulseCaptureServer, name_prefix: str) -> PipeSink:
        """
        Create a new uniquely-named pipe sink on the given capture server.

        The pipe-sink module creates the FIFO file itself.

        :param server: An acquired PulseCaptureServer.
        :param name_prefix: Prefix for the generated sink name (e.g. a provider
            instance id); a short unique suffix is appended.
        """
        sink_name = f"{name_prefix}_{uuid.uuid4().hex[:8]}"
        fifo_path = server._base_dir / f"{sink_name}.pcm"
        argument = (
            f"sink_name={sink_name} file={fifo_path} "
            f"format={CAPTURE_SAMPLE_FORMAT} rate={CAPTURE_SAMPLE_RATE} "
            f"channels={CAPTURE_CHANNELS}"
        )
        # snapshot before the load: a restart during the load would otherwise
        # pair a module index from the dead daemon with the new generation,
        # and a later unload could hit an unrelated module on the replacement
        generation = server.generation
        module_index = await server._load_module("module-pipe-sink", argument)
        if module_index is None:
            raise RuntimeError(f"Failed to load module-pipe-sink for {sink_name}")
        if server.generation != generation:
            raise RuntimeError(f"capture daemon restarted while creating sink {sink_name}")
        return cls(server, sink_name, fifo_path, module_index, generation)

    @property
    def sink_name(self) -> str:
        """The PA sink name (pass to PulseCaptureServer.child_env for the client)."""
        return self._sink_name

    @property
    def fifo_path(self) -> Path:
        """Path of the FIFO delivering this sink's PCM audio."""
        return self._fifo_path

    async def set_volume(self, volume_pct: float) -> None:
        """
        Set the sink's raw (linear) volume.

        A sink from a previous daemon generation ignores the call (recreate the
        sink after a restart).

        :param volume_pct: 100 is unity gain; values above 100 amplify (e.g. 400
            for reciprocal cubic compensation). Clamped to MAX_RAW_VOLUME_PCT.
        """
        if self._generation != self._server.generation:
            LOGGER.debug("Ignoring volume for stale sink %s", self._sink_name)
            return
        if not await self._server._set_sink_volume_raw(self._sink_name, volume_pct):
            raise RuntimeError(f"Failed to set volume on capture sink {self._sink_name}")

    async def suspend(self) -> None:
        """Suspend the sink (its FIFO stops producing audio until resumed)."""
        await self._set_suspended(True)

    async def resume(self) -> None:
        """Resume a suspended sink."""
        await self._set_suspended(False)

    async def unload(self) -> None:
        """
        Unload the sink's module and remove its FIFO (idempotent).

        A sink whose daemon has restarted since creation is already gone; only
        the leftover FIFO file is cleaned up in that case.
        """
        module_index = self._module_index
        self._module_index = None
        if module_index is not None and self._generation == self._server.generation:
            # best effort: a failure usually means the daemon/module is already
            # gone, and a restart reclaims all modules anyway
            if not await self._server._unload_module(module_index):
                LOGGER.warning("Failed to unload capture sink module %s", self._sink_name)
        with suppress(OSError):
            await asyncio.to_thread(self._fifo_path.unlink)

    async def _set_suspended(self, suspended: bool) -> None:
        """Toggle the sink's suspend state via pactl."""
        if self._generation != self._server.generation:
            LOGGER.debug("Ignoring suspend toggle for stale sink %s", self._sink_name)
            return
        returncode, output = await check_output(
            "pactl",
            "--server",
            self._server.server_address,
            "suspend-sink",
            self._sink_name,
            "1" if suspended else "0",
            env={"PULSE_SERVER": self._server.server_address},
            timeout=5,
        )
        if returncode != 0:
            LOGGER.warning(
                "pactl suspend-sink %s %d failed: %s",
                self._sink_name,
                int(suspended),
                output.decode("utf-8", errors="replace").strip(),
            )


class PAVolumeController:
    """
    Shared libpulse connection for PA sink volume and module control.

    One instance is shared per PA server. All calls are blocking and must be
    invoked via run_in_executor/to_thread from async code.
    """

    def __init__(self, server: str | None = None) -> None:
        """
        Connect to PulseAudio and start the threaded mainloop.

        :param server: PA server address to connect to (e.g. "unix:<socket>").
            Uses env/default socket discovery when omitted.
        """
        self._lib = _get_full_lib()
        self._lock = threading.Lock()
        self._mainloop = self._lib.pa_threaded_mainloop_new()
        if not self._mainloop:
            raise OSError("pa_threaded_mainloop_new returned NULL")

        api = self._lib.pa_threaded_mainloop_get_api(self._mainloop)
        self._context = self._lib.pa_context_new(api, b"music-assistant-volume")
        if not self._context:
            self._lib.pa_threaded_mainloop_free(self._mainloop)
            self._mainloop = None
            raise OSError("pa_context_new returned NULL")

        self._ready = threading.Event()
        self._failed = threading.Event()

        def _state_cb_impl(_ctx: int, _userdata: int) -> None:
            state = self._lib.pa_context_get_state(self._context)
            if state == PA_CONTEXT_READY:
                self._ready.set()
            elif state in (PA_CONTEXT_FAILED, PA_CONTEXT_TERMINATED):
                self._failed.set()

        self._state_cb = _CONTEXT_NOTIFY_CB(_state_cb_impl)  # keep reference alive — GC
        self._lib.pa_context_set_state_callback(self._context, self._state_cb, None)

        pulse_server = server or get_default_pulse_server()
        ret = self._lib.pa_context_connect(
            self._context,
            pulse_server.encode() if pulse_server else None,
            PA_CONTEXT_NOAUTOSPAWN,
            None,
        )
        if ret < 0:
            self.close()
            raise OSError(f"pa_context_connect failed (ret={ret})")

        self._lib.pa_threaded_mainloop_start(self._mainloop)

        if not self._ready.wait(timeout=5.0):
            self.close()
            raise OSError("Timed out connecting to PulseAudio for volume control")

    def set_sink_volume(self, sink_name: str, volume_pct: int, channels: int = 2) -> bool:
        """
        Set hardware volume on a named PA sink.

        :param sink_name: PA sink name as returned by ``enumerate_pa_sinks()``.
        :param volume_pct: Volume level 0-100, mapped through an exponential
            audio taper curve before being sent to PA.
        :param channels: Channel count for the PA volume structure. Should
            match the sink's actual channel count.
        :returns: True if PA reported success.
        """
        amplitude = volume_pct_to_amplitude(volume_pct)
        # cube root counteracts PA's own cubic volume curve
        pa_vol = round(PA_VOLUME_NORM * amplitude ** (1.0 / 3.0))
        return self._apply_sink_volume(sink_name, pa_vol, channels)

    def set_sink_volume_raw(self, sink_name: str, volume_pct: float, channels: int = 2) -> bool:
        """
        Set raw (linear) volume on a named PA sink, without the audio taper.

        :param sink_name: PA sink name.
        :param volume_pct: Linear percentage where 100 maps exactly onto
            PA_VOLUME_NORM (0 dB). Values above 100 amplify (e.g. 400 for
            reciprocal cubic compensation); clamped to MAX_RAW_VOLUME_PCT.
        :param channels: Channel count for the PA volume structure.
        :returns: True if PA reported success.
        """
        volume_pct = min(max(volume_pct, 0.0), MAX_RAW_VOLUME_PCT)
        pa_vol = round(PA_VOLUME_NORM * volume_pct / 100.0)
        return self._apply_sink_volume(sink_name, pa_vol, channels)

    def load_module(self, module_name: str, argument: str) -> int | None:
        """
        Load a PulseAudio module (e.g. module-remap-sink) via libpulse.

        :param module_name: PA module name, e.g. "module-remap-sink".
        :param argument: Module argument string, e.g.
            "sink_name=Foo master=bar channels=2 master_channel_map=...
            channel_map=front-left,front-right remix=no".

        Blocks (up to ~2s) for PA's response.
        :returns: The loaded module's index, or None on failure/timeout.
        """
        with self._lock:
            if self._failed.is_set() or not self._mainloop or not self._context:
                return None

            done = threading.Event()
            result: dict[str, int] = {}

            def _index_cb_impl(_ctx: int, idx: int, _userdata: int) -> None:
                result["index"] = idx
                done.set()

            index_cb = _CONTEXT_INDEX_CB(_index_cb_impl)

            self._lib.pa_threaded_mainloop_lock(self._mainloop)
            try:
                op = self._lib.pa_context_load_module(
                    self._context,
                    module_name.encode(),
                    argument.encode(),
                    index_cb,
                    None,
                )
                if not op:
                    return None
            finally:
                self._lib.pa_threaded_mainloop_unlock(self._mainloop)

            if not done.wait(timeout=2.0):
                self._cancel_operation(op)
                return None
            self._lib.pa_operation_unref(op)
            idx = result.get("index", PA_INVALID_INDEX)
            return None if idx == PA_INVALID_INDEX else idx

    def unload_module(self, module_index: int) -> bool:
        """
        Unload a previously-loaded PulseAudio module by index.

        Blocks (up to ~2s) for PA's response.
        :returns: True if PA reported success.
        """
        with self._lock:
            if self._failed.is_set() or not self._mainloop or not self._context:
                return False

            done = threading.Event()
            result: dict[str, int] = {}

            def _success_cb_impl(_ctx: int, success: int, _userdata: int) -> None:
                result["success"] = success
                done.set()

            success_cb = _CONTEXT_SUCCESS_CB(_success_cb_impl)

            self._lib.pa_threaded_mainloop_lock(self._mainloop)
            try:
                op = self._lib.pa_context_unload_module(
                    self._context, module_index, success_cb, None
                )
                if not op:
                    return False
            finally:
                self._lib.pa_threaded_mainloop_unlock(self._mainloop)

            if not done.wait(timeout=2.0):
                self._cancel_operation(op)
                return False
            self._lib.pa_operation_unref(op)
            return bool(result.get("success", 0))

    def close(self) -> None:
        """Disconnect and tear down the mainloop."""
        with self._lock:
            if self._context:
                self._lib.pa_context_disconnect(self._context)
                self._lib.pa_context_unref(self._context)
                self._context = None
            if self._mainloop:
                self._lib.pa_threaded_mainloop_stop(self._mainloop)
                self._lib.pa_threaded_mainloop_free(self._mainloop)
                self._mainloop = None

    def _cancel_operation(self, op: int) -> None:
        """
        Detach a timed-out operation's callback before dropping the reference.

        PA may still deliver the response later from the mainloop thread; without
        the cancel it would invoke the (garbage-collected) ctypes trampoline of a
        callback that went out of scope — undefined behavior.
        """
        self._lib.pa_operation_cancel(op)
        self._lib.pa_operation_unref(op)

    def _apply_sink_volume(self, sink_name: str, pa_volume: int, channels: int) -> bool:
        """
        Send an already-mapped raw PA volume to a named sink.

        :returns: True if PA reported success.
        """
        with self._lock:
            if self._failed.is_set() or not self._mainloop or not self._context:
                return False
            cvol = _PACVolume()
            self._lib.pa_cvolume_set(ctypes.byref(cvol), channels, pa_volume)

            done = threading.Event()
            result: dict[str, int] = {}

            def _success_cb_impl(_ctx: int, success: int, _userdata: int) -> None:
                result["success"] = success
                done.set()

            success_cb = _CONTEXT_SUCCESS_CB(_success_cb_impl)

            self._lib.pa_threaded_mainloop_lock(self._mainloop)
            try:
                op = self._lib.pa_context_set_sink_volume_by_name(
                    self._context,
                    sink_name.encode(),
                    ctypes.byref(cvol),
                    success_cb,
                    None,
                )
                if not op:
                    return False
            finally:
                self._lib.pa_threaded_mainloop_unlock(self._mainloop)

            if not done.wait(timeout=_SET_VOLUME_TIMEOUT):
                self._cancel_operation(op)
                return False
            self._lib.pa_operation_unref(op)
            return bool(result.get("success", 0))


# One shared server per MusicAssistant instance; weak keys so a discarded mass
# (tests) does not pin its server object forever.
_servers: weakref.WeakKeyDictionary[MusicAssistant, PulseCaptureServer] = (
    weakref.WeakKeyDictionary()
)


class _PACVolume(ctypes.Structure):
    _fields_: ClassVar = [
        ("channels", ctypes.c_uint8),
        ("values", ctypes.c_uint32 * PA_CHANNELS_MAX),
    ]


def _log_supervisor_exit(task: asyncio.Task[None]) -> None:
    """Surface a supervisor that died on an unexpected error (restarts stop with it)."""
    if not task.cancelled() and task.exception() is not None:
        LOGGER.error("PulseAudio capture supervisor died unexpectedly: %s", task.exception())


def _close_controller_result(fut: asyncio.Future[PAVolumeController]) -> None:
    """Close a controller whose awaiting task was cancelled mid-construction."""
    with suppress(Exception):
        fut.result().close()


def _load_full_lib() -> ctypes.CDLL:
    """
    Load and configure libpulse for use by PAVolumeController.

    Called once per process; the result is cached by _get_full_lib().
    """
    lib = ctypes.CDLL("libpulse.so.0")

    lib.pa_threaded_mainloop_new.restype = ctypes.c_void_p
    lib.pa_threaded_mainloop_get_api.restype = ctypes.c_void_p
    lib.pa_threaded_mainloop_get_api.argtypes = [ctypes.c_void_p]
    lib.pa_threaded_mainloop_start.restype = ctypes.c_int
    lib.pa_threaded_mainloop_start.argtypes = [ctypes.c_void_p]
    lib.pa_threaded_mainloop_stop.argtypes = [ctypes.c_void_p]
    lib.pa_threaded_mainloop_free.argtypes = [ctypes.c_void_p]
    lib.pa_threaded_mainloop_lock.argtypes = [ctypes.c_void_p]
    lib.pa_threaded_mainloop_unlock.argtypes = [ctypes.c_void_p]

    lib.pa_context_new.restype = ctypes.c_void_p
    lib.pa_context_new.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lib.pa_context_set_state_callback.argtypes = [
        ctypes.c_void_p,
        _CONTEXT_NOTIFY_CB,
        ctypes.c_void_p,
    ]
    lib.pa_context_connect.restype = ctypes.c_int
    lib.pa_context_connect.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_void_p,
    ]
    lib.pa_context_get_state.restype = ctypes.c_int
    lib.pa_context_get_state.argtypes = [ctypes.c_void_p]
    lib.pa_context_disconnect.argtypes = [ctypes.c_void_p]
    lib.pa_context_unref.argtypes = [ctypes.c_void_p]

    lib.pa_cvolume_set.restype = ctypes.c_void_p
    lib.pa_cvolume_set.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint32]

    lib.pa_context_set_sink_volume_by_name.restype = ctypes.c_void_p
    lib.pa_context_set_sink_volume_by_name.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_void_p,
        _CONTEXT_SUCCESS_CB,
        ctypes.c_void_p,
    ]
    lib.pa_operation_unref.argtypes = [ctypes.c_void_p]
    lib.pa_operation_cancel.argtypes = [ctypes.c_void_p]

    lib.pa_context_load_module.restype = ctypes.c_void_p
    lib.pa_context_load_module.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        _CONTEXT_INDEX_CB,
        ctypes.c_void_p,
    ]
    lib.pa_context_unload_module.restype = ctypes.c_void_p
    lib.pa_context_unload_module.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        _CONTEXT_SUCCESS_CB,
        ctypes.c_void_p,
    ]
    return lib


_full_lib: ctypes.CDLL | None = None


def _get_full_lib() -> ctypes.CDLL:
    global _full_lib  # noqa: PLW0603
    if _full_lib is None:
        _full_lib = _load_full_lib()
    return _full_lib
