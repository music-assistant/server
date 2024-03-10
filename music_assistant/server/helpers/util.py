"""Various (server-only) tools and helpers."""

from __future__ import annotations

import asyncio
import importlib
import logging
import platform
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from functools import lru_cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from typing import TYPE_CHECKING

import ifaddr
import memory_tempfile

if TYPE_CHECKING:
    from collections.abc import Iterator

    from music_assistant.server.models import ProviderModuleType

LOGGER = logging.getLogger(__name__)

HA_WHEELS = "https://wheels.home-assistant.io/musllinux/"


async def install_package(package: str) -> None:
    """Install package with pip, raise when install failed."""
    cmd = f"python3 -m pip install --find-links {HA_WHEELS} {package}"
    proc = await asyncio.create_subprocess_shell(
        cmd, stderr=asyncio.subprocess.STDOUT, stdout=asyncio.subprocess.PIPE
    )

    stdout, _ = await proc.communicate()

    if proc.returncode != 0:
        msg = f"Failed to install package {package}\n{stdout.decode()}"
        raise RuntimeError(msg)


async def get_package_version(pkg_name: str) -> str:
    """
    Return the version of an installed (python) package.

    Will return `0.0.0` if the package is not found.
    """
    try:
        installed_version = await asyncio.to_thread(pkg_version, pkg_name)
        if installed_version is None:
            return "0.0.0"  # type: ignore[unreachable]
        return installed_version
    except PackageNotFoundError:
        return "0.0.0"


async def get_ips(include_ipv6: bool = False, ignore_loopback: bool = True) -> set[str]:
    """Return all IP-adresses of all network interfaces."""

    def call() -> set[str]:
        result: set[str] = set()
        adapters = ifaddr.get_adapters()
        for adapter in adapters:
            for ip in adapter.ips:
                if ip.is_IPv6 and not include_ipv6:
                    continue
                if ip.ip == "127.0.0.1" and ignore_loopback:
                    continue
                result.add(ip.ip)
        return result

    return await asyncio.to_thread(call)


async def is_hass_supervisor() -> bool:
    """Return if we're running inside the HA Supervisor (e.g. HAOS)."""

    def _check():
        try:
            urllib.request.urlopen("http://supervisor/core", timeout=1)
        except urllib.error.URLError as err:
            # this should return a 401 unauthorized if it exists
            return getattr(err, "code", 999) == 401
        except Exception:
            return False
        return False

    return await asyncio.to_thread(_check)


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


def divide_chunks(data: bytes, chunk_size: int) -> Iterator[bytes]:
    """Chunk bytes data into smaller chunks."""
    for i in range(0, len(data), chunk_size):
        yield data[i : i + chunk_size]
