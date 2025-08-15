"""Helpers/utils for the Spotify musicprovider."""

from __future__ import annotations

import os
import platform

from music_assistant.helpers.process import check_output


async def get_librespot_binary() -> str:
    """Find the correct librespot binary belonging to the platform."""

    # ruff: noqa: SIM102
    async def check_librespot(librespot_path: str) -> str | None:
        try:
            returncode, output = await check_output(librespot_path, "--version")
            if returncode == 0 and b"librespot" in output:
                return librespot_path
        except OSError:
            return None

    base_path = os.path.join(os.path.dirname(__file__), "bin")
    system = platform.system().lower().replace("darwin", "macos")
    architecture = platform.machine().lower()

    if librespot_binary := await check_librespot(
        os.path.join(base_path, f"librespot-{system}-{architecture}")
    ):
        return librespot_binary

    msg = f"Unable to locate Librespot for {system}/{architecture}"
    raise RuntimeError(msg)
