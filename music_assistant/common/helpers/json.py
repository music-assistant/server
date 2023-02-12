"""Helpers to work with (de)serializing of json."""

from types import MethodType
from typing import Any
import base64
import orjson
import aiofiles
from _collections_abc import dict_keys, dict_values

JSON_ENCODE_EXCEPTIONS = (TypeError, ValueError)
JSON_DECODE_EXCEPTIONS = (orjson.JSONDecodeError,)


def json_encoder_default(obj: Any) -> Any:
    """Convert Special objects.

    Hand other objects to the original method.
    """
    if getattr(obj, "do_not_serialize", None):
        return None
    if (
        isinstance(obj, (list, set, filter, tuple, dict_values, dict_keys, dict_values))
        or obj.__class__ == "dict_valueiterator"
    ):
        return list(obj)
    if hasattr(obj, "as_dict"):
        return obj.as_dict()
    if hasattr(obj, "to_dict"):
        return obj.to_dict(omit_none=True)
    if isinstance(obj, bytes):
        return base64.b64encode(obj).decode("ascii")
    if isinstance(obj, MethodType):
        return None
    raise TypeError


def json_dumps(data: Any) -> str:
    """Dump json string."""
    return orjson.dumps(
        data,
        option=orjson.OPT_NON_STR_KEYS | orjson.OPT_INDENT_2,
        default=json_encoder_default,
    ).decode("utf-8")


json_loads = orjson.loads


async def load_json_file(path: str) -> dict:
    """Load JSON from file."""
    async with aiofiles.open(path, "r") as _file:
        content = await _file.read()
        return json_loads(content)
