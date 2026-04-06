"""Minimal ctypes wrapper around libpulse-simple for direct PA sink PCM streaming."""
from __future__ import annotations

import ctypes
from typing import ClassVar, Final

PA_SAMPLE_S16LE: Final = 3
PA_STREAM_PLAYBACK: Final = 1


class _PASampleSpec(ctypes.Structure):
    _fields_: ClassVar = [
        ("format", ctypes.c_int),
        ("rate", ctypes.c_uint32),
        ("channels", ctypes.c_uint8),
    ]


def _load_lib() -> ctypes.CDLL:
    lib = ctypes.CDLL("libpulse-simple.so.0")
    lib.pa_simple_new.restype = ctypes.c_void_p
    lib.pa_simple_new.argtypes = [
        ctypes.c_char_p,  # server (NULL = default)
        ctypes.c_char_p,  # app name
        ctypes.c_int,     # direction
        ctypes.c_char_p,  # sink name
        ctypes.c_char_p,  # stream description
        ctypes.c_void_p,  # sample spec
        ctypes.c_void_p,  # channel map (NULL)
        ctypes.c_void_p,  # buffer attr (NULL)
        ctypes.c_void_p,  # error ptr
    ]
    lib.pa_simple_write.restype = ctypes.c_int
    lib.pa_simple_write.argtypes = [
        ctypes.c_void_p,  # stream
        ctypes.c_void_p,  # data
        ctypes.c_size_t,  # byte count — must be c_size_t not c_int
        ctypes.c_void_p,  # error ptr
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
    """Synchronous PCM playback stream to a named PulseAudio sink."""

    def __init__(self, sink_name: str, app_name: str, rate: int, channels: int) -> None:
        lib = _get_lib()
        spec = _PASampleSpec(format=PA_SAMPLE_S16LE, rate=rate, channels=channels)
        error = ctypes.c_int(0)
        self._lib = lib
        self._conn: int | None = lib.pa_simple_new(
            None,
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
                f"pa_simple_new failed for sink '{sink_name}' (pa_error={error.value})"
            )

    def write(self, data: bytes) -> None:
        """Write a PCM chunk. Blocks until PA has buffered it."""
        error = ctypes.c_int(0)
        ret = self._lib.pa_simple_write(self._conn, data, len(data), ctypes.byref(error))
        if ret < 0:
            raise OSError(f"pa_simple_write failed (pa_error={error.value})")

    def drain(self) -> None:
        """Block until all buffered audio has played out."""
        error = ctypes.c_int(0)
        self._lib.pa_simple_drain(self._conn, ctypes.byref(error))

    def close(self) -> None:
        if self._conn:
            self._lib.pa_simple_free(self._conn)
            self._conn = None

    def __enter__(self) -> PASimpleStream:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
