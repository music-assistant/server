"""Various (server-only) tools and helpers."""
from __future__ import annotations

import asyncio
import importlib
import logging
import platform
import socket
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from contextlib import suppress
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


async def get_ips(include_ipv6: bool = False) -> set[str]:
    """Return all IP-adresses of all network interfaces."""

    def call() -> set[str]:
        result: set[str] = set()
        for item in socket.getaddrinfo(socket.gethostname(), None):
            protocol, *_, (ip, *_) = item
            if protocol == socket.AddressFamily.AF_INET or (
                include_ipv6 and protocol == socket.AddressFamily.AF_INET6
            ):
                result.add(ip)
        return result

    return await asyncio.to_thread(call)


async def is_hass_supervisor() -> bool:
    """Return if we're running inside the HA Supervisor (e.g. HAOS)."""
    with suppress(urllib.error.URLError):
        res = await asyncio.to_thread(urllib.request.urlopen, "ws://supervisor/core/websocket")
        return res.code == 401
    return False


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
