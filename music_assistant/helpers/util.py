"""Helper and utility functions."""
import asyncio
import functools
import logging
import os
import platform
import re
import socket
import struct
import tempfile
import urllib.request
from enum import Enum
from io import BytesIO
from typing import Any, Callable, TypeVar

import memory_tempfile
import orjson
import unidecode

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
    """Run a coroutine at interval."""

    def scheduler(fcn):
        async def async_wrapper(*args, **kwargs):
            while True:
                asyncio.create_task(fcn(*args, **kwargs))
                await asyncio.sleep(period)

        return async_wrapper

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


def run_background_task(corofn, *args, executor=None):
    """Run non-async task in background."""
    return asyncio.get_event_loop().run_in_executor(executor, corofn, *args)


def run_async_background_task(executor, corofn, *args):
    """Run async task in background."""

    def run_task(corofn, *args):
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        coro = corofn(*args)
        res = new_loop.run_until_complete(coro)
        new_loop.close()
        return res

    return asyncio.get_event_loop().run_in_executor(executor, run_task, corofn, *args)


def get_sort_name(name):
    """Create a sort name for an artist/title."""
    sort_name = name
    for item in ["The ", "De ", "de ", "Les "]:
        if name.startswith(item):
            sort_name = "".join(name.split(item)[1:])
    return sort_name


def try_parse_int(possible_int):
    """Try to parse an int."""
    try:
        return int(possible_int)
    except (TypeError, ValueError):
        return 0


async def async_iter_items(items):
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
    # pylint: disable=broad-except
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
    try:
        return socket.inet_pton(socket.AF_INET, get_ip())
    except OSError:
        return socket.inet_pton(socket.AF_INET6, get_ip())


# pylint: enable=broad-except


def get_hostname():
    """Get hostname for this machine."""
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


# pylint: disable=invalid-name
json_serializer = functools.partial(orjson.dumps, option=orjson.OPT_NAIVE_UTC)
# pylint: enable=invalid-name


def get_compare_string(input_str):
    """Return clean lowered string for compare actions."""
    unaccented_string = unidecode.unidecode(input_str)
    return re.sub(r"[^a-zA-Z0-9]", "", unaccented_string).lower()


def compare_strings(str1, str2, strict=False):
    """Compare strings and return True if we have an (almost) perfect match."""
    match = str1.lower() == str2.lower()
    if not match and not strict:
        match = get_compare_string(str1) == get_compare_string(str2)
    return match


def merge_dict(base_dict: dict, new_dict: dict):
    """Merge dict without overwriting existing values."""
    for key, value in new_dict.items():
        if base_dict.get(key) and isinstance(value, dict):
            base_dict[key] = merge_dict(base_dict[key], value)
        elif not base_dict.get(key):
            base_dict[key] = value
    return base_dict


def try_load_json_file(jsonfile):
    """Try to load json from file."""
    try:
        with open(jsonfile, "rb") as _file:
            return orjson.loads(_file.read())
    except (FileNotFoundError, orjson.JSONDecodeError) as exc:
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


async def async_yield_chunks(_obj, chunk_size):
    """Yield successive n-sized chunks from list/str/bytes."""
    chunk_size = int(chunk_size)
    for i in range(0, len(_obj), chunk_size):
        yield _obj[i : i + chunk_size]


class CustomIntEnum(int, Enum):
    """Base for IntEnum with some helpers."""

    # when serializing we prefer the string (name) representation
    # internally (database) we use the int value

    def __int__(self):
        """Return integer value."""
        return super().value

    def __str__(self):
        """Return string value."""
        # pylint: disable=no-member
        return self._name_.lower()

    @property
    def value(self):
        """Return the (json friendly) string name."""
        return self.__str__()

    @classmethod
    def from_string(cls, string):
        """Create IntEnum from it's string equivalent."""
        for key, value in cls.__dict__.items():
            if key.lower() == string or value == try_parse_int(string):
                return value
        return KeyError


def create_wave_header(samplerate=44100, channels=2, bitspersample=16, duration=3600):
    """Generate a wave header from given params."""
    file = BytesIO()
    numsamples = samplerate * duration

    # Generate format chunk
    format_chunk_spec = b"<4sLHHLLHH"
    format_chunk = struct.pack(
        format_chunk_spec,
        b"fmt ",  # Chunk id
        16,  # Size of this chunk (excluding chunk id and this field)
        1,  # Audio format, 1 for PCM
        channels,  # Number of channels
        int(samplerate),  # Samplerate, 44100, 48000, etc.
        int(samplerate * channels * (bitspersample / 8)),  # Byterate
        int(channels * (bitspersample / 8)),  # Blockalign
        bitspersample,  # 16 bits for two byte samples, etc.
    )
    # Generate data chunk
    data_chunk_spec = b"<4sL"
    datasize = int(numsamples * channels * (bitspersample / 8))
    data_chunk = struct.pack(
        data_chunk_spec,
        b"data",  # Chunk id
        int(datasize),  # Chunk size (excluding chunk id and this field)
    )
    sum_items = [
        # "WAVE" string following size field
        4,
        # "fmt " + chunk size field + chunk size
        struct.calcsize(format_chunk_spec),
        # Size of data chunk spec + data size
        struct.calcsize(data_chunk_spec) + datasize,
    ]
    # Generate main header
    all_chunks_size = int(sum(sum_items))
    main_header_spec = b"<4sL4s"
    main_header = struct.pack(main_header_spec, b"RIFF", all_chunks_size, b"WAVE")
    # Write all the contents in
    file.write(main_header)
    file.write(format_chunk)
    file.write(data_chunk)

    # return file.getvalue(), all_chunks_size + 8
    return file.getvalue()
