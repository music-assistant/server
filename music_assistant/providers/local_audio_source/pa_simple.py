"""Ctypes wrapper around libpulse-simple for direct PA source capture."""

from __future__ import annotations

import ctypes
import threading
from typing import Any, ClassVar, Final, Self

from music_assistant.helpers.pulseaudio import (
    PA_SAMPLE_S16LE,
    PA_STREAM_RECORD,
    PCM_FORMAT_TO_BIT_DEPTH,
    get_pulse_server,
    get_simple_lib,
    run_pactl_json,
)
from music_assistant.helpers.pulseaudio import (
    PASampleSpec as _PASampleSpec,
)

_PA_BUFATTR_IGNORED: Final = 0xFFFFFFFF


class _PABufferAttr(ctypes.Structure):
    """pa_buffer_attr."""

    _fields_: ClassVar = [
        ("maxlength", ctypes.c_uint32),
        ("tlength", ctypes.c_uint32),
        ("prebuf", ctypes.c_uint32),
        ("minreq", ctypes.c_uint32),
        ("fragsize", ctypes.c_uint32),
    ]


class PASimpleRecordStream:
    """
    Synchronous PCM capture stream from a named PulseAudio/PipeWire source.

    All calls must be invoked via run_in_executor from async code.
    """

    def __init__(
        self,
        source_name: str,
        app_name: str,
        rate: int,
        channels: int,
        sample_format: int = PA_SAMPLE_S16LE,
    ) -> None:
        """Open a synchronous PCM capture stream from the named PA/PipeWire source."""
        lib = get_simple_lib()
        spec = _PASampleSpec(format=sample_format, rate=rate, channels=channels)
        fragsize = max(1, int(rate * channels * 2 * 0.02))
        buffer_attr = _PABufferAttr(
            maxlength=fragsize * 4,
            tlength=_PA_BUFATTR_IGNORED,
            prebuf=_PA_BUFATTR_IGNORED,
            minreq=_PA_BUFATTR_IGNORED,
            fragsize=fragsize,
        )
        error = ctypes.c_int(0)
        self._lib = lib
        self._lock = threading.Lock()
        pulse_server = get_pulse_server()
        self._conn: int | None = lib.pa_simple_new(
            pulse_server.encode() if pulse_server else None,
            app_name.encode(),
            PA_STREAM_RECORD,
            source_name.encode(),
            b"record",
            ctypes.byref(spec),
            None,
            ctypes.byref(buffer_attr),
            ctypes.byref(error),
        )
        if not self._conn:
            raise OSError(
                f"pa_simple_new failed for source '{source_name}' "
                f"(pa_error={error.value}, server={pulse_server!r})"
            )

    def read(self, num_bytes: int) -> bytes:
        """Read exactly num_bytes of PCM. Blocks until that many bytes are available."""
        with self._lock:
            if not self._conn:
                return b""
            buf = ctypes.create_string_buffer(num_bytes)
            error = ctypes.c_int(0)
            ret = self._lib.pa_simple_read(self._conn, buf, num_bytes, ctypes.byref(error))
            if ret < 0:
                raise OSError(f"pa_simple_read failed (pa_error={error.value})")
            return buf.raw

    def close(self) -> None:
        """Free the PA stream."""
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


def enumerate_pa_sources() -> list[dict[str, Any]]:
    """
    Enumerate PulseAudio/PipeWire capture sources via pactl.

    :raises FileNotFoundError: if pactl is not installed.
    :raises RuntimeError: if pactl returns unexpected output.
    :returns: List of source dicts with name, description, sample rate,
        bit depth and channel count.
    """
    sources = []
    for source in run_pactl_json("sources"):
        name: str = source.get("name", "")
        if not name:
            continue
        desc: str = source.get("description", name)
        spec_str: str = source.get("sample_specification", "")
        try:
            parts = spec_str.split()
            fmt = parts[0]
            channels = int(parts[1].replace("ch", ""))
            sample_rate = int(parts[2].replace("Hz", ""))
            bit_depth = PCM_FORMAT_TO_BIT_DEPTH.get(fmt.lower(), 16)
        except IndexError, ValueError:
            channels, sample_rate, bit_depth = 2, 44100, 16

        sources.append(
            {
                "name": name,
                "description": desc,
                "channels": channels,
                "sample_rate": sample_rate,
                "bit_depth": bit_depth,
                "is_monitor": name.endswith(".monitor"),
            }
        )
    return sources
