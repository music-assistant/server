"""Various (server-only) tools and helpers."""

from __future__ import annotations

import asyncio
import functools
import importlib
import logging
import platform
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine
from contextlib import suppress
from functools import lru_cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from types import TracebackType
from typing import TYPE_CHECKING, Any, ParamSpec, Self, TypeVar

import cchardet as chardet
import ifaddr
import memory_tempfile
from zeroconf import IPVersion

from music_assistant.server.helpers.process import check_output

if TYPE_CHECKING:
    from collections.abc import Iterator

    from zeroconf.asyncio import AsyncServiceInfo

    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderModuleType

LOGGER = logging.getLogger(__name__)

HA_WHEELS = "https://wheels.home-assistant.io/musllinux/"


async def install_package(package: str) -> None:
    """Install package with pip, raise when install failed."""
    LOGGER.debug("Installing python package %s", package)
    args = ["uv", "pip", "install", "--no-cache", "--find-links", HA_WHEELS, package]
    return_code, output = await check_output(*args)

    if return_code != 0 and "Permission denied" in output.decode():
        # try again with regular pip
        # uv pip seems to have issues with permissions on docker installs
        args = [
            "pip",
            "install",
            "--no-cache-dir",
            "--no-input",
            "--find-links",
            HA_WHEELS,
            package,
        ]
        return_code, output = await check_output(*args)

    if return_code != 0:
        msg = f"Failed to install package {package}\n{output.decode()}"
        raise RuntimeError(msg)


async def get_package_version(pkg_name: str) -> str | None:
    """
    Return the version of an installed (python) package.

    Will return None if the package is not found.
    """
    try:
        return await asyncio.to_thread(pkg_version, pkg_name)
    except PackageNotFoundError:
        return None


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


async def load_provider_module(domain: str, requirements: list[str]) -> ProviderModuleType:
    """Return module for given provider domain and make sure the requirements are met."""

    @lru_cache
    def _get_provider_module(domain: str) -> ProviderModuleType:
        return importlib.import_module(f".{domain}", "music_assistant.server.providers")

    # ensure module requirements are met
    for requirement in requirements:
        if "==" not in requirement:
            # we should really get rid of unpinned requirements
            continue
        package_name, version = requirement.split("==", 1)
        installed_version = await get_package_version(package_name)
        if installed_version == "0.0.0":
            # ignore editable installs
            continue
        if installed_version != version:
            await install_package(requirement)

    # try to load the module
    try:
        return await asyncio.to_thread(_get_provider_module, domain)
    except ImportError:
        # (re)install ALL requirements
        for requirement in requirements:
            await install_package(requirement)
    # try loading the provider again to be safe
    # this will fail if something else is wrong (as it should)
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


def get_primary_ip_address_from_zeroconf(discovery_info: AsyncServiceInfo) -> str | None:
    """Get primary IP address from zeroconf discovery info."""
    for address in discovery_info.parsed_addresses(IPVersion.V4Only):
        if address.startswith("127"):
            # filter out loopback address
            continue
        if address.startswith("169.254"):
            # filter out APIPA address
            continue
        return address
    return None


def get_port_from_zeroconf(discovery_info: AsyncServiceInfo) -> str | None:
    """Get primary IP address from zeroconf discovery info."""
    return discovery_info.port


async def close_async_generator(agen: AsyncGenerator[Any, None]) -> None:
    """Force close an async generator."""
    task = asyncio.create_task(agen.__anext__())
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    await agen.aclose()


async def detect_charset(data: bytes, fallback="utf-8") -> str:
    """Detect charset of raw data."""
    try:
        detected = await asyncio.to_thread(chardet.detect, data)
        if detected and detected["encoding"] and detected["confidence"] > 0.75:
            return detected["encoding"]
    except Exception as err:
        LOGGER.debug("Failed to detect charset: %s", err)
    return fallback


class TaskManager:
    """
    Helper class to run many tasks at once.

    This is basically an alternative to asyncio.TaskGroup but this will not
    cancel all operations when one of the tasks fails.
    Logging of exceptions is done by the mass.create_task helper.
    """

    def __init__(self, mass: MusicAssistant, limit: int = 0):
        """Initialize the TaskManager."""
        self.mass = mass
        self._tasks: list[asyncio.Task] = []
        self._semaphore = asyncio.Semaphore(limit) if limit else None

    def create_task(self, coro: Coroutine) -> asyncio.Task:
        """Create a new task and add it to the manager."""
        task = self.mass.create_task(coro)
        self._tasks.append(task)
        return task

    async def create_task_with_limit(self, coro: Coroutine) -> None:
        """Create a new task with semaphore limit."""
        assert self._semaphore is not None

        def task_done_callback(_task: asyncio.Task) -> None:
            self._tasks.remove(task)
            self._semaphore.release()

        await self._semaphore.acquire()
        task: asyncio.Task = self.create_task(coro)
        task.add_done_callback(task_done_callback)

    async def __aenter__(self) -> Self:
        """Enter context manager."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        """Exit context manager."""
        if len(self._tasks) > 0:
            await asyncio.wait(self._tasks)
            self._tasks.clear()


_R = TypeVar("_R")
_P = ParamSpec("_P")


def lock(
    func: Callable[_P, Awaitable[_R]],
) -> Callable[_P, Coroutine[Any, Any, _R]]:
    """Call async function using a Lock."""

    @functools.wraps(func)
    async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        """Call async function using the throttler with retries."""
        if not (func_lock := getattr(func, "lock", None)):
            func_lock = asyncio.Lock()
            func.lock = func_lock
        async with func_lock:
            return await func(*args, **kwargs)

    return wrapper


class TimedAsyncGenerator:
    """
    Async iterable that times out after a given time.

    Source: https://medium.com/@dmitry8912/implementing-timeouts-in-pythons-asynchronous-generators-f7cbaa6dc1e9
    """

    def __init__(self, iterable, timeout=0):
        """
        Initialize the AsyncTimedIterable.

        Args:
            iterable: The async iterable to wrap.
            timeout: The timeout in seconds for each iteration.
        """

        class AsyncTimedIterator:
            def __init__(self):
                self._iterator = iterable.__aiter__()

            async def __anext__(self):
                result = await asyncio.wait_for(self._iterator.__anext__(), int(timeout))
                if not result:
                    raise StopAsyncIteration
                return result

        self._factory = AsyncTimedIterator

    def __aiter__(self):
        """Return the async iterator."""
        return self._factory()
