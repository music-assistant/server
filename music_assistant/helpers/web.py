"""Various helpers for web requests."""

import asyncio
import inspect
import ipaddress
import re
from dataclasses import dataclass
from functools import wraps
from typing import Any, Callable, Dict, Optional, Tuple, Union, get_args, get_origin

import ujson
from aiohttp import web
from music_assistant.helpers.typing import MusicAssistant


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
        if (
            isinstance(val, (list, set, filter, tuple))
            or val.__class__ == "dict_valueiterator"
        ):
            return [get_val(x) for x in val]
        if isinstance(val, dict):
            return {key: get_val(value) for key, value in val.items()}
        try:
            return val.to_dict()
        except AttributeError:
            return val

    return get_val(obj)


def json_serializer(data):
    """Json serializer to recursively create serializable values for custom data types."""
    return ujson.dumps(serialize_values(data))


async def async_json_serializer(data):
    """Run json serializer in executor for large data."""
    if isinstance(data, list) and len(data) > 100:
        return await asyncio.get_running_loop().run_in_executor(
            None, json_serializer, data
        )
    return json_serializer(data)


def json_response(data: Any, status: int = 200):
    """Return json in web request."""
    return web.Response(
        body=json_serializer(data), status=200, content_type="application/json"
    )


async def async_json_response(data: Any, status: int = 200):
    """Return json in web request."""
    return web.Response(
        body=await async_json_serializer(data),
        status=200,
        content_type="application/json",
    )


def api_route(api_path, method="GET"):
    """Decorate a function as API route/command."""

    def decorate(func):
        func.api_path = api_path
        func.api_method = method
        return func

    return decorate


def get_match_pattern(api_path: str) -> Optional[re.Pattern]:
    """Return match pattern for given path."""
    if "{" in api_path and "}" in api_path:
        regex_parts = []
        for part in api_path.split("/"):
            if part.startswith("{") and part.endswith("}"):
                # path variable, create named capture group
                regex_parts.append(part.replace("{", "(?P<").replace("}", ">[^{}/]+)"))
            else:
                # literal string
                regex_parts.append(r"\b" + part + r"\b")
        path_regex = "/" if api_path.startswith("/") else ""
        path_regex += "/".join(regex_parts)
        if api_path.endswith("/"):
            path_regex += "/"
        return re.compile(path_regex)
    return None


def create_api_route(
    api_path: str,
    handler: Callable,
    method: str = "GET",
):
    """Create APIRoute instance from given params."""
    return APIRoute(
        path=api_path,
        method=method,
        pattern=get_match_pattern(api_path),
        part_count=api_path.count("/"),
        signature=get_typed_signature(handler),
        target=handler,
    )


def get_typed_signature(call: Callable) -> inspect.Signature:
    """Parse signature of function to do type validation and/or api spec generation."""
    signature = inspect.signature(call)
    return signature


def parse_arguments(mass: MusicAssistant, func_sig: inspect.Signature, args: dict):
    """Parse (and convert) incoming arguments to correct types."""
    final_args = {}
    for key, value in args.items():
        if key not in func_sig.parameters:
            raise KeyError("Invalid parameter: '%s'" % key)
        arg_type = func_sig.parameters[key].annotation
        final_args[key] = convert_value(key, value, arg_type)
    # check for missing args
    for key, value in func_sig.parameters.items():
        if key == "mass":
            final_args[key] = mass
        elif value.default is inspect.Parameter.empty:
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


@dataclass
class APIRoute:
    """Model for an API route."""

    path: str
    method: str
    pattern: Optional[re.Pattern]
    part_count: int
    signature: inspect.Signature
    target: Callable

    def match(
        self, matchpath: str, method: str
    ) -> Optional[Tuple["APIRoute", Dict[str, str]]]:
        """Match this route with given path and return the route and resolved params."""
        if matchpath.endswith("/"):
            matchpath = matchpath[0:-1]
        if self.method.upper() != method.upper():
            return None
        if self.part_count != matchpath.count("/"):
            return None
        if self.pattern is not None:
            match = re.match(self.pattern, matchpath)
            if match:
                return self, match.groupdict()
        match = self.path.lower() == matchpath.lower()
        if match:
            return self, {}
        return None
