"""Various (server-only) tools and helpers."""
from __future__ import annotations

import asyncio
import importlib
import logging
from collections.abc import AsyncGenerator, Iterator
from functools import lru_cache
from typing import TYPE_CHECKING, Any

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


async def async_iter(sync_iterator: Iterator, *args, **kwargs) -> AsyncGenerator[Any, None]:
    """Wrap blocking iterator into an asynchronous one."""
    # inspired by: https://stackoverflow.com/questions/62294385/synchronous-generator-in-asyncio
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue(1)
    _end_ = object()

    def iter_to_queue():
        try:
            for item in sync_iterator(*args, **kwargs):
                if queue is None:
                    break
                asyncio.run_coroutine_threadsafe(queue.put(item), loop).result()
        finally:
            asyncio.run_coroutine_threadsafe(queue.put(_end_), loop).result()

    iter_fut = loop.run_in_executor(None, iter_to_queue)
    try:
        while True:
            next_item = await queue.get()
            if next_item is _end_:
                break
            yield next_item
    finally:
        queue = None
        if not iter_fut.done():
            iter_fut.cancel()
