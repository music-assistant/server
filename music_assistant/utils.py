"""Helper and utility functions."""
import asyncio
import dataclasses
import functools
import logging
import os
import platform
import re
import socket
import tempfile
from enum import Enum
from typing import Any, Callable, TypeVar

import memory_tempfile
import unidecode

try:
    import simplejson as json
except ImportError:
    import json


# pylint: disable=invalid-name
T = TypeVar("T")
_UNDEF: dict = {}
CALLABLE_T = TypeVar("CALLABLE_T", bound=Callable)
CALLBACK_TYPE = Callable[[], None]
# pylint: enable=invalid-name


def callback(func: CALLABLE_T) -> CALLABLE_T:
    """Annotation to mark method as safe to call from within the event loop."""
    setattr(func, "_mass_callback", True)
    return func


def is_callback(func: Callable[..., Any]) -> bool:
    """Check if function is safe to be called in the event loop."""
    return getattr(func, "_mass_callback", False) is True


def run_periodic(period):
    def scheduler(fcn):
        async def async_wrapper(*args, **kwargs):
            while True:
                asyncio.create_task(fcn(*args, **kwargs))
                await asyncio.sleep(period)

        return async_wrapper

    return scheduler


def filename_from_string(string):
    """create filename from unsafe string"""
    keepcharacters = (" ", ".", "_")
    return "".join(c for c in string if c.isalnum() or c in keepcharacters).rstrip()


def run_background_task(corofn, *args, executor=None):
    """run non-async task in background"""
    return asyncio.get_event_loop().run_in_executor(executor, corofn, *args)


def run_async_background_task(executor, corofn, *args):
    """run async task in background"""

    def run_task(corofn, *args):
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        coro = corofn(*args)
        res = new_loop.run_until_complete(coro)
        new_loop.close()
        return res

    return asyncio.get_event_loop().run_in_executor(executor, run_task, corofn, *args)


def get_sort_name(name):
    """create a sort name for an artist/title"""
    sort_name = name
    for item in ["The ", "De ", "de ", "Les "]:
        if name.startswith(item):
            sort_name = "".join(name.split(item)[1:])
    return sort_name


def try_parse_int(possible_int):
    try:
        return int(possible_int)
    except (TypeError, ValueError):
        return 0


async def async_iter_items(items):
    """fake async iterator for compatability reasons."""
    if not isinstance(items, list):
        yield items
    else:
        for item in items:
            yield item


def try_parse_float(possible_float):
    try:
        return float(possible_float)
    except (TypeError, ValueError):
        return 0.0


def try_parse_bool(possible_bool):
    if isinstance(possible_bool, bool):
        return possible_bool
    else:
        return possible_bool in ["true", "True", "1", "on", "ON", 1]


def parse_title_and_version(track_title, track_version=None):
    """try to parse clean track title and version from the title"""
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
                    " instrumental",
                    "karaoke",
                    "remaster",
                    "versie",
                    "radio",
                    "unplugged",
                    "disco",
                ]:
                    if version_str in title_part:
                        version = title_part
                        title = title.split(splitter + version)[0]
    title = title.strip().title()
    if not version and track_version:
        version = track_version
    version = get_version_substitute(version).title()
    return title, version


def get_version_substitute(version_str):
    """transform provider version str to universal version type"""
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


# pylint: disable=broad-except
def get_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # doesn't even have to be reachable
        s.connect(("10.255.255.255", 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = "127.0.0.1"
    finally:
        s.close()
    return IP


# pylint: enable=broad-except


def get_hostname():
    """Get hostname for this machine."""
    return socket.gethostname()


def get_folder_size(folderpath):
    """get folder size in gb"""
    total_size = 0
    # pylint: disable=unused-variable
    for dirpath, dirnames, filenames in os.walk(folderpath):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            total_size += os.path.getsize(fp)
    # pylint: enable=unused-variable
    total_size_gb = total_size / float(1 << 30)
    return total_size_gb


class EnhancedJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if dataclasses.is_dataclass(obj):
            return dataclasses.asdict(obj)
        if isinstance(obj, Enum):
            return str(obj)
        if isinstance(obj, Enum):
            return int(obj)
        if hasattr(obj, "to_dict"):
            return obj.to_dict()
        if hasattr(obj, "items"):
            return obj.items()
        return super().default(obj)


json_serializer = functools.partial(json.dumps, cls=EnhancedJSONEncoder)

# def json_serializer(obj):
#     """Recursively create serializable values for (custom) data types."""

#     def get_val(val):
#         if isinstance(val, (int, str, bool, float, tuple)):
#             return val
#         elif isinstance(val, list):
#             new_list = []
#             for item in val:
#                 new_list.append(get_val(item))
#             return new_list
#         elif hasattr(val, "to_dict"):
#             return get_val(val.to_dict())
#         elif isinstance(val, dict):
#             new_dict = {}
#             for key, value in val.items():
#                 new_dict[key] = get_val(value)
#             return new_dict
#         elif hasattr(val, "__dict__"):
#             new_dict = {}
#             for key, value in val.__dict__.items():
#                 new_dict[key] = get_val(value)
#             return new_dict

#     return get_val(obj)


def get_compare_string(input_str):
    """get clean lowered string for compare actions"""
    unaccented_string = unidecode.unidecode(input_str)
    return re.sub(r"[^a-zA-Z0-9]", "", unaccented_string).lower()


def compare_strings(str1, str2, strict=False):
    """compare strings and return True if we have an (almost) perfect match"""
    match = str1.lower() == str2.lower()
    if not match and not strict:
        match = get_compare_string(str1) == get_compare_string(str2)
    return match


# def json_serializer(obj):
#     """json serializer to recursively create serializable values for custom data types"""
#     return json.dumps(json_serializer(obj), skipkeys=True)


def try_load_json_file(jsonfile):
    """try to load json from file"""
    try:
        with open(jsonfile) as f:
            return json.loads(f.read())
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logging.getLogger().debug("Could not load json from file %s", jsonfile, exc_info=exc)
        return None


def create_tempfile():
    """Return a (named) temporary file."""
    if platform.system() == "Linux":
        return memory_tempfile.MemoryTempfile(fallback=True).NamedTemporaryFile(buffering=0)
    return tempfile.NamedTemporaryFile(buffering=0)
