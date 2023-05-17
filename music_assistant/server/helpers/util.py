"""Various (server-only) tools and helpers."""
from __future__ import annotations

import asyncio
import importlib
import logging
import platform
import tempfile
from functools import lru_cache
from typing import TYPE_CHECKING

import memory_tempfile

if TYPE_CHECKING:
    from music_assistant.server.models import ProviderModuleType

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


async def get_provider_module(domain: str) -> ProviderModuleType:
    """Return module for given provider domain."""

    @lru_cache
    def _get_provider_module(domain: str) -> ProviderModuleType:
        return importlib.import_module(f".{domain}", "music_assistant.server.providers")

    return await asyncio.to_thread(_get_provider_module, domain)


def create_tempfile():
    """Return a (named) temporary file."""
    if platform.system() == "Linux":
        return memory_tempfile.MemoryTempfile(fallback=True).NamedTemporaryFile(buffering=0)
    return tempfile.NamedTemporaryFile(buffering=0)
