"""Helpers/utils for the AirPlay Receiver plugin."""

from __future__ import annotations

import os
import platform
import shutil

from music_assistant.helpers.process import check_output


async def get_shairport_sync_binary() -> str:
    """Find the shairport-sync binary (bundled or system-installed)."""

    async def check_shairport_sync(shairport_path: str) -> str | None:
        """Check if shairport-sync binary is valid."""
        try:
            returncode, _ = await check_output(shairport_path, "--version")
            if returncode == 0:
                return shairport_path
            return None
        except OSError:
            return None

    # First, check if bundled binary exists
    base_path = os.path.join(os.path.dirname(__file__), "bin")
    system = platform.system().lower().replace("darwin", "macos")
    architecture = platform.machine().lower()

    if shairport_binary := await check_shairport_sync(
        os.path.join(base_path, f"shairport-sync-{system}-{architecture}")
    ):
        return shairport_binary

    # If no bundled binary, check system PATH
    if system_binary := shutil.which("shairport-sync"):
        if shairport_binary := await check_shairport_sync(system_binary):
            return shairport_binary

    msg = (
        f"Unable to locate shairport-sync for {system}/{architecture}. "
        "Please install shairport-sync on your system or provide a bundled binary."
    )
    raise RuntimeError(msg)
