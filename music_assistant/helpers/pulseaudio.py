"""Shared low-level PulseAudio/PipeWire helpers (libpulse-simple ctypes, pactl)."""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
from typing import Any, ClassVar, Final

PA_STREAM_PLAYBACK: Final = 1
PA_STREAM_RECORD: Final = 2

PA_SAMPLE_S16LE: Final = 3
PA_SAMPLE_S32LE: Final = 7
PA_SAMPLE_S24LE: Final = 9

PCM_FORMAT_TO_BIT_DEPTH: Final[dict[str, int]] = {
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


class PASampleSpec(ctypes.Structure):
    """pa_sample_spec."""

    _fields_: ClassVar = [
        ("format", ctypes.c_int),
        ("rate", ctypes.c_uint32),
        ("channels", ctypes.c_uint8),
    ]


def get_pulse_server() -> str:
    """Return the PulseAudio/PipeWire server address, or "" to use the default."""
    if server := os.environ.get("PULSE_SERVER"):
        return server
    for path in ("/run/audio/pulse.sock", "/run/pulse/native", "/var/run/pulse/native"):
        if os.path.exists(path):
            return f"unix:{path}"
    return ""


def _load_simple_lib() -> ctypes.CDLL:
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
    lib.pa_simple_read.restype = ctypes.c_int
    lib.pa_simple_read.argtypes = [
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


_simple_lib: ctypes.CDLL | None = None


def get_simple_lib() -> ctypes.CDLL:
    """Return the cached libpulse-simple.so.0 handle, loading it on first use."""
    global _simple_lib  # noqa: PLW0603
    if _simple_lib is None:
        _simple_lib = _load_simple_lib()
    return _simple_lib


def run_pactl_json(list_what: str) -> list[dict[str, Any]]:
    """
    Run `pactl --format=json list <list_what>` and return the parsed entries.

    :param list_what: "sinks" or "sources".
    :raises FileNotFoundError: if pactl is not installed.
    :raises RuntimeError: if pactl exits non-zero.
    """
    if not (pactl_bin := shutil.which("pactl")):
        raise FileNotFoundError("pactl not found — please install pulseaudio-utils")
    env = {**os.environ}
    if pulse_server := get_pulse_server():
        env["PULSE_SERVER"] = pulse_server
    result = subprocess.run(  # noqa: S603
        [pactl_bin, "--format=json", "list", list_what],
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pactl exited {result.returncode}: {result.stderr.strip()}")
    return list(json.loads(result.stdout))
