"""Minimal ctypes wrapper around libpulse-simple for direct PA sink PCM streaming."""

from __future__ import annotations

import ctypes
import os
import threading
from typing import Any, ClassVar, Final

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
    """Return the PulseAudio server path, checked fresh on each call.

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

PA_CONTEXT_READY: Final = 4
PA_CONTEXT_FAILED: Final = 5
PA_CONTEXT_TERMINATED: Final = 6
PA_CONTEXT_NOAUTOSPAWN: Final = 1

_CONTEXT_NOTIFY_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)
_CONTEXT_SUCCESS_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p)


class _PACVolume(ctypes.Structure):
    _fields_: ClassVar = [
        ("channels", ctypes.c_uint8),
        ("values", ctypes.c_uint32 * PA_CHANNELS_MAX),
    ]


def _load_full_lib() -> ctypes.CDLL:
    """Load full libpulse (not -simple) for the async context API.

    Required for pa_context_set_sink_volume_by_name(), which has no
    equivalent in libpulse-simple. libpulse.so.0 is a transitive dependency
    of libpulse-simple.so.0, so it is present anywhere PASimpleStream works.
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
    return lib


_full_lib: ctypes.CDLL | None = None


def _get_full_lib() -> ctypes.CDLL:
    global _full_lib  # noqa: PLW0603
    if _full_lib is None:
        _full_lib = _load_full_lib()
    return _full_lib


class PAVolumeController:
    """Shared async libpulse connection for hardware PA sink volume control.

    One instance is shared across all PA sink bridges. Connects once via a
    threaded mainloop; set_sink_volume() calls are then direct function calls
    into the existing connection — no subprocess, no fork/exec per change.

    A pa_cvolume with channels=1 is used for all sinks regardless of their
    actual channel count: PA remaps a single-channel volume uniformly across
    the sink's full channel map (same behavior as `pactl set-sink-volume
    <sink> N%`), so no per-sink channel-count detection is needed. This also
    works correctly for module-remap-sink.c sinks — each remap sink has an
    independent volume that does not affect its master sink or siblings.

    All calls are blocking (bounded by short timeouts) and must be invoked
    via run_in_executor from async code.
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

    def set_sink_volume(self, sink_name: str, volume_pct: int) -> bool:
        """Set hardware volume on a PA sink by name (0-100%).

        Blocks (up to ~2s) for PA's success/failure response.
        :returns: True if PA reported success.
        """
        with self._lock:
            if self._failed.is_set():
                return False
            pa_vol = int(PA_VOLUME_NORM * max(0, min(volume_pct, 100)) / 100)
            cvol = _PACVolume()
            self._lib.pa_cvolume_set(ctypes.byref(cvol), 1, pa_vol)

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
    """Synchronous PCM playback stream to a named PulseAudio sink.

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
        """Free the PA stream.

        Acquires the lock before zeroing _conn and calling pa_simple_free,
        ensuring no concurrent write() or drain() can touch the pointer
        between the None assignment and the free call.
        """
        with self._lock:
            conn, self._conn = self._conn, None
            if conn:
                self._lib.pa_simple_free(conn)

    def __enter__(self) -> PASimpleStream:
        """Enter context manager."""
        return self

    def __exit__(self, *_: object) -> None:
        """Exit context manager and close the stream."""
        self.close()


def enumerate_alsa_devices() -> list[dict[str, Any]]:
    """Enumerate stereo-capable ALSA output devices via sounddevice/PortAudio.

    Returns list of dicts compatible with the PA sink dict shape so that
    LocalAudioBridgeManager can use the same registration path for both
    backends.  Keys returned:
      - name: stable device name (used for UUID / player-id generation)
      - description: human-readable label (MA player display name)
      - pa_sink_name: None (not a PA device)
      - max_output_channels: number of channels
      - sample_rate: device default sample rate
      - bit_depth: fixed 16 (PortAudio ALSA path; bridge uses int16 dtype)
      - is_remap: False
      - index: sounddevice device index
      - hostapi: host API index
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
                "index": idx,
                "hostapi": dev.get("hostapi", 0),
            }
        )
    return devices


def enumerate_pa_sinks() -> list[dict[str, Any]]:
    """Enumerate stereo-capable PulseAudio sinks via pactl JSON output.

    Uses pactl --format=json list sinks which always returns the sink's
    native sample rate and format regardless of active stream state —
    unlike pulsectl/libpulse which reports the currently negotiated format
    when streams are active (which can differ from native hardware format).

    Returns list of dicts with keys:
      - name: display name (PA sink description)
      - pa_sink_name: internal PA sink name
      - max_output_channels: number of channels
      - sample_rate: sink native sample rate in Hz
      - bit_depth: sink native bit depth (16, 24, or 32)
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
        except (IndexError, ValueError):
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
                "is_remap": driver == "module-remap-sink.c",
            }
        )
    return sinks