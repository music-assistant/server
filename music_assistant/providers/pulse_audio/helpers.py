"""Shared helpers for Local PulseAudio Out provider."""
from __future__ import annotations

import os
import shutil

from .pa_simple import PULSE_SERVER


def find_pactl() -> str:
    """Find the pactl binary, preferring the bundled version."""
    bundled = os.path.join(os.path.dirname(__file__), "bin", "pactl")
    if os.path.isfile(bundled):
        if not os.access(bundled, os.X_OK):
            # Fix permissions if the wheel lost the execute bit
            try:
                os.chmod(bundled, 0o777)
            except OSError:
                pass
        if os.access(bundled, os.X_OK):
            return bundled
    if path := shutil.which("pactl"):
        return path
    for candidate in ("/usr/bin/pactl", "/usr/local/bin/pactl", "/bin/pactl"):
        if os.path.isfile(candidate):
            return candidate
    raise FileNotFoundError(
        "pactl not found — bundled binary missing and pulseaudio-utils not installed"
    )

def pactl_env() -> dict[str, str]:
    """Build environment dict for pactl subprocess calls.

    Sets LD_LIBRARY_PATH to include the bundled lib directory so that
    libpulsecommon is found, and sets PULSE_SERVER to the detected socket.
    """
    lib_dir = os.path.join(os.path.dirname(__file__), "lib")
    existing_ld = os.environ.get("LD_LIBRARY_PATH", "")
    ld_path = f"{lib_dir}:{existing_ld}" if existing_ld else lib_dir
    env = {**os.environ, "LD_LIBRARY_PATH": ld_path}
    if PULSE_SERVER:
        env["PULSE_SERVER"] = PULSE_SERVER
    return env
