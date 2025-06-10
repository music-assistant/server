"""Various (server-only) tools and helpers."""

from __future__ import annotations

import asyncio
import functools
import importlib
import logging
import os
import re
import shutil
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import suppress
from functools import lru_cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from types import TracebackType
from typing import TYPE_CHECKING, Any, ParamSpec, Self, TypeVar, cast
from urllib.parse import urlparse

import chardet
import ifaddr
from zeroconf import IPVersion

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.process import check_output

if TYPE_CHECKING:
    from collections.abc import Iterator

    from chardet.resultdict import ResultDict
    from zeroconf.asyncio import AsyncServiceInfo

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderModuleType

LOGGER = logging.getLogger(__name__)

HA_WHEELS = "https://wheels.home-assistant.io/musllinux/"

T = TypeVar("T")
CALLBACK_TYPE = Callable[[], None]


keyword_pattern = re.compile("title=|artist=")
title_pattern = re.compile(r"title=\"(?P<title>.*?)\"")
artist_pattern = re.compile(r"artist=\"(?P<artist>.*?)\"")
dot_com_pattern = re.compile(r"(?P<netloc>\(?\w+\.(?:\w+\.)?(\w{2,3})\)?)")
ad_pattern = re.compile(r"((ad|advertisement)_)|^AD\s\d+$|ADBREAK", flags=re.IGNORECASE)
title_artist_order_pattern = re.compile(r"(?P<title>.+)\sBy:\s(?P<artist>.+)", flags=re.IGNORECASE)
multi_space_pattern = re.compile(r"\s{2,}")
end_junk_pattern = re.compile(r"(.+?)(\s\W+)$")

VERSION_PARTS = (
    # list of common version strings
    "version",
    "live",
    "edit",
    "remix",
    "mix",
    "acoustic",
    "instrumental",
    "karaoke",
    "remaster",
    "versie",
    "unplugged",
    "disco",
    "akoestisch",
    "deluxe",
)
IGNORE_TITLE_PARTS = (
    # strings that may be stripped off a title part
    # (most important the featuring parts)
    "feat.",
    "featuring",
    "ft.",
    "with ",
    "explicit",
)


def filename_from_string(string: str) -> str:
    """Create filename from unsafe string."""
    keepcharacters = (" ", ".", "_")
    return "".join(c for c in string if c.isalnum() or c in keepcharacters).rstrip()


def try_parse_int(possible_int: Any, default: int | None = 0) -> int | None:
    """Try to parse an int."""
    try:
        return int(float(possible_int))
    except (TypeError, ValueError):
        return default


def try_parse_float(possible_float: Any, default: float | None = 0.0) -> float | None:
    """Try to parse a float."""
    try:
        return float(possible_float)
    except (TypeError, ValueError):
        return default


def try_parse_bool(possible_bool: Any) -> bool:
    """Try to parse a bool."""
    if isinstance(possible_bool, bool):
        return possible_bool
    return possible_bool in ["true", "True", "1", "on", "ON", 1]


def try_parse_duration(duration_str: str) -> float:
    """Try to parse a duration in seconds from a duration (HH:MM:SS) string."""
    milliseconds = float("0." + duration_str.split(".")[-1]) if "." in duration_str else 0.0
    duration_parts = duration_str.split(".")[0].split(",")[0].split(":")
    if len(duration_parts) == 3:
        seconds = sum(x * int(t) for x, t in zip([3600, 60, 1], duration_parts, strict=False))
    elif len(duration_parts) == 2:
        seconds = sum(x * int(t) for x, t in zip([60, 1], duration_parts, strict=False))
    else:
        seconds = int(duration_parts[0])
    return seconds + milliseconds


def parse_title_and_version(title: str, track_version: str | None = None) -> tuple[str, str]:
    """Try to parse version from the title."""
    version = track_version or ""
    for regex in (r"\(.*?\)", r"\[.*?\]", r" - .*"):
        for title_part in re.findall(regex, title):
            for ignore_str in IGNORE_TITLE_PARTS:
                if ignore_str in title_part.lower():
                    title = title.replace(title_part, "").strip()
                    continue
            for version_str in VERSION_PARTS:
                if version_str not in title_part.lower():
                    continue
                version = (
                    title_part.replace("(", "")
                    .replace(")", "")
                    .replace("[", "")
                    .replace("]", "")
                    .replace("-", "")
                    .strip()
                )
                title = title.replace(title_part, "").strip()
                return (title, version)
    return title, version


