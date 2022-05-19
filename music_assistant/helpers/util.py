"""Helper and utility functions."""
from __future__ import annotations

import os
import platform
import re
import socket
import tempfile
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, TypeVar

import memory_tempfile
import unidecode

# pylint: disable=invalid-name
T = TypeVar("T")
_UNDEF: dict = {}
CALLABLE_T = TypeVar("CALLABLE_T", bound=Callable)
CALLBACK_TYPE = Callable[[], None]
# pylint: enable=invalid-name


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


def create_clean_string(input_str: str) -> str:
    """Return clean lowered string for compare actions."""
    input_str = input_str.lower().strip()
    for item in ["the ", "de ", "les "]:
        if input_str.startswith(item):
            input_str = input_str.replace(item, "")
    unaccented_string = unidecode.unidecode(input_str)
    return re.sub(r"[^a-zA-Z0-9]", "", unaccented_string)


def parse_title_and_version(title: str, track_version: str = None):
    """Try to parse clean track title and version from the title."""
    version = ""
    for splitter in [" (", " [", " - ", " (", " [", "-"]:
        if splitter in title:
            title_parts = title.split(splitter)
            for title_part in title_parts:
                # look for the end splitter
                for end_splitter in [")", "]"]:
                    if end_splitter in title_part:
                        title_part = title_part.split(end_splitter)[0]
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
                        title_part = title_part.split(end_splitter)[0]
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
    elif version_str.endswith("remaster"):
        version_str = "remaster"
    elif version_str.endswith("remastered"):
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


def is_port_in_use(port: int) -> bool:
    """Check if port is in use."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _sock:
        return _sock.connect_ex(("localhost", port)) == 0


def select_stream_port() -> int:
    """Automatically find available stream port, prefer the default 8095."""
    for port in range(8095, 8195):
        if not is_port_in_use(port):
            return port


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
        if final_dict.get(key) and isinstance(value, tuple):
            final_dict[key] = merge_tuples(final_dict[key], value)
        if final_dict.get(key) and isinstance(value, list):
            final_dict[key] = merge_lists(final_dict[key], value)
        elif not final_dict.get(key) or allow_overwite:
            final_dict[key] = value
    return final_dict


def merge_tuples(base: tuple, new: tuple) -> Tuple:
    """Merge 2 tuples."""
    return tuple(x for x in base if x not in new) + tuple(new)


def merge_lists(base: list, new: list) -> list:
    """Merge 2 lists."""
    return list(x for x in base if x not in new) + list(new)


def create_tempfile():
    """Return a (named) temporary file."""
    if platform.system() == "Linux":
        return memory_tempfile.MemoryTempfile(fallback=True).NamedTemporaryFile(
            buffering=0
        )
    return tempfile.NamedTemporaryFile(buffering=0)


def get_changed_keys(
    dict1: Dict[str, Any],
    dict2: Dict[str, Any],
    ignore_keys: Optional[List[str]] = None,
) -> Set[str]:
    """Compare 2 dicts and return set of changed keys."""
    if not dict2:
        return set(dict1.keys())
    changed_keys = set()
    for key, value in dict2.items():
        if ignore_keys and key in ignore_keys:
            continue
        if key not in dict1:
            changed_keys.add(key)
        elif isinstance(value, dict):
            changed_keys.update(get_changed_keys(dict1[key], value))
        elif dict1[key] != value:
            changed_keys.add(key)
    return changed_keys
