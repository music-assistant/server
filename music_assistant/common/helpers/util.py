"""Helper and utility functions."""

from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import Callable
from typing import Any, TypeVar
from uuid import UUID

# pylint: disable=invalid-name
T = TypeVar("T")
_UNDEF: dict = {}
CALLABLE_T = TypeVar("CALLABLE_T", bound=Callable)
CALLBACK_TYPE = Callable[[], None]
# pylint: enable=invalid-name


def filename_from_string(string: str) -> str:
    """Create filename from unsafe string."""
    keepcharacters = (" ", ".", "_")
    return "".join(c for c in string if c.isalnum() or c in keepcharacters).rstrip()


def try_parse_int(possible_int: Any, default: int | None = 0) -> int | None:
    """Try to parse an int."""
    try:
        return int(possible_int)
    except (TypeError, ValueError):
        return default


def try_parse_float(possible_float: Any, default: float | None = 0.0) -> float | None:
    """Try to parse a float."""
    try:
        return float(possible_float)
    except (TypeError, ValueError):
        return default


def try_parse_bool(possible_bool: Any) -> str:
    """Try to parse a bool."""
    if isinstance(possible_bool, bool):
        return possible_bool
    return possible_bool in ["true", "True", "1", "on", "ON", 1]


def create_sort_name(input_str: str) -> str:
    """Create sort name/title from string."""
    input_str = input_str.lower().strip()
    for item in ["the ", "de ", "les ", "dj ", ".", "-", "'", "`"]:
        if input_str.startswith(item):
            input_str = input_str.replace(item, "")
    return input_str.strip()


def parse_title_and_version(title: str, track_version: str | None = None):
    """Try to parse clean track title and version from the title."""
    version = ""
    for splitter in [" (", " [", " - ", " (", " [", "-"]:
        if splitter in title:
            title_parts = title.split(splitter)
            for title_part in title_parts:
                # look for the end splitter
                for end_splitter in [")", "]"]:
                    if end_splitter in title_part:
                        title_part = title_part.split(end_splitter)[0]  # noqa: PLW2901
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
                    if version_str in title_part.lower():
                        version = title_part
                        title = title.split(splitter + version)[0]
    title = clean_title(title)
    if not version and track_version:
        version = track_version
    version = get_version_substitute(version).title()
    if version == title:
        version = ""
    return title, version


def clean_title(title: str) -> str:
    """Strip unwanted additional text from title."""
    for splitter in [" (", " [", " - ", " (", " [", "-"]:
        if splitter in title:
            title_parts = title.split(splitter)
            for title_part in title_parts:
                # look for the end splitter
                for end_splitter in [")", "]"]:
                    if end_splitter in title_part:
                        title_part = title_part.split(end_splitter)[0]  # noqa: PLW2901
                for ignore_str in ["feat.", "featuring", "ft.", "with ", "explicit"]:
                    if ignore_str in title_part.lower():
                        return title.split(splitter + title_part)[0].strip()
    return title.strip()


def get_version_substitute(version_str: str):
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
    elif "remaster" in version_str:
        version_str = "remaster"
    return version_str.strip()


async def get_ip():
    """Get primary IP-address for this host."""

    def _get_ip():
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

    return await asyncio.to_thread(_get_ip)


async def select_free_port(range_start: int, range_end: int) -> int:
    """Automatically find available port within range."""

    def is_port_in_use(port: int) -> bool:
        """Check if port is in use."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _sock:
            try:
                _sock.bind(("0.0.0.0", port))
            except OSError:
                return True
        return False

    def _select_free_port():
        for port in range(range_start, range_end):
            if not is_port_in_use(port):
                return port
        msg = "No free port available"
        raise OSError(msg)

    return await asyncio.to_thread(_select_free_port)


async def get_ip_from_host(dns_name: str) -> str | None:
    """Resolve (first) IP-address for given dns name."""

    def _resolve():
        try:
            return socket.gethostbyname(dns_name)
        except Exception:  # pylint: disable=broad-except
            # fail gracefully!
            return None

    return await asyncio.to_thread(_resolve)


async def get_ip_pton(ip_string: str | None = None):
    """Return socket pton for local ip."""
    if ip_string is None:
        ip_string = await get_ip()
    # pylint:disable=no-member
    try:
        return await asyncio.to_thread(socket.inet_pton, socket.AF_INET, ip_string)
    except OSError:
        return await asyncio.to_thread(socket.inet_pton, socket.AF_INET6, ip_string)


def get_folder_size(folderpath):
    """Return folder size in gb."""
    total_size = 0
    # pylint: disable=unused-variable
    for dirpath, _dirnames, filenames in os.walk(folderpath):
        for _file in filenames:
            _fp = os.path.join(dirpath, _file)
            total_size += os.path.getsize(_fp)
    # pylint: enable=unused-variable
    return total_size / float(1 << 30)


def merge_dict(base_dict: dict, new_dict: dict, allow_overwite=False):
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


def merge_tuples(base: tuple, new: tuple) -> tuple:
    """Merge 2 tuples."""
    return tuple(x for x in base if x not in new) + tuple(new)


def merge_lists(base: list, new: list) -> list:
    """Merge 2 lists."""
    return [x for x in base if x not in new] + list(new)


def get_changed_keys(
    dict1: dict[str, Any],
    dict2: dict[str, Any],
    ignore_keys: list[str] | None = None,
) -> set[str]:
    """Compare 2 dicts and return set of changed keys."""
    return get_changed_values(dict1, dict2, ignore_keys).keys()


def get_changed_values(
    dict1: dict[str, Any],
    dict2: dict[str, Any],
    ignore_keys: list[str] | None = None,
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
        elif isinstance(value, dict):
            changed_values.update(get_changed_values(dict1[key], value, ignore_keys))
        elif dict1[key] != value:
            changed_values[key] = (dict1[key], value)
    return changed_values


def empty_queue(q: asyncio.Queue) -> None:
    """Empty an asyncio Queue."""
    for _ in range(q.qsize()):
        try:
            q.get_nowait()
            q.task_done()
        except (asyncio.QueueEmpty, ValueError):
            pass


def is_valid_uuid(uuid_to_test: str) -> bool:
    """Check if uuid string is a valid UUID."""
    try:
        uuid_obj = UUID(uuid_to_test)
    except ValueError:
        return False
    return str(uuid_obj) == uuid_to_test
