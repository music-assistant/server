"""Minimal ctypes wrapper around libpulse-simple for direct PA sink PCM streaming."""

from __future__ import annotations

import ctypes
import os
import threading
from typing import TYPE_CHECKING, Any, ClassVar, Final, Self

from .constants import volume_pct_to_amplitude

if TYPE_CHECKING:
    from .card_profiles import CardSnapshot

PA_STREAM_PLAYBACK: Final = 1

PA_SAMPLE_S16LE: Final = 3
PA_SAMPLE_S32LE: Final = 7  # verified via pa_sample_format_to_string
PA_SAMPLE_S24LE: Final = 9  # packed 3-byte LE — native format of s24le PA sinks

# Map PA sample format constant -> bit depth
_PA_FORMAT_TO_BIT_DEPTH: Final[dict[int, int]] = {
    PA_SAMPLE_S16LE: 16,
    PA_SAMPLE_S24LE: 24,
    PA_SAMPLE_S32LE: 32,
}


def _pa_sample_format(bit_depth: int) -> int:
    """Return PA sample format constant for given bit depth."""
    if bit_depth == 32:
        return PA_SAMPLE_S32LE
    if bit_depth == 24:
        # MA delivers in 32-bit containers; _apply_software_volume repacks to
        # packed 3-byte before writing, so PA sees s24le here.
        return PA_SAMPLE_S24LE
    return PA_SAMPLE_S16LE


class _PASampleSpec(ctypes.Structure):
    _fields_: ClassVar = [
        ("format", ctypes.c_int),
        ("rate", ctypes.c_uint32),
        ("channels", ctypes.c_uint8),
    ]


def _find_pulse_server() -> str:
    """Detect the PulseAudio server socket path."""
    if server := os.environ.get("PULSE_SERVER"):
        return server
    for path in (
        "/run/audio/pulse.sock",
        "/run/pulse/native",
        "/var/run/pulse/native",
    ):
        if os.path.exists(path):
            return f"unix:{path}"
    return ""


def _get_pulse_server() -> str:
    """
    Return the PulseAudio server path, checked fresh on each call.

    Intentionally not cached — the socket may not exist at import time
    but appear later once the audio addon has fully started.
    """
    return _find_pulse_server()


def _load_lib() -> ctypes.CDLL:
    lib = ctypes.CDLL("libpulse-simple.so.0")
    lib.pa_simple_new.restype = ctypes.c_void_p
    lib.pa_simple_new.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    lib.pa_simple_write.restype = ctypes.c_int
    lib.pa_simple_write.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
    ]
    lib.pa_simple_drain.restype = ctypes.c_int
    lib.pa_simple_drain.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.pa_simple_free.restype = None
    lib.pa_simple_free.argtypes = [ctypes.c_void_p]
    return lib


_lib: ctypes.CDLL | None = None


def _get_lib() -> ctypes.CDLL:
    global _lib  # noqa: PLW0603
    if _lib is None:
        _lib = _load_lib()
    return _lib


# --- Hardware sink volume control (full libpulse, async) ----------------------

PA_VOLUME_NORM: Final = 65536
PA_CHANNELS_MAX: Final = 32


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


class _PACVolume(ctypes.Structure):
    _fields_: ClassVar = [
        ("channels", ctypes.c_uint8),
        ("values", ctypes.c_uint32 * PA_CHANNELS_MAX),
    ]


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


class PAVolumeController:
    """
    Shared libpulse connection for hardware PA sink volume control.

    One instance is shared across all PA sink bridges. All calls are blocking
    and must be invoked via run_in_executor from async code.
    """

    def __init__(self) -> None:
        """Connect to PulseAudio and start the threaded mainloop."""
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

        pulse_server = _get_pulse_server()
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
        with self._lock:
            if self._failed.is_set():
                return False
            amplitude = volume_pct_to_amplitude(volume_pct)
            pa_vol = round(PA_VOLUME_NORM * amplitude ** (1.0 / 3.0))
            cvol = _PACVolume()
            self._lib.pa_cvolume_set(ctypes.byref(cvol), channels, pa_vol)

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
                self._lib.pa_operation_unref(op)
                return False
            self._lib.pa_operation_unref(op)
            return bool(result.get("success", 0))

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
            if self._failed.is_set():
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
                self._lib.pa_operation_unref(op)
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
            if self._failed.is_set():
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
                self._lib.pa_operation_unref(op)
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


