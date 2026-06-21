"""PulseAudio Simple API fallback for environments where PortAudio cannot enumerate devices.

Used inside the Home Assistant OS Supervisor addon container where /proc/asound
is masked, preventing PortAudio/ALSA from discovering sound cards. The PulseAudio
socket at /run/audio/pulse.sock remains accessible.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
from typing import Any

_LOGGER = logging.getLogger(__name__)

# The Supervisor mounts the PulseAudio socket at this path inside addon containers.
_DEFAULT_PULSE_SOCKET = "unix:/run/audio/pulse.sock"
PULSE_SERVER = os.environ.get("PULSE_SERVER", _DEFAULT_PULSE_SOCKET)

PA_STREAM_PLAYBACK = 1
PA_SAMPLE_S16LE = 3


class pa_sample_spec(ctypes.Structure):  # noqa: N801
    """PulseAudio sample specification (pa_sample_spec)."""

    _fields_ = [
        ("format", ctypes.c_int),
        ("rate", ctypes.c_uint32),
        ("channels", ctypes.c_uint8),
    ]


def _load_libpulse_simple() -> ctypes.CDLL | None:
    """Load libpulse-simple shared library, return None if unavailable."""
    try:
        lib = ctypes.CDLL("libpulse-simple.so.0")
    except OSError:
        return None

    lib.pa_simple_new.restype = ctypes.c_void_p
    lib.pa_simple_new.argtypes = [
        ctypes.c_char_p,  # server
        ctypes.c_char_p,  # name
        ctypes.c_int,  # dir
        ctypes.c_char_p,  # dev (sink name)
        ctypes.c_char_p,  # stream_name
        ctypes.POINTER(pa_sample_spec),  # ss
        ctypes.c_void_p,  # channel_map (NULL)
        ctypes.c_void_p,  # attr (NULL)
        ctypes.POINTER(ctypes.c_int),  # error
    ]
    lib.pa_simple_free.argtypes = [ctypes.c_void_p]
    lib.pa_simple_free.restype = None
    lib.pa_simple_write.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_int),
    ]
    lib.pa_simple_write.restype = ctypes.c_int
    lib.pa_simple_drain.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
    lib.pa_simple_drain.restype = ctypes.c_int
    return lib


_libpulse = _load_libpulse_simple()


def is_available() -> bool:
    """Return True if PulseAudio Simple API is usable."""
    return _libpulse is not None


def _get_sink_descriptions() -> dict[str, str]:
    """Query PulseAudio for sink descriptions (friendly names).

    Returns a mapping of sink_name -> description.
    """
    descriptions: dict[str, str] = {}
    try:
        result = subprocess.run(
            ["pactl", "list", "sinks"],
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "PULSE_SERVER": PULSE_SERVER},
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return descriptions

    if result.returncode != 0:
        return descriptions

    current_name: str | None = None
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Name: "):
            current_name = stripped[6:]
        elif stripped.startswith("Description: ") and current_name:
            descriptions[current_name] = stripped[13:]
            current_name = None

    return descriptions


def enumerate_pulse_sinks() -> list[dict[str, Any]]:
    """Enumerate PulseAudio output sinks via pactl.

    Returns a list of dicts compatible with the device format expected by
    LocalAudioBridgeManager.discover_and_register().
    """
    devices: list[dict[str, Any]] = []
    try:
        result = subprocess.run(
            ["pactl", "list", "sinks", "short"],
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "PULSE_SERVER": PULSE_SERVER},
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return devices

    if result.returncode != 0:
        return devices

    # Get friendly descriptions for each sink
    descriptions = _get_sink_descriptions()

    for line in result.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        sink_index = int(parts[0])
        sink_name = parts[1]

        # Use PulseAudio's own description if available, otherwise derive one
        display_name = descriptions.get(sink_name)
        if not display_name:
            display_name = (
                sink_name.replace("alsa_output.", "")
                .replace(".", " ")
                .replace("-", " ")
            )
            for suffix in (" analog stereo", " analog mono"):
                if display_name.lower().endswith(suffix):
                    display_name = display_name[: -len(suffix)]
            display_name = display_name.strip()

        devices.append(
            {
                "index": sink_index,
                "name": display_name,
                "pulse_sink": sink_name,
                "hostapi": 0,
                "max_output_channels": 2,
            }
        )

    _LOGGER.debug("PulseAudio enumeration found %d sink(s)", len(devices))
    return devices


class PulseOutputStream:
    """Write-based output stream using PulseAudio Simple API.

    Provides the same interface (start/write/stop/close) as sd.RawOutputStream
    so it can be used as a drop-in replacement in the audio writer loop.
    """

    def __init__(
        self,
        sink_name: str | None = None,
        samplerate: int = 48000,
        channels: int = 2,
    ) -> None:
        if _libpulse is None:
            msg = "libpulse-simple.so.0 not available"
            raise RuntimeError(msg)

        spec = pa_sample_spec(PA_SAMPLE_S16LE, samplerate, channels)
        error = ctypes.c_int(0)
        sink_bytes = sink_name.encode() if sink_name else None

        self._handle = _libpulse.pa_simple_new(
            PULSE_SERVER.encode(),
            b"music_assistant",
            PA_STREAM_PLAYBACK,
            sink_bytes,
            b"local_audio_out",
            ctypes.byref(spec),
            None,
            None,
            ctypes.byref(error),
        )
        if not self._handle:
            msg = f"Failed to open PulseAudio stream to sink '{sink_name}' (error {error.value})"
            raise RuntimeError(msg)

    def write(self, data: bytes) -> None:
        """Write raw PCM data to the stream (blocking)."""
        error = ctypes.c_int(0)
        ret = _libpulse.pa_simple_write(
            self._handle,
            data,
            len(data),
            ctypes.byref(error),
        )
        if ret < 0:
            msg = f"PulseAudio write error (code {error.value})"
            raise RuntimeError(msg)

    def start(self) -> None:
        """No-op for API compatibility with sd.RawOutputStream."""

    def stop(self) -> None:
        """Drain remaining buffered audio."""
        if self._handle:
            error = ctypes.c_int(0)
            _libpulse.pa_simple_drain(self._handle, ctypes.byref(error))

    def close(self) -> None:
        """Free the PulseAudio connection."""
        if self._handle:
            _libpulse.pa_simple_free(self._handle)
            self._handle = None
