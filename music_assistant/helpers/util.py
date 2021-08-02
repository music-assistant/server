"""Helper and utility functions."""
import asyncio
import functools
import logging
import os
import platform
import socket
import tempfile
import threading
import urllib.request
from asyncio.events import AbstractEventLoop
from typing import Any, Callable, Dict, List, Optional, Set, TypeVar, Union

import memory_tempfile
import ujson

from .typing import MediaType

# pylint: disable=invalid-name
T = TypeVar("T")
_UNDEF: dict = {}
CALLABLE_T = TypeVar("CALLABLE_T", bound=Callable)
CALLBACK_TYPE = Callable[[], None]
# pylint: enable=invalid-name

DEFAULT_LOOP = None


def callback(func: CALLABLE_T) -> CALLABLE_T:
    """Annotation to mark method as safe to call from within the event loop."""
    setattr(func, "_mass_callback", True)
    return func


def is_callback(func: Callable[..., Any]) -> bool:
    """Check if function is safe to be called in the event loop."""
    return getattr(func, "_mass_callback", False) is True


def create_task(
    target: Callable[..., Any],
    *args: Any,
    loop: AbstractEventLoop = None,
    **kwargs: Any,
) -> Union[asyncio.Task, asyncio.Future]:
    """Create Task on (main) event loop from Callable or awaitable.

    target: target to call.
    loop: Running (main) event loop, defaults to loop in current thread
    args/kwargs: parameters for method to call.
    """
    try:
        loop = loop or asyncio.get_running_loop()
    except RuntimeError:
        # try to fetch the default loop from global variable
        loop = DEFAULT_LOOP

    # Check for partials to properly determine if coroutine function
    check_target = target
    while isinstance(check_target, functools.partial):
        check_target = check_target.func

    async def cb_wrapper(_target: Callable, *_args, **_kwargs):
        return _target(*_args, **_kwargs)

    async def executor_wrapper(_target: Callable, *_args, **_kwargs):
        return await loop.run_in_executor(None, _target, *_args, **_kwargs)

    # called from other thread
    if threading.current_thread() is not threading.main_thread():
        if asyncio.iscoroutine(check_target):
            return asyncio.run_coroutine_threadsafe(target, loop)
        if asyncio.iscoroutinefunction(check_target):
            return asyncio.run_coroutine_threadsafe(target(*args), loop)
        if is_callback(check_target):
            return asyncio.run_coroutine_threadsafe(
                cb_wrapper(target, *args, **kwargs), loop
            )
        return asyncio.run_coroutine_threadsafe(
            executor_wrapper(target, *args, **kwargs), loop
        )

    if asyncio.iscoroutine(check_target):
        return loop.create_task(target)
    if asyncio.iscoroutinefunction(check_target):
        return loop.create_task(target(*args))
    if is_callback(check_target):
        return loop.create_task(cb_wrapper(target, *args, **kwargs))
    return loop.create_task(executor_wrapper(target, *args, **kwargs))


def run_periodic(period):
    """Run a coroutine at interval."""

    def scheduler(fcn):
        async def wrapper(*args, **kwargs):
            while True:
                asyncio.create_task(fcn(*args, **kwargs))
                await asyncio.sleep(period)

        return wrapper

    return scheduler


def get_external_ip():
    """Try to get the external (WAN) IP address."""
    # pylint: disable=broad-except
    try:
        return urllib.request.urlopen("https://ident.me").read().decode("utf8")
    except Exception:
        return None


def filename_from_string(string):
    """Create filename from unsafe string."""
    keepcharacters = (" ", ".", "_")
    return "".join(c for c in string if c.isalnum() or c in keepcharacters).rstrip()


def try_parse_int(possible_int):
    """Try to parse an int."""
    try:
        return int(possible_int)
    except (TypeError, ValueError):
        return 0


async def iter_items(items):
    """Fake async iterator for compatability reasons."""
    if not isinstance(items, list):
        yield items
    else:
        for item in items:
            yield item


def try_parse_float(possible_float):
    """Try to parse a float."""
    try:
        return float(possible_float)
    except (TypeError, ValueError):
        return 0.0


def try_parse_bool(possible_bool):
    """Try to parse a bool."""
    if isinstance(possible_bool, bool):
        return possible_bool
    return possible_bool in ["true", "True", "1", "on", "ON", 1]


def parse_title_and_version(track_title, track_version=None):
    """Try to parse clean track title and version from the title."""
    title = track_title.lower()
    version = ""
    for splitter in [" (", " [", " - ", " (", " [", "-"]:
        if splitter in title:
            title_parts = title.split(splitter)
            for title_part in title_parts:
                # look for the end splitter
                for end_splitter in [")", "]"]:
                    if end_splitter in title_part:
                        title_part = title_part.split(end_splitter)[0]
                for ignore_str in [
                    "feat.",
                    "featuring",
                    "ft.",
                    "with ",
                    " & ",
                    "explicit",
                ]:
                    if ignore_str in title_part:
                        title = title.split(splitter + title_part)[0]
                for version_str in [
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
                    "radio",
                    "unplugged",
                    "disco",
                    "akoestisch",
                    "deluxe",
                ]:
                    if version_str in title_part:
                        version = title_part
                        title = title.split(splitter + version)[0]
    title = title.strip().title()
    if not version and track_version:
        version = track_version
    version = get_version_substitute(version).title()
    if version == title:
        version = ""
    return title, version


