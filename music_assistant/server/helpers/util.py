"""Various (server-only) tools and helpers."""

import asyncio
import logging

LOGGER = logging.getLogger(__name__)

HA_WHEELS = "https://wheels.home-assistant.io/musllinux/"


async def install_package(package: str) -> None:
    """Install package with pip, raise when install failed."""
    cmd = f"python3 -m pip install --find-links {HA_WHEELS} {package}"
    proc = await asyncio.create_subprocess_shell(
        cmd, stderr=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL
    )

    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        msg = f"Failed to install package {package}\n{stderr.decode()}"
        raise RuntimeError(msg)
