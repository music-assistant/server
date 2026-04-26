"""Helpers/utils for Ariacast Receiver plugin."""

from __future__ import annotations

import os
import platform
import stat
from pathlib import Path


def _get_binary_path() -> str:
    """Locate the correct binary for the current OS/Arch."""
    base_dir = os.path.join(os.path.dirname(__file__), "bin")
    system = platform.system().lower()
    machine = platform.machine().lower()

    if machine in ("x86_64", "amd64"):
        arch = "amd64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    else:
        raise RuntimeError(f"Unsupported architecture: {machine}")

    binary_name = f"ariacast_{system}_{arch}"
    binary_path = os.path.join(base_dir, binary_name)

    if not os.path.exists(binary_path):
        raise FileNotFoundError(f"Binary not found at {binary_path}")

    Path(binary_path).chmod(Path(binary_path).stat().st_mode | stat.S_IEXEC)

    return binary_path