def strip_ads(line: str) -> str:
    """Strip Ads from line."""
    if ad_pattern.search(line):
        return "Advert"
    return line


def strip_url(line: str) -> str:
    """Strip URL from line."""
    return (
        " ".join([p for p in line.split() if (not urlparse(p).scheme or not urlparse(p).netloc)])
    ).rstrip()


def strip_dotcom(line: str) -> str:
    """Strip scheme-less netloc from line."""
    return dot_com_pattern.sub("", line)


def strip_end_junk(line: str) -> str:
    """Strip non-word info from end of line."""
    return end_junk_pattern.sub(r"\1", line)


def swap_title_artist_order(line: str) -> str:
    """Swap title/artist order in line."""
    return title_artist_order_pattern.sub(r"\g<artist> - \g<title>", line)


def strip_multi_space(line: str) -> str:
    """Strip multi-whitespace from line."""
    return multi_space_pattern.sub(" ", line)


def multi_strip(line: str) -> str:
    """Strip assorted junk from line."""
    return strip_multi_space(
        swap_title_artist_order(strip_end_junk(strip_dotcom(strip_url(strip_ads(line)))))
    ).rstrip()


def clean_stream_title(line: str) -> str:
    """Strip junk text from radio streamtitle."""
    title: str = ""
    artist: str = ""

    if not keyword_pattern.search(line):
        return multi_strip(line)

    if match := title_pattern.search(line):
        title = multi_strip(match.group("title"))

    if match := artist_pattern.search(line):
        possible_artist = multi_strip(match.group("artist"))
        if possible_artist and possible_artist != title:
            artist = possible_artist

    if not title and not artist:
        return ""

    if title:
        if re.search(" - ", title) or not artist:
            return title
        if artist:
            return f"{artist} - {title}"

    if artist:
        return artist

    return line


async def get_ip_addresses(include_ipv6: bool = False) -> tuple[str, ...]:
    """Return all IP-adresses of all network interfaces."""

    def call() -> tuple[str, ...]:
        result: list[tuple[int, str]] = []
        # try to get the primary IP address
        # this is the IP address of the default route
        _sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _sock.settimeout(0)
        try:
            # doesn't even have to be reachable
            _sock.connect(("10.254.254.254", 1))
            primary_ip = _sock.getsockname()[0]
        except Exception:
            primary_ip = ""
        finally:
            _sock.close()
        # get all IP addresses of all network interfaces
        adapters = ifaddr.get_adapters()
        for adapter in adapters:
            for ip in adapter.ips:
                if ip.is_IPv6 and not include_ipv6:
                    continue
                ip_str = str(ip.ip)
                if ip_str.startswith(("127", "169.254")):
                    # filter out IPv4 loopback/APIPA address
                    continue
                if ip_str.startswith(("::1", "::ffff:", "fe80")):
                    # filter out IPv6 loopback/link-local address
                    continue
                if ip_str == primary_ip:
                    score = 10
                elif ip_str.startswith(("192.168.",)):
                    # we rank the 192.168 range a bit higher as its most
                    # often used as the private network subnet
                    score = 2
                elif ip_str.startswith(("172.", "10.", "192.")):
                    # we rank the 172 range a bit lower as its most
                    # often used as the private docker network
                    score = 1
                else:
                    score = 0
                result.append((score, ip_str))
        result.sort(key=lambda x: x[0], reverse=True)
        return tuple(ip[1] for ip in result)

    return await asyncio.to_thread(call)


async def get_primary_ip_address() -> str | None:
    """Return the primary IP address of the system."""


async def is_port_in_use(port: int) -> bool:
    """Check if port is in use."""

    def _is_port_in_use() -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _sock:
            try:
                _sock.bind(("0.0.0.0", port))
            except OSError:
                return True
        return False

    return await asyncio.to_thread(_is_port_in_use)


async def select_free_port(range_start: int, range_end: int) -> int:
    """Automatically find available port within range."""
    for port in range(range_start, range_end):
        if not await is_port_in_use(port):
            return port
    msg = "No free port available"
    raise OSError(msg)


async def get_ip_from_host(dns_name: str) -> str | None:
    """Resolve (first) IP-address for given dns name."""

    def _resolve() -> str | None:
        try:
            return socket.gethostbyname(dns_name)
        except Exception:
            # fail gracefully!
            return None

    return await asyncio.to_thread(_resolve)


