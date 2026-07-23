"""
Minimal ctypes wrapper around libpulse-simple for direct PA source capture.

Modeled after the playback-direction PASimpleStream in server's
local_audio provider (pa_simple.py) — same libpulse-simple primitives
(pa_simple_new/pa_simple_free, pa_threaded_mainloop-free "simple" API),
mirrored for the read/PA_STREAM_RECORD direction instead of
write/PA_STREAM_PLAYBACK. Device *discovery* also follows that module's
pattern: pactl --format=json is used for enumeration (structured, stable),
while the hot data path goes through libpulse-simple directly (no
subprocess per audio chunk).
"""

from __future__ import annotations

import ctypes
import os
import threading
from typing import Any, ClassVar, Final, Self

PA_STREAM_RECORD: Final = 2

PA_SAMPLE_S16LE: Final = 3


class _PASampleSpec(ctypes.Structure):
    _fields_: ClassVar = [
        ("format", ctypes.c_int),
        ("rate", ctypes.c_uint32),
        ("channels", ctypes.c_uint8),
    ]


# pa_buffer_attr — lets us request a small server-side record buffer instead
# of whatever default latency target pa_simple_new() guesses when attr=NULL,
# which can be considerably larger than we need for a low-latency capture
# path. tlength/prebuf/minreq are playback-side knobs; for a record stream
# they're ignored by the server, so we pass PA's own "don't care" sentinel
# (uint32 -1) for them rather than guessing meaningful values.
class _PABufferAttr(ctypes.Structure):
    _fields_: ClassVar = [
        ("maxlength", ctypes.c_uint32),
        ("tlength", ctypes.c_uint32),
        ("prebuf", ctypes.c_uint32),
        ("minreq", ctypes.c_uint32),
        ("fragsize", ctypes.c_uint32),
    ]


_PA_BUFATTR_IGNORED: Final = 0xFFFFFFFF


def _find_pulse_server() -> str:
    """Detect the PulseAudio/PipeWire-pulse server socket path."""
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
    but appear later once the audio stack has fully started.
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
    lib.pa_simple_read.restype = ctypes.c_int
    lib.pa_simple_read.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
    ]
    lib.pa_simple_free.restype = None
    lib.pa_simple_free.argtypes = [ctypes.c_void_p]
    return lib


_lib: ctypes.CDLL | None = None


def _get_lib() -> ctypes.CDLL:
    global _lib  # noqa: PLW0603
    if _lib is None:
        _lib = _load_lib()
    return _lib


class PASimpleRecordStream:
    """
    Synchronous PCM capture stream from a named PulseAudio/PipeWire source.

    All calls are blocking libpulse-simple calls and must be invoked via
    run_in_executor from async code — same calling convention as
    PASimpleStream.write() in server's local_audio provider.

    Caveat inherited from that same reference implementation: pa_simple_read()
    is a blocking C call with no cancellation hook. If a source stops
    delivering samples entirely (e.g. device unplugged mid-stream), the
    executor thread running read() stays blocked, and close() — which also
    takes the same lock — will block right along with it until the call
    eventually returns. In practice PulseAudio/PipeWire sources deliver
    samples continuously once opened, so this is a rare edge case, not a
    round-trip-per-chunk risk.
    """

    def __init__(
        self,
        source_name: str,
        app_name: str,
        rate: int,
        channels: int,
    ) -> None:
        """Open a synchronous PCM capture stream from the named PA/PipeWire source."""
        lib = _get_lib()
        spec = _PASampleSpec(
            format=PA_SAMPLE_S16LE,
            rate=rate,
            channels=channels,
        )
        # Target one ~20ms chunk per fragment (matches the provider's own
        # read chunk size) instead of pa_simple_new()'s default latency
        # guess, so the server doesn't accumulate more audio server-side
        # than we're about to consume anyway.
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
        pulse_server = _get_pulse_server()
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
        """
        Free the PA stream.

        Acquires the lock before zeroing _conn and calling pa_simple_free,
        ensuring no concurrent read() can touch the pointer between the
        None assignment and the free call.
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


def enumerate_pa_sources() -> list[dict[str, Any]]:
    """
    Enumerate PulseAudio/PipeWire capture sources via pactl.

    Includes monitor sources (loopback capture of what a sink is currently
    playing), which pactl/PipeWire label with a ".monitor" name suffix and
    a "Monitor of ..." description — left in rather than filtered out, since
    "capture what's currently playing on sink X" is a valid use case here.

    :raises FileNotFoundError: if pactl is not installed.
    :raises RuntimeError: if pactl returns unexpected output.
    :returns: List of source dicts with name, description, sample rate,
        bit depth and channel count.
    """
    import json  # noqa: PLC0415
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    if not (path := shutil.which("pactl")):
        raise FileNotFoundError("pactl not found — please install pulseaudio-utils")
    pactl_bin = path

    env = {**os.environ}
    if pulse_server := _get_pulse_server():
        env["PULSE_SERVER"] = pulse_server

    result = subprocess.run(  # noqa: S603
        [pactl_bin, "--format=json", "list", "sources"],
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pactl exited {result.returncode}: {result.stderr.strip()}")

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

    sources = []
    for source in json.loads(result.stdout):
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
            bit_depth = _fmt_to_depth.get(fmt.lower(), 16)
        except IndexError, ValueError:
            channels, sample_rate, bit_depth = 2, 44100, 16

        sources.append(
            {
                "name": name,  # stable PA/PipeWire source name — used as the config value
                "description": desc,  # human-readable label
                "channels": channels,
                "sample_rate": sample_rate,
                "bit_depth": bit_depth,
                "is_monitor": name.endswith(".monitor"),
            }
        )
    return sources
