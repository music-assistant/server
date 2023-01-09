"""Helpers to work with (de)serializing of json."""

from typing import Any

import orjson


JSON_ENCODE_EXCEPTIONS = (TypeError, ValueError)
JSON_DECODE_EXCEPTIONS = (orjson.JSONDecodeError,)


def json_encoder_default(obj: Any) -> Any:
    """Convert Special objects.

    Hand other objects to the original method.
    """
    if getattr(obj, "do_not_serialize", None):
        return None
    if (
        isinstance(obj, (list, set, filter, tuple))
        or obj.__class__ == "dict_valueiterator"
    ):
        return list(obj)
    if hasattr(obj, "as_dict"):
        return obj.as_dict()
    if isinstance(obj, bytes):
        return str(obj)

    raise TypeError


def json_dumps(data: Any) -> str:
    """Dump json string."""
    return orjson.dumps(
        data,
        option=orjson.OPT_NON_STR_KEYS | orjson.OPT_INDENT_2,
        default=json_encoder_default,
    ).decode("utf-8")


json_loads = orjson.loads