def get_version_substitute(version_str):
    """Transform provider version str to universal version type."""
    version_str = version_str.lower()
    # substitute edit and edition with version
    if "edition" in version_str or "edit" in version_str:
        version_str = version_str.replace(" edition", " version")
        version_str = version_str.replace(" edit ", " version")
    if version_str.startswith("the "):
        version_str = version_str.split("the ")[1]
    if "radio mix" in version_str:
        version_str = "radio version"
    elif "video mix" in version_str:
        version_str = "video version"
    elif "spanglish" in version_str or "spanish" in version_str:
        version_str = "spanish version"
    elif version_str.endswith("remaster"):
        version_str = "remaster"
    return version_str.strip()


def get_ip():
    """Get primary IP-address for this host."""
    # pylint: disable=broad-except,no-member
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # doesn't even have to be reachable
        sock.connect(("10.255.255.255", 1))
        _ip = sock.getsockname()[0]
    except Exception:
        _ip = "127.0.0.1"
    finally:
        sock.close()
    return _ip


def get_ip_pton():
    """Return socket pton for local ip."""
    # pylint:disable=no-member
    try:
        return socket.inet_pton(socket.AF_INET, get_ip())
    except OSError:
        return socket.inet_pton(socket.AF_INET6, get_ip())


# pylint: enable=broad-except


def get_hostname():
    """Get hostname for this machine."""
    # pylint:disable=no-member
    return socket.gethostname()


def get_folder_size(folderpath):
    """Return folder size in gb."""
    total_size = 0
    # pylint: disable=unused-variable
    for dirpath, dirnames, filenames in os.walk(folderpath):
        for _file in filenames:
            _fp = os.path.join(dirpath, _file)
            total_size += os.path.getsize(_fp)
    # pylint: enable=unused-variable
    total_size_gb = total_size / float(1 << 30)
    return total_size_gb


def merge_dict(base_dict: dict, new_dict: dict, allow_overwite=False):
    """Merge dict without overwriting existing values."""
    final_dict = base_dict.copy()
    for key, value in new_dict.items():
        if final_dict.get(key) and isinstance(value, dict):
            final_dict[key] = merge_dict(final_dict[key], value)
        if final_dict.get(key) and isinstance(value, list):
            final_dict[key] = merge_list(final_dict[key], value)
        elif not final_dict.get(key) or allow_overwite:
            final_dict[key] = value
    return final_dict


def merge_list(base_list: list, new_list: list) -> Set:
    """Merge 2 lists."""
    final_list = set(base_list)
    for item in new_list:
        if hasattr(item, "item_id"):
            for prov_item in final_list:
                if prov_item.item_id == item.item_id:
                    prov_item = item
        if item not in final_list:
            final_list.add(item)
    return final_list


def try_load_json_file(jsonfile):
    """Try to load json from file."""
    try:
        with open(jsonfile, "r") as _file:
            return ujson.loads(_file.read())
    except (FileNotFoundError, ValueError) as exc:
        logging.getLogger().debug(
            "Could not load json from file %s", jsonfile, exc_info=exc
        )
        return None


def create_tempfile():
    """Return a (named) temporary file."""
    if platform.system() == "Linux":
        return memory_tempfile.MemoryTempfile(fallback=True).NamedTemporaryFile(
            buffering=0
        )
    return tempfile.NamedTemporaryFile(buffering=0)


async def yield_chunks(_obj, chunk_size):
    """Yield successive n-sized chunks from list/str/bytes."""
    chunk_size = int(chunk_size)
    for i in range(0, len(_obj), chunk_size):
        yield _obj[i : i + chunk_size]


def get_changed_keys(
    dict1: Dict[str, Any], dict2: Dict[str, Any], ignore_keys: Optional[Set[str]] = None
) -> Set[str]:
    """Compare 2 dicts and return set of changed keys."""
    if not dict2:
        return set(dict1.keys())
    changed_keys = set()
    for key, value in dict2.items():
        if ignore_keys and key in ignore_keys:
            continue
        if isinstance(value, dict):
            changed_keys.update(get_changed_keys(dict1[key], value))
        elif dict1[key] != value:
            changed_keys.add(key)
    return changed_keys


def create_uri(media_type: MediaType, provider: str, item_id: str):
    """Create uri for mediaitem."""
    return f"{provider}://{media_type.value}/{item_id}"


class LimitedList(list):
    """Implementation of a size limited list."""

    @property
    def max_len(self):
        """Return list's max length."""
        return self._max_len

    def __init__(self, lst: Optional[List] = None, max_len=500):
        """Initialize instance."""
        self._max_len = max_len
        if lst is not None:
            super().__init__(lst)
        else:
            super().__init__()

    def _truncate(self):
        """Call by various methods to reinforce the maximum length."""
        dif = len(self) - self._max_len
        if dif > 0:
            self[:dif] = []

    def append(self, x):
        """Append item x to the list."""
        super().append(x)
        self._truncate()

    def insert(self, *args):
        """Insert items at position x to the list."""
        super().insert(*args)
        self._truncate()

    def extend(self, x):
        """Extend the list."""
        super().extend(x)
        self._truncate()

    def __setitem__(self, *args):
        """Internally set."""
        super().__setitem__(*args)
        self._truncate()

    # def __setslice__(self, *args):
    #     """Internally set slice."""
    #     super().__setslice__(*args)
    #     self._truncate()