async def get_ip_pton(ip_string: str) -> bytes:
    """Return socket pton for a local ip."""
    try:
        return await asyncio.to_thread(socket.inet_pton, socket.AF_INET, ip_string)
    except OSError:
        return await asyncio.to_thread(socket.inet_pton, socket.AF_INET6, ip_string)


async def get_folder_size(folderpath: str) -> float:
    """Return folder size in gb."""

    def _get_folder_size(folderpath: str) -> float:
        total_size = 0
        for dirpath, _dirnames, filenames in os.walk(folderpath):
            for _file in filenames:
                _fp = os.path.join(dirpath, _file)
                total_size += os.path.getsize(_fp)
        return total_size / float(1 << 30)

    return await asyncio.to_thread(_get_folder_size, folderpath)


def get_changed_keys(
    dict1: dict[str, Any],
    dict2: dict[str, Any],
    ignore_keys: list[str] | None = None,
    recursive: bool = False,
) -> set[str]:
    """Compare 2 dicts and return set of changed keys."""
    return set(get_changed_values(dict1, dict2, ignore_keys, recursive).keys())


def get_changed_values(
    dict1: dict[str, Any],
    dict2: dict[str, Any],
    ignore_keys: list[str] | None = None,
    recursive: bool = False,
) -> dict[str, tuple[Any, Any]]:
    """
    Compare 2 dicts and return dict of changed values.

    dict key is the changed key, value is tuple of old and new values.
    """
    if not dict1 and not dict2:
        return {}
    if not dict1:
        return {key: (None, value) for key, value in dict2.items()}
    if not dict2:
        return {key: (None, value) for key, value in dict1.items()}
    changed_values = {}
    for key, value in dict2.items():
        if ignore_keys and key in ignore_keys:
            continue
        if key not in dict1:
            changed_values[key] = (None, value)
        elif isinstance(value, dict) or isinstance(dict1[key], dict):
            changed_subvalues = get_changed_values(dict1[key], value, ignore_keys, recursive)
            if recursive:
                changed_values.update(changed_subvalues)
            elif changed_subvalues:
                changed_values[key] = (dict1[key], value)
        elif dict1[key] != value:
            changed_values[key] = (dict1[key], value)
    return changed_values


def empty_queue(q: asyncio.Queue[T]) -> None:
    """Empty an asyncio Queue."""
    for _ in range(q.qsize()):
        try:
            q.get_nowait()
            q.task_done()
        except (asyncio.QueueEmpty, ValueError):
            pass


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


async def is_hass_supervisor() -> bool:
    """Return if we're running inside the HA Supervisor (e.g. HAOS)."""

    def _check() -> bool:
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
        return cast(
            "ProviderModuleType", importlib.import_module(f".{domain}", "music_assistant.providers")
        )

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


async def has_tmpfs_mount() -> bool:
    """Check if we have a tmpfs mount."""

    def _has_tmpfs_mount() -> bool:
        """Check if we have a tmpfs mount."""
        try:
            with open("/proc/mounts") as file:
                for line in file:
                    if "tmpfs /tmp tmpfs rw" in line:
                        return True
        except (FileNotFoundError, OSError, PermissionError):
            pass
        return False

    return await asyncio.to_thread(_has_tmpfs_mount)


async def get_free_space(folder: str) -> float:
    """Return free space on given folderpath in GB."""

    def _get_free_space(folder: str) -> float:
        """Return free space on given folderpath in GB."""
        try:
            res = shutil.disk_usage(folder)
            return res.free / float(1 << 30)
        except (FileNotFoundError, OSError, PermissionError):
            return 0.0

    return await asyncio.to_thread(_get_free_space, folder)


async def get_free_space_percentage(folder: str) -> float:
    """Return free space on given folderpath in percentage."""

    def _get_free_space(folder: str) -> float:
        """Return free space on given folderpath in GB."""
        try:
            res = shutil.disk_usage(folder)
            return res.free / res.total * 100
        except (FileNotFoundError, OSError, PermissionError):
            return 0.0

    return await asyncio.to_thread(_get_free_space, folder)


async def has_enough_space(folder: str, size: int) -> bool:
    """Check if folder has enough free space."""
    return await get_free_space(folder) > size


def divide_chunks(data: bytes, chunk_size: int) -> Iterator[bytes]:
    """Chunk bytes data into smaller chunks."""
    for i in range(0, len(data), chunk_size):
        yield data[i : i + chunk_size]