class PASimpleStream:
    """
    Synchronous PCM playback stream to a named PulseAudio sink.

    All libpulse calls are serialized behind a threading.Lock so that
    concurrent executor threads cannot simultaneously write/free the
    same pa_simple connection, which causes assertion failures in libpulse.
    """

    def __init__(
        self,
        sink_name: str,
        app_name: str,
        rate: int,
        channels: int,
        bit_depth: int = 16,
    ) -> None:
        """Open a synchronous PCM playback stream to the named PulseAudio sink."""
        lib = _get_lib()
        spec = _PASampleSpec(
            format=_pa_sample_format(bit_depth),
            rate=rate,
            channels=channels,
        )
        error = ctypes.c_int(0)
        self._lib = lib
        self._lock = threading.Lock()
        pulse_server = _get_pulse_server()
        self._conn: int | None = lib.pa_simple_new(
            pulse_server.encode() if pulse_server else None,
            app_name.encode(),
            PA_STREAM_PLAYBACK,
            sink_name.encode(),
            b"playback",
            ctypes.byref(spec),
            None,
            None,
            ctypes.byref(error),
        )
        if not self._conn:
            raise OSError(
                f"pa_simple_new failed for sink '{sink_name}' "
                f"(pa_error={error.value}, server={pulse_server!r})"
            )

    def write(self, data: bytes) -> None:
        """Write a PCM chunk. Blocks until PA has buffered it."""
        with self._lock:
            if not self._conn:
                return
            error = ctypes.c_int(0)
            ret = self._lib.pa_simple_write(self._conn, data, len(data), ctypes.byref(error))
            if ret < 0:
                raise OSError(f"pa_simple_write failed (pa_error={error.value})")

    def drain(self) -> None:
        """Block until all buffered audio has played out."""
        with self._lock:
            if not self._conn:
                return
            error = ctypes.c_int(0)
            self._lib.pa_simple_drain(self._conn, ctypes.byref(error))

    def close(self) -> None:
        """
        Free the PA stream.

        Acquires the lock before zeroing _conn and calling pa_simple_free,
        ensuring no concurrent write() or drain() can touch the pointer
        between the None assignment and the free call.
        """
        with self._lock:
            conn, self._conn = self._conn, None
            if conn:
                self._lib.pa_simple_free(conn)

    def __enter__(self) -> Self:
        """Enter context manager."""
        return self

    def __exit__(self, *_: object) -> None:
        """Exit context manager and close the stream."""
        self.close()


def enumerate_alsa_devices() -> list[dict[str, Any]]:
    """
    Enumerate stereo-capable ALSA output devices via PortAudio.

    Returns device dicts in the same shape as ``enumerate_pa_sinks()`` so
    both backends share the same bridge registration path.
    """
    import sounddevice as _sd  # noqa: PLC0415

    # Find the ALSA host API index
    alsa_hostapi_index: int | None = None
    for i, api in enumerate(_sd.query_hostapis()):
        if "alsa" in api.get("name", "").lower():
            alsa_hostapi_index = i
            break

    devices: list[dict[str, Any]] = []
    for idx, dev in enumerate(_sd.query_devices()):
        if dev.get("max_output_channels", 0) < 2:
            continue
        if alsa_hostapi_index is not None and dev.get("hostapi") != alsa_hostapi_index:
            continue
        try:
            test = _sd.RawOutputStream(
                device=idx,
                samplerate=int(dev.get("default_samplerate", 48000)),
                channels=2,
                dtype="int16",
            )
            test.close()
        except _sd.PortAudioError:
            continue
        name: str = dev.get("name", f"alsa-device-{idx}")

        # Skip virtual ALSA PCM plugins — only keep real hardware nodes.
        # PortAudio enumerates both hw: entries and virtual plugins
        # (sysdefault, front, surround*, dmix, lavrate, upmix, …).
        # Hardware entries always contain "(hw:" in their name.
        if "(hw:" not in name:
            continue

        sample_rate = int(dev.get("default_samplerate", 48000))

        # Build a clean display name: strip the " (hw:C,D)" suffix so the
        # MA player name reads e.g. "Intel Audio: ALC889A Analog" not
        # "Intel Audio: ALC889A Analog (hw:1,0)".
        import re as _re  # noqa: PLC0415

        description = _re.sub(r"\s*\(hw:\d+,\d+\)$", "", name).strip()

        devices.append(
            {
                "name": name,  # stable key — includes (hw:C,D) for uniqueness
                "description": description,  # human-readable MA player label
                "pa_sink_name": None,
                "max_output_channels": dev.get("max_output_channels", 2),
                "sample_rate": sample_rate,
                "bit_depth": 16,
                "is_remap": False,
                "master_device": None,
                "index": idx,
                "hostapi": dev.get("hostapi", 0),
            }
        )
    return devices


