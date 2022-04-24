"""Various helpers for web requests."""
from __future__ import annotations

import asyncio
import json


def serialize_values(obj):
    """Recursively create serializable values for (custom) data types."""

    def get_val(val):
        if (
            isinstance(val, (list, set, filter, tuple))
            or val.__class__ == "dict_valueiterator"
        ):
            return [get_val(x) for x in val] if val else []
        if isinstance(val, dict):
            return {key: get_val(value) for key, value in val.items()}
        try:
            return val.to_dict()
        except AttributeError:
            return val
        except Exception:  # pylint: disable=broad-except
            return val

    return get_val(obj)


def json_serializer(data):
    """Json serializer to recursively create serializable values for custom data types."""
    return json.dumps(serialize_values(data))


async def async_json_serializer(data):
    """Run json serializer in executor for large data."""
    if isinstance(data, list) and len(data) > 100:
        return await asyncio.get_running_loop().run_in_executor(
            None, json_serializer, data
        )
    return json_serializer(data)