async def remove_file(file_path: str) -> None:
    """Remove file path (if it exists)."""
    if not await asyncio.to_thread(os.path.exists, file_path):
        return
    await asyncio.to_thread(os.remove, file_path)
    LOGGER.log(VERBOSE_LOG_LEVEL, "Removed file: %s", file_path)


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


def get_port_from_zeroconf(discovery_info: AsyncServiceInfo) -> int | None:
    """Get port from zeroconf discovery info."""
    return discovery_info.port


async def close_async_generator(agen: AsyncGenerator[Any, None]) -> None:
    """Force close an async generator."""
    task = asyncio.create_task(agen.__anext__())
    task.cancel()
    with suppress(asyncio.CancelledError, StopAsyncIteration):
        await task
    await agen.aclose()


async def detect_charset(data: bytes, fallback: str = "utf-8") -> str:
    """Detect charset of raw data."""
    try:
        detected: ResultDict = await asyncio.to_thread(chardet.detect, data)
        if detected and detected["encoding"] and detected["confidence"] > 0.75:
            assert isinstance(detected["encoding"], str)  # for type checking
            return detected["encoding"]
    except Exception as err:
        LOGGER.debug("Failed to detect charset: %s", err)
    return fallback


def merge_dict(
    base_dict: dict[Any, Any],
    new_dict: dict[Any, Any],
    allow_overwite: bool = False,
) -> dict[Any, Any]:
    """Merge dict without overwriting existing values."""
    final_dict = base_dict.copy()
    for key, value in new_dict.items():
        if final_dict.get(key) and isinstance(value, dict):
            final_dict[key] = merge_dict(final_dict[key], value)
        if final_dict.get(key) and isinstance(value, tuple):
            final_dict[key] = merge_tuples(final_dict[key], value)
        if final_dict.get(key) and isinstance(value, list):
            final_dict[key] = merge_lists(final_dict[key], value)
        elif not final_dict.get(key) or allow_overwite:
            final_dict[key] = value
    return final_dict


def merge_tuples(base: tuple[Any, ...], new: tuple[Any, ...]) -> tuple[Any, ...]:
    """Merge 2 tuples."""
    return tuple(x for x in base if x not in new) + tuple(new)


def merge_lists(base: list[Any], new: list[Any]) -> list[Any]:
    """Merge 2 lists."""
    return [x for x in base if x not in new] + list(new)


def percentage(part: float, whole: float) -> int:
    """Calculate percentage."""
    return int(100 * float(part) / float(whole))


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
        self._tasks: list[asyncio.Task[None]] = []
        self._semaphore = asyncio.Semaphore(limit) if limit else None

    def create_task(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Create a new task and add it to the manager."""
        task = self.mass.create_task(coro)
        self._tasks.append(task)
        return task

    async def create_task_with_limit(self, coro: Coroutine[Any, Any, None]) -> None:
        """Create a new task with semaphore limit."""
        assert self._semaphore is not None

        def task_done_callback(_task: asyncio.Task[None]) -> None:
            assert self._semaphore is not None  # for type checking
            self._tasks.remove(task)
            self._semaphore.release()

        await self._semaphore.acquire()
        task: asyncio.Task[None] = self.create_task(coro)
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
        return None


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
            func.lock = func_lock  # type: ignore[attr-defined]
        async with func_lock:
            return await func(*args, **kwargs)

    return wrapper


class TimedAsyncGenerator:
    """
    Async iterable that times out after a given time.

    Source: https://medium.com/@dmitry8912/implementing-timeouts-in-pythons-asynchronous-generators-f7cbaa6dc1e9
    """

    def __init__(self, iterable: AsyncIterator[Any], timeout: int = 0):
        """
        Initialize the AsyncTimedIterable.

        Args:
            iterable: The async iterable to wrap.
            timeout: The timeout in seconds for each iteration.
        """

        class AsyncTimedIterator:
            def __init__(self) -> None:
                self._iterator = iterable.__aiter__()

            async def __anext__(self) -> Any:
                result = await asyncio.wait_for(self._iterator.__anext__(), int(timeout))
                if not result:
                    raise StopAsyncIteration
                return result

        self._factory = AsyncTimedIterator

    def __aiter__(self):  # type: ignore[no-untyped-def]
        """Return the async iterator."""
        return self._factory()