def enumerate_pa_sinks() -> list[dict[str, Any]]:
    """
    Enumerate PulseAudio output sinks via pactl.

    :raises FileNotFoundError: if pactl is not installed.
    :raises RuntimeError: if pactl returns unexpected output.
    :returns: List of sink dicts, one per sink, containing name, description,
        sample rate, bit depth, channel map, and remap-sink metadata.
    """
    import json  # noqa: PLC0415
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    # Locate pactl — requires pulseaudio-utils to be installed
    if not (path := shutil.which("pactl")):
        raise FileNotFoundError("pactl not found — please install pulseaudio-utils")
    pactl_bin = path

    env = {**os.environ}
    pulse_server = _get_pulse_server()
    if pulse_server:
        env["PULSE_SERVER"] = pulse_server

    result = subprocess.run(  # noqa: S603
        [pactl_bin, "--format=json", "list", "sinks"],
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pactl exited {result.returncode}: {result.stderr.strip()}")

    sinks = []
    for sink in json.loads(result.stdout):
        name: str = sink.get("name", "")
        desc: str = sink.get("description", name)
        spec_str: str = sink.get("sample_specification", "")
        driver: str = sink.get("driver", "")
        properties: dict[str, str] = sink.get("properties", {})
        # device.master_device is set by module-remap-sink itself on every
        # sink it creates, and only on those sinks — so it's a reliable way
        # to detect a remap-sink child regardless of what the host reports
        # in the top-level "driver" field. Real PulseAudio reports the
        # literal module name there ("module-remap-sink.c" /
        # "module-alsa-card.c"), but PipeWire's PulseAudio-compatibility
        # layer instead reports a generic "PipeWire" driver string for
        # every sink it manages, so "driver == module-remap-sink.c" alone
        # would silently misdetect every remap sink (and every ALSA-card
        # master sink) as something else on a PipeWire-only system.
        master_device: str | None = properties.get("device.master_device")
        is_remap = master_device is not None or driver == "module-remap-sink.c"
        alsa_card_name: str | None = properties.get("alsa.card_name")
        alsa_card_index: str | None = properties.get("alsa.card")
        # Structured connection-type signal from PulseAudio's own kernel-bus
        # detection ("pci", "usb", "bluetooth", ...) — not vendor-supplied
        # naming, so it's reliable even for USB devices whose card/product
        # name gives no hint they're USB (e.g. some class-compliant DACs).
        device_bus: str | None = properties.get("device.bus")
        # pactl --format=json represents channel_map as a comma-separated
        # string (e.g. "front-left,front-right,rear-left,rear-right,...").
        channel_map_str: str = sink.get("channel_map", "")
        channel_map: list[str] = [c for c in channel_map_str.split(",") if c]
        try:
            parts = spec_str.split()
            fmt = parts[0]  # e.g. 's32le'
            channels = int(parts[1].replace("ch", ""))
            sample_rate = int(parts[2].replace("Hz", ""))
            # Parse bit depth from PA format string using explicit lookup.
            # Avoids s24-32le parsing as 2432 with the digit-filter approach.
            _fmt_to_depth = {
                "u8": 8,
                "s16le": 16,
                "s16be": 16,
                "s24le": 24,
                "s24be": 24,
                "s24-32le": 32,
                "s24-32be": 32,
                "s32le": 32,
                "s32be": 32,
                "float32le": 32,
                "float32be": 32,
            }
            bit_depth = _fmt_to_depth.get(fmt.lower(), 16)
        except (IndexError, ValueError):  # fmt: skip
            continue
        if channels < 2:
            continue
        sinks.append(
            {
                "name": name,  # stable PA sink name — used for UUID/player-id generation
                "description": desc,  # human-readable label — used as MA player display name
                "pa_sink_name": name,
                "max_output_channels": channels,
                "sample_rate": sample_rate,
                "bit_depth": bit_depth,
                "is_remap": is_remap,
                "master_device": master_device,
                "driver": driver,
                "channel_map": channel_map,
                "alsa_card_name": alsa_card_name,
                "alsa_card_index": alsa_card_index,
                "device_bus": device_bus,
            }
        )

    # Disambiguate duplicate display names — two identical cards produce the
    # same PA description string (e.g. two X-Fi cards both appear as "Creative
    # X-Fi Analog Surround 7.1"). Append the ALSA card index so the MA player
    # display names are distinct ("Creative X-Fi Analog Surround 7.1 (card 0)"
    # vs "Creative X-Fi Analog Surround 7.1 (card 1)").
    descriptions = [s["description"] for s in sinks]
    for sink in sinks:
        if descriptions.count(sink["description"]) > 1:
            card_idx = sink.get("alsa_card_index")
            if card_idx is not None:
                sink["description"] = f"{sink['description']} (card {card_idx})"

    return sinks


def suspend_resume_sink(sink_name: str) -> None:
    """
    Suspend then resume a PA sink to reset its underlying ALSA driver state.

    Works around the snd_ctxfi mmap bug (kernel 6.12.x, commit 391e69143d0a)
    where the X-Fi card's DMA transfer stalls after driver init, causing
    pa_simple_write to timeout. A suspend/resume cycle re-initialises the
    ALSA PCM device and clears the stall without requiring a PA or system
    restart.

    Called on ALSA-card master sinks after remap-sink topology creation at
    provider load/reload time. No-op if pactl is not available.

    :param sink_name: PA sink name to suspend and resume.
    """
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import time  # noqa: PLC0415

    if not (pactl_bin := shutil.which("pactl")):
        return

    env = {**os.environ}
    if pulse_server := _get_pulse_server():
        env["PULSE_SERVER"] = pulse_server

    try:
        subprocess.run(  # noqa: S603
            [pactl_bin, "suspend-sink", sink_name, "1"],
            check=True,
            capture_output=True,
            timeout=3,
            env=env,
        )
        time.sleep(0.5)
        subprocess.run(  # noqa: S603
            [pactl_bin, "suspend-sink", sink_name, "0"],
            check=True,
            capture_output=True,
            timeout=3,
            env=env,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):  # fmt: skip
        pass


# Channel-enable mixer elements the remap-sink topology actually routes
# through (front/surround/center/LFE/side zones — see
# remap_topology.STEREO_PAIRS). Deliberately excludes anything else with a
# playback switch on the affected Creative/HD-Audio cards — Line, Mic,
# Front Mic, Rear Mic, Loopback Mixing, IEC958, Headphone, PCM, Master —
# which are real analog input monitors or unrelated controls, not the
# surround-channel-enable quirk this function exists to correct. Card and
# separate-vs-combined naming vary by driver ("Center/LFE" on some cards,
# "Center" + "LFE" separately on others), so both forms are listed.
_UNMUTE_TARGET_ELEMENTS: Final[frozenset[str]] = frozenset(
    {"Front", "Surround", "Center", "LFE", "Center/LFE", "Side"}
)


def _load_asound_lib() -> ctypes.CDLL:
    lib = ctypes.CDLL("libasound.so.2")
    lib.snd_mixer_open.restype = ctypes.c_int
    lib.snd_mixer_open.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_int]
    lib.snd_mixer_attach.restype = ctypes.c_int
    lib.snd_mixer_attach.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lib.snd_mixer_selem_register.restype = ctypes.c_int
    lib.snd_mixer_selem_register.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    lib.snd_mixer_load.restype = ctypes.c_int
    lib.snd_mixer_load.argtypes = [ctypes.c_void_p]
    lib.snd_mixer_first_elem.restype = ctypes.c_void_p
    lib.snd_mixer_first_elem.argtypes = [ctypes.c_void_p]
    lib.snd_mixer_elem_next.restype = ctypes.c_void_p
    lib.snd_mixer_elem_next.argtypes = [ctypes.c_void_p]
    lib.snd_mixer_selem_has_playback_switch.restype = ctypes.c_int
    lib.snd_mixer_selem_has_playback_switch.argtypes = [ctypes.c_void_p]
    lib.snd_mixer_selem_get_playback_switch.restype = ctypes.c_int
    lib.snd_mixer_selem_get_playback_switch.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_int),
    ]
    lib.snd_mixer_selem_set_playback_switch_all.restype = ctypes.c_int
    lib.snd_mixer_selem_set_playback_switch_all.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    lib.snd_mixer_selem_get_name.restype = ctypes.c_char_p
    lib.snd_mixer_selem_get_name.argtypes = [ctypes.c_void_p]
    lib.snd_mixer_close.restype = None
    lib.snd_mixer_close.argtypes = [ctypes.c_void_p]
    return lib


