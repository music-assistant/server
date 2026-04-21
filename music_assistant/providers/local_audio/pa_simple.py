"""Minimal ctypes wrapper around libpulse-simple for direct PA sink PCM streaming."""
from __future__ import annotations

import contextlib
import ctypes
import os
from pathlib import Path
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

    # Locate pactl — prefer bundled binary, fall back to system
    bundled = os.path.join(os.path.dirname(__file__), "bin", "pactl")
    if os.path.isfile(bundled):
        if not os.access(bundled, os.X_OK):
            with contextlib.suppress(OSError):
                Path(bundled).chmod(0o755)  # noqa: S103
    if os.path.isfile(bundled) and os.access(bundled, os.X_OK):
        pactl_bin = bundled
    elif path := shutil.which("pactl"):
        pactl_bin = path
    else:
        raise FileNotFoundError(
            "pactl not found — bundled binary missing and pulseaudio-utils not installed"
        )

    # Build environment with bundled lib dir and PULSE_SERVER
    lib_dir = os.path.join(os.path.dirname(__file__), "bin", "lib")
    existing_ld = os.environ.get("LD_LIBRARY_PATH", "")
    ld_path = f"{lib_dir}:{existing_ld}" if existing_ld else lib_dir
    env = {**os.environ, "LD_LIBRARY_PATH": ld_path}
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
        raise RuntimeError(
            f"pactl exited {result.returncode}: {result.stderr.strip()}"
        )

    sinks = []
    for sink in json.loads(result.stdout):
        name: str = sink.get("name", "")
        desc: str = sink.get("description", name)
        spec_str: str = sink.get("sample_specification", "")
        driver: str = sink.get("driver", "")
        try:
            parts = spec_str.split()
            fmt = parts[0]          # e.g. 's32le'
            channels = int(parts[1].replace("ch", ""))
            sample_rate = int(parts[2].replace("Hz", ""))
            bit_depth = int(
                "".join(filter(str.isdigit, fmt.split("le")[0].split("be")[0]))
            )
        except (IndexError, ValueError):
            continue
        if channels < 2:
            continue
        sinks.append({
            "name": desc,
            "pa_sink_name": name,
            "max_output_channels": channels,
            "sample_rate": sample_rate,
            "bit_depth": bit_depth,
            "is_remap": driver == "module-remap-sink.c",
        })
    return sinks
