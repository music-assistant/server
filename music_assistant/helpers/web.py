"""Various helpers for web requests."""

import inspect
import ipaddress
from functools import wraps
from typing import Any, Callable, Union

import ujson
from aiohttp import web

try:
    # python 3.8+
    from typing import get_args, get_origin
except ImportError:
    # python 3.7
    from typing_inspect import get_args, get_origin


def require_local_subnet(func):
    """Return decorator to specify web method as available locally only."""

    @wraps(func)
    async def wrapped(*args, **kwargs):
        request = args[-1]

        if isinstance(request, web.View):
            request = request.request

        if not isinstance(request, web.BaseRequest):  # pragma: no cover
            raise RuntimeError(
                "Incorrect usage of decorator." "Expect web.BaseRequest as an argument"
            )

        if not ipaddress.ip_address(request.remote).is_private:
            raise web.HTTPUnauthorized(reason="Not remote available")

        return await func(*args, **kwargs)

    return wrapped


def serialize_values(obj):
    """Recursively create serializable values for (custom) data types."""

    def get_val(val):
        if hasattr(val, "to_dict"):
            return val.to_dict()
        if isinstance(val, (list, set, filter, tuple)):
            return [get_val(x) for x in val]
        if val.__class__ == "dict_valueiterator":
            return [get_val(x) for x in val]
        if isinstance(val, dict):
            return {key: get_val(value) for key, value in val.items()}
        return val

    return get_val(obj)


def json_serializer(obj):
    """Json serializer to recursively create serializable values for custom data types."""
    return ujson.dumps(serialize_values(obj))
    # return ujson.dumps(obj)


def json_response(data: Any, status: int = 200):
    """Return json in web request."""
    return web.Response(
        body=json_serializer(data), status=200, content_type="application/json"
    )


def api_route(ws_cmd_path, ws_require_auth=True):
    """Decorate a function as websocket command."""

    def decorate(func):
        func.ws_cmd_path = ws_cmd_path
        func.ws_require_auth = ws_require_auth
        return func

    return decorate


def get_typed_signature(call: Callable) -> inspect.Signature:
    """Parse signature of function to do type vaildation and/or api spec generation."""
    signature = inspect.signature(call)
    typed_params = [
        inspect.Parameter(
            name=param.name,
            kind=param.kind,
            default=param.default,
            annotation=param.annotation,
        )
        for param in signature.parameters.values()
    ]
    typed_signature = inspect.Signature(typed_params)
    return typed_signature


def parse_arguments(call: Callable, args: dict):
    """Parse (and convert) incoming arguments to correct types."""
    final_args = {}
    if isinstance(call, type({}.values)):
        return args
    func_sig = get_typed_signature(call)
    for key, value in args.items():
        if key not in func_sig.parameters:
            raise KeyError("Invalid parameter: '%s'" % key)
        arg_type = func_sig.parameters[key].annotation
        final_args[key] = convert_value(key, value, arg_type)
    # check for missing args
    for key, value in func_sig.parameters.items():
        if value.default is inspect.Parameter.empty:
            if key not in final_args:
                raise KeyError("Missing parameter: '%s'" % key)
    return final_args


def convert_value(arg_key, value, arg_type):
    """Convert dict value to one of our models."""
    if arg_type == inspect.Parameter.empty:
        return value
    if get_origin(arg_type) is list:
        return [
            convert_value(arg_key, subval, get_args(arg_type)[0]) for subval in value
        ]
    if get_origin(arg_type) is Union:
        # try all possible types
        for sub_arg_type in get_args(arg_type):
            try:
                return convert_value(arg_key, value, sub_arg_type)
            except Exception:  # pylint: disable=broad-except
                pass
        raise ValueError("Error parsing '%s', possibly wrong type?" % arg_key)
    if hasattr(arg_type, "from_dict"):
        return arg_type.from_dict(value)
    if value is None:
        return value
    if arg_type is Any:
        return value
    return arg_type(value)