_asound_lib: ctypes.CDLL | None = None


def _get_asound_lib() -> ctypes.CDLL:
    """Load and cache libasound.so.2 with prototypes declared once, not per call."""
    global _asound_lib  # noqa: PLW0603
    if _asound_lib is None:
        _asound_lib = _load_asound_lib()
    return _asound_lib


def unmute_playback_switches(alsa_card_index: str) -> str:
    """
    Detect-then-correct: unmute known-muted channel-enable elements this card's topology feeds.

    On some multi-instance sound cards, certain playback-enable mixer
    elements (e.g. for surround/center/side channels) don't default to
    unmuted on every card instance, even though volume and PCM routing
    are otherwise correct — hardware is confirmed consuming audio via
    /proc/asound/cardN/pcm0p/sub0/status, but no sound is produced because
    the relevant channel switch is muted. This is a driver/hardware
    init-order quirk independent of physical PCI slot, observed on setups
    with two identical cards installed.

    Only ever touches elements in _UNMUTE_TARGET_ELEMENTS, and only writes
    one whose playback switch reads as off — a genuine no-op on a healthy
    card, rather than force-enabling every playback-switch-capable element
    on the card (which on these cards also includes analog input monitors
    like Line/Mic, and on some HD-Audio chips a Loopback Mixing control —
    silently mixing live input into the output on machines that never had
    the bug this exists to fix).

    Talks directly to libasound's simple-mixer API rather than shelling
    out to the `amixer` binary, since alsa-utils is not guaranteed to be
    present in every deployment. Note this requires the calling process
    to have direct access to /dev/snd — going through PulseAudio's socket
    (as suspend_resume_sink() does) is not sufficient, since PA does not
    generically expose arbitrary ALSA mixer element control over its
    protocol. In containerized deployments where only a PA socket is
    passed through (no /dev/snd bind-mount), this will return
    "attach_failed" and have no effect.

    Called on ALSA-card master sinks after remap-sink topology creation,
    alongside suspend_resume_sink().

    :param alsa_card_index: ALSA card index (the "alsa.card" PA property),
        e.g. "0" or "4".
    :return: short status string for logging/diagnostics — "ok:<n>" on
        success (n = number of elements actually changed; 0 means every
        target element already read as unmuted, the expected result on a
        healthy card), or one of "no_libasound", "open_failed",
        "attach_failed", "register_failed", "load_failed" describing
        where it stopped. A "set_failed:<n>" suffix is appended to a
        successful "ok:<n>" result if any individual
        set_playback_switch_all call itself reported failure (n = how
        many) — those elements' actual state is unknown, since the
        library call that was supposed to change them didn't confirm it.
    """
    try:
        lib = _get_asound_lib()
    except OSError:
        return "no_libasound"

    mixer = ctypes.c_void_p()
    if lib.snd_mixer_open(ctypes.byref(mixer), 0) < 0:
        return "open_failed"

    try:
        card_name = f"hw:{alsa_card_index}".encode()
        if lib.snd_mixer_attach(mixer, card_name) < 0:
            return "attach_failed"
        if lib.snd_mixer_selem_register(mixer, None, None) < 0:
            return "register_failed"
        if lib.snd_mixer_load(mixer) < 0:
            return "load_failed"

        # SND_MIXER_SCHN_MONO and SND_MIXER_SCHN_FRONT_LEFT are both 0 in
        # ALSA's channel enum — querying channel 0 is the conventional
        # "representative channel" check (the same one amixer/alsamixer
        # effectively show first), sufficient here since
        # set_playback_switch_all always writes every channel together.
        mono_channel = 0

        changed_count = 0
        set_failed_count = 0
        elem = lib.snd_mixer_first_elem(mixer)
        while elem:
            if lib.snd_mixer_selem_has_playback_switch(elem):
                name_bytes = lib.snd_mixer_selem_get_name(elem)
                name = name_bytes.decode("utf-8", errors="replace") if name_bytes else ""
                if name in _UNMUTE_TARGET_ELEMENTS:
                    current = ctypes.c_int(1)
                    got = lib.snd_mixer_selem_get_playback_switch(
                        elem, mono_channel, ctypes.byref(current)
                    )
                    if got >= 0 and current.value == 0:
                        if lib.snd_mixer_selem_set_playback_switch_all(elem, 1) < 0:
                            set_failed_count += 1
                        else:
                            changed_count += 1
            elem = lib.snd_mixer_elem_next(elem)
        result = f"ok:{changed_count}"
        if set_failed_count:
            result += f":set_failed:{set_failed_count}"
        return result
    finally:
        lib.snd_mixer_close(mixer)


