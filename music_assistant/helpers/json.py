"""Helpers to work with (de)serializing of json."""

import asyncio
import base64
import re
from _collections_abc import dict_keys, dict_values
from types import MethodType
from typing import Any, TypeVar

import aiofiles
import orjson
from mashumaro.mixins.orjson import DataClassORJSONMixin

JSON_ENCODE_EXCEPTIONS = (TypeError, ValueError)
JSON_DECODE_EXCEPTIONS = (orjson.JSONDecodeError,)

DO_NOT_SERIALIZE_TYPES = (MethodType, asyncio.Task)

_MIN_CODE_FENCE_LENGTH = 3
# Applied to the opening line only, so a fence that carries anything other than a bare
# language tag is left alone instead of having that line silently discarded.
_CODE_FENCE_TAG_PATTERN = re.compile(r"[A-Za-z0-9+#._-]*")

# Type alias for plain, JSON-serializable data.
# Note: tuples are accepted but will be returned as lists after a JSON round-trip.
SerializableType = str | int | float | bool | None | list[Any] | tuple[Any, ...] | dict[str, Any]


def get_serializable_value(obj: Any) -> Any:
    """Parse the value to its serializable equivalent."""
    if getattr(obj, "do_not_serialize", None):
        return None
    if isinstance(obj, list | set | filter | tuple | dict_values | dict_keys) or (
        obj.__class__.__name__ == "dict_valueiterator"
    ):
        return [get_serializable_value(x) for x in obj]
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, bytes):
        return base64.b64encode(obj).decode("ascii")
    if isinstance(obj, DO_NOT_SERIALIZE_TYPES):
        return None
    # unhandled values are returned as-is on purpose: serialize_to_json and the
    # recursion above rely on natively serializable values passing through
    return obj


def make_utf8_safe(value: str) -> str:
    """
    Return the given string made encodable if it is not valid UTF-8.

    Lone surrogates, as the os module returns for undecodable filesystem paths, come
    back as their backslash escapes.

    :param value: The string to make safe.
    """
    try:
        value.encode()
    except UnicodeEncodeError:
        try:
            # a surrogate escape stands for one raw byte, so map it back to show
            # the actual filename bytes instead of the surrogates
            raw = value.encode("utf-8", "surrogateescape")
        except UnicodeEncodeError:
            # any other lone surrogate has no byte to map back to
            return value.encode("utf-8", "backslashreplace").decode()
        return raw.decode("utf-8", "backslashreplace")
    return value


def serialize_to_json(obj: Any) -> Any:
    """Serialize a value (or a list of values) to json."""
    if obj is None:
        return obj
    if hasattr(obj, "to_json"):
        return obj.to_json()
    return json_dumps(get_serializable_value(obj))


def json_dumps(data: Any, indent: bool = False) -> str:
    """Dump json string."""
    # we use the passthrough dataclass option because we use mashumaro for that
    option = orjson.OPT_OMIT_MICROSECONDS | orjson.OPT_PASSTHROUGH_DATACLASS
    if indent:
        option |= orjson.OPT_INDENT_2
    return orjson.dumps(
        data,
        default=_json_default,
        option=option,
    ).decode("utf-8")


async def async_json_dumps(data: Any, indent: bool = False) -> str:
    """Dump json string async."""
    return await asyncio.to_thread(json_dumps, data, indent)


json_loads = orjson.loads


async def async_json_loads(data: str) -> Any:
    """Load json string async."""
    return await asyncio.to_thread(json_loads, data)


def strip_code_fence(text: str) -> str:
    """
    Remove a markdown code fence that wraps an entire text, plus surrounding whitespace.

    Anything else is returned with only its whitespace trimmed: a fence around part of
    the text, more than one fenced block, an unterminated fence, a fence whose opening
    line carries more than a language tag, or a body containing the fence itself. Text
    that is not exactly one fenced block therefore stays as invalid to a strict parser
    as it was before.

    :param text: Text that may be wrapped in a markdown code fence.
    :return: The fenced content, or the trimmed text when it is not a single fenced block.
    """
    stripped = text.strip()
    fence_length = len(stripped) - len(stripped.lstrip("`"))
    if fence_length < _MIN_CODE_FENCE_LENGTH:
        return stripped
    fence = "`" * fence_length
    inner = stripped[fence_length:]
    if not inner.endswith(fence):
        return stripped
    inner = inner[:-fence_length]
    # A closing fence must be the final run of backticks, matching the opening length.
    if inner.endswith("`"):
        return stripped
    tag, separator, body = inner.partition("\n")
    if not separator or not _CODE_FENCE_TAG_PATTERN.fullmatch(tag.strip()):
        return stripped
    if fence in body:
        return stripped
    return body.strip()


TargetT = TypeVar("TargetT", bound=DataClassORJSONMixin)


async def load_json_file[TargetT: DataClassORJSONMixin](
    path: str, target_class: type[TargetT]
) -> TargetT:
    """Load JSON from file."""
    async with aiofiles.open(path) as _file:
        content = await _file.read()
        return target_class.from_json(content)


async def load_json_dict(path: str) -> dict[str, Any]:
    """
    Load a plain JSON object from file as a dict.

    For JSON files that are not backed by a dataclass (e.g. translation strings files).

    :param path: Absolute path to the JSON file.
    """
    async with aiofiles.open(path, "rb") as _file:
        content = await _file.read()
    data = orjson.loads(content)
    if not isinstance(data, dict):
        msg = f"Expected a JSON object in {path}, got {type(data).__name__}"
        raise TypeError(msg)
    return data


def _json_default(obj: Any) -> Any:
    """Convert a value for orjson, raising a descriptive error for unhandled types."""
    value = get_serializable_value(obj)
    if value is obj:
        cls = type(obj)
        msg = (
            f"unhandled type for json serialization: {cls.__module__}.{cls.__qualname__}"
            " - pass a dict (e.g. via .to_dict()) instead of the raw object"
        )
        raise TypeError(msg)
    return value
