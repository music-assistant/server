"""Helpers to work with (de)serializing of json."""

import asyncio
import base64
import logging
from _collections_abc import dict_keys, dict_values
from dataclasses import asdict, is_dataclass
from types import MethodType
from typing import Any, TypeVar

import aiofiles
import orjson
from mashumaro.mixins.orjson import DataClassORJSONMixin

LOGGER = logging.getLogger(__name__)

JSON_ENCODE_EXCEPTIONS = (TypeError, ValueError)
JSON_DECODE_EXCEPTIONS = (orjson.JSONDecodeError,)

DO_NOT_SERIALIZE_TYPES = (MethodType, asyncio.Task)


def get_serializable_value(obj: Any, raise_unhandled: bool = False) -> Any:
    """Parse the value to its serializable equivalent.

    This function will convert dataclasses, dicts and iterable containers into
    JSON-serializable primitives. It intentionally returns primitives (lists,
    dicts, strings, numbers) for any complex object that cannot be serialized
    directly by orjson to avoid infinite recursion in the default serializer.
    """
    if getattr(obj, "do_not_serialize", None):
        return None

    # Convert dataclass *instances* to dicts so nested dataclasses get handled recursively.
    # `is_dataclass` can also return True for dataclass *types*, so ensure we only
    # pass instances to `asdict` to avoid type errors (mypy).
    if is_dataclass(obj) and not isinstance(obj, type):
        return get_serializable_value(asdict(obj))

    # Handle plain dicts
    if isinstance(obj, dict):
        return {k: get_serializable_value(v) for k, v in obj.items()}

    # Handle iterable containers
    if (
        isinstance(obj, list | set | filter | tuple | dict_values | dict_keys | dict_values)
        or obj.__class__ == "dict_valueiterator"
    ):
        return [get_serializable_value(x) for x in obj]

    # If an object provides an explicit to_dict use that
    if hasattr(obj, "to_dict"):
        return obj.to_dict()

    # Fallback to to_json if available
    if hasattr(obj, "to_json"):
        try:
            return obj.to_json()
        except Exception as exc:  # pragma: no cover - defensive logging
            LOGGER.debug("Failed to use to_json() for %s: %s", type(obj), exc)

    if isinstance(obj, bytes):
        return base64.b64encode(obj).decode("ascii")
    if isinstance(obj, DO_NOT_SERIALIZE_TYPES):
        return None
    if raise_unhandled:
        raise TypeError
    return obj


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
        default=get_serializable_value,
        option=option,
    ).decode("utf-8")


async def async_json_dumps(data: Any, indent: bool = False) -> str:
    """Dump json string async."""
    return await asyncio.to_thread(json_dumps, data, indent)


json_loads = orjson.loads


async def async_json_loads(data: str) -> Any:
    """Load json string async."""
    return await asyncio.to_thread(json_loads, data)


TargetT = TypeVar("TargetT", bound=DataClassORJSONMixin)


async def load_json_file[TargetT: DataClassORJSONMixin](
    path: str, target_class: type[TargetT]
) -> TargetT:
    """Load JSON from file."""
    async with aiofiles.open(path) as _file:
        content = await _file.read()
        return target_class.from_json(content)