def enumerate_pa_cards() -> list[CardSnapshot]:
    """
    Enumerate PulseAudio/PipeWire cards with their profiles via pactl.

    Same invocation idiom as enumerate_pa_sinks() — pactl JSON output over
    the addon-aware PULSE_SERVER socket — but for cards, which carry the
    profile set that decides which of a card's sinks/sources exist at all
    (see the card_profiles module for the selection policy built on this).

    :raises FileNotFoundError: if pactl is not installed.
    :raises RuntimeError: if pactl fails or returns unexpected output.
    :returns: One CardSnapshot per card.
    """
    import json  # noqa: PLC0415
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    from .card_profiles import CardProfile, CardSnapshot  # noqa: PLC0415

    if not (path := shutil.which("pactl")):
        raise FileNotFoundError("pactl not found — please install pulseaudio-utils")

    env = {**os.environ}
    if pulse_server := _get_pulse_server():
        env["PULSE_SERVER"] = pulse_server

    result = subprocess.run(  # noqa: S603
        [path, "--format=json", "list", "cards"],
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pactl exited {result.returncode}: {result.stderr.strip()}")
    try:
        raw_cards = json.loads(result.stdout)
    except json.JSONDecodeError as err:
        raise RuntimeError(f"pactl list cards returned invalid JSON: {err}") from err

    cards: list[CardSnapshot] = []
    for raw in raw_cards:
        properties: dict[str, Any] = raw.get("properties", {})
        profiles = tuple(
            CardProfile(
                name=name,
                description=str(info.get("description", name)),
                n_sinks=int(info.get("sinks", 0)),
                n_sources=int(info.get("sources", 0)),
                priority=int(info.get("priority", 0)),
                available=bool(info.get("available", True)),
            )
            for name, info in raw.get("profiles", {}).items()
        )
        cards.append(
            CardSnapshot(
                name=str(raw.get("name", "")),
                index=int(raw.get("index", -1)),
                active_profile=str(raw.get("active_profile", "")),
                profiles=profiles,
                alsa_card_name=properties.get("alsa.card_name"),
                alsa_card_index=properties.get("alsa.card"),
            )
        )
    return cards


def set_card_profile(card_name: str, profile_name: str) -> bool:
    """
    Activate a profile on a card.

    :param card_name: The card's PA name (CardSnapshot.name).
    :param profile_name: The profile's machine name.
    :returns: True on success, False on failure or timeout.
    :raises FileNotFoundError: if pactl is not installed.
    """
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    if not (path := shutil.which("pactl")):
        raise FileNotFoundError("pactl not found — please install pulseaudio-utils")

    env = {**os.environ}
    if pulse_server := _get_pulse_server():
        env["PULSE_SERVER"] = pulse_server

    try:
        result = subprocess.run(  # noqa: S603
            [path, "set-card-profile", card_name, profile_name],
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0
