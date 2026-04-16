"""Minimal ctypes wrapper around libpulse-simple for direct PA sink PCM streaming."""
from __future__ import annotations

import ctypes
import os
import threading
from typing import Any, ClassVar, Final

PA_STREAM_PLAYBACK: Final = 1

PA_SAMPLE_S16LE:    Final = 3
PA_SAMPLE_S32LE:    Final = 7   # verified via pa_sample_format_to_string
PA_SAMPLE_S24LE:    Final = 9   # packed 3-byte LE — native format of s24le PA sinks

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


class PASimpleStream:
    """Synchronous PCM playback stream to a named PulseAudio sink.

    All libpulse calls are serialized behind a threading.Lock so that
    concurrent executor threads cannot simultaneously write/free the
    same pa_simple connection, which causes assertion failures in libpulse.
    """

    def __init__(self, sink_name: str, app_name: str, rate: int, channels: int, bit_depth: int = 16) -> None:
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
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

def enumerate_pa_sinks() -> list[dict[str, Any]]:
    """Enumerate stereo-capable PulseAudio sinks via pulsectl."""
    import pulsectl
    sinks = []
    with pulsectl.Pulse("ma-local-audio-enum") as pulse:
        for sink in pulse.sink_list():
            channels = sink.sample_spec.channels
            if channels < 2:
                continue
            fmt = sink.sample_spec.format.name  # e.g. 's32le'
            try:
                bit_depth = int(
                    "".join(filter(str.isdigit, fmt.split("le")[0].split("be")[0]))
                )
            except ValueError:
                bit_depth = 16
            sinks.append({
                "name": sink.description or sink.name,
                "pa_sink_name": sink.name,
                "max_output_channels": channels,
                "sample_rate": sink.sample_spec.rate,
                "bit_depth": bit_depth,
            })
    return sinks

    class _PASampleSpecFull(ctypes.Structure):
        _fields_ = [
            ("format", ctypes.c_int),
            ("rate", ctypes.c_uint32),
            ("channels", ctypes.c_uint8),
        ]

    class _PASinkInfo(ctypes.Structure):
        _fields_ = [
            ("name", ctypes.c_char_p),
            ("index", ctypes.c_uint32),
            ("description", ctypes.c_char_p),
            ("sample_spec", _PASampleSpecFull),
        ]

    sinks: list[dict[str, Any]] = []

    SINK_CB = ctypes.CFUNCTYPE(
        None,
        ctypes.c_void_p,
        ctypes.POINTER(_PASinkInfo),
        ctypes.c_int,
        ctypes.c_void_p,
    )

    def _sink_cb(
        context: ctypes.c_void_p,
        info_ptr: ctypes.POINTER(_PASinkInfo),
        eol: int,
        userdata: ctypes.c_void_p,
    ) -> None:
        if eol or not info_ptr:
            return
        info = info_ptr.contents
        name = info.name.decode() if info.name else ""
        desc = info.description.decode() if info.description else name
        channels = info.sample_spec.channels
        sample_rate = info.sample_spec.rate
        bit_depth = _PA_FORMAT_TO_BIT_DEPTH.get(info.sample_spec.format, 16)
        if channels >= 2:
            sinks.append({
                "name": desc,
                "pa_sink_name": name,
                "max_output_channels": channels,
                "sample_rate": sample_rate,
                "bit_depth": bit_depth,
            })

    sink_cb = SINK_CB(_sink_cb)

    mainloop = lib.pa_mainloop_new()
    if not mainloop:
        raise OSError("pa_mainloop_new failed")

    try:
        api = lib.pa_mainloop_get_api(mainloop)
        ctx = lib.pa_context_new(api, b"music-assistant-enum")
        if not ctx:
            raise OSError("pa_context_new failed")

        pulse_server = _get_pulse_server()
        server = pulse_server.encode() if pulse_server else None
        ret = lib.pa_context_connect(ctx, server, 0, None)
        if ret < 0:
            lib.pa_context_unref(ctx)
            raise OSError(f"pa_context_connect failed (ret={ret}, server={pulse_server!r})")

        # Wait for context to become ready (max ~2s)
        for _ in range(2000):
            lib.pa_mainloop_iterate(mainloop, 0, None)
            state = lib.pa_context_get_state(ctx)
            if state == PA_CONTEXT_READY:
                break
            if state in (PA_CONTEXT_FAILED, PA_CONTEXT_TERMINATED):
                lib.pa_context_unref(ctx)
                raise OSError(f"PA context failed to connect (state={state}, server={pulse_server!r})")
        else:
            lib.pa_context_unref(ctx)
            raise OSError(f"Timed out waiting for PA context to become ready (server={pulse_server!r})")

        # Issue get_sink_info_list and pump mainloop until operation completes
        op = lib.pa_context_get_sink_info_list(ctx, sink_cb, None)
        if not op:
            lib.pa_context_disconnect(ctx)
            lib.pa_context_unref(ctx)
            raise OSError("pa_context_get_sink_info_list failed")

        for _ in range(2000):
            lib.pa_mainloop_iterate(mainloop, 0, None)
            if lib.pa_operation_get_state(op) == PA_OPERATION_DONE:
                break

        lib.pa_operation_unref(op)
        lib.pa_context_disconnect(ctx)
        lib.pa_context_unref(ctx)

    finally:
        lib.pa_mainloop_free(mainloop)

    return sinks
